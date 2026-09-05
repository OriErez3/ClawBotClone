# ErezBot

A personal AI assistant that lives in Telegram. It's powered by Gemini and can use a real set of tools on your machine: run shell commands and interactive processes, manage files, browse the web, read and send Gmail, manage Google Calendar and Drive, remember facts about you, learn reusable procedures as skills, and run tasks on a schedule — including proactively checking your email and calendar every hour and messaging you if something needs your attention. It understands photos and voice messages, deploys itself when you push to main, and updates on command with `/update`.

The bot only responds to a single Telegram user (you). Anyone else who messages it is ignored.

## Features

- **Agentic tool loop** — the model chains tool calls (up to 20 per request, 100 in persist mode) until the task is done, with duplicate-call detection so it can't get stuck repeating itself.
- **Photo and voice input** — send a photo (with or without a caption) or a voice message and the model sees/hears it directly. Voice commands run the full tool loop, so you can dictate tasks. Photos are downloaded at a cost-capped resolution.
- **Shell access with guardrails** — risky tools (shell commands, background processes, sending email, moving files) require a one-tap Approve/Deny confirmation in the chat before they run (typing "yes" works too). Truly destructive commands (`format`, `diskpart`, fork bombs, ...) are always blocked, even if approved.
- **Long-running processes** — servers and watchers run in the background with an ID; the bot can read their output, type into their console (answer prompts, pick menu options), list them, and stop them. A shell command that turns out to be long-running is automatically moved to the background instead of hanging.
- **Browser automation** — a visible Chromium window driven by Playwright: navigate, screenshot, click (by coordinate or by indexed element map), type, scroll.
- **Google integration** — Gmail (list/read/send/mark read), Calendar (list/create events), Drive (list/read/upload/download to disk).
- **Memory** — the bot saves important facts about you to a local SQLite database and loads them into every conversation.
- **Skills** — reusable step-by-step playbooks the bot writes for itself (markdown files in `skills/`, gitignored). Every conversation sees a one-line index; the full playbook is loaded only when a task matches, keeping prompt costs flat as knowledge grows. Teach it a procedure once ("save that as a skill") and it follows it in every future conversation.
- **Multiple conversations** — create, list, switch, and rename separate conversation histories.
- **Scheduled tasks** — "remind me at 5pm to..." style tasks persist in the database, survive restarts, and execute with the full tool loop.
- **Proactive check-ins** — every hour the bot checks for unread email and messages you only if something genuinely matters (a real person, a bill, a security notice — not marketing).
- **Morning briefing** — at 8am the bot messages you what's on for today and tomorrow: your classes (time, course, room) merged with your Google Calendar events into one time-ordered list. Classes come from `schedule.txt`, a free-form text file you maintain — copy `schedule.example.txt` and fill in your timetable. It's gitignored, and you can ask about it any time ("what room is my next class in?"). No schedule file just means the briefing covers calendar events only.
- **Daily LeetCode practice, adaptive** — the same briefing hands you two problems from `leetcode.txt`, a pre-shuffled queue. Tell the bot how they went ("first was easy, second I brute-forced") and it logs a row per problem to your tracker spreadsheet. It also keeps a local copy of that history, and uses it: one problem each day keeps you moving through the queue, the other drills whichever topic you've been getting wrong most (once there's enough history to tell — two attempts in a topic minimum), and the briefing says why it picked it. Problems already attempted are never handed out twice, and both picks always come from the queue file, so it can't invent one. If you haven't reported by 8pm it sends a one-line reminder. No queue file means the feature is simply off.

## Setup

### Quick start (recommended)

Requires Python 3.10+. Run the interactive setup script and follow the prompts:

```
python setup_bot.py
```

It installs the dependencies and the Playwright browser, walks you through creating the `.env`, and connects your Google account if `credentials.json` is present. It's safe to re-run at any time. The sections below explain the same steps manually, including how to obtain each key and `credentials.json`.

### 1. Install dependencies

Requires Python 3.10+.

```
pip install -r requirements.txt
playwright install chromium
```

### 2. Create a `.env` file

In the project root:

```
TELEGRAM_TOKEN=<your bot token from @BotFather>
GEMINI_API_KEY=<your key from Google AI Studio>
TAVILY_KEY=<your key from tavily.com, used for web search>
ALLOWED_USER_ID=<your numeric Telegram user id>
```

- **TELEGRAM_TOKEN**: message [@BotFather](https://t.me/BotFather) on Telegram, create a bot with `/newbot`, and copy the token.
- **GEMINI_API_KEY**: create one at [Google AI Studio](https://aistudio.google.com/apikey).
- **TAVILY_KEY**: sign up at [tavily.com](https://tavily.com) (free tier is fine).
- **ALLOWED_USER_ID**: your own Telegram user id (a number, not your @username). Message [@userinfobot](https://t.me/userinfobot) to get it. This is required — the bot has shell and email access, so it must only obey you.

Optional extras:

- `GEMINI_MODEL=<model name>` — use a different Gemini model (defaults to `gemini-3.1-flash-lite`).
- `TIMEZONE=<IANA name>` — the timezone for the daily jobs (morning briefing, LeetCode reminder). Defaults to `America/New_York`. This matters: python-telegram-bot reads a naive time as **UTC**, so without it an 8am briefing fires at 8am UTC.
- `BROWSER_HEADLESS=true` — run the automation browser without a window. Needed on machines with no display, unless you run the bot under `xvfb-run` (preferred — some sites bot-detect headless Chromium).

### 3. Connect your Google account

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**, **Google Calendar API**, and **Google Drive API**.
2. Create an **OAuth client ID** of type **Desktop app** (you may need to configure the OAuth consent screen first and add yourself as a test user).
3. Download the client secret JSON and save it as `credentials.json` in the project root.
4. Run the one-time consent flow from a terminal:

```
python -c "import google_services; google_services.setup_auth()"
```

A browser opens; sign in and approve the scopes. This writes `token.json`, which the bot uses from then on (it refreshes itself automatically).

`credentials.json`, `token.json`, and the `memory.db` database are gitignored — never commit them.

### 4. Run the bot

```
python main.py
```

Then message your bot on Telegram. The first message also registers your chat as the destination for proactive check-ins and scheduled-task reports.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Greet the bot (sanity check that it's running) |
| `/cancel` | Stop the task the bot is currently working on (it finishes the action already in progress, then stops and reports; also denies any pending confirmation prompt) |
| `/clear` | Clear the active conversation's history |
| `/new` | Start a new conversation |
| `/list` | List all conversations |
| `/switch <number>` | Switch to a conversation by number |
| `/rename <name>` | Rename the active conversation |
| `/persist` | Toggle persistent mode — the bot won't give up on a task until it's done (raises the tool-call cap from 20 to 100) |
| `/bypass` | ⚠️ Toggle skipping the confirmation prompt for risky actions. The hard blocklist still applies. Use with care. |
| `/update` | Pull the latest commits for the current branch and restart on the new code (manual counterpart to the auto-deploy timer) |
| `/prompts` | Toggle whether answered confirmation prompts stay in the chat (marked ✅/❌, the default) or disappear once answered. The live status message re-bottoms itself after each prompt either way. |

## Project layout

| File | Purpose |
|---|---|
| `main.py` | Telegram handlers, system prompt, the tool loop, confirmations, scheduled-task and check-in jobs |
| `tools.py` | Local tools: shell, background processes, files, downloads, web search, memory, browser automation |
| `google_services.py` | Gmail, Calendar, and Drive tools plus the OAuth flow |
| `database.py` | SQLite storage: conversations, memory, settings, scheduled tasks, LeetCode history |
| `setup_bot.py` | Interactive first-time setup (see Quick start) |
| `test_helpers.py` | Unit tests for the command guardrails, reply helpers, and skills — run with `python -m unittest test_helpers` |
| `skills/` | The bot's saved playbooks, one markdown file each (created at runtime; gitignored — they contain machine-specific detail) |
| `schedule.txt` | Your weekly class timetable, free-form text (gitignored; copy `schedule.example.txt` to create it) |
| `leetcode.txt` | Your practice queue, one `Problem Name \| Topic` per line, pre-shuffled (gitignored; copy `leetcode.example.txt`) |

For the LeetCode logging to work, the bot needs to know your tracker spreadsheet: tell it once — *"my leetcode sheet is &lt;link&gt;, save that as leetcode_sheet_id"* — and it saves the link to memory. The sheet's columns are expected to be `Name | Topic | Got it? | Notes`.
| `deploy/` | Auto-deploy script + systemd units for the poll-based deployer (see below) |
| `.github/workflows/ci.yml` | CI: runs the test suite on every push and pull request |

Tool declarations are generated automatically from each function's signature and docstring — to add a tool, write the function and register it in `tool_dict` in `main.py`.

## Deploying on a headless Linux server

Code travels via git; the gitignored secrets travel via `scp`:

```bash
# on the server
git clone <your repo url> ~/ErezBot
# from your old machine
scp .env credentials.json token.json memory.db user@server:~/ErezBot/
```

Copying `token.json` matters: the Google consent flow (`setup_auth`) needs a browser, which a headless server doesn't have — but a `token.json` created elsewhere works anywhere and refreshes itself. `memory.db` is optional (brings conversations, memories, and pending scheduled tasks along), as are the `skills/` folder if you've accumulated playbooks (`scp -r skills ...`), `schedule.txt` if you keep a class timetable, and `leetcode.txt` if you use the practice queue.

```bash
sudo apt update && sudo apt install -y python3-venv xvfb
cd ~/ErezBot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # --with-deps needs sudo: installs Chromium's system libraries
sudo timedatectl set-timezone <your zone>   # the bot resolves "tomorrow at 5pm" in server-local time
```

Test it interactively first: `xvfb-run -a python main.py` (or set `BROWSER_HEADLESS=true` in `.env` and run plain `python main.py`). Then install it as a service so it starts on boot and restarts on crashes — `/etc/systemd/system/erezbot.service`:

```ini
[Unit]
Description=ErezBot Telegram assistant
After=network-online.target
Wants=network-online.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/ErezBot
ExecStart=/usr/bin/xvfb-run -a /home/youruser/ErezBot/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
# Optional, for small servers sharing RAM with other services (e.g. a game server):
# MemoryMax=1500M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now erezbot
journalctl -u erezbot -f    # live logs
```

Important: run only ONE instance per bot token. Two pollers (e.g. the old Windows machine and the server) fight over Telegram updates and both break — stop the old one before starting the new one.

### Auto-deploy on new commits

Every push also runs the unit tests in GitHub Actions (see the repo's Actions tab), so a broken commit shows a red ✗ before the server picks it up.

The `deploy/` folder contains a poll-based deployer: every 2 minutes the server checks `origin/main`, and if there are new commits it pulls them, updates dependencies, and restarts the bot. No exposed ports and no GitHub secrets — the server only ever connects outward. (A push-triggered self-hosted runner would be instant, but GitHub advises against self-hosted runners on public repos.)

Setup on the server (after the base install above):

```bash
cd ~/ErezBot

# Allow the deploy script (running as your user) to restart the bot - and nothing else:
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart erezbot" | sudo tee /etc/sudoers.d/erezbot-deploy

# Edit User= and paths in deploy/erezbot-deploy.service to match your user, then:
sudo cp deploy/erezbot-deploy.service deploy/erezbot-deploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now erezbot-deploy.timer

journalctl -u erezbot-deploy -f     # watch deploys happen
systemctl list-timers erezbot-deploy.timer   # see when the next check fires
```

From then on, `git push` to main is a deploy: the bot restarts on the new code within ~2 minutes. The script uses `git reset --hard origin/main`, so never make local edits in the server checkout — they'll be discarded (untracked state — `.env`, `token.json`, `credentials.json`, `memory.db`, `schedule.txt`, `leetcode.txt`, and the `skills/` folder — is safe).

## Security notes

- The bot answers **only** the `ALLOWED_USER_ID` account. Keep it that way — it can run shell commands and send email as you.
- Risky tools prompt for confirmation with a 2-minute timeout that defaults to deny.
- `/bypass` disables those prompts. The blocklist of unrecoverable commands is enforced regardless, but bypass mode still means the bot can act (send email, run commands) without asking. Leave it off unless you're actively supervising.
