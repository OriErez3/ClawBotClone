import json
import logging
import re
import subprocess
import threading
from collections import deque
from datetime import datetime
from database import add_to_memory
from database import read_memory as read_memory_db
from database import delete_memory as delete_memory_db
from database import add_scheduled_task, get_setting, set_setting
import database
import embeddings
import google_services as gs
import os
import shutil
import time
import urllib.request
from dotenv import load_dotenv
from tavily import TavilyClient
from playwright.async_api import (
    async_playwright,
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
)
import base64
from PIL import Image, ImageDraw
import io
from typing import Optional, Union

load_dotenv()
logger = logging.getLogger(__name__)
tav_key = os.getenv("TAVILY_KEY")
tav_client = TavilyClient(tav_key)

SHELL_TIMEOUT_SECONDS = 60
#Set BROWSER_HEADLESS=true in .env on machines with no display (a headless server).
#Headed (the default) is preferred where possible - some sites bot-detect headless Chromium.
#Alternative on a server: keep this false and run the bot under `xvfb-run -a`.
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "").lower() in ("1", "true", "yes")

#Hard floor: truly unrecoverable commands that must never run, even if the user approves
#them at the confirmation prompt. This is a backstop, not the primary defense - the
#human-confirmation step in the tool loop catches everything else.
_BLOCKED_PATTERNS = [
    r"\bformat\b\s+[a-z]:",       # format C:
    r"\bdiskpart\b",
    r"\bmkfs\b",                  # unix filesystem wipe
    r"rm\s+-rf?\s+/(?:\s|$)",     # rm -rf / (root)
    r"\bshutdown\b",              # Windows shutdown/reboot (shutdown /s, /r)
    r"\breboot\b",                # unix equivalent; plain 'restart' was removed - it false-positived on innocent commands like `npm run restart`
    r"reg\s+delete\s+hk",         # registry hive deletion
    r":\(\)\s*\{\s*:\s*\|",       # fork bomb
]

def _is_blocked(command: str) -> Optional[str]:
    for pat in _BLOCKED_PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            return f"Blocked: '{command}' matches a prohibited pattern and will not be run."
    return None

def _download_command_redirect(command: str) -> Optional[str]:
    """Detects shell commands that download a file to disk (curl -o, wget, Invoke-WebRequest
    -OutFile) and redirects to the download_file tool. curl/wget save error pages as if they
    were the file (that's how a 215-byte 'server.jar' slips through); download_file errors
    cleanly instead. A bare `curl https://api...` with no output flag is left alone - that's
    an API call, use fetch_url for that."""
    c = command.lower()
    is_download = (
        ("curl" in c and re.search(r"(^|\s)-o\b|--output|--remote-name", c)) or
        re.search(r"(^|\s)wget\s+\S", c) or  # wget followed by an argument - not `pip install wget` or a passing mention
        ("invoke-webrequest" in c and "outfile" in c) or
        (re.search(r"(^|\s)iwr\b", c) and "outfile" in c)
    )
    if is_download:
        return ("Use the download_file tool to download files, not a shell command. It streams to "
                "disk and errors cleanly on failure instead of saving an error page as a corrupt "
                "file. To read a page's contents (e.g. to find a link), use fetch_url.")
    return None

def run_shell(command: str) -> str:
    """Runs a shell command and waits for it to finish, returning its output. Use this ONLY for
    commands that complete on their own (e.g. `pip install ...`, `npm run build`, a one-off
    `java -jar installer.jar` that unpacks and exits) - things no other tool can do.

    Prefer run_background to start a server or any process that runs indefinitely (e.g. launching
    a Minecraft/web server) - it starts the process without blocking and lets you read its output.
    (If you do use run_shell for one, it won't hang forever: after a timeout it's moved to the
    background automatically and you get a process id back - but run_background is cleaner.)

    Do NOT use it for file operations or downloads. Use the dedicated tools instead:
    list_directory (not `dir`/`ls`), read_file (not `type`/`cat`), write_file (not `echo > file`),
    move_file, find_file, and download_file (not `curl`/`wget`). They are more reliable - e.g.
    `echo eula=true > eula.txt` writes a stray trailing space that silently breaks the file
    (Minecraft then rejects the EULA), whereas write_file writes exactly the content you give it.

    Args:
        command: The shell command to run.
    """
    blocked = _is_blocked(command)
    if blocked:
        return blocked
    redirect = _download_command_redirect(command)
    if redirect:
        return redirect
    global _next_bg_id
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE,   # lets send_process_input type into it if it turns interactive
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",  # replaces undecodable characters instead of crashing
        )
    except Exception as e:
        return f"Error: {e}"
    buf: deque = deque(maxlen=2000)
    drain = threading.Thread(target=_drain_output, args=(proc, buf), daemon=True)
    drain.start()
    try:
        proc.wait(timeout=SHELL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        #Still running at the timeout - it's a long-running process (a server, etc.). Don't kill
        #it; hand it to the background registry so it keeps running and the caller gets a handle.
        #This way using run_shell for a server isn't fatal - it transparently becomes a background
        #process instead of being killed.
        with _bg_lock:
            proc_id = _next_bg_id
            _next_bg_id += 1
            _bg_processes[proc_id] = {"process": proc, "buffer": buf, "command": command}
        lines = "\n".join(list(buf)) or "(no output yet)"
        return (f"This command is still running after {SHELL_TIMEOUT_SECONDS}s, so it's a long-running "
                f"process (e.g. a server). I've moved it to the background as process {proc_id} - it is "
                f"still running. Use read_process_output({proc_id}) to check on it or stop_process({proc_id}) "
                f"to stop it.\nOutput so far:\n{lines}")
    drain.join(timeout=1)  # let the drain thread finish reading any remaining buffered output
    output = "\n".join(list(buf))
    return output if output else "(command finished with no output)"

_bg_processes: dict[int, dict] = {}
_bg_lock = threading.Lock()
_next_bg_id = 1

def _drain_output(proc: subprocess.Popen, buf: deque) -> None:
    try:
        for line in proc.stdout:  # type: ignore
            buf.append(line.rstrip("\n"))
    except Exception:
        pass

def run_background(command: str, working_directory: str = "") -> str:
    """Starts a long-running shell command in the background without blocking. Returns a
    process ID for use with read_process_output, list_processes, and stop_process. Use
    this for servers or watchers that run indefinitely; use run_shell for commands that
    finish on their own.

    Args:
        command: The shell command to run.
        working_directory: Absolute path to run the command from. Defaults to the bot working directory if omitted.
    """
    blocked = _is_blocked(command)
    if blocked:
        return blocked
    redirect = _download_command_redirect(command)
    if redirect:
        return redirect
    global _next_bg_id
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE,   # lets send_process_input type into it (menus, consoles, y/n prompts)
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=working_directory if working_directory else None,
        )
        buf: deque = deque(maxlen=500)
        threading.Thread(target=_drain_output, args=(proc, buf), daemon=True).start()
        with _bg_lock:
            proc_id = _next_bg_id
            _next_bg_id += 1
            _bg_processes[proc_id] = {"process": proc, "buffer": buf, "command": command}
        time.sleep(1.5)  # let initial output accumulate before returning
        lines = list(buf)
        status = "running" if proc.poll() is None else f"exited with code {proc.poll()}"
        output = "\n".join(lines) if lines else "(no output yet)"
        return f"Process {proc_id} started ({status}).\nInitial output:\n{output}"
    except Exception as e:
        return f"Error: {e}"

def read_process_output(process_id: int, last_n_lines: int = 50) -> str:
    """Returns the most recent output lines from a background process.

    Args:
        process_id: The ID returned by run_background.
        last_n_lines: How many recent lines to return. Defaults to 50.
    """
    with _bg_lock:
        entry = _bg_processes.get(process_id)
    if entry is None:
        return f"No background process with ID {process_id}. Use list_processes to see what's running."
    proc = entry["process"]
    lines = list(entry["buffer"])[-last_n_lines:]
    status = "running" if proc.poll() is None else f"exited with code {proc.poll()}"
    output = "\n".join(lines) if lines else "(no output yet)"
    return f"Process {process_id} ({status}):\n{output}"

def list_processes() -> str:
    """Lists all background processes started with run_background and their current status."""
    with _bg_lock:
        entries = list(_bg_processes.items())
    if not entries:
        return "No background processes running."
    lines = []
    for proc_id, entry in entries:
        status = "running" if entry["process"].poll() is None else f"exited ({entry['process'].poll()})"
        lines.append(f"[{proc_id}] {status}: {entry['command']}")
    return "\n".join(lines)

def send_process_input(process_id: int, text: str) -> str:
    """Types a line into a running background process's console (its standard input),
    like typing at its prompt and pressing Enter. Use this to answer interactive prompts
    (y/n questions, menu selections) or to issue console commands to a process started
    with run_background (or one run_shell moved to the background). Returns the process's
    recent output so you can see how it reacted.

    Args:
        process_id: The ID returned by run_background or shown by list_processes.
        text: The line to type. A newline (Enter) is appended automatically.
    """
    with _bg_lock:
        entry = _bg_processes.get(process_id)
    if entry is None:
        return f"No background process with ID {process_id}. Use list_processes to see what's running."
    proc = entry["process"]
    if proc.poll() is not None:
        return f"Process {process_id} already exited (code {proc.poll()}) - there's nothing to type into."
    try:
        proc.stdin.write(text + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]
    except Exception as e:
        return f"Error sending input to process {process_id}: {e}"
    time.sleep(1.0)  # give it a moment to react so the output below shows the response
    lines = list(entry["buffer"])[-15:]
    status = "running" if proc.poll() is None else f"exited with code {proc.poll()}"
    output = "\n".join(lines) if lines else "(no output yet)"
    return f"Input sent. Process {process_id} ({status}), recent output:\n{output}"

def stop_process(process_id: int) -> str:
    """Stops a background process started with run_background.

    Args:
        process_id: The ID returned by run_background.
    """
    with _bg_lock:
        entry = _bg_processes.pop(process_id, None)
    if entry is None:
        return f"No background process with ID {process_id}."
    proc = entry["process"]
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return f"Process {process_id} stopped."
    except Exception as e:
        return f"Error stopping process {process_id}: {e}"

def load_tools(groups: str) -> str:
    """Loads additional tool groups when a task needs a capability you don't currently have.
    Your tools are limited per message to the ones relevant to the request; if partway through
    a task you need others (e.g. you're filling out a form in the browser but now need to send
    an email), call this FIRST to load them, then use them. Available groups:
      - browser: open websites, read/click/type on pages, fill and submit forms, apply online
      - google: Gmail (read/search/send), Google Calendar, Google Drive, Google Sheets
      - files: list/read/write/find/move local files, download files, fetch web page contents
      - processes: run shell commands, start/stop servers, read their output, schedule tasks

    Args:
        groups: Comma-separated group names to load, e.g. "google" or "files,processes".
    """
    return f"Requested tool groups: {groups}"

def save_memory(key: str, value: str) -> str:
    """Saves a key-value pair to the bot's memory. Use this to remember important
    information about the user, such as their name, preferences, goals, or facts
    about their life.

    Args:
        key: The key to identify the memory.
        value: The value to remember.
    """
    add_to_memory(key, value)
    #Also embed the fact for semantic recall; replace any prior embedding for this key so an
    #updated fact doesn't leave a stale copy behind. ref ties the embedding to the memory key.
    embeddings.remember("memory", f"{key}: {value}", ref=f"memory:{key}", replace=True)
    return f"Memory saved: {key} = {value}"

def delete_memory(key: str) -> str:
    """Deletes a key-value pair from the bot's memory. Use this to remove information
    that is no longer relevant or that the user wants to forget.

    Args:
        key: The key of the memory to delete.
    """
    if delete_memory_db(key):
        embeddings.forget(f"memory:{key}")  #drop its embedding too
        return f"Memory deleted: {key}"
    return f"Memory not found: {key}"

def read_memory() -> str:
    """Reads the bot's saved memory and returns it to you."""
    try:
        memory = read_memory_db()  # your existing db function
        if not memory:
            return "No memories saved."
        return memory
    except Exception as e:
        return f"Error: {e}"


# Skills: reusable procedure playbooks the model loads on demand. One markdown file per
# skill in skills/: line 1 is the one-line description (shown in the system-prompt index),
# the rest is the playbook. The folder is gitignored - skills hold server-specific detail
# (usernames, paths) that doesn't belong in a public repo.
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

def _skill_path(name: str) -> Optional[str]:
    """Maps a skill name to a safe filename: lowercased, [a-z0-9-] only. Returns None if
    nothing survives sanitization. Keeps traversal tricks ('../x') from escaping SKILLS_DIR."""
    clean = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not clean:
        return None
    return os.path.join(SKILLS_DIR, f"{clean}.md")

def skills_index() -> str:
    """Returns one '- name: description' line per saved skill (for the system prompt).
    Not a model tool - the index is injected into every conversation automatically."""
    if not os.path.isdir(SKILLS_DIR):
        return ""
    lines = []
    for filename in sorted(os.listdir(SKILLS_DIR)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(SKILLS_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                description = next((line.strip() for line in f if line.strip()), "(no description)")
        except OSError:
            continue
        lines.append(f"- {filename[:-3]}: {description}")
    return "\n".join(lines)

def load_skill(name: str) -> str:
    """Loads the full text of a saved skill - a step-by-step playbook for a specific kind
    of task. ALWAYS load the matching skill before starting a task that fits one of the
    skill descriptions listed in your instructions, and follow what it says.

    Args:
        name: The skill's name, exactly as listed in the skills index.
    """
    path = _skill_path(name)
    if path is None or not os.path.exists(path):
        available = skills_index() or "(none saved yet)"
        return f"No skill named '{name}'. Available skills:\n{available}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error: {e}"

def save_skill(name: str, description: str, content: str) -> str:
    """Saves (or overwrites) a skill: a reusable playbook for a recurring multi-step task,
    written for your future self. Use this when the user teaches you a procedure, corrects
    you on one, or asks you to remember how to do something - include exact commands, paths,
    and pitfalls. Prefer skills for procedures; save_memory is for short standalone facts.

    Args:
        name: Short kebab-case name, e.g. 'terraria-server'.
        description: One line saying when this skill applies - it's how you'll find it later.
        content: The full playbook: steps, exact commands, paths, warnings.
    """
    path = _skill_path(name)
    if path is None:
        return "Error: skill name must contain letters or digits."
    try:
        os.makedirs(SKILLS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{description.strip()}\n\n{content.strip()}\n")
        return f"Skill saved: {os.path.basename(path)[:-3]} - {description.strip()}"
    except Exception as e:
        return f"Error: {e}"

def delete_skill(name: str) -> str:
    """Deletes a saved skill that is wrong or no longer needed.

    Args:
        name: The skill's name, exactly as listed in the skills index.
    """
    path = _skill_path(name)
    if path is None or not os.path.exists(path):
        return f"No skill named '{name}'."
    try:
        os.remove(path)
        return f"Skill deleted: {name}"
    except Exception as e:
        return f"Error: {e}"

# Class schedule: a plain-text file the user maintains with their recurring weekly classes
# (day, time, class name, room). Deliberately free-form - the model reads it, so any layout
# a human would understand works, and the user can edit it in any text editor. Gitignored
# alongside skills/ and memory.db: it's personal detail that doesn't belong in a public repo.
SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.txt")

def schedule_text() -> str:
    """Returns the raw contents of the user's class-schedule file, or '' if there isn't one
    (or it's empty/unreadable). Not a model tool - the morning briefing injects this directly,
    so the schedule is always in the prompt without spending a tool call on it."""
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            #Drop '#' comment lines so the user can annotate the file (and keep the template's
            #own header) without that text reaching the model as part of their timetable
            lines = [ln for ln in f.read().splitlines() if not ln.strip().startswith("#")]
    except OSError:
        return ""
    return "\n".join(lines).strip()

def read_schedule() -> str:
    """Reads the user's weekly class schedule - which classes they have on each day of the
    week, at what times, and in which rooms. Use this whenever the user asks about their
    classes, timetable, lectures, or where they need to be for school ("what do I have
    today?", "what room is my next class in?"). The schedule recurs every week, so work out
    the relevant weekday from the current date/time in your system info. The output ends with
    the file's path - to change the schedule, write the full updated text back there with
    write_file."""
    text = schedule_text()
    if not text:
        return (f"No class schedule saved yet. It lives at {SCHEDULE_FILE} - one line per class "
                "(day, time, class name, room). The user can create it themselves, or ask you "
                "to write it there with write_file.")
    return f"{text}\n\n(Schedule file: {SCHEDULE_FILE})"

# LeetCode practice queue: one "Problem Name | Topic" per line, in the order they get handed
# out. The order is shuffled ONCE when the file is seeded (see leetcode.example.txt) rather
# than at pick time - walking a pre-shuffled list keeps selection O(1) and guarantees no
# repeats, where reshuffling every morning would need the whole solved-log to avoid them.
# Gitignored like schedule.txt: it's personal progress, not repo content.
LEETCODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leetcode.txt")

#Everything below this line in leetcode.txt has already been handed out. The entries are moved
#there commented-out, so leetcode_queue() (which skips '#' lines) ignores them automatically and
#the file stays one readable list: what's coming on top, what's been used underneath. To put a
#problem back, delete its '#' prefix and move the line up.
LEETCODE_DONE_MARKER = "# ---- handed out (most recent last) ----"

def pop_leetcode(names: list) -> int:
    """Moves the named problems out of the active queue into the handed-out section, and returns
    how many moved. Called when problems are ASSIGNED, so the file itself records what's been
    consumed - the queue no longer depends on the user reporting anything, and what the bot
    knows is exactly what the user can see by opening the file.

    Writes via a temp file and os.replace so an interrupted write can't truncate the queue."""
    try:
        with open(LEETCODE_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return 0
    wanted = {n.strip().lower() for n in names}
    marker_at = next((i for i, l in enumerate(lines) if l.strip() == LEETCODE_DONE_MARKER), None)
    active = lines[:marker_at] if marker_at is not None else lines
    done = lines[marker_at + 1:] if marker_at is not None else []
    today = datetime.now().strftime("%Y-%m-%d")
    kept, moved = [], []
    for line in active:
        name = line.split("|")[0].strip().lower()
        if name in wanted and not line.strip().startswith("#") and line.strip():
            moved.append(f"# {today}  {line.strip()}")
        else:
            kept.append(line)
    if not moved:
        return 0
    while kept and not kept[-1].strip():
        kept.pop()  #keep exactly one blank line before the marker
    out = kept + ["", LEETCODE_DONE_MARKER] + done + moved
    tmp = LEETCODE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, LEETCODE_FILE)
    except OSError as e:
        logger.warning("Could not update the LeetCode queue file: %s", e)
        return 0
    return len(moved)

def leetcode_queue() -> list:
    """Returns the practice queue as [(name, topic)], or [] if there's no file, it's empty, or
    it can't be read. Topic is '' on a line with no '|'. Not a model tool - the morning briefing
    walks this list itself so the model never picks (and so never repeats or invents) problems."""
    try:
        with open(LEETCODE_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        return []
    queue = []
    for line in lines:
        name, _, topic = line.partition("|")
        if name.strip():
            queue.append((name.strip(), topic.strip()))
    return queue

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")

def _leetcode_sheet_id() -> str:
    """The tracker spreadsheet's id, read from the memory fact the model saves under
    'leetcode_sheet_id'. Accepts a full sheet URL stored there (that's what a user pastes)
    and pulls the id out of it. '' when nothing is saved yet."""
    raw = (database.get_memory("leetcode_sheet_id") or "").strip()
    match = _SHEET_ID_RE.search(raw)
    return match.group(1) if match else raw

def log_leetcode_result(problem: str, got_it: str, notes: str = "", topic: str = "") -> str:
    """Records how ONE LeetCode problem went. Call this once per problem as soon as the user
    says how it went - it writes the row to their tracker spreadsheet AND updates the local
    history the morning picks are based on, so don't also call sheets_append for it. When every
    problem assigned today is logged, the assignment clears and the evening reminder goes quiet.

    Works for ANY problem, not only today's pair: the user also does extra problems and catches
    up on old ones, and those count too. For a problem that isn't today's and isn't in their
    practice queue, pass the topic yourself (e.g. 'Linked List', 'Dynamic Programming') since
    there's nowhere to look it up.

    Args:
        problem: The problem's name, e.g. 'Merge Two Sorted Lists'.
        got_it: Exactly one of 'yes' (solved it), 'partly' (got there with hints or too slowly), or 'no' (couldn't).
        notes: The user's own comment in their words - what tripped them up, what they'd revisit. Empty if they didn't say.
        topic: The problem's topic. Only needed for problems outside today's assignment and the queue; otherwise leave empty and it's looked up.
    """
    got_it = (got_it or "").strip().lower()
    if got_it not in database.GOT_IT_SCORES:
        return f"Error: got_it must be exactly one of {', '.join(database.GOT_IT_SCORES)}."
    raw = get_setting("leetcode_pending")
    try:
        problems = (json.loads(raw) if raw else {}).get("problems") or []
    except ValueError:
        problems = []
    #Resolve the name and topic from the most authoritative source available, so the sheet keeps
    #one spelling per problem: today's assignment, then the practice queue, then whatever the
    #caller supplied for a problem that exists in neither (an extra the user did on their own).
    wanted = problem.strip().lower()
    match = next((p for p in problems if p[0].strip().lower() == wanted), None)
    if match is not None:
        name, resolved_topic = match[0], (match[1] if len(match) > 1 else "")
    else:
        entry = next((q for q in leetcode_queue() if q[0].strip().lower() == wanted), None)
        if entry is not None:
            name, resolved_topic = entry
        else:
            name, resolved_topic = problem.strip(), topic.strip()
    if not resolved_topic:
        return (f"'{name}' isn't today's assignment or in the practice queue, so I need its topic "
                "to file it. Call this again with topic set (e.g. topic='Linked List').")
    sheet_id = _leetcode_sheet_id()
    if not sheet_id:
        return ("No tracker spreadsheet is saved yet. Ask the user for the link to their LeetCode "
                "sheet, save it with save_memory under the key 'leetcode_sheet_id', then call this again.")
    #Sheet first: if the user-visible record fails to write, log nothing locally, so the two
    #never drift apart and the problem stays outstanding for a retry.
    appended = gs.sheets_append(sheet_id, json.dumps([[name, resolved_topic, got_it, notes]]))
    if appended.startswith("Error"):
        return f"Nothing was logged - writing to the spreadsheet failed: {appended}"
    database.add_leetcode_attempt(name, resolved_topic, got_it, notes)
    if match is None:
        #An extra problem, not today's pair - today's assignment stays outstanding
        outstanding = ", ".join(p[0] for p in problems)
        return (f"Logged '{name}' ({resolved_topic}, {got_it})."
                + (f" Today's assignment is still outstanding: {outstanding}." if outstanding else ""))
    remaining = [p for p in problems if p is not match]
    if remaining:
        set_setting("leetcode_pending", json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                                    "problems": remaining}))
        return f"Logged '{name}' ({got_it}). Still outstanding: {', '.join(p[0] for p in remaining)}."
    set_setting("leetcode_pending", "")
    return f"Logged '{name}' ({got_it}). That's today's assignment done - cleared."

def schedule_task(when: str, task: str) -> str:
    """Schedules a task to be executed automatically at a specific future time, using the
    same tools available now (e.g. send an email, create a file, post a calendar event).
    Use the current date/time from the system info to resolve relative times like
    'in 30 minutes' or 'at 5pm'. The task description should be self-contained and
    specific, since it will be executed without further input from the user.

    Args:
        when: When to run the task, as an RFC3339 datetime with offset, e.g. 2026-06-12T17:00:00-04:00.
        task: A self-contained description of exactly what to do, including all details needed (recipients, file names/content, etc.).
    """
    try:
        dt = datetime.fromisoformat(when)
        chat_id = get_setting("chat_id")
        if not chat_id:
            return "Error: no chat registered yet - the user needs to message the bot first."
        add_scheduled_task(chat_id, task, dt.timestamp())
        return f"Task scheduled for {when}: {task}"
    except Exception as e:
        return f"Error: {e}"


def list_directory(path: str = ".") -> str:
    """Returns the directory list of whatever path is inputted.

    Args:
        path: The path to list.
    """
    try:
        # normalize the path so forward and back slashes both work
        path = os.path.normpath(path)
        logger.debug("Listing: %s (exists: %s)", path, os.path.exists(path))
        if not os.path.exists(path):
            return f"Path does not exist: {path}"
        if not os.path.isdir(path):
            return f"{path} is a file, not a directory"
        files = os.listdir(path)
        if not files:
            return f"Directory is empty: {path}"
        return "\n".join(files)
    except Exception as e:
        return f"Error: {e}"

def read_file(file_path: str) -> str:
    """Reads a given path to a file, and returns the contents of the file.

    Args:
        file_path: The path to read.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()
            return content
    except Exception as e:
        return f"Error: {e}"

def write_file(path: str, content: str, binary: bool = False) -> str:
    """Writes a file to whatever path is inputted.

    Args:
        path: The path to write a file to.
        content: The content of the file (base64-encoded if binary is true).
        binary: Set to true if the content is base64-encoded binary data.
    """
    try:
        if binary:
            with open(path, "wb") as f:
                f.write(base64.b64decode(content))
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return "Successfully written!"
    except Exception as e:
        return f"Error: {e}"

DOWNLOAD_TIMEOUT_SECONDS = 120

def download_file(url: str, destination_path: str) -> str:
    """Downloads a file from a URL directly to disk, without opening a browser. Use this for
    direct download links (installers, server jars, datasets, etc.). Streams to disk so large
    files don't exhaust memory.

    Args:
        url: The direct URL to download from (must start with http:// or https://).
        destination_path: Absolute path to save the file to, including the filename.
    """
    if not url.lower().startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    try:
        # A browser-like User-Agent; some hosts reject the default urllib agent
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        parent = os.path.dirname(destination_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            with open(destination_path, "wb") as f:
                shutil.copyfileobj(response, f)  # streams in chunks, no full-file buffering
        size = os.path.getsize(destination_path)
        # A real file download shouldn't come back as an HTML page. If it does, the URL was
        # almost certainly a download *page*, not the direct file - don't leave the misleading
        # file on disk (that's how a 215-byte "server.jar" error page slips through).
        if content_type == "text/html":
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return ("Error: the URL returned an HTML web page (Content-Type: text/html), not a file - "
                    "it's probably a download page, not a direct link. Use fetch_url to read the page "
                    "and find the direct file URL, then download that. Nothing was saved.")
        return f"Downloaded {size} bytes to {destination_path}"
    except Exception as e:
        return f"Error: {e}"

FETCH_URL_MAX_CHARS = 15000

def fetch_url(url: str) -> str:
    """Fetches the raw text/HTML of a web page without opening a browser. Use this to read a
    page's source and find links (e.g. the direct download URL behind a button) before calling
    download_file. Output is truncated to avoid flooding context; for JS-rendered pages that
    return little useful HTML, fall back to the browser tools.

    Args:
        url: The page URL to fetch (must start with http:// or https://).
    """
    if not url.lower().startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            raw = response.read(FETCH_URL_MAX_CHARS * 4)  # bytes; decoded text is usually smaller
        text = raw.decode("utf-8", errors="replace")
        if len(text) > FETCH_URL_MAX_CHARS:
            return text[:FETCH_URL_MAX_CHARS] + "\n... (truncated)"
        return text
    except Exception as e:
        return f"Error: {e}"



FIND_FILE_MAX_MATCHES = 50
#Hidden/system dirs that make walking user folders slow and full of junk results
FIND_FILE_SKIP_DIRS = {"AppData", "node_modules", "__pycache__", "$RECYCLE.BIN"}
#Registry value names for Desktop, Documents, Downloads in User Shell Folders
_SHELL_FOLDER_VALUES = ("Desktop", "Personal", "{374DE290-123F-4565-9164-39C4925E467B}")

def _user_search_roots() -> list:
    """Returns the real Desktop/Documents/Downloads paths. Windows can relocate these
    (on this machine they live on D:\\, not under C:\\Users), and the actual locations
    are recorded in the registry - blindly using ~/Desktop etc. would search the wrong
    (often empty) folders."""
    folders = []
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        for value_name in _SHELL_FOLDER_VALUES:
            try:
                raw, _ = winreg.QueryValueEx(key, value_name)
                if raw:
                    folders.append(os.path.expandvars(raw))
            except OSError:
                pass
    except Exception:
        pass  # not on Windows or registry unreadable - fall back to the defaults below
    for name in ("Desktop", "Documents", "Downloads"):
        folders.append(os.path.join(os.path.expanduser("~"), name))
    #Keep existing dirs, dedupe, and drop any root nested inside another root
    real = []
    for f in folders:
        f = os.path.normpath(f)
        if os.path.isdir(f) and f not in real:
            real.append(f)
    return [f for f in real if not any(f != other and f.startswith(other + os.sep) for other in real)]

def find_file(filename: str) -> str:
    """Searches for a file by name in the user's Desktop, Documents, and Downloads
    folders. Use this when the user wants to find a file but doesn't know the exact
    path. Returns at most 50 matches.

    Args:
        filename: The file name (or part of it) to look for.
    """
    matches = []
    for search_root in _user_search_roots():
        for root, dirs, files in os.walk(search_root):
            #Prune in place so os.walk never descends into hidden/system dirs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in FIND_FILE_SKIP_DIRS]
            for file in files:
                if filename.lower() in file.lower():
                    matches.append(os.path.join(root, file))
                    if len(matches) >= FIND_FILE_MAX_MATCHES:
                        return "\n".join(matches) + f"\n... stopped at {FIND_FILE_MAX_MATCHES} matches - use a more specific name to narrow it down."
    return "\n".join(matches) if matches else "No files found"

def move_file(source: str, destination: str) -> str:
    """Moves a file from a given source to a given destination.

    Args:
        source: The path of the file you want moved.
        destination: The path of where the file is going.
    """
    try:
        shutil.move(source,destination)
        return(f"{source} moved to {destination}")
    except Exception as e:
        return f"Error: {e}"

def web_search(query: str) -> str:
    """Searches the web. Takes a query input, returns a formulated answer, as well as sources.

    Args:
        query: What you want to search up.
    """
    try:
        response = tav_client.search(query=query, max_results=5, include_answer=True)
        answer = response.get("answer", "")
        results = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nContent: {r['content'][:500]}"
            for r in response["results"]
        )
        if answer:
            return f"Direct answer: {answer}\n\nSources:\n{results}"
        return f"Sources:\n{results}"
    except Exception as e:
        return f"Error: {e}"


# Browser things

def _normalize_url(url: str) -> str:
    return url.strip().rstrip('/')

#One consistent, actionable message for every tool that needs a live page. Covers both
#"never opened" and "the user closed the window" - the fix is the same either way.
_DEAD_PAGE_MSG = ("Error: no browser page is open (it was closed or never opened). "
                  "Use browser_navigate to open one - it will relaunch the browser if needed.")

class BrowserSession:
    """Encapsulates a single Playwright browser/page and the element index cache for it."""

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.elements_cache: dict[int, dict] = {}

    def _page_dead(self) -> bool:
        """True when there's no usable page - never opened, or the user closed the
        window/tab, or the browser crashed. Dead handles aren't None, so a plain None
        check misses all of those cases."""
        return (
            self._browser is None
            or not self._browser.is_connected()
            or self.page is None
            or self.page.is_closed()
        )

    async def _reset(self) -> None:
        """Drops all handles to a dead browser so _ensure_page relaunches from scratch."""
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._browser = None
        self.page = None
        self.elements_cache.clear()

    async def _ensure_page(self) -> Page:
        #Recover if the user closed the Chromium window (or it crashed) - without this,
        #the stale handles make every browser tool error until the bot restarts.
        if self._browser is not None and not self._browser.is_connected():
            await self._reset()
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=BROWSER_HEADLESS,
                args=["--disable-blink-features=AutomationControlled"],
            )
        if self.page is None or self.page.is_closed():
            #Browser alive but the tab was closed - just open a fresh one
            self.page = await self._browser.new_page(viewport={"width": 1280, "height": 720})
            self.elements_cache.clear()
        return self.page

    def _invalidate_elements_cache(self) -> None:
        # The DOM/layout may have changed, so cached element positions can no longer be trusted.
        self.elements_cache.clear()

    async def _screenshot_b64(self) -> str:
        assert self.page is not None
        screenshot = await self.page.screenshot()
        return base64.b64encode(screenshot).decode("utf-8")

    async def _settle(self) -> None:
        # Give navigations triggered by the action time to finish, plus a short
        # grace period for JS-driven UI updates, before screenshotting.
        assert self.page is not None
        try:
            await self.page.wait_for_load_state("load", timeout=3000)
        except PlaywrightTimeoutError:
            pass
        await self.page.wait_for_timeout(300)

    async def navigate(self, url: str) -> Union[str, tuple[str, str]]:
        try:
            page = await self._ensure_page()
            if _normalize_url(url) == _normalize_url(page.url):
                return await self._screenshot_b64(), "Already on this page - navigation skipped to avoid losing progress."
            await page.goto(url)
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    def get_current_url(self) -> str:
        return "No page open" if self._page_dead() else self.page.url  #type: ignore[union-attr]

    async def screenshot(self) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    async def get_elements(self, screenshot: bool = False) -> Union[str, tuple[str, str]]:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            # Query DOM
            elements = await self.page.evaluate("""
                () => {
                    const selectors = 'a, button, input, select, textarea, [role=button], [role=link], [onclick]';
                    const els = document.querySelectorAll(selectors);
                    return Array.from(els).map((el, i) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            index: i + 1,
                            tag: el.tagName.toLowerCase(),
                            text: (el.innerText || el.value || el.placeholder || el.ariaLabel || '').slice(0, 30).trim(),
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height
                        };
                    }).filter(el => el.width > 0 && el.height > 0 && el.y >= 0);
                }
            """)
            self.elements_cache.clear()
            for el in elements:
                self.elements_cache[el['index']] = el
            # Build element map, capped to avoid flooding context on pages with huge DOMs
            MAX_ELEMENTS = 80
            shown_elements = elements[:MAX_ELEMENTS]
            element_map = "\n".join([
                f"[{el['index']}] {el['tag']}: '{el['text']}' at ({el['x']:.0f}, {el['y']:.0f})"
                for el in shown_elements
            ])
            remaining = len(elements) - len(shown_elements)
            if remaining > 0:
                element_map += f"\n... and {remaining} more elements not shown."

            if not screenshot:
                return element_map

            # Take and annotate screenshot
            shot = await self.page.screenshot()
            img = Image.open(io.BytesIO(shot))
            draw = ImageDraw.Draw(img)

            for el in elements:
                x, y, w, h = el['x'], el['y'], el['width'], el['height']
                draw.rectangle([x, y, x+w, y+h], outline="red", width=2)
                draw.rectangle([x, y-15, x+25, y], fill="red")
                draw.text((x+2, y-14), str(el['index']), fill="white")

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            return image_b64, element_map

        except Exception as e:
            return f"Error: {e}"

    async def click_element(self, index: int) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            el = self.elements_cache.get(index)
            logger.debug("Clicking element %s: %s", index, el)
            if el is None:
                return "Error: Element not found. Run browser_get_elements first."

            x = el['x'] + el['width'] / 2
            y = el['y'] + el['height'] / 2
            await self.page.mouse.click(x, y)
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    async def click(self, x: int, y: int) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            await self.page.mouse.click(x, y)
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    async def type_text(self, text: str, press_enter: bool = False) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            await self.page.keyboard.type(text)
            if press_enter:
                await self.page.keyboard.press("Enter")
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    async def scroll(self, direction: str, amount: int = 300) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            if direction == "down":
                await self.page.mouse.wheel(0, amount)
            elif direction == "up":
                await self.page.mouse.wheel(0, -amount)
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"

    async def go_back(self) -> str:
        if self._page_dead():
            return _DEAD_PAGE_MSG
        try:
            await self.page.go_back()
            self._invalidate_elements_cache()
            await self._settle()
            return await self._screenshot_b64()
        except Exception as e:
            return f"Error: {e}"


_browser_session = BrowserSession()

async def browser_navigate(url: str) -> Union[str, tuple[str, str]]:
    """Goes to a given URL and returns the screenshot data.

    Args:
        url: The website you want to go to.
    """
    return await _browser_session.navigate(url)

def browser_current_url() -> str:
    """Returns the current browser URL. Use to see if you actually need to change URLs."""
    return _browser_session.get_current_url()

async def browser_screenshot() -> str:
    """Takes a screenshot of the current browser page and returns it. Use this to see
    the current state of the page after any action."""
    return await _browser_session.screenshot()

async def browser_get_elements(screenshot: bool = False) -> Union[str, tuple[str, str]]:
    """Gives you a text map, and an annotated image of all the elements on a website.
    Takes a boolean, which determines if you need the annotated screenshot.

    Args:
        screenshot: Returns a screenshot with the elements annotated if set to true.
    """
    return await _browser_session.get_elements(screenshot)

async def browser_click_element(index: int) -> str:
    """Allows you to click an element in the text map generated by browser_get_elements.
    Takes the index of the element, returns the updated screen after you click it.

    Args:
        index: The index of the element to click, from the element map.
    """
    return await _browser_session.click_element(index)

async def browser_click(x: int, y: int) -> str:
    """Clicks at whatever coordinate you want, waits for it to load, and returns a
    screenshot of what happened.

    Args:
        x: The X-axis of where you want to click.
        y: The Y-axis of where you want to click.
    """
    return await _browser_session.click(x, y)

async def browser_type(text: str, press_enter: bool = False) -> str:
    """Types text into the currently focused element. Set press_enter=True to immediately
    submit/confirm afterward (e.g. search bars, login forms, chat inputs, game guesses
    like Wordle) - this is usually what you want instead of typing and then taking a
    separate action to submit.

    Args:
        text: Whatever message you want to type.
        press_enter: Set to true to press Enter right after typing, submitting/confirming the input. Only leave false if you specifically need to type without submitting (e.g. a multi-line text area).
    """
    return await _browser_session.type_text(text, press_enter)

async def browser_scroll(direction: str, amount: int = 300) -> str:
    """Scrolls up or down depending on what's passed in.

    Args:
        direction: Takes either 'up' or 'down' as directions to scroll.
        amount: How much to scroll up or down. Defaults to 300.
    """
    return await _browser_session.scroll(direction, amount)

async def browser_go_back() -> str:
    """Goes back to the previous page."""
    return await _browser_session.go_back()
