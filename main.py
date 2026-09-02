import telegram
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes, CommandHandler, MessageHandler, filters
from dotenv import load_dotenv
import os
import asyncio
from google import genai
from google.genai import errors, types
from database import add_to_conversation, read_conversation, read_memory
import tools as t
import google_services as gs
import database
import embeddings
import platform
import inspect
import subprocess
import sys
import base64
import logging
import re
from datetime import datetime, time as dt_time, timedelta
from typing import Any
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
#Loads the environment variables for the APIs
telegram_key = os.getenv("TELEGRAM_TOKEN")
gemini_key = os.getenv("GEMINI_API_KEY")
allowed_user_id = os.getenv("ALLOWED_USER_ID")
if telegram_key is None:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")
if gemini_key is None:
    raise ValueError("GEMINI_API_KEY environment variable is required")
if allowed_user_id is None:
    raise ValueError("ALLOWED_USER_ID environment variable is required - the bot has shell access, so it must only answer you. Set it to your Telegram user id.")
ALLOWED_USER_ID = int(allowed_user_id)
#Optional override so trying a different model doesn't require a code change
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

client = genai.Client(api_key=gemini_key)

os_name = platform.system()
#Tool declarations are generated from the functions in tool_dict below: the signature
#(parameter names, type hints, defaults) becomes the schema and the docstring becomes
#the description the model reads. To add a tool, write the function and register it here.
tool_dict = {
    "run_shell": t.run_shell,
    "list_directory": t.list_directory,
    "read_file": t.read_file,
    "write_file": t.write_file,
    "download_file": t.download_file,
    "fetch_url": t.fetch_url,
    "load_tools": t.load_tools,
    "save_memory": t.save_memory,
    "delete_memory": t.delete_memory,
    "read_memory": t.read_memory,
    "load_skill": t.load_skill,
    "save_skill": t.save_skill,
    "delete_skill": t.delete_skill,
    "read_schedule": t.read_schedule,
    "find_file": t.find_file,
    "move_file": t.move_file,
    "web_search": t.web_search,
    "browser_navigate": t.browser_navigate,
    "browser_screenshot": t.browser_screenshot,
    "browser_click": t.browser_click,
    "browser_type": t.browser_type,
    "browser_scroll": t.browser_scroll,
    "browser_get_elements": t.browser_get_elements,
    "browser_click_element" : t.browser_click_element,
    "browser_go_back": t.browser_go_back,
    "browser_current_url": t.browser_current_url,
    "gmail_list_messages": gs.gmail_list_messages,
    "gmail_read_message": gs.gmail_read_message,
    "gmail_send_email": gs.gmail_send_email,
    "gmail_mark_as_read": gs.gmail_mark_as_read,
    "calendar_list_events": gs.calendar_list_events,
    "calendar_create_event": gs.calendar_create_event,
    "drive_list_files": gs.drive_list_files,
    "drive_read_file": gs.drive_read_file,
    "drive_upload_file": gs.drive_upload_file,
    "drive_download_file": gs.drive_download_file,
    "sheets_read": gs.sheets_read,
    "sheets_update": gs.sheets_update,
    "sheets_append": gs.sheets_append,
    "schedule_task": t.schedule_task,
    "run_background": t.run_background,
    "read_process_output": t.read_process_output,
    "send_process_input": t.send_process_input,
    "list_processes": t.list_processes,
    "stop_process": t.stop_process,
}

#Pre-build each tool's declaration once (introspecting a callable isn't free) so a request can
#cheaply assemble a Tool from any subset of names. `tools` is the full set - the fallback when
#dynamic selection is off or unavailable.
_tool_declarations = {
    name: types.FunctionDeclaration.from_callable(client=client, callable=fn)
    for name, fn in tool_dict.items()
}
tools = types.Tool(function_declarations=list(_tool_declarations.values()))

#Dynamic tool loading: instead of sending all ~40 tool schemas every call, embed each group's
#intent description and, per message, load only the groups most similar to it (plus CORE_TOOLS).
#Tools travel in workflow packs (a browser task needs the whole browser group), so we select
#whole GROUPS, not individual tools. Descriptions are written with user-intent phrasing so they
#match how people actually ask ("apply to this job" -> browser group).
CORE_TOOLS = {  #always available regardless of the task
    "load_tools",  #lets the model pull in more groups mid-task when it needs a capability it lacks
    "save_memory", "delete_memory", "read_memory",
    "load_skill", "save_skill", "delete_skill", "web_search",
    "read_schedule",  #class-timetable lookups don't match any group description
}
TOOL_GROUPS = {
    "files": {
        "description": "Working with local files and downloads: list folders, read and write "
                       "files, find or move files, download a file from a URL, fetch the contents of a web page.",
        "tools": {"list_directory", "read_file", "write_file", "download_file", "fetch_url", "find_file", "move_file"},
    },
    "browser": {
        "description": "Using a web browser to interact with websites: open pages, read the "
                       "elements on a page, click buttons and links, type into forms and search "
                       "boxes, scroll, fill out and submit online forms, apply to jobs, sign in.",
        "tools": {"browser_navigate", "browser_screenshot", "browser_click", "browser_type",
                  "browser_scroll", "browser_get_elements", "browser_click_element", "browser_go_back", "browser_current_url"},
    },
    "google": {
        "description": "The user's Google account: read, search and send Gmail email; view and "
                       "create Google Calendar events; list, read, upload and download Google "
                       "Drive files; read and edit Google Sheets spreadsheets.",
        "tools": {"gmail_list_messages", "gmail_read_message", "gmail_send_email", "gmail_mark_as_read",
                  "calendar_list_events", "calendar_create_event", "drive_list_files", "drive_read_file",
                  "drive_upload_file", "drive_download_file", "sheets_read", "sheets_update", "sheets_append"},
    },
    "processes": {
        "description": "Running programs, servers and shell commands: run a terminal command, "
                       "start or stop a long-running background process or server, read its "
                       "output, type into it, schedule a task to run at a later time.",
        "tools": {"run_shell", "run_background", "read_process_output", "send_process_input",
                  "list_processes", "stop_process", "schedule_task"},
    },
}
TOOL_GROUPS_SELECTED = 2  #load this many best-matching groups (plus core) per request
_group_embeddings: dict[str, list] = {}  #lazily filled cache of group-description embeddings

def _validate_tool_groups() -> None:
    #Every tool must be reachable via core or some group, or it could never be offered when
    #dynamic selection is on. Also catch names that don't match a real tool.
    grouped = set(CORE_TOOLS)
    for g in TOOL_GROUPS.values():
        grouped |= g["tools"]
    registered = set(tool_dict)
    if grouped - registered:
        raise RuntimeError(f"tool groups reference unknown tools: {grouped - registered}")
    if registered - grouped:
        raise RuntimeError(f"tools not in any group or core (would be unreachable when tools are limited): {registered - grouped}")

_validate_tool_groups()

def _cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0

def _ensure_group_embeddings() -> dict:
    """Embeds each group's description once and caches it (in-memory; only a few groups).
    Returns the cache, which may be partial/empty if embedding failed - callers then fall
    back to offering all tools."""
    if _group_embeddings:
        return _group_embeddings
    for name, group in TOOL_GROUPS.items():
        vec = embeddings.embed_text(group["description"])
        if vec:
            _group_embeddings[name] = vec
    return _group_embeddings

def _select_groups(query_vector: list | None) -> set | None:
    """Returns the set of group names to load for this query - the TOOL_GROUPS_SELECTED groups
    most similar to `query_vector`. Returns None to mean 'all tools', the safe fallback when
    there's no query vector or group embeddings couldn't be built."""
    if not query_vector:
        return None
    group_vecs = _ensure_group_embeddings()
    if not group_vecs:
        return None
    ranked = sorted(group_vecs, key=lambda g: _cosine(query_vector, group_vecs[g]), reverse=True)
    return set(ranked[:TOOL_GROUPS_SELECTED])

def _tool_object_for_groups(group_names: set | None) -> types.Tool:
    """Builds a Tool from CORE_TOOLS plus the given groups' tools. group_names=None -> all tools.
    Used both for the initial selection and when load_tools expands the set mid-task."""
    if group_names is None:
        return tools
    names = set(CORE_TOOLS)
    for g in group_names:
        names |= TOOL_GROUPS.get(g, {}).get("tools", set())
    return types.Tool(function_declarations=[_tool_declarations[n] for n in names])

#Test to make sure everything is working
async def start(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("Hello! I'm your Google AI assistant.")
def build_system_instruction(memory: str, browser_url: str, now: str, persist_mode: bool = False, tool_log: str = "", recall: str = "") -> str:
    cwd = os.getcwd()
    #`cd /d` is cmd.exe-only (switches drive too); POSIX shells just use `cd`
    cd_example = "cd /d <folder> &&" if os_name == "Windows" else "cd <folder> &&"
    path_example = r"C:\Users\orier\Desktop\project" if os_name == "Windows" else "/home/user/project"
    instruction = f"""You are a personal AI assistant. Be helpful and concise.

System info:
- OS: {os_name}
- Current date/time: {now}
- Bot working directory: {cwd} (this is where the bot itself lives — NOT where user files should go)

Saved memory about the user:
{memory}

Skills - saved step-by-step playbooks for recurring tasks (full text via load_skill):
{t.skills_index() or "(none saved yet)"}

Browser state:
- Current browser page: {browser_url}
- This reflects reality right now, even if earlier conversation mentions a different page. Trust this over anything you previously said about the browser.

Tool rules:
- Use save_memory for important user facts, delete_memory when outdated, read_memory to recall facts
- Skills: BEFORE starting a task that matches a skill description above, call load_skill and follow the playbook - it reflects how the user wants that exact task done and overrides your general approach
- Skills: when the user teaches you a multi-step procedure, corrects how you did one, or you work out a non-obvious sequence worth reusing, offer to save_skill it (capture exact commands, paths, and pitfalls). Procedures go in skills; short standalone facts go in save_memory
- For file operations ALWAYS use the dedicated tools: list_directory to see a folder's contents, read_file to read, write_file to write, move_file to move, find_file to locate, download_file to download. Do NOT use run_shell for these - never run `dir`, `ls`, `type`, `cat`, `copy`, `move`, `del`, `mkdir` and the like. run_shell is a last resort for things no dedicated tool covers (running a program, installing packages); it also interrupts the user with a confirmation prompt every time, so reaching for it when a dedicated tool exists is slow and annoying for them
- File paths: always use absolute paths (e.g. {path_example}). Never use relative paths or write files into the bot working directory unless the user explicitly asks to
- Working directory: when a task lives in a specific folder (setting up a server, building a project, etc.), pick that folder's absolute path ONCE and do everything inside it. Every write_file/download_file/read_file uses paths within it, AND every command must run there too: pass working_directory=<folder> to run_background, and prefix run_shell with `{cd_example}`. If you omit this, commands run in the bot directory ({cwd}) and won't see the files you created - which is almost always a bug. Never split one task across two folders or copy the files between them.
- Downloads: to fetch a file, prefer fetch_url (to read a page and find the direct link) then download_file (to save it) - this avoids the browser entirely. Only open the browser if you genuinely can't obtain the direct link that way.
- If fetch_url returns a page that doesn't contain the link or data you need, the page builds it with JavaScript - do NOT guess or fabricate a download URL (you'll get 404s). Instead web_search for the site's official JSON/API or manifest endpoint, fetch_url that to get the real link, then download_file it. Only if no such API exists, open the browser to read the real link off the page.
- Never give up on a task without actually attempting it first
- Browser: only call browser_navigate for new URLs, never to save files
- Browser: re-navigating to the page you're already on is now a safe no-op (it won't reload), but prefer browser_get_elements/browser_screenshot to inspect the current page instead of calling browser_navigate again
- Browser: always call browser_get_elements before deciding you cannot complete a task
- Browser: use write_file to save any content to disk
- Browser: for a new or ambiguous request, check "Current browser page" above first, and use browser_get_elements/browser_screenshot to see what's actually on screen before acting - don't assume you're still on a page from earlier conversation
- Browser: never call browser_go_back unless the user explicitly asks to go back; it can navigate away from the page you're supposed to be working on
- Browser: when typing into a search bar, form field, or game input that should be submitted (e.g. a Wordle guess), call browser_type with press_enter=True instead of typing and then taking a separate action to submit
- After every browser action check the result before deciding what to do next
- Google account: use gmail_* tools to read/search/send email. Confirm with the user before sending an email unless they explicitly asked you to send it
- Google account: use calendar_* tools to view and create events. Resolve relative dates (e.g. "tomorrow", "next Monday") using the current date/time above
- Google account: use drive_* tools for files in the user's Google Drive - this is separate from the local filesystem tools
- Only ask the user for help if truly stuck after exhausting all options
- Always reply to the user in your own clear words. Never paste raw HTML, element-map/tool output, or URLs with tracking parameters directly into your reply
- If the user changes topic or asks you to abandon the current task, fully switch focus to their new request and disregard unrelated state or results from the abandoned task"""
    if persist_mode:
        instruction += "\n- PERSISTENT MODE IS ON: do not give up, ask the user for help, or stop early. Keep trying different approaches until the task is fully complete. Only stop if you hit an unrecoverable API error."
    if tool_log:
        instruction += f"\n\nRecent tool calls in this conversation:\n{tool_log}"
    if recall:
        instruction += ("\n\nRelevant things you recall (retrieved by similarity to the user's current "
                        "message - may be older conversation from beyond the recent history, saved facts, "
                        f"or emails; use if relevant, ignore if not):\n{recall}")
    return instruction
#Function to handle messages. Used the most often.

screenshot_tools = {"browser_navigate", "browser_screenshot", "browser_click", "browser_type", "browser_scroll", "browser_click_element", "browser_go_back"}
#Tools where repeating the exact same call is legitimate - observing state again (the page
#or inbox may have changed) or paging through content (scroll). These skip the duplicate
#check; the iteration cap still limits them. Action tools (write_file, gmail_send_email,
#browser_click...) keep the strict check - repeating those identically means a stuck loop.
duplicate_exempt_tools = {"browser_screenshot", "browser_get_elements", "browser_current_url", "browser_scroll", "read_memory", "read_file", "list_directory", "load_skill", "gmail_list_messages", "gmail_read_message", "calendar_list_events", "drive_list_files", "drive_read_file", "sheets_read", "load_tools"}
MAX_TOOL_ITERATIONS = 20
PERSIST_MAX_TOOL_ITERATIONS = 100
TELEGRAM_MAX_MESSAGE_CHARS = 4000 #Telegram rejects messages over 4096 chars - leave headroom
KEEP_RECENT_SCREENSHOTS = 2
PERSIST_MODE = False
#When True, risky tools run without a confirmation prompt. The blocklist (truly unrecoverable
#commands) is still enforced regardless - bypass only skips the "are you sure?" step.
BYPASS_CONFIRM = False
#Tools that change the outside world irreversibly enough to warrant a confirmation prompt
#before they run. write_file is intentionally excluded to avoid confirmation fatigue during
#normal multi-file work - writes are usually recoverable.
RISKY_TOOLS = {"run_shell", "run_background", "gmail_send_email", "move_file"}
CONFIRM_TIMEOUT_SECONDS = 120 #How long to wait for a yes/no before defaulting to deny
_APPROVE_WORDS = {"yes", "y", "approve", "confirm", "ok", "sure"}
#callback_data values for the inline Approve/Deny buttons on confirmation prompts
CONFIRM_YES = "confirm_yes"
CONFIRM_NO = "confirm_no"
#Maps a chat_id to a Future that the tool loop is awaiting; resolved by tapping an inline
#button (on_confirm_button) or by the chat's next text message (intercepted at the top of
#respond() - typing 'yes' still works). One pending confirmation per chat.
_pending_confirmations: dict[int, asyncio.Future] = {}
#Chats with a generation currently in flight, and chats that asked to /cancel it. The tool
#loop checks _cancel_requests before each tool call, so cancelling stops the task after the
#action already in progress finishes - it can't abort a tool mid-execution.
_active_generations: set[int] = set()
_cancel_requests: set[int] = set()
#One generation at a time per chat: with concurrent_updates(True) a second message would
#otherwise start a parallel tool loop that fights the first over the shared browser, the
#conversation history, and the single confirmation slot. Messages queue on the lock instead.
#Confirmation replies and /cancel are handled before/without the lock, so they still get
#through instantly while a task runs.
_generation_locks: dict[int, asyncio.Lock] = {}

def _get_generation_lock(chat_id: int) -> asyncio.Lock:
    lock = _generation_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _generation_locks[chat_id] = lock
    return lock
CHECKIN_INTERVAL_MINUTES = 60
CHECKIN_PROMPT = (
    "[Automated periodic check-in] Use gmail_list_messages with query "
    "'is:unread (category:primary OR (category:updates is:important))' to check for new mail. "
    "This deliberately limits results to the Primary inbox plus important Updates, and excludes "
    "Promotions, Social, and Forums - do NOT broaden the query to plain 'is:unread'. Only flag "
    "email that genuinely matters to the user (a real message from a person, a bill or "
    "account/security notice) - ignore marketing, newsletters, receipts, and automated noise "
    "even if it slips through. This is an email-only check - do NOT look at the calendar (a "
    "separate daily briefing handles events). Compare against what you've already told the user "
    "in the recent conversation above - do not repeat something you already flagged unless a new "
    "reply came in. If there's an email worth telling the user about, reply with a short message "
    "and call gmail_mark_as_read on each email you report. If there's nothing new and noteworthy, "
    "reply with exactly: NOTHING_TO_REPORT"
)
#Calendar lives here, not in the hourly check-in: a once-a-day briefing means the user hears
#about an event once (in the morning) instead of every hour. Runs at MORNING_BRIEFING_HOUR
#in the server's local timezone.
MORNING_BRIEFING_HOUR = 8
#Built fresh at fire time (see morning_briefing) so the two weekday names and the user's class
#schedule are baked straight in. The schedule is injected rather than fetched via read_schedule:
#the briefing ALWAYS needs it, so spending a tool call - and risking the model skipping it - buys
#nothing. {schedule_section} is empty when there's no schedule file, which degrades the briefing
#to calendar events only (exactly what it did before).
MORNING_PROMPT = (
    "[Morning briefing] Good morning. Today is {today}. Tomorrow is {tomorrow}.\n\n"
    "{schedule_section}"
    "Use calendar_list_events to look up the user's upcoming events, then give a short, friendly "
    "briefing of what's on for TODAY and TOMORROW only, under 'Today' and 'Tomorrow' headings. "
    "Under each heading merge that day's classes and calendar events into ONE list ordered by "
    "time, soonest first. Give every class its start time, name and room, e.g. "
    "\"09:00 - Linear Algebra (Room 302)\". Use the current date/time in your system info to skip "
    "anything that has already ended today. Keep it brief - a day with nothing on it just gets a "
    "line saying so. If there are genuinely no classes AND no events on either day, reply with "
    "exactly: NOTHING_TO_REPORT"
)
MORNING_SCHEDULE_SECTION = (
    "The user's class schedule, which repeats every week. Use ONLY the entries for the two "
    "weekdays named above and ignore the rest; treat those classes as fixed commitments "
    "alongside the calendar events:\n{schedule}\n\n"
)
SCHEDULED_TASK_PROMPT = (
    "[Scheduled task] The user previously asked you to do the following at this exact time. "
    "It is pre-approved - complete it now using your tools without asking for confirmation, "
    "then briefly tell the user what you did.\n\nTask: {task}"
)
SCHEDULED_TASK_POLL_SECONDS = 30
# Matches raw element-map lines (e.g. "[12] a: 'text' at (100, 200)") or pasted-HTML fragments
INVALID_REPLY_PATTERN = re.compile(r"^\[\d+\]\s+\w+:|target=\"_blank\"|utm_source=")

def _validate_tool_registrations() -> None:
    #Declarations are generated from tool_dict, so they can't drift apart anymore - the
    #one desync still possible is a dict key not matching its function's actual name
    #(the model calls tools by declaration name, which from_callable takes from __name__)
    mismatched = {name for name, fn in tool_dict.items() if fn.__name__ != name}
    if mismatched:
        raise RuntimeError(f"tool_dict keys don't match their function names: {mismatched}")
    registered = set(tool_dict)
    if not screenshot_tools <= registered:
        raise RuntimeError(f"screenshot_tools has unknown tools: {screenshot_tools - registered}")
    if not duplicate_exempt_tools <= registered:
        raise RuntimeError(f"duplicate_exempt_tools has unknown tools: {duplicate_exempt_tools - registered}")

_validate_tool_registrations()

async def _execute_tool(tool_name: str, args: dict) -> Any:
    """Looks up and runs a tool by name, returning its result (or an error string)."""
    if tool_name not in tool_dict:
        return f'Tool: {tool_name} not found'
    try:
        if inspect.iscoroutinefunction(tool_dict[tool_name]):
            return await tool_dict[tool_name](**args)
        return await asyncio.to_thread(tool_dict[tool_name], **args) #Sync tools run in a worker thread so they don't block the event loop
    except Exception as e:
        result = f"Error executing {tool_name}: {e}"
        logger.warning(result)
        return result

def _tool_result_parts(func: Any, tool_name: str, result: Any) -> list:
    """Builds the message parts for one tool's result, attaching an image part if the
    result includes a screenshot. Sending happens once per batch in _run_tool_loop."""
    def _text_part(text: str) -> list:
        return [types.Part(function_response=types.FunctionResponse(name=tool_name, id=func.id, response={"result": text}))]

    def _image_parts(image_b64: str, note: str) -> list:
        #A malformed/truncated screenshot must NOT crash the whole tool loop - degrade to a text
        #note so the model can recover (e.g. read the page with browser_get_elements) and continue.
        try:
            image_bytes = base64.b64decode(image_b64)
        except (ValueError, TypeError) as e:  #binascii.Error subclasses ValueError
            logger.warning("Bad screenshot from %s (%s); result[:80]=%r", tool_name, e, str(image_b64)[:80])
            return _text_part("Screenshot could not be captured this time. Use browser_get_elements to read the page instead.")
        return [
            types.Part(function_response=types.FunctionResponse(name=tool_name, id=func.id, response={"result": note})),
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
        ]

    if isinstance(result, tuple):  # (screenshot b64, element map)
        image_b64, element_map = result
        return _image_parts(image_b64, element_map)
    if tool_name in screenshot_tools and not result.startswith("Error"):
        return _image_parts(result, "Screenshot taken successfully")
    return _text_part(result)

def _prune_old_screenshots(chat: Any, keep_recent: int = KEEP_RECENT_SCREENSHOTS) -> None:
    """Strips inline image data from older tool-result turns in the live chat history,
    keeping only the most recent `keep_recent` screenshots so context size stays bounded
    no matter how many tool calls the loop makes."""
    history = chat.get_history(curated=True)
    image_indices = [
        i for i, content in enumerate(history)
        if content.parts and any(p.inline_data is not None for p in content.parts)
    ]
    for i in image_indices[:-keep_recent] if keep_recent else image_indices:
        content = history[i]
        content.parts = [
            types.Part(text="[older screenshot omitted to save context]")
            if p.inline_data is not None else p
            for p in content.parts
        ]

#Tool results whose old output goes stale as the task moves on: browser element maps describe a
#page the model has already acted on and left. Their text is the biggest text-bloat in a long
#browser loop, so old ones are collapsed to a placeholder (the recent few stay intact for the
#current page). Reference-data results (file reads, emails, Drive, sheets) are NOT pruned - the
#model may still need them turns later.
PRUNABLE_RESULT_TOOLS = {"browser_get_elements"}
KEEP_RECENT_TOOL_RESULTS = 3

def _prune_old_tool_results(chat: Any, keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> None:
    """Replaces the text of older prunable tool results (stale browser element maps) with a
    short placeholder, keeping the most recent `keep_recent` intact. The function_response part
    stays in place (the API requires one per call) - only its payload is shrunk."""
    history = chat.get_history(curated=True)
    targets = [  #(content_index, part_index) of each prunable function-response, in order
        (ci, pi)
        for ci, content in enumerate(history) if content.parts
        for pi, p in enumerate(content.parts)
        if getattr(p, "function_response", None) is not None and p.function_response.name in PRUNABLE_RESULT_TOOLS
    ]
    for ci, pi in (targets[:-keep_recent] if keep_recent else targets):
        fr = history[ci].parts[pi].function_response
        history[ci].parts[pi] = types.Part(function_response=types.FunctionResponse(
            name=fr.name, id=fr.id, response={"result": "[earlier page state omitted to save context]"}))

_TOOL_STATUS: dict[str, str] = {
    "run_shell":             "Running a command...",
    "run_background":        "Starting a background process...",
    "read_process_output":   "Reading process output...",
    "send_process_input":    "Typing into a process...",
    "stop_process":          "Stopping a process...",
    "list_processes":        "Listing processes...",
    "write_file":            "Writing a file...",
    "read_file":             "Reading a file...",
    "list_directory":        "Listing files...",
    "find_file":             "Searching for a file...",
    "move_file":             "Moving a file...",
    "web_search":            "Searching the web...",
    "browser_navigate":      "Browsing the web...",
    "browser_screenshot":    "Taking a screenshot...",
    "browser_click":         "Clicking...",
    "browser_click_element": "Clicking...",
    "browser_type":          "Typing in browser...",
    "browser_scroll":        "Scrolling...",
    "browser_get_elements":  "Reading the page...",
    "browser_go_back":       "Going back...",
    "gmail_list_messages":   "Checking email...",
    "gmail_read_message":    "Reading an email...",
    "gmail_send_email":      "Sending an email...",
    "gmail_mark_as_read":    "Marking email as read...",
    "calendar_list_events":  "Checking calendar...",
    "calendar_create_event": "Creating a calendar event...",
    "drive_list_files":      "Listing Drive files...",
    "drive_read_file":       "Reading a Drive file...",
    "drive_upload_file":     "Uploading to Drive...",
    "drive_download_file":   "Downloading from Drive...",
    "sheets_read":           "Reading a spreadsheet...",
    "sheets_update":         "Updating a spreadsheet...",
    "sheets_append":         "Adding to a spreadsheet...",
    "load_tools":            "Loading more tools...",
    "save_memory":           "Saving to memory...",
    "read_memory":           "Reading memory...",
    "load_skill":            "Loading a skill...",
    "read_schedule":         "Checking your class schedule...",
    "save_skill":            "Saving a skill...",
    "delete_skill":          "Deleting a skill...",
    "schedule_task":         "Scheduling a task...",
}

async def _keep_typing(bot: Any, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

#When True (default), answered confirmation prompts stay in the chat, edited to show the
#outcome (an audit trail of what was approved). When False, they're deleted once answered.
#Timed-out prompts always stay - a missed confirmation shouldn't vanish silently.
KEEP_CONFIRM_PROMPTS = True

class _StatusMessage:
    """The single live 'what the bot is doing' message. Besides updating its text, it can
    bump itself to the bottom of the chat (delete + re-send with the same text) - needed
    after a confirmation prompt, which otherwise strands the status message above it."""
    def __init__(self, bot: Any, chat_id: int, silent: bool = False):
        self.bot = bot
        self.chat_id = chat_id
        self.silent = silent #proactive check-ins/scheduled tasks shouldn't ping the user's phone
        self.msg: Any = None
        self.text = "Thinking..."

    async def start(self, text: str) -> None:
        self.text = text
        self.msg = await self.bot.send_message(chat_id=self.chat_id, text=text, disable_notification=self.silent)

    async def update(self, text: str) -> None:
        self.text = text
        try:
            await self.msg.edit_text(text)
        except Exception:
            pass

    async def bump_to_bottom(self) -> None:
        try:
            await self.msg.delete()
        except Exception:
            pass
        try:
            #Always silent: this is a reposition, not news worth a notification
            self.msg = await self.bot.send_message(chat_id=self.chat_id, text=self.text, disable_notification=True)
        except Exception:
            pass

    async def delete(self) -> None:
        try:
            await self.msg.delete()
        except Exception:
            pass

def _make_confirm_callback(bot: Any, chat_id: int, status: Any = None):
    """Builds an async callback the tool loop can await before running a risky tool. It
    messages the owner, then blocks on a Future that respond() resolves with the owner's
    next message. Times out to a denial so an unattended risky call is cancelled, not run."""
    async def confirm(description: str) -> bool:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _pending_confirmations[chat_id] = fut
        keyboard = telegram.InlineKeyboardMarkup([[
            telegram.InlineKeyboardButton("✅ Approve", callback_data=CONFIRM_YES),
            telegram.InlineKeyboardButton("❌ Deny", callback_data=CONFIRM_NO),
        ]])
        prompt_text = f"⚠️ I want to run:\n{description}"
        answer: str | None = None #None = timed out (vs an explicit denial)
        try:
            prompt_msg = await bot.send_message(chat_id=chat_id, text=prompt_text, reply_markup=keyboard)
            answer = await asyncio.wait_for(fut, timeout=CONFIRM_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass
        finally:
            _pending_confirmations.pop(chat_id, None)
        approved = answer is not None and answer.strip().lower() in _APPROVE_WORDS
        if approved:
            outcome = "✅ Approved"
        elif answer is None:
            outcome = "⏰ No response - action cancelled."
        else:
            outcome = "❌ Denied"
        if KEEP_CONFIRM_PROMPTS or answer is None:
            #Edit the prompt to show the outcome and drop the buttons, so a stale prompt can't
            #be tapped later and the chat log reads as a record of what was approved.
            try:
                await prompt_msg.edit_text(f"{prompt_text}\n\n{outcome}")
            except Exception:
                pass
        else:
            #Disappear mode: the answered prompt is clutter - remove it entirely
            try:
                await prompt_msg.delete()
            except Exception:
                pass
        #Either way the status message is no longer the newest message (the prompt and/or
        #the user's typed reply landed after it) - move it back to the bottom.
        if status is not None:
            await status.bump_to_bottom()
        return approved
    return confirm

async def _run_tool_loop(chat: Any, response: Any, persist_mode: bool = False, status_callback: Any = None, conversation_id: int = 0, confirm_callback: Any = None, chat_id: int = 0, rebuild_chat: Any = None, active_groups: set | None = None) -> tuple[Any, bool, list[str]]:
    """Repeatedly executes tool calls requested by the model until it stops calling tools,
    a duplicate call is detected, or the iteration cap is exceeded. Returns the final
    response, whether the loop gave up early, and a compact log of every tool call made.
    When the model calls load_tools, the requested groups are added to `active_groups` and the
    chat is rebuilt (via rebuild_chat) with the expanded tool set before the next model turn."""
    seen_calls = set()
    give_up = False
    iteration_count = 0
    tool_entries: list[str] = []
    max_iterations = PERSIST_MAX_TOOL_ITERATIONS if persist_mode else MAX_TOOL_ITERATIONS
    while response.function_calls and not give_up: #Checks if the AI called any tools in its response
        #Execute every call in this response first, collect all the results, then answer
        #them in ONE message - the API requires a response for each call in the turn, and
        #sending mid-batch would generate a new model response while we're still iterating
        #over the old one
        parts = []
        stop_executing = False #Set when the cap/duplicate check trips mid-batch; the rest get a skip message
        needs_rebuild = False #Set when load_tools expanded the group set; rebuild before the next turn
        for func in response.function_calls:
            call_key = f"{func.name}_{func.args}"
            tool_name = func.name
            logger.debug("Tool call: %s", tool_name)

            iteration_count += 1
            if stop_executing:
                result = "Skipped - stopping tool use now."
            elif chat_id in _cancel_requests:
                _cancel_requests.discard(chat_id)
                result = ("The user sent /cancel - stop this task NOW. Do not call any more tools. "
                          "Reply with a brief note on what you finished and what's left undone.")
                give_up = True
                stop_executing = True
            elif iteration_count > max_iterations:
                result = "You've taken too many actions on this task. Stop here and respond to the user now, summarizing what you've done so far and what's left."
                give_up = True
                stop_executing = True
            elif call_key in seen_calls and tool_name not in duplicate_exempt_tools:
                if persist_mode:
                    result = "You've already tried that exact call. Try a different approach instead of repeating it."
                else:
                    result = "This approach isn't working. Tell the user you're unable to complete the task and ask them for more information."
                    give_up = True
                    stop_executing = True
            elif tool_name == "load_tools":
                #Meta-tool: expand the loaded tool groups instead of executing anything. Handled
                #here (not via _execute_tool) so it can trigger the chat rebuild below.
                seen_calls.add(call_key)
                if active_groups is None:
                    result = "You already have all tools available - no need to load more."
                else:
                    requested = {g.strip() for g in str(func.args.get("groups", "")).split(",") if g.strip() in TOOL_GROUPS}
                    newly = requested - active_groups
                    active_groups |= requested
                    if newly:
                        needs_rebuild = True
                        result = f"Loaded tool groups: {', '.join(sorted(newly))}. Those tools are now available - use them."
                    elif requested:
                        result = "Those tool groups are already loaded and available."
                    else:
                        result = f"No known tool groups in '{func.args.get('groups', '')}'. Valid groups: {', '.join(TOOL_GROUPS)}."
                tool_entries.append(f"load_tools({func.args.get('groups', '')}) → {result[:80]}")
                parts.extend(_tool_result_parts(func, tool_name, result))
                continue
            else:
                seen_calls.add(call_key)
                if confirm_callback and tool_name in RISKY_TOOLS and not BYPASS_CONFIRM:
                    approved = await confirm_callback(f"{tool_name}({str(dict(func.args))[:200]})")
                    if not approved:
                        result = "User declined this action. Do not retry it; ask what they'd like instead."
                        tool_entries.append(f"{tool_name}(...) → DECLINED by user")
                        parts.extend(_tool_result_parts(func, tool_name, result))
                        continue
                if status_callback:
                    try:
                        await status_callback(_TOOL_STATUS.get(tool_name, f"Using {tool_name}..."))
                    except Exception:
                        pass
                result = await _execute_tool(tool_name, func.args)
                args_summary = str(dict(func.args))[:150]
                result_text = result[1] if isinstance(result, tuple) else str(result)
                tool_entries.append(f"{tool_name}({args_summary}) → {result_text[:200]}")
            parts.extend(_tool_result_parts(func, tool_name, result))
        if needs_rebuild and rebuild_chat is not None:
            #Recreate the chat with the expanded tool set, carrying the history forward so the
            #model's next turn can actually call the newly-loaded tools.
            chat = rebuild_chat(chat.get_history(), active_groups)
            logger.info("Tools expanded mid-task -> now: %s", ",".join(sorted(active_groups)))
        response = await chat.send_message(parts)
        _prune_old_screenshots(chat)
        _prune_old_tool_results(chat)
    return response, give_up, tool_entries

def _finalize_reply(response: Any, give_up: bool) -> str:
    """Validates the model's final text, substituting a friendly message if it's empty or
    looks like raw tool/HTML output that shouldn't be shown to the user."""
    final_text = response.text #type: ignore
    if not final_text or not final_text.strip():
        finish_reason = None
        try:
            finish_reason = response.candidates[0].finish_reason #type: ignore
        except (IndexError, AttributeError):
            pass
        logger.warning("Model returned an empty response (finish_reason=%s, give_up=%s)", finish_reason, give_up)
        if give_up:
            return "I got stuck while working on that and wasn't able to finish. Could you give me more guidance, or try a different approach?"
        return "Sorry, I couldn't come up with a response there. Could you try rephrasing?"
    if INVALID_REPLY_PATTERN.search(final_text):
        logger.warning("Model returned an invalid/raw reply, discarding: %r", final_text)
        return "Sorry, I couldn't come up with a response there. Could you try rephrasing?"
    return final_text

def _prep_context(prompt: str, enable_recall: bool, recent_texts: set, limit_tools: bool) -> tuple[list, Any]:
    """Blocking work done in one worker-thread hop: embed the message ONCE, then reuse that
    vector for both semantic recall (older relevant context) and dynamic tool-group selection.
    Returns (recalled_results, selected_group_names_or_None). None means 'all tools'."""
    vector = embeddings.embed_text(prompt) if (enable_recall or limit_tools) and prompt else None
    recalled = embeddings.recall_with_vector(vector, RECALL_K, recent_texts) if enable_recall else []
    groups = _select_groups(vector) if limit_tools else None
    return recalled, groups

async def _generate_response(prompt: str, chat_id: int = 0, persist_mode: bool = False, status_callback: Any = None, confirm_callback: Any = None, media_parts: list | None = None, enable_recall: bool = False, limit_tools: bool = False) -> tuple[str, bool, list[str]]:
    """Builds a chat from the stored conversation history, sends `prompt` to the model, runs the
    tool loop, and returns (final_text, give_up). Does not touch the conversation history table -
    callers decide what (if anything) to record. media_parts optionally attaches images/audio
    (as types.Part) ahead of the prompt text, for photo/voice messages. enable_recall pulls in
    semantically-related older context; limit_tools loads only the tool groups relevant to the
    message (both only for focused requests, not arbitrary scheduled tasks)."""
    _active_generations.add(chat_id)
    _cancel_requests.discard(chat_id) #a stale /cancel from an earlier task must not kill this one
    try:
        memory = read_memory() #Reads the bot's memory. This memory stores important information only.
        conversation_id = database.get_active_conversation_id()
        conversation = read_conversation(RECENT_HISTORY_MESSAGES, conversation_id) #Recent turns for conversational continuity; older-but-relevant context comes from semantic recall below
        contents=[types.Content(role=msg["role"], parts=[types.Part(text=msg["parts"][0])]) for msg in conversation] #Converts the conversation history into the correct format for Gemini API
        #Embed the message once (off the event loop) and reuse it for recall + tool selection
        recent_texts = {msg["parts"][0] for msg in conversation} if enable_recall else set()
        recall_results, active_groups = await asyncio.to_thread(_prep_context, prompt, enable_recall, recent_texts, limit_tools)
        recalled = "\n".join(f"- ({r['source']}) {r['text']}" for r in recall_results)
        browser_url = t.browser_current_url() #Grounds the model in the browser's actual current page, regardless of what past conversation text says
        now = datetime.now().astimezone().isoformat() #Grounds the model in the current date/time for resolving relative dates (e.g. calendar events)
        logs = database.get_tool_logs(conversation_id)
        tool_log = "\n".join(f"- {entry}" for entry in logs)
        system_instruction = build_system_instruction(memory, browser_url, now, persist_mode, tool_log, recalled)
        if limit_tools:
            selected = "all" if active_groups is None else ",".join(sorted(active_groups))
            n = len(tool_dict) if active_groups is None else len(_tool_object_for_groups(active_groups).function_declarations)
            logger.info("Tool selection: groups=[%s] -> %d/%d tools", selected, n, len(tool_dict))
        #Rebuilds the chat with a (possibly expanded) tool set while preserving history - used to
        #create the initial chat and again when load_tools pulls in more groups mid-task.
        def rebuild_chat(history: Any, groups: set | None) -> Any:
            return client.aio.chats.create(
                model=GEMINI_MODEL, history=history, #type: ignore
                config=types.GenerateContentConfig(tools=[_tool_object_for_groups(groups)], system_instruction=system_instruction),
            )
        chat = rebuild_chat(contents, active_groups)
        #With media attached, the message is a list of Parts (media first, then the text)
        message = media_parts + [types.Part(text=prompt)] if media_parts else prompt
        response = await chat.send_message(message)
        response, give_up, tool_entries = await _run_tool_loop(chat, response, persist_mode, status_callback, conversation_id, confirm_callback, chat_id, rebuild_chat, active_groups) #Executes any tool calls the model requested
        return _finalize_reply(response, give_up), give_up, tool_entries
    finally:
        _active_generations.discard(chat_id)
        _cancel_requests.discard(chat_id) #a cancel that arrived too late to be seen shouldn't linger

def _describe_error(e: Exception) -> str:
    """Turns an exception into a friendly user-facing message, using the API's real
    error codes instead of string-matching the error text."""
    if isinstance(e, errors.APIError):
        if e.code == 429:
            return "I've hit my API rate limit. Please wait a moment and try again."
        if e.code == 503:
            return "Gemini is currently unavailable. Please try again in a few minutes."
        if e.code == 400 and "token" in str(e).lower():
            return "The conversation is too long. Try /clear and start fresh."
    return f"Something went wrong: {e}"

def _chunk_message(text: str) -> list:
    """Splits a reply into pieces under Telegram's message length limit - sending one
    oversized message raises BadRequest and the user would get nothing at all."""
    return [text[i:i + TELEGRAM_MAX_MESSAGE_CHARS] for i in range(0, len(text), TELEGRAM_MAX_MESSAGE_CHARS)] or [text]

#Recent turns kept in-context for conversational continuity (recency/order/pronoun resolution).
#Trimmed from 30 to 10 once semantic recall existed to backfill older-but-relevant context.
RECENT_HISTORY_MESSAGES = 10
RECALL_K = 5  #how many semantically-similar past items to pull into the prompt per message
#Holds references to fire-and-forget background tasks (embedding). Without a live reference
#the event loop can garbage-collect a running task; the done-callback drops it when finished.
_background_tasks: set = set()

def _embed_in_background(source: str, text: str) -> None:
    """Embeds `text` for semantic recall without blocking the reply path - the network embed
    call runs in a worker thread, fire-and-forget (remember() swallows its own errors)."""
    if not text or not text.strip():
        return
    task = asyncio.create_task(asyncio.to_thread(embeddings.remember, source, text))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

async def _handle_user_request(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE, user_message: str, history_text: str, media_parts: list | None = None) -> None:
    """Shared pipeline for every kind of user message (text, photo, voice): queue on the
    per-chat lock, show status, run the generation, record history, and send the reply.
    `user_message` is the prompt text the model sees; `history_text` is what the conversation
    log records (media bytes aren't persisted - just a placeholder like '[photo] ...')."""
    if update.message is None or update.effective_chat is None:
        return
    int_chat_id = update.effective_chat.id
    lock = _get_generation_lock(int_chat_id)
    if lock.locked():
        #A task is already running - tell the user this message is queued, not lost
        await update.message.reply_text(
            "Got it - I'll start on this as soon as the current task finishes. (Send /cancel to stop that one.)",
            disable_notification=True,
        )
    async with lock:
        status = _StatusMessage(context.bot, int_chat_id)
        await status.start("Thinking...")
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(context.bot, int_chat_id, stop_typing))
        confirm = _make_confirm_callback(context.bot, int_chat_id, status)

        try:
            chat_id = str(int_chat_id) #Captures where to send proactive/unprompted messages later
            if database.get_setting("chat_id") != chat_id:
                database.set_setting("chat_id", chat_id)
            conversation_id = database.get_active_conversation_id()
            add_to_conversation("user", history_text, conversation_id) #Saves the user's message to the conversation history in the database
            final_text, _, tool_entries = await _generate_response(user_message, chat_id=int_chat_id, persist_mode=PERSIST_MODE, status_callback=status.update, confirm_callback=confirm, media_parts=media_parts, enable_recall=True, limit_tools=True)
            add_to_conversation("model", final_text, conversation_id, tool_log="\n".join(tool_entries)) #Saves the AI's response to the conversation history in the database
            for chunk in _chunk_message(final_text): #Sends the AI's response back to the user on Telegram
                await update.message.reply_text(chunk)
            #Embed this turn (user + reply) for long-term semantic recall - off the reply path
            _embed_in_background("message", history_text)
            _embed_in_background("message", final_text)
        except Exception as e:
            logger.exception("Failed to handle message") #Keep a full traceback in the console, not just the Telegram reply
            await update.message.reply_text(_describe_error(e))
        finally:
            stop_typing.set()
            typing_task.cancel()
            await status.delete()

async def respond(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    #Edited messages, channel posts, reactions etc. arrive with message=None - nothing to respond to
    if update.message is None or update.message.text is None or update.effective_chat is None:
        return
    int_chat_id = update.effective_chat.id
    #If the tool loop is waiting on a confirmation for this chat, this message IS the answer -
    #hand it to the waiting Future and stop here instead of starting a new generation.
    pending = _pending_confirmations.get(int_chat_id)
    if pending is not None and not pending.done():
        pending.set_result(update.message.text)
        return
    await _handle_user_request(update, context, user_message=update.message.text, history_text=update.message.text)

#Cost control: Gemini bills images by 768px tiles, so resolution directly drives token cost.
#Telegram offers each photo in several sizes (~90/320/800/1280/2560px); we take the largest
#one at or under this cap instead of the full-res original. 800 keeps screenshots/receipts
#readable at a fraction of the cost - bump to 1280 if fine text comes out misread.
PHOTO_MAX_DIMENSION = 800

async def respond_photo(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles a photo message: downloads a cost-capped resolution (see PHOTO_MAX_DIMENSION)
    and sends it to the model as an image part, with the caption (if any) as the prompt.
    History gets a '[photo]' placeholder - the image itself is only seen for this turn."""
    if update.message is None or not update.message.photo or update.effective_chat is None:
        return
    #Sizes come smallest-first; take the largest within the cap, or the smallest if somehow all exceed it
    photo = next(
        (p for p in reversed(update.message.photo) if max(p.width, p.height) <= PHOTO_MAX_DIMENSION),
        update.message.photo[0],
    )
    file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())
    caption = (update.message.caption or "").strip()
    user_message = caption if caption else "The user sent this photo with no caption. Look at it and respond appropriately."
    history_text = f"[photo] {caption}" if caption else "[photo]"
    media = [types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")] #Telegram photos are always JPEG
    await _handle_user_request(update, context, user_message=user_message, history_text=history_text, media_parts=media)

VOICE_MAX_BYTES = 20 * 1024 * 1024 #Telegram's bot-API download limit; larger files error anyway

async def respond_voice(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles a voice message: downloads the audio and sends it to the model directly
    (Gemini understands speech natively - no separate transcription service). History gets
    a '[voice message]' placeholder - the audio is only heard for this turn."""
    if update.message is None or update.message.voice is None or update.effective_chat is None:
        return
    voice = update.message.voice
    if voice.file_size and voice.file_size > VOICE_MAX_BYTES:
        await update.message.reply_text("That voice message is too large for me to download (Telegram caps bot downloads at 20MB). Could you send a shorter one?")
        return
    file = await context.bot.get_file(voice.file_id)
    audio_bytes = bytes(await file.download_as_bytearray())
    user_message = ("The user sent a voice message (attached). Listen to it and respond to what "
                    "they say exactly as if they had typed it - including using your tools if "
                    "they ask you to do something.")
    history_text = "[voice message]"
    media = [types.Part.from_bytes(data=audio_bytes, mime_type=voice.mime_type or "audio/ogg")] #Telegram voice notes are OGG/Opus
    await _handle_user_request(update, context, user_message=user_message, history_text=history_text, media_parts=media)
   

   
    
   
async def on_confirm_button(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolves the pending confirmation Future when the owner taps Approve/Deny on a
    confirmation prompt. Stale taps (no confirmation waiting anymore) just clear the buttons."""
    query = update.callback_query
    if query is None:
        return
    if update.effective_user is None or update.effective_user.id != ALLOWED_USER_ID:
        await query.answer() #CallbackQueryHandler can't take a user filter, so enforce it here
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    pending = _pending_confirmations.get(chat_id) if chat_id is not None else None
    if pending is None or pending.done():
        await query.answer("This confirmation has expired.")
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    pending.set_result("yes" if query.data == CONFIRM_YES else "no")
    await query.answer()

async def cancel(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stops the task currently running for this chat. The flag is picked up before the next
    tool call, so the action already in progress still finishes; a pending risky-tool
    confirmation is auto-denied so the loop isn't left waiting on it."""
    if update.message is None or update.effective_chat is None:
        return
    chat_id = update.effective_chat.id
    pending = _pending_confirmations.get(chat_id)
    if chat_id not in _active_generations and (pending is None or pending.done()):
        await update.message.reply_text("Nothing is running right now.")
        return
    _cancel_requests.add(chat_id)
    if pending is not None and not pending.done():
        pending.set_result("no") #deny the pending confirmation so the tool loop unblocks immediately
    await update.message.reply_text("Okay - cancelling. I'll stop after the current action finishes.")

async def clear(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    database.clear_conversation(database.get_active_conversation_id())
    await update.message.reply_text("Conversation cleared!")

async def toggle_persist(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    global PERSIST_MODE
    PERSIST_MODE = not PERSIST_MODE
    if PERSIST_MODE:
        status = "ON - I won't give up on a task until it's done or I hit an API error."
    else:
        status = "OFF."
    await update.message.reply_text(f"Persistent mode is now {status}")

async def toggle_prompts(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    global KEEP_CONFIRM_PROMPTS
    KEEP_CONFIRM_PROMPTS = not KEEP_CONFIRM_PROMPTS
    if KEEP_CONFIRM_PROMPTS:
        status = "KEPT - answered confirmation prompts stay in the chat, marked ✅/❌, as a record of what you approved."
    else:
        status = "CLEANED UP - confirmation prompts disappear once you answer them. (Timed-out prompts always stay so a missed one isn't hidden.)"
    await update.message.reply_text(f"Confirmation prompts are now {status}")

async def toggle_bypass(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    global BYPASS_CONFIRM
    BYPASS_CONFIRM = not BYPASS_CONFIRM
    if BYPASS_CONFIRM:
        status = ("🚨 ON - I will run shell commands, send emails, and move files WITHOUT asking "
                  "you first. This is dangerous; only leave it on if you know what you're doing. "
                  "(Truly destructive commands like format/shutdown are still always blocked.) "
                  "Send /bypass again to turn it back off.")
    else:
        status = "OFF - I'll ask before risky actions again."
    await update.message.reply_text(f"Confirmation bypass is now {status}")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

async def update_bot(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual deploy trigger: pulls the latest commits for the *current branch* and restarts
    the bot on the new code. Complements the auto-deploy timer (which only watches main) -
    useful for 'I want it now' and for testing a feature branch on the server."""
    if update.message is None or update.effective_chat is None:
        return
    if _get_generation_lock(update.effective_chat.id).locked():
        await update.message.reply_text("A task is running right now - restarting would kill it. Finish or /cancel it first, then /update again.")
        return
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=BASE_DIR)
    #Refuse to clobber uncommitted work - matters on a dev machine, never on the server
    dirty = await asyncio.to_thread(git, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        await update.message.reply_text("There are uncommitted local changes - refusing to update over them.")
        return
    branch = (await asyncio.to_thread(git, "rev-parse", "--abbrev-ref", "HEAD")).stdout.strip()
    fetch = await asyncio.to_thread(git, "fetch", "origin", branch)
    if fetch.returncode != 0:
        await update.message.reply_text(f"git fetch failed:\n{fetch.stderr[:500]}")
        return
    local = (await asyncio.to_thread(git, "rev-parse", "HEAD")).stdout.strip()
    remote = (await asyncio.to_thread(git, "rev-parse", f"origin/{branch}")).stdout.strip()
    if local == remote:
        await update.message.reply_text(f"Already up to date ({branch} @ {local[:7]}).")
        return
    reset = await asyncio.to_thread(git, "reset", "--hard", f"origin/{branch}")
    if reset.returncode != 0:
        await update.message.reply_text(f"git reset failed:\n{reset.stderr[:500]}")
        return
    pip = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "pip", "install", "-r", os.path.join(BASE_DIR, "requirements.txt")],
        capture_output=True, text=True,
    )
    if pip.returncode != 0:
        #New code is on disk but deps failed - don't restart into a broken state
        await update.message.reply_text(f"pip install failed - NOT restarting:\n{pip.stderr[-500:]}")
        return
    await update.message.reply_text(f"Updated {branch}: {local[:7]} -> {remote[:7]}. Restarting - back in a few seconds.")
    args = [sys.executable, *sys.argv]
    if os.name == "nt":
        #Windows execv doesn't quote args itself; paths with spaces break without this
        args = [f'"{a}"' if " " in a else a for a in args]
    os.execv(sys.executable, args) #Replaces this process with a fresh one on the new code

async def new_conversation(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    conv_id = database.create_conversation()
    database.set_active_conversation_id(conv_id)
    await update.message.reply_text(f"Started Conversation {conv_id}. This is now active.")

async def list_conversations_cmd(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    active_id = database.get_active_conversation_id()
    lines = [
        f"{'-> ' if c['id'] == active_id else '   '}{c['id']}. {c['title']}"
        for c in database.list_conversations()
    ]
    await update.message.reply_text("\n".join(lines))

async def switch_conversation(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /switch <number>")
        return
    conv_id = int(context.args[0])
    if not database.conversation_exists(conv_id):
        await update.message.reply_text(f"No Conversation {conv_id}. Use /list to see all conversations.")
        return
    database.set_active_conversation_id(conv_id)
    await update.message.reply_text(f"Switched to Conversation {conv_id}.")

async def rename_conversation_cmd(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Usage: /rename <name>")
        return
    name = " ".join(context.args)
    active_id = database.get_active_conversation_id()
    database.rename_conversation(active_id, name)
    await update.message.reply_text(f"Renamed Conversation {active_id} to '{name}'.")

async def _run_unprompted_check(context: ContextTypes.DEFAULT_TYPE, prompt: str, status_text: str, skip_if_busy: bool = True, limit_tools: bool = True) -> None:
    """Shared machinery for the timer-driven jobs (hourly email check-in, morning briefing):
    take the per-chat lock, run a generation on `prompt`, and message the user unless the
    model replies NOTHING_TO_REPORT. skip_if_busy=True bails when the user has a task in flight
    (fine for the hourly check - another runs soon); False waits for the lock instead, so a
    once-a-day briefing isn't lost just because a task happened to be running when it fired.
    limit_tools=False offers the full tool set instead of the groups matching the prompt - for a
    prompt carrying a big chunk of injected text (the morning briefing's schedule), which would
    otherwise dilute the embedding that picks those groups."""
    chat_id = database.get_setting("chat_id")
    if not chat_id:
        return
    int_chat_id = int(chat_id)
    lock = _get_generation_lock(int_chat_id)
    if skip_if_busy and lock.locked():
        return #the user's task takes priority - skip this cycle, the next one is soon
    async with lock:
        #Silent - these fire unprompted and often find nothing; the notification for the
        #deleted status message would still ping the user's phone. A real report still notifies.
        status = _StatusMessage(context.bot, int_chat_id, silent=True)
        await status.start(status_text)
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(_keep_typing(context.bot, int_chat_id, stop_typing))
        confirm = _make_confirm_callback(context.bot, int_chat_id, status)
        try:
            final_text, give_up, tool_entries = await _generate_response(prompt, chat_id=int_chat_id, confirm_callback=confirm, limit_tools=limit_tools)
        except Exception:
            logger.exception("Unprompted check failed")
            return
        finally:
            stop_typing.set()
            typing_task.cancel()
            await status.delete()
        if give_up or final_text.strip().upper() == "NOTHING_TO_REPORT":
            return
        for chunk in _chunk_message(final_text):
            await context.bot.send_message(chat_id=int_chat_id, text=chunk)
        add_to_conversation("model", final_text, database.get_active_conversation_id(), tool_log="\n".join(tool_entries))

async def proactive_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly: checks for noteworthy unread email and messages the user if something matters."""
    await _run_unprompted_check(context, CHECKIN_PROMPT, "Checking in...")

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Once daily: today's and tomorrow's classes (from the user's schedule file) merged with
    their calendar events. Names both weekdays explicitly so picking the right rows out of a
    weekly schedule isn't left to the model's own date arithmetic."""
    now = datetime.now().astimezone()
    schedule = t.schedule_text()
    #str.format doesn't re-scan substituted values, so stray braces in the schedule file are safe
    prompt = MORNING_PROMPT.format(
        today=now.strftime("%A, %B %d"),
        tomorrow=(now + timedelta(days=1)).strftime("%A, %B %d"),
        schedule_section=MORNING_SCHEDULE_SECTION.format(schedule=schedule) if schedule else "",
    )
    #Full tool set: the injected schedule text would skew the group-selection embedding away
    #from the google group this briefing needs, and it's one call a day - not worth the risk.
    await _run_unprompted_check(context, prompt, "Preparing your morning briefing...", skip_if_busy=False, limit_tools=False)

async def check_scheduled_tasks(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs and clears any scheduled tasks whose due time has passed, using the full tool loop
    so the model can actually act (send emails, create files, etc.), then reports back to the
    user."""
    now_ts = datetime.now().timestamp()
    for task in database.get_due_tasks(now_ts):
        #Mark running instead of deleting up front - if the bot crashes mid-task, the row
        #survives and gets re-queued on the next startup instead of silently vanishing
        database.mark_task_running(task["id"])
        int_chat_id = int(task["chat_id"])
        #Waits for the lock (unlike the check-in, which skips): the user explicitly asked
        #for this to run, so it queues behind whatever is in flight rather than being dropped.
        async with _get_generation_lock(int_chat_id):
            status = _StatusMessage(context.bot, int_chat_id, silent=True)
            await status.start("Running scheduled task...")
            stop_typing = asyncio.Event()
            typing_task = asyncio.create_task(_keep_typing(context.bot, int_chat_id, stop_typing))
            confirm = _make_confirm_callback(context.bot, int_chat_id, status)
            try:
                final_text, _, tool_entries = await _generate_response(SCHEDULED_TASK_PROMPT.format(task=task["task"]), chat_id=int_chat_id, confirm_callback=confirm)
            except Exception:
                logger.exception("Scheduled task failed")
                final_text = f"I tried to run a scheduled task but hit an error: {task['task']}"
                tool_entries = []
            finally:
                stop_typing.set()
                typing_task.cancel()
                await status.delete()
            database.delete_scheduled_task(task["id"])
            for chunk in _chunk_message(final_text):
                await context.bot.send_message(chat_id=int_chat_id, text=chunk)
            add_to_conversation("model", final_text, database.get_active_conversation_id(), tool_log="\n".join(tool_entries))


async def _post_init(application: Any) -> None:
    await application.bot.set_my_commands([
        telegram.BotCommand("start", "Greet the bot"),
        telegram.BotCommand("cancel", "Stop the task the bot is currently working on"),
        telegram.BotCommand("clear", "Clear the active conversation's history"),
        telegram.BotCommand("persist", "Toggle persistent mode (don't give up until the task is done)"),
        telegram.BotCommand("bypass", "DANGER: toggle skipping the confirmation prompt for risky actions"),
        telegram.BotCommand("prompts", "Toggle whether answered confirmation prompts stay in the chat or disappear"),
        telegram.BotCommand("new", "Start a new conversation"),
        telegram.BotCommand("list", "List all conversations"),
        telegram.BotCommand("switch", "Switch to a conversation by number"),
        telegram.BotCommand("rename", "Rename the active conversation"),
        telegram.BotCommand("update", "Pull the latest code from the repo and restart"),
    ])

def main() -> None:
    requeued = database.reset_running_tasks()
    if requeued:
        logger.warning("Re-queued %d scheduled task(s) interrupted by a previous shutdown", requeued)
    #Only respond to the owner - the bot has shell/email access, so ignore everyone else
    user_filter = filters.User(user_id=ALLOWED_USER_ID)
    #concurrent_updates so a confirmation reply is processed while the tool loop awaits it -
    #the default sequential mode would deadlock (the awaiting handler blocks the next update)
    application = ApplicationBuilder().token(telegram_key).post_init(_post_init).concurrent_updates(True).build() # type: ignore
    application.add_handler(CommandHandler("start", start, filters=user_filter))
    application.add_handler(CallbackQueryHandler(on_confirm_button, pattern=r"^confirm_"))
    application.add_handler(CommandHandler("cancel", cancel, filters=user_filter))
    application.add_handler(CommandHandler("clear", clear, filters=user_filter))
    application.add_handler(CommandHandler("persist", toggle_persist, filters=user_filter))
    application.add_handler(CommandHandler("bypass", toggle_bypass, filters=user_filter))
    application.add_handler(CommandHandler("prompts", toggle_prompts, filters=user_filter))
    application.add_handler(CommandHandler("new", new_conversation, filters=user_filter))
    application.add_handler(CommandHandler("list", list_conversations_cmd, filters=user_filter))
    application.add_handler(CommandHandler("switch", switch_conversation, filters=user_filter))
    application.add_handler(CommandHandler("rename", rename_conversation_cmd, filters=user_filter))
    application.add_handler(CommandHandler("update", update_bot, filters=user_filter))
    application.job_queue.run_repeating( #type: ignore
        proactive_check,
        interval=CHECKIN_INTERVAL_MINUTES * 60,
        first=CHECKIN_INTERVAL_MINUTES * 60,
    )
    application.job_queue.run_repeating( #type: ignore
        check_scheduled_tasks,
        interval=SCHEDULED_TASK_POLL_SECONDS,
        first=SCHEDULED_TASK_POLL_SECONDS,
    )
    application.job_queue.run_daily( #type: ignore
        morning_briefing,
        time=dt_time(hour=MORNING_BRIEFING_HOUR, minute=0),  #server-local time (set the box's tz)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, respond))
    application.add_handler(MessageHandler(filters.PHOTO & user_filter, respond_photo))
    application.add_handler(MessageHandler(filters.VOICE & user_filter, respond_voice))
    application.run_polling(allowed_updates=telegram.Update.ALL_TYPES)

if __name__ == "__main__":
    main()