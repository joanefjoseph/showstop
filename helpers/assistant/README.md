# LinkedIn + Email Drafting Assistant


A local Python app that reads your **LinkedIn DMs** and your **work email inbox (Namecheap Private Email)** three times a day. It uses **Google Gemini** to draft replies based on your company context, and it shows every draft in a small web page where you read, edit, and approve it before anything is sent.

By default, drafts are written in the **Gemini web app with your own Google account** (for example Gemini Pro). The app drives a dedicated browser window for you, so there's no copying and pasting. You can also use the **Gemini API**, or let the app try the API first and fall back to the web app.


It can also:


- write **first-contact messages** (LinkedIn connect notes, LinkedIn DMs, cold emails) for people you want to start a business conversation with;
- keep a **manual export/import to the Gemini web app** as a last-resort fallback.


> **Nothing is ever sent without you.** The AI only writes drafts. A message leaves only when you click **Send** on that specific draft, or when you paste it into LinkedIn or webmail yourself (**Copy & Open**).


---


## Table of contents


1. [Features](#features)
2. [How it works](#how-it-works)
3. [File architecture](#file-architecture)
4. [Requirements](#requirements)
5. [Installation & first-time setup](#installation--first-time-setup)
6. [Configuration reference (`.env`)](#configuration-reference-env)
7. [Running it](#running-it)
8. [Using the app](#using-the-app)
9. [Safety & design decisions](#safety--design-decisions)
10. [Troubleshooting](#troubleshooting)
11. [Important caveats](#important-caveats)
12. [Your data & resetting](#your-data--resetting)
13. [Changelog](#changelog)


---


## Features


### Reading & drafting
- **Scheduled scans** at 9:00, 13:00 and 17:00 by default, plus a **Scan now** button.
  - Scans survive laptop sleep. A scan that was due while the computer slept runs within about a minute of waking, as long as it's less than 4 hours late.
- **LinkedIn replies.** It finds conversations where the other person spoke last and drafts a reply using the whole thread.
- **Connection-request follow-ups.** It detects conversations containing exactly **one message, written by you, ≤ 300 characters**. That is a connect note the person just accepted. It drafts a follow-up that thanks them, briefly introduces your company and product in terms relevant to them, and invites them to a short conversation.
- **Email replies.** It reads unanswered emails in your inbox without marking them as read. It collects earlier messages in the thread from Inbox and Sent, and drafts a reply.
- **"No response needed" detection.** Gemini flags messages that probably don't need an answer, such as DMARC reports, auto-replies, bounces, newsletters, receipts and no-reply senders, and explains why. You approve each flag individually or all at once, or ask for a reply anyway.
- **Sensible meeting suggestions.**
  - A meeting is only suggested when the conversation or your goals call for one.
  - By default the suggestion is open-ended ("would you be up for a quick call? let me know what works for you").
  - Specific days or times are proposed only if you ask for them or the other person asked. Even then they are placeholders for you to fill in.
- **Your context is used in every draft:**
  - company, product and role (set once);
  - current product phase and current focus (updated regularly, with version history);
  - conversation goals;
  - optional guidance for follow-ups, email, cold outreach and writing style.
- **Your instructions win.** Context guidance, the outreach form's extra instruction and Regenerate instructions take precedence over the assistant's default tone, length, call-to-action and meeting behaviour.


### Reviewing & sending
- A local web UI at `http://127.0.0.1:5055` for each draft: edit, **Regenerate** with an instruction (e.g. "shorter, leave the time open"), **Skip**, **Send**, or **Copy & Open**.
- **LinkedIn Send** types and posts the approved text in a real browser. It first checks that no new messages arrived since the draft was written.
- **Email Send** uses SMTP and:
  - replies to all by default, with editable To and Cc;
  - threads the reply correctly and quotes the original;
  - **saves a copy to your Sent folder** without creating duplicates;
  - flags the original email as *Answered*.
- **Copy & Open** copies the text and opens the LinkedIn thread, profile or webmail so you can paste and send it yourself.


### New outreach
- A form for people you haven't talked to yet: name, LinkedIn URL, email address, role and company, who they are, why you want to talk, and an optional **extra instruction** (e.g. "suggest a call but let them pick the time").
- It drafts a **LinkedIn connect note** (with a live character counter), a **LinkedIn DM**, or a **cold email** with subject and body. The closing ask matches the outcome you described; it isn't automatically a call.
- What you enter about a person is **reused later**. When they accept your note or reply to your message, the reply draft knows who they are. The extra instruction is stored with the draft and used again when you regenerate.


### Drafting with Gemini
You choose the drafting backend with `DRAFT_BACKEND`:

| Backend | What happens |
|---|---|
| `web` *(default)* | Drafts are written in the **Gemini web app** with your signed-in Google account, using whatever model you selected there (e.g. Pro). No API key needed. |
| `api` | Drafts are written with the **Gemini API**. Items the API can't draft wait in a queue for the next scan or a manual export. |
| `auto` | The API is tried first. Anything it can't draft is handed to the Gemini web app automatically. |

- **Automated Gemini web drafting.**
  - At the end of each scan, everything waiting for a draft is sent to Gemini in batches, one new chat per batch.
  - Gemini's JSON answer is read back from the page and imported as normal drafts. The browser window closes when it's done.
  - Regenerate, "Write a reply anyway" and New outreach use the same route.
- **Resilient API drafting.**
  - Each request times out after 2 minutes.
  - Overload and timeout errors (429, 500, 502, 503, 504, network errors) are retried with backoff.
  - Repeated server errors stop further API calls for that scan, and each item is attempted at most once per scan.
- **Manual fallback.** **Export to Gemini web** downloads a JSON file, copies instructions and opens Gemini; **Import results from Gemini web** turns Gemini's answer back into drafts.


---


## How it works


```
 Scheduler (9:00 / 13:00 / 17:00, ±2 min jitter;      or  "Scan now"
 a watchdog re-checks the clock every 60 s)
        │
        ▼
 pipeline.run_scan()
   ├─ 1. [api / auto] Retry the "Waiting for a draft" queue with the Gemini API
   ├─ 2. LinkedIn  (linkedin.py, Playwright + your saved login)
   │       • the other person spoke last                → reply
   │       • one short message from you (accepted note) → connection follow-up
   ├─ 3. Email     (email_client.py, IMAP)
   │       • unanswered Inbox emails, newest in each thread, with thread history
   │         (drafts for emails you already answered elsewhere are retired)
   └─ 4. [web / auto] Send everything still waiting to the Gemini web app (gemini_web.py)
        │
        ▼
 handoff.draft_or_queue()   (each item at most once per scan)
   ├─ DRAFT_BACKEND=web            → queued for the Gemini web batch in step 4
   ├─ api / auto, API works         → llm.py builds prompt → structured JSON draft → saved as a draft
   └─ API down / quota / failed     → queued (auto: drafted in step 4; api: next scan or manual export)
        │
        ▼
 Gemini web batch (gemini_web.py)
   export JSON (handoff.build_export) → typed into a new Gemini chat → answer read from the page
   → handoff.import_results() → normal drafts
        │
        ▼
 Web UI (web.py) ── you review ──► Send (LinkedIn browser / SMTP + Sent folder)
                                   Copy & Open (you paste & send)
                                   Approve "no response needed" · Skip · Regenerate
```


---


## File architecture


```
linkedin-dm-assistant/
├── README.md
├── requirements.txt            # Python dependencies (incl. truststore)
├── .env.example                # All settings with defaults - copy to .env
├── .env                        # Your settings and secrets (never commit)
├── data/                       # Created at runtime (never commit)
│   ├── assistant.db            #   SQLite: context, drafts, queue, run log
│   ├── browser-profile/        #   Saved LinkedIn browser session
│   └── gemini-chrome/          #   Saved Gemini web session (dedicated Chrome/Edge profile)
└── dm_assistant/
    ├── __init__.py
    ├── __main__.py             # CLI entry point (serve, scan, login, gemini-login, gemini-test,
    │                           #   context, email-test, models, llm-test)
    ├── config.py               # Reads .env into a Settings object
    ├── db.py                   # SQLite schema, migrations and all data access
    ├── selectors.py            # LinkedIn page selectors - the file to fix if LinkedIn changes its layout
    ├── gemini_selectors.py     # Gemini web page selectors - the file to fix if Gemini changes its layout
    ├── linkedin.py             # Playwright: read the inbox, detect accepted connect notes, send approved replies
    ├── email_client.py         # IMAP/SMTP: read the inbox, thread context, automation signals, send + save to Sent
    ├── llm.py                  # Gemini API client, prompts (replies, follow-ups, email, outreach), retries, model fallback
    ├── gemini_web.py           # Drives the Gemini web app: batches, regenerate, sign-in, self-test
    ├── handoff.py              # Draft-or-queue per backend, Gemini-web export/import, payload builders
    ├── pipeline.py             # One scan: API queue retry → LinkedIn → email → Gemini web batches
    ├── scheduler.py            # APScheduler jobs for RUN_TIMES + wake-up watchdog
    ├── web.py                  # Flask UI + JSON API (review, send, regenerate, outreach, queue, export/import)
    └── templates/
        ├── base.html           # Layout, navigation, shared "Export to Gemini web" script
        ├── index.html          # Drafts page: review, flagged, queue, import, history
        ├── context.html        # Context page: your company / phase / focus / goals (+ history)
        └── outreach.html       # New outreach form
```


### Module responsibilities


| Module | Responsibility |
|---|---|
| `config.py` | Every setting comes from environment variables / `.env`. See the [reference](#configuration-reference-env). |
| `db.py` | Tables: `context_entries` (versioned context), `drafts` (every draft and its status), `llm_queue` (waiting for a draft; also the work list for the Gemini web backend), `email_seen` and `checked_previews` (bookkeeping so nothing is processed twice), `runs` (scan log). Migrates older databases automatically. |
| `selectors.py` | CSS selectors for LinkedIn's messaging page. LinkedIn changes its markup from time to time; update this file when scanning breaks. |
| `gemini_selectors.py` | CSS selectors for gemini.google.com: chat box, send/stop buttons, answers, code blocks, signed-out marker and model menu. Update this file when Gemini web drafting breaks. |
| `linkedin.py` | Opens a real Chromium browser with your saved session and reads the conversation list and threads. Decides whether each thread is a reply or a connection follow-up. Posts a reply **only** when called by the Send button, after checking for new messages. |
| `email_client.py` | Reads over IMAP with `BODY.PEEK`, so emails are not marked as read. Builds thread context from `References` headers and detects automation headers. Sends replies and new emails over SMTP, then appends a copy to Sent and sets `\Answered`. TLS uses the `truststore` packag for verifcation via the Windows certificate verifier. |
| `llm.py` | Builds the system prompt from your context and the channel-specific user prompts. This includes the meeting rule, which is open-ended by default, and the rule that your instructions take precedence. Calls Gemini with a JSON schema and a 2-minute timeout, throttles for the free tier, retries transient errors (429/500/502/503/504 and network errors), and falls back to other models if one is retired. |
| `gemini_web.py` | Starts the dedicated Chrome/Edge profile with a local debugging port and attaches Playwright to it. Types each batch into a new Gemini chat, waits for the answer to finish, reads the JSON from the page and imports it. Also handles one-off redrafts, `gemini-login` and `gemini-test`. Only one Gemini session runs at a time. |
| `handoff.py` | Turns every "thing to draft" into a payload. Drafts it with the API, or queues it, depending on `DRAFT_BACKEND` and API health. Each item is tried at most once per scan, and two server errors in a row mark the API as down for the rest of the scan. Builds the Gemini-web export, including revision prompts for regenerate, and parses Gemini's answer on import. |
| `pipeline.py` | Runs one scan. Each channel runs separately, so a LinkedIn error doesn't block email and vice versa. With the web backend, the queue is sent to Gemini web at the end. Overlapping scans are skipped and logged. |
| `scheduler.py` | Schedules `RUN_TIMES` with ±2 minutes of jitter and a 4-hour grace window for missed runs. A watchdog wakes the scheduler every 60 s, because a long timer started before the computer sleeps doesn't fire when it wakes. |
| `web.py` | Local Flask server, bound to `127.0.0.1`. It rejects cross-site POSTs. Regenerate and outreach go through the configured backend; Gemini web work runs in the background while the page polls. |


### Draft statuses


| Status | Meaning |
|---|---|
| `pending` | Waiting for your review. Shown under **Drafts to review**, or under **Flagged** if no reply is needed. |
| `sending` | A send is in progress. This prevents double-sends. |
| `sent` | Sent by the app, or marked sent after Copy & Open. |
| `skipped` | You skipped it. |
| `no_reply` | You approved "no response needed". |
| `superseded` | Replaced by a newer draft, or you already replied elsewhere. |

Items in **⏳ Waiting for a draft** have their own statuses:
- `queued`: not drafted yet, with the reason shown;
- `done`: a draft was created;
- `discarded`: you discarded it, or a newer version of the conversation replaced it.


---


## Requirements


- **Python 3.10+**
- **For the Gemini web backend (default):**
  - Google Chrome or Microsoft Edge;
  - a Google account with access to Gemini. Gemini Pro is recommended, since drafts use whichever model you select there.
- **For the `api` / `auto` backends:** a Google AI Studio API key (free): https://aistudio.google.com/apikey.
- **A LinkedIn account**, for the LinkedIn features.
- **A Namecheap Private Email mailbox**, or any IMAP/SMTP mailbox, for the email features.
- **A computer that is on** around the scheduled times. If it was asleep, a missed scan runs within about a minute of waking, as long as it's less than 4 hours late.


---


## Installation & first-time setup


```bash
# 1. Get the code and create a virtual environment
cd linkedin-dm-assistant
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate


# 2. Install dependencies and the browser used for LinkedIn
pip install -r requirements.txt
playwright install chromium


# 3. Create your settings file and lock it down (it holds passwords)
cp .env.example .env
chmod 600 .env                     # macOS/Linux
```


**4. Edit `.env`.** At minimum, set these:


```ini
DRAFT_BACKEND=web                       # web (Gemini app), api, or auto (API first, then Gemini app)
LINKEDIN_MY_NAME=Jane Founder           # exactly as LinkedIn shows your name
EMAIL_ADDRESS=jane@yourcompany.com      # leave empty to disable email
EMAIL_PASSWORD=your-mailbox-password
GEMINI_API_KEY=your-ai-studio-key       # only needed for DRAFT_BACKEND=api or auto
```


**5. Sign in and check each connection:**


```bash
python -m dm_assistant login          # opens a browser - log in to LinkedIn once (incl. 2FA), then press Enter
python -m dm_assistant gemini-login   # opens Chrome/Edge on Gemini - sign in, pick your model, CLOSE the window, press Enter
python -m dm_assistant gemini-test    # one test prompt through the Gemini web app
python -m dm_assistant email-test     # IMAP + SMTP login, shows which folder is used as "Sent"

# Only for DRAFT_BACKEND=api or auto:
python -m dm_assistant models         # models your key can use - make sure GEMINI_MODEL is listed
python -m dm_assistant llm-test       # one test request to the Gemini API
```


After `gemini-login`, close the Gemini browser window before pressing Enter. The app needs to start that profile itself. If `gemini-test` fails to find the chat box or read the answer, see [Troubleshooting](#troubleshooting); Gemini's page layout may need a selector update.


**6. Start the app:**


```bash
python -m dm_assistant serve
```


**7. Add your context.** Open **http://127.0.0.1:5055**, go to **Context**, and fill in at least:
- *Company, product & your role*
- *Current product phase*
- *Current focus*
- *Goals for DM conversations*


Then click **Scan LinkedIn + email now** to try it out.


**8. Keep secrets out of version control.** Add this `.gitignore`:


```gitignore
.env
data/
.venv/
__pycache__/
```


---


## Configuration reference (`.env`)


### Drafting backend
| Variable | Default | Description |
|---|---|---|
| `DRAFT_BACKEND` | `web` | `web`: the Gemini web app with your Google account. `api`: the Gemini API only. `auto`: the API first, then the web app for anything the API couldn't draft. |
| `GEMINI_WEB_URL` | `https://gemini.google.com/app` | Gemini web app address. |
| `GEMINI_WEB_BROWSER` | *(auto-detect)* | Full path to `chrome.exe` or `msedge.exe`, if auto-detection fails. |
| `GEMINI_WEB_DEBUG_PORT` | `9333` | Local DevTools port used to control the Gemini browser. Change it if something else uses the port. |
| `GEMINI_WEB_MODEL` | *(empty)* | Text of the entry to pick in Gemini's model menu, e.g. `Pro`. Leave empty to keep whatever is selected. If selection fails, the current model is used and a warning is logged. |
| `GEMINI_WEB_BATCH_SIZE` | `8` | Messages per Gemini chat (one prompt per batch). Lower it if long batches fail to type in or time out. |
| `GEMINI_WEB_TIMEOUT_SECONDS` | `600` | How long to wait for Gemini's answer. Thinking models can take a few minutes on a big batch. |
| `GEMINI_WEB_HEADLESS` | `false` | Run the Gemini browser without a window. This is experimental; Google may treat headless browsers differently. |


### Gemini API (only for `DRAFT_BACKEND=api` or `auto`)
| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(empty)* | Google AI Studio key. With the `web` backend it isn't used. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model to use. Run `python -m dm_assistant models` to see which models your key can access. |
| `GEMINI_FALLBACK_MODELS` | *(empty)* | Comma-separated backups, tried in order if the main model is retired or unavailable. |
| `GEMINI_TEMPERATURE` | *(empty)* | Leave empty to use the model's default. |
| `LLM_MIN_INTERVAL_SECONDS` | `7` | Minimum gap between API calls, to stay under free-tier per-minute limits. |

The API request timeout (2 minutes) and retry schedule (10 s / 30 s / 60 s) are set in `llm.py`.


### Schedule & UI
| Variable | Default | Description |
|---|---|---|
| `RUN_TIMES` | `09:00,13:00,17:00` | Scan times, 24-hour format. Each run gets ±2 minutes of random jitter. |
| `TIMEZONE` | *(system)* | For example `America/New_York`. |
| `HOST` / `PORT` | `127.0.0.1` / `5055` | Address of the local web UI. |
| `OPEN_BROWSER_ON_NEW_DRAFTS` | `true` | Open the UI after a scheduled scan that produced drafts. |
| `DATA_DIR` | `./data` | Where the database and browser sessions are stored (relative to the working directory). |

The 4-hour grace window for missed runs and the 60-second watchdog interval are set in `scheduler.py` (`MISFIRE_GRACE_S`, `WAKEUP_EVERY_S`).


### LinkedIn
| Variable | Default | Description |
|---|---|---|
| `LINKEDIN_ENABLED` | `true` | Set to `false` to run email-only. |
| `LINKEDIN_MY_NAME` | — | Your display name; comma-separate any aliases. Used to tell your messages from theirs. |
| `MAX_THREADS_PER_RUN` | `15` | Maximum "they spoke last" threads opened per scan. |
| `MAX_MESSAGES_PER_THREAD` | `30` | Number of recent messages sent to the model. |
| `CONVERSATION_SCAN_DEPTH` | `40` | How many conversations from the top of the inbox to look at. |
| `MAX_FOLLOWUP_CHECKS_PER_RUN` | `10` | Maximum "You:" threads opened per scan to look for accepted connect notes. |
| `CONNECTION_NOTE_MAX_CHARS` | `300` | A single message from you at or under this length counts as a connect note. |
| `FOLLOWUP_MAX_AGE_DAYS` | `14` | Only follow up on connections accepted within this many days (`0` means no limit). |
| `LINKEDIN_CONNECT_NOTE_LIMIT` | `300` | Maximum length of connect notes the assistant **writes**. Set `200` if your account has the shorter limit. |
| `HEADLESS` | `false` | Show the browser window while scanning or sending (less likely to be flagged by LinkedIn). |
| `BROWSER_CHANNEL` | *(empty)* | Set to `chrome` to use your installed Google Chrome. |
| `ENABLE_DIRECT_SEND` | `true` | Set to `false` to remove the LinkedIn **Send** button (Copy & Open only). |
| `MEETING_LINK` | *(empty)* | Booking link the assistant may share when a meeting is appropriate. It doesn't make every message ask for a meeting. |


### Email (Namecheap Private Email defaults)
| Variable | Default | Description |
|---|---|---|
| `EMAIL_ADDRESS` / `EMAIL_PASSWORD` | — | Your mailbox login. Email features are enabled only when both are set. |
| `EMAIL_DISPLAY_NAME` | `LINKEDIN_MY_NAME` | Name shown in the From header. |
| `EMAIL_ALIASES` | *(empty)* | Other addresses that are also you. |
| `EMAIL_IMAP_HOST` / `EMAIL_IMAP_PORT` | `mail.privateemail.com` / `993` | IMAP server. |
| `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` | `mail.privateemail.com` / `465` | SMTP server. Use `465` for SSL or `587` for STARTTLS. |
| `EMAIL_SENT_FOLDER` | *(auto-detect)* | Set this if the wrong folder is detected. |
| `EMAIL_LOOKBACK_DAYS` | `3` | Only consider unanswered emails from this many days back. |
| `EMAIL_MAX_PER_RUN` | `20` | Maximum new emails processed per scan. |
| `EMAIL_BODY_MAX_CHARS` | `6000` | Maximum characters of each email sent to the model. |
| `EMAIL_QUOTE_ORIGINAL` | `true` | Quote the original email under your reply. |
| `EMAIL_SIGNATURE` | *(empty)* | Appended to every email draft. Use `\n` for line breaks. |
| `EMAIL_WEBMAIL_URL` | `https://privateemail.com/appsuite/` | Page opened by Copy & Open for emails. |


---


## Running it


### CLI commands


Run these from the project folder with the virtual environment active:


| Command | What it does |
|---|---|
| `python -m dm_assistant serve` | Starts the web UI and the scheduler. This is the default when no command is given. |
| `python -m dm_assistant login` | Opens a browser so you can log in to LinkedIn once; the session is saved in `data/`. |
| `python -m dm_assistant gemini-login` | Opens Chrome/Edge on Gemini with the dedicated profile so you can sign in once. Close the window, then press Enter. |
| `python -m dm_assistant gemini-test` | Sends one small test prompt through the Gemini web app and checks the answer can be read. |
| `python -m dm_assistant scan` | Runs one scan now and prints a summary. |
| `python -m dm_assistant context show` | Prints your current context. |
| `python -m dm_assistant context set <key> --text "..."` | Updates a context field. Keys: `company`, `phase`, `focus`, `goals`, `followup`, `email`, `outreach`, `style`. |
| `python -m dm_assistant context set <key> --file notes.md` | Updates a context field from a file. |
| `python -m dm_assistant email-test` | Checks IMAP and SMTP login and shows the detected Sent folder. |
| `python -m dm_assistant models` | Lists the Gemini models available to your API key (API backends). |
| `python -m dm_assistant llm-test` | Sends one small test request to the configured API model (API backends). |


Example: update your current focus from the terminal.


```bash
python -m dm_assistant context set focus --text "Closing 3 pilot customers in logistics this month"
```


### Keep it running in the background


The app must be running at the scheduled times. Always start it **from the project folder**, because `.env` and `data/` are found relative to it.

With the Gemini web backend, the app opens a browser window during each scan, so run it **in your logged-in desktop session**, not as a background service without a display. Alternatively, try `GEMINI_WEB_HEADLESS=true`.


<details>
<summary><b>macOS (launchd)</b></summary>


Save as `~/Library/LaunchAgents/com.you.dm-assistant.plist`, with your paths filled in:


```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.you.dm-assistant</string>
  <key>ProgramArguments</key><array>
    <string>/Users/you/linkedin-dm-assistant/.venv/bin/python</string>
    <string>-m</string><string>dm_assistant</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/you/linkedin-dm-assistant</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```


Then run `launchctl load ~/Library/LaunchAgents/com.you.dm-assistant.plist`.

The Gemini web backend auto-detects Chrome and Edge on Windows. On macOS, set `GEMINI_WEB_BROWSER=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
</details>


<details>
<summary><b>Windows (Task Scheduler)</b></summary>


Create a task with trigger **At log on** and action **Start a program**:
- Program: `C:\path\to\linkedin-dm-assistant\.venv\Scripts\python.exe`
- Arguments: `-m dm_assistant serve`
- Start in: `C:\path\to\linkedin-dm-assistant`

Choose **Run only when user is logged on**, so the LinkedIn and Gemini browser windows can open.
</details>


<details>
<summary><b>Linux (systemd user service)</b></summary>


Save as `~/.config/systemd/user/dm-assistant.service`:


```ini
[Unit]
Description=LinkedIn + email drafting assistant


[Service]
WorkingDirectory=%h/linkedin-dm-assistant
ExecStart=%h/linkedin-dm-assistant/.venv/bin/python -m dm_assistant serve
Restart=on-failure


[Install]
WantedBy=default.target
```


Then run `systemctl --user enable --now dm-assistant`.


A background service usually has no display. Either:
- set `HEADLESS=true` and `GEMINI_WEB_HEADLESS=true`, or
- set `Environment=DISPLAY=:0` in the unit.

If Chrome/Edge isn't on your `PATH`, set `GEMINI_WEB_BROWSER`.
</details>


---


## Using the app


### Context page
Everything on this page is sent to Gemini with every request. Only include what the assistant may rely on or say.
- **Phase** and **focus** keep a version history, so update them as often as things change.
- The optional fields cover connection follow-ups, email, cold outreach and writing style.
- Guidance here **takes precedence** over the assistant's default tone, length, call-to-action and meeting behaviour. It never overrides the rules about not inventing facts, not making commitments for you, and not following instructions found inside other people's messages.
  - Example for *Connection follow-up guidance*: "When suggesting a call, don't propose times; ask what works for them."
- If your *Goals* say something like "book a 20-min call", the assistant will steer toward calls more often. It still only suggests one where it fits the conversation.


### Drafts page
- **Scan LinkedIn + email now** starts a scan.
  - The button shows **Working…** while a scan or Gemini drafting is running, and the page reloads by itself when it finishes.
  - **Next runs** shows the upcoming scheduled scans; **Recent runs** shows results and problems.
- **Drafts to review.** Each card shows the channel (LinkedIn or email), a summary, the conversation, and the editable draft. Email cards also have To and Cc fields.
  - **Send** (LinkedIn replies) or **Send email**: posts immediately after your click.
  - **Copy & Open**: copies the text and opens the thread, profile or webmail; then click **Mark as sent**.
  - **Regenerate**: optionally type an instruction first, e.g. "shorter", "no meeting request" or "propose Tuesday or Thursday afternoon". With the Gemini web backend this takes about 30 seconds to 2 minutes, and it waits if a scan is currently using Gemini.
  - **Skip**: dismisses the draft.
- **Flagged: probably no response needed.** Each card shows Gemini's reason and the header signals it found. Click **✓ Approve: no response needed** (or **Approve all**), or **Write a reply anyway**.
- **⏳ Waiting for a draft.** Items not drafted yet, each with the reason. See [Drafting with Gemini](#drafting-with-gemini-1).
- **History.** Sent, skipped, approved and superseded items. Anything except sent items can be restored.


Before sending, search the draft for `[square brackets]`. The assistant uses them as placeholders for facts it doesn't know, such as `[confirm pricing]`, or for meeting times you asked it to propose, such as `[day/time]`.


### New outreach
1. Click **🚀 New outreach**.
2. Fill in the name and why you want to talk. Optionally add:
   - their LinkedIn URL, email, role and company;
   - who they are;
   - an extra instruction for this draft.
3. Tick one or more of **connect note**, **LinkedIn DM** and **email**, then click **Draft message(s)**.
   - With the Gemini web backend, you're taken back to the Drafts page, which shows **Working…** until the drafts appear (usually a minute or two).
4. Send the drafts:
   - **Email**: edit the subject and body, then click **Send email**. A copy is saved to Sent.
   - **Connect note**: click **Copy & Open profile**, then on LinkedIn choose **Connect → Add a note**, paste, and send. When they accept, the follow-up is drafted automatically on a later scan.
   - **LinkedIn DM**: click **Copy & Open profile**, then **Message**, paste, and send.


The model only knows what you type; it does not research the person. One specific, true detail in "Who are they?" improves the draft more than anything else. The closing ask follows the outcome you describe in "Why do you want to start this conversation"; say "a reply" or "an intro to the right person" if you don't want a call.


### Drafting with Gemini

**Gemini web backend (`DRAFT_BACKEND=web`, or the second step of `auto`)**
- At the end of each scan, a browser window opens with your dedicated Gemini profile. Everything waiting for a draft is sent to Gemini in batches of `GEMINI_WEB_BATCH_SIZE`, one new chat per batch, and the window closes when it's done.
- Don't type into that window while it's working.
- Anything Gemini couldn't draft stays in **⏳ Waiting for a draft** with the reason. The next scan retries it; **✨ Draft these in Gemini now** retries immediately.
- Each batch leaves a chat in your Gemini history. Delete them there whenever you like.
- Gemini subscriptions have their own daily prompt limits. Batching keeps usage to a few prompts per day.

**Gemini API backend (`DRAFT_BACKEND=api`, or the first step of `auto`)**
- Transient errors (429, 500, 502, 503, 504, timeouts) are retried after 10 s, 30 s and 60 s.
- The API is treated as down for the rest of the scan after any of these:
  - quota or auth errors;
  - a retired model with no working fallback;
  - two server errors in a row.

  Remaining items are then queued. With `auto` they go to Gemini web in the same scan; with `api` the next scan retries them.
- Each item is attempted at most once per scan, even if it's in the queue and also found again in the inbox.

**Manual fallback (any backend)**
1. In **⏳ Waiting for a draft**, click **Export to Gemini web (manual)**. A JSON file downloads, instructions are copied, and Gemini opens.
2. In Gemini, paste into the chat, attach the JSON file, and send.
3. Copy Gemini's JSON answer using the copy button on its code block.
4. Paste it into **⤵ Import results from Gemini web** and click **Import drafts**.


The New outreach page has the same option: **Export to Gemini web instead**.


---


## Safety & design decisions


- **Human approval for every message.**
  - The scan pipeline has no access to sending code.
  - Sending is only reachable from a button click on a specific draft.
  - A draft is locked while sending, so a double click can't send twice.
- **Conflict checks before a LinkedIn send.** If new messages arrived, or you already replied from your phone, you are asked before anything is posted.
- **LinkedIn outreach is Copy & Open only.** Automated connection requests are the activity LinkedIn restricts accounts for most readily, and clicking buttons on profile pages risks messaging the wrong person.
- **Low-profile LinkedIn reading.**
  - Only three scans a day, with human-like pauses and a visible, real browser profile.
  - Threads already handled are cached so they aren't reopened.
- **Gemini web automation is read-and-draft only.**
  - It uses a **separate** browser profile that you sign into by hand; the app never sees your Google password.
  - Your normal browser profile and your clipboard are never touched.
  - The app only types the export into a new chat and reads the answer back; the result becomes an ordinary draft for you to review.
  - The browser is started as a normal Chrome/Edge and controlled over a local DevTools port. This avoids Google's block on signing in inside automation browsers.
- **Scans don't silently stop.**
  - A watchdog keeps the scheduler on time across sleep and wake.
  - Each channel runs independently, and overlapping scans are skipped and logged.
  - API calls have timeouts, and a struggling API stops being called for the rest of the scan.
- **Email safety.**
  - Emails are fetched without being marked as read.
  - A failure to save a copy to Sent is reported as a warning after the email has already gone out. This prevents accidental resends.
- **Prompt-injection guard.** Message and email content is passed to the model as untrusted data, and the model is told not to follow instructions inside it. Your own instructions can change tone and format but not this rule. Human review remains the final safeguard.
- **Local-only UI.** The server binds to `127.0.0.1` and rejects POST requests from other websites.


---


## Troubleshooting


| Symptom | Fix |
|---|---|
| `Not logged in to LinkedIn` | Run `python -m dm_assistant login` again. |
| "Conversation list not found", or no LinkedIn drafts even though you have DMs | LinkedIn changed its layout. Update `dm_assistant/selectors.py` (open linkedin.com/messaging, right-click → Inspect). |
| Your own messages are treated as theirs | Set `LINKEDIN_MY_NAME` exactly as LinkedIn shows it, adding aliases if needed. |
| Non-English LinkedIn interface | Update `YOU_PREFIXES` in `selectors.py`. |
| Scheduled scans didn't run; **Next runs** shows times in the past | Make sure you're running the current `scheduler.py` (with the watchdog) and restart the app. A scan missed by more than 4 hours is skipped until the next slot. Check **Recent runs** and the console. |
| The UI shows **Working…** for a very long time | A scan or Gemini batch is still running; the console shows which step. Later scans are skipped and logged (`skipped - a previous scan is still running`) until it finishes. Restart the app if it is truly stuck. |
| `Not signed in to Gemini` | Run `python -m dm_assistant gemini-login`. |
| `The Gemini browser closed right after starting` | The Gemini profile is already open in another window, e.g. left over from `gemini-login`. Close it, or end the Chrome/Edge process in Task Manager. |
| `Couldn't find Chrome or Edge` | Set `GEMINI_WEB_BROWSER` to the full path of `chrome.exe` or `msedge.exe`. |
| `Gemini's chat box wasn't found`, or answers never finish / aren't read | Gemini changed its layout. Update `dm_assistant/gemini_selectors.py`: run `gemini-login`, right-click the element → Inspect, then `gemini-test`. |
| `Only N of M characters reached Gemini's input box` | Lower `GEMINI_WEB_BATCH_SIZE`. |
| `Gemini didn't finish answering within …s` | Raise `GEMINI_WEB_TIMEOUT_SECONDS`, lower `GEMINI_WEB_BATCH_SIZE`, or pick a faster model. |
| `Couldn't find any results … Gemini said: …` | Gemini answered without JSON, often because of a usage-limit or error message. Retry later with **✨ Draft these in Gemini now**. |
| `Couldn't select Gemini model …` (warning) | The model menu changed or the label didn't match. Fix `GEMINI_WEB_MODEL` or the model-menu selectors; drafting continues with the current model. |
| Gemini `404 … model no longer available` (API) | Run `python -m dm_assistant models`, update `GEMINI_MODEL`, and set `GEMINI_FALLBACK_MODELS`. Also run `pip install -U google-genai`. |
| Gemini `429` / quota exhausted (API) | Items are queued automatically. With `DRAFT_BACKEND=auto` they're drafted in Gemini web during the same scan; otherwise use **Export to Gemini web**, or wait for the quota to reset. Free-tier limits are listed at https://ai.google.dev/gemini-api/docs/rate-limits. |
| Gemini `503` / `504 DEADLINE_EXCEEDED` (API) | These are temporary overload or timeout errors and are retried automatically. If they persist, remaining items are queued for that scan. Consider `DRAFT_BACKEND=auto` or `web`. |
| Email: `CERTIFICATE_VERIFY_FAILED … unable to get local issuer certificate` | Run `pip install truststore` (it's in `requirements.txt`), make sure `email_client.py` uses it, then run `python -m dm_assistant email-test`. Without it, Python relies on the Windows certificate store, which is filled in lazily, so the error comes and goes. |
| Email login fails | Check the address and password. If your mailbox uses two-factor authentication, you may need an application-specific password from the webmail security settings. |
| Sent copies go to the wrong folder | Set `EMAIL_SENT_FOLDER` (see the folder shown by `email-test`). |
| Drafts propose meetings you don't want, or specific times | Check *Goals* and *Connection follow-up guidance* on the Context page and state your preference there. For a single draft, use a Regenerate instruction. |
| Export didn't open Gemini or copy the text | Your browser blocked the pop-up or clipboard. The status message shows a link and the text to copy manually. |
| Gemini won't accept the `.json` file | Rename it to `.txt`, or paste its contents into the chat. |
| A scan finished with problems | Expand **Recent runs** on the Drafts page for per-item errors. |


---


## Important caveats


- **LinkedIn terms of service.** LinkedIn has no messaging API for individual accounts, so this tool automates a browser with your own session. LinkedIn's User Agreement prohibits automated access, and accounts can be restricted. Use it at your own risk. For zero automated *sending*, set `ENABLE_DIRECT_SEND=false`.
- **Google terms of service.** The Gemini web backend automates the consumer Gemini app with your account. Google's terms restrict automated access to its services. The app keeps usage low (a few batched prompts per day), but there is some risk to the account; use `DRAFT_BACKEND=api` if that matters to you. Google can also change the Gemini page at any time, which breaks drafting until `gemini_selectors.py` is updated. Items simply wait in the queue until then.
- **Threads get marked as read.** Opening a LinkedIn thread to read it marks it as read. This does not happen for emails.
- **Privacy.** Your DMs, emails and contact notes are sent to Google.
  - On the **free API tier**, Google may use this content to improve its products.
  - In the **Gemini web app**, every batch is saved as a chat in your history. If **Gemini Apps Activity** is on, Google may use it to improve its models, including human review. Consider turning that setting off for the account you use here.
  - These are other people's messages, so consider whether that is acceptable. A paid API tier has different data terms.
- **Drafts may be imperfect.** The model may misread a conversation. It is told not to invent facts and to use `[placeholders]` instead, but always review before sending.


---


## Your data & resetting


| Item | What it holds | Reset |
|---|---|---|
| `data/assistant.db` | Context history, drafts, queue and run log | Delete it to start fresh (your Context is lost too). |
| `data/browser-profile/` | Your logged-in LinkedIn session | Delete it and run `login` again. **Treat it like a password.** |
| `data/gemini-chrome/` | Your signed-in Google/Gemini session for the web backend | Delete it and run `gemini-login` again. **Treat it like a password**; it's a signed-in Google account. |
| `.env` | API key and mailbox password | Keep it private (`chmod 600 .env`) and never commit it. |


Back up `data/assistant.db` if you want to keep your context history and sent-message records.


---


## Changelog


- **Gemini web backend.**
  - New `DRAFT_BACKEND` setting (`web` is the default), new `gemini_web.py` and `gemini_selectors.py`, and new `gemini-login` and `gemini-test` commands.
  - Drafts are written in the Gemini app with your own account, with no copy/paste.
  - New **✨ Draft these in Gemini now** button; regenerate and outreach use the configured backend.
  - The manual export/import remains as a fallback.
- **Scheduler survives sleep.**
  - A 60-second watchdog fixes scheduled scans silently stopping after the computer slept.
  - The grace window for missed runs grew from 1 hour to 4 hours.
  - Overlapping scans are now logged.
- **Meeting suggestions.**
  - Removed the rigid "two time slots like `[Tue 2pm]` / `[Thu 10am]`" rule.
  - Meetings are suggested only when relevant and are open-ended by default.
  - Connection follow-ups and outreach no longer force a call.
- **Your instructions take precedence** over default tone, length, call-to-action and meeting behaviour. The outreach form's **extra instruction** is now actually used (it was previously ignored) and is kept for regenerate.
- **API robustness.**
  - 2-minute request timeout.
  - 502, 504 and network errors are now retried.
  - Two server errors in a row stop API calls for that scan.
  - Queued items are no longer sent to Gemini twice in the same scan.
  - API errors are logged as one line instead of a full traceback.
- **Email TLS** uses the `truststore` Windows certificate verifier, fixing intermittent `CERTIFICATE_VERIFY_FAILED` errors on Windows.