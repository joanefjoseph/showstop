# LinkedIn + Email Drafting Assistant

A local Python app that reads your **LinkedIn DMs** and your **work email inbox (Namecheap Private Email)** three times a day. It uses **Google Gemini** to draft replies based on your company context, and it shows every draft in a small web page where you read, edit, and approve it before anything is sent.

It can also:

- write **first-contact messages** (LinkedIn connect notes, LinkedIn DMs, cold emails) for people you want to start a business conversation with;
- **hand the work off to the Gemini web app** when your free API quota runs out.

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

---

## Features

### Reading & drafting
- **Scheduled scans** at 9:00, 13:00 and 17:00 by default, plus a **Scan now** button.
- **LinkedIn replies.** It finds conversations where the other person spoke last and drafts a reply using the whole thread.
- **Connection-request follow-ups.** It detects conversations containing exactly **one message, written by you, ≤ 300 characters**. That is a connect note the person just accepted. It drafts a follow-up that thanks them, introduces your company and product, and proposes a meeting within the next week.
- **Email replies.** It reads unanswered emails in your inbox without marking them as read. It collects earlier messages in the thread from Inbox and Sent, and drafts a reply.
- **"No response needed" detection.** Gemini flags messages that probably don't need an answer, such as DMARC reports, auto-replies, bounces, newsletters, receipts and no-reply senders, and explains why. You approve each flag individually or all at once, or ask for a reply anyway.
- **Your context is used in every draft:**
  - company, product and role (set once);
  - current product phase and current focus (updated regularly, with version history);
  - conversation goals;
  - optional guidance for follow-ups, email, cold outreach and writing style.

### Reviewing & sending
- A local web UI at `http://127.0.0.1:5055` for each draft: edit, **Regenerate** with an instruction (e.g. "shorter, propose Thursday"), **Skip**, **Send**, or **Copy & Open**.
- **LinkedIn Send** types and posts the approved text in a real browser. It first checks that no new messages arrived since the draft was written.
- **Email Send** uses SMTP and:
  - replies to all by default, with editable To and Cc;
  - threads the reply correctly and quotes the original;
  - **saves a copy to your Sent folder** without creating duplicates;
  - flags the original email as *Answered*.
- **Copy & Open** copies the text and opens the LinkedIn thread, profile or webmail so you can paste and send it yourself.

### New outreach
- A form for people you haven't talked to yet: name, LinkedIn URL, email address, role and company, who they are, and why you want to talk.
- It drafts a **LinkedIn connect note** (with a live character counter), a **LinkedIn DM**, or a **cold email** with subject and body.
- What you enter about a person is **reused later**. When they accept your note or reply to your message, the reply draft knows who they are.

### Gemini web hand-off (when the API quota runs out)
- Messages the API couldn't draft go to a **"Waiting for a draft"** queue.
- **Export to Gemini web** does three things:
  1. downloads a JSON file with the system prompt, all context, and each item's exact instructions and data;
  2. copies a short explanation to your clipboard;
  3. opens `https://gemini.google.com/app`.
- **Import results from Gemini web** turns Gemini's JSON answer back into normal drafts, so Send, saving to Sent and the other checks still work.

---

## How it works

```
 Scheduler (9:00 / 13:00 / 17:00)  or  "Scan now"
        │
        ▼
 pipeline.run_scan()
   ├─ 1. Retry anything in the "Waiting for a draft" queue with the Gemini API
   ├─ 2. LinkedIn  (linkedin.py, Playwright + your saved login)
   │       • the other person spoke last              → reply
   │       • one short message from you (accepted note) → connection follow-up
   └─ 3. Email     (email_client.py, IMAP)
           • unanswered Inbox emails, newest in each thread, with thread history
        │
        ▼
 handoff.draft_or_queue()
   ├─ Gemini API available → llm.py builds prompt → structured JSON draft → saved as a draft
   └─ quota used up / model unavailable / no key → saved to the queue (export to Gemini web later)
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
├── requirements.txt            # Python dependencies
├── .env.example                # All settings with defaults - copy to .env
├── .env                        # Your settings and secrets (never commit)
├── data/                       # Created at runtime (never commit)
│   ├── assistant.db            #   SQLite: context, drafts, queue, run log
│   └── browser-profile/        #   Saved LinkedIn browser session
└── dm_assistant/
    ├── __init__.py
    ├── __main__.py             # CLI entry point (login, serve, scan, context, email-test, models, llm-test)
    ├── config.py               # Reads .env into a Settings object
    ├── db.py                   # SQLite schema, migrations and all data access
    ├── selectors.py            # LinkedIn page selectors - the one file to fix if LinkedIn changes its layout
    ├── linkedin.py             # Playwright: read the inbox, detect accepted connect notes, send approved replies
    ├── email_client.py         # IMAP/SMTP: read the inbox, thread context, automation signals, send + save to Sent
    ├── llm.py                  # Gemini client, prompts (replies, follow-ups, email, outreach), model fallback
    ├── handoff.py              # Draft-or-queue logic, Gemini-web export/import, payload builders
    ├── pipeline.py             # One scan: drain queue → LinkedIn → email
    ├── scheduler.py            # APScheduler jobs for RUN_TIMES
    ├── web.py                  # Flask UI + JSON API (review, send, outreach, export/import)
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
| `db.py` | Tables: `context_entries` (versioned context), `drafts` (every draft and its status), `llm_queue` (waiting for a draft), `email_seen` and `checked_previews` (bookkeeping so nothing is processed twice), `runs` (scan log). Migrates older databases automatically. |
| `selectors.py` | CSS selectors for LinkedIn's messaging page. LinkedIn changes its markup from time to time; update this file when scanning breaks. |
| `linkedin.py` | Opens a real Chromium browser with your saved session and reads the conversation list and threads. Decides whether each thread is a reply or a connection follow-up. Posts a reply **only** when called by the Send button, after checking for new messages. |
| `email_client.py` | Reads over IMAP with `BODY.PEEK`, so emails are not marked as read. Builds thread context from `References` headers and detects automation headers. Sends replies and new emails over SMTP, then appends a copy to Sent and sets `\Answered`. |
| `llm.py` | Builds the system prompt from your context and the channel-specific user prompts. Calls Gemini with a JSON schema, throttles for the free tier, retries on rate limits, and falls back to other models if one is retired. |
| `handoff.py` | Turns every "thing to draft" into a payload. Drafts it with the API or queues it. Builds the Gemini-web export and parses Gemini's answer on import. |
| `pipeline.py` | Runs one scan. Each channel runs separately, so a LinkedIn error doesn't block email and vice versa. |
| `web.py` | Local Flask server, bound to `127.0.0.1`. It rejects cross-site POSTs. |

### Draft statuses

| Status | Meaning |
|---|---|
| `pending` | Waiting for your review. Shown under **Drafts to review**, or under **Flagged** if no reply is needed. |
| `sending` | A send is in progress. This prevents double-sends. |
| `sent` | Sent by the app, or marked sent after Copy & Open. |
| `skipped` | You skipped it. |
| `no_reply` | You approved "no response needed". |
| `superseded` | Replaced by a newer draft, or you already replied elsewhere. |

---

## Requirements

- **Python 3.10+**
- **A Google AI Studio API key** (free): https://aistudio.google.com/apikey. This is optional; without it, everything runs through the Gemini web hand-off.
- **A LinkedIn account**, for the LinkedIn features.
- **A Namecheap Private Email mailbox**, or any IMAP/SMTP mailbox, for the email features.
- **A computer that is on** at the scheduled times. Missed runs still fire if the computer wakes within one hour.

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
GEMINI_API_KEY=your-ai-studio-key
LINKEDIN_MY_NAME=Jane Founder          # exactly as LinkedIn shows your name
EMAIL_ADDRESS=jane@yourcompany.com      # leave empty to disable email
EMAIL_PASSWORD=your-mailbox-password
```

**5. Check each connection:**

```bash
python -m dm_assistant models       # models your key can use - make sure GEMINI_MODEL is listed
python -m dm_assistant llm-test     # one test request to Gemini
python -m dm_assistant email-test   # IMAP + SMTP login, shows which folder is used as "Sent"
python -m dm_assistant login        # opens a browser - log in to LinkedIn once (incl. 2FA), then press Enter
```

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

### Gemini
| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | *(empty)* | Google AI Studio key. If empty, all drafting goes to the Gemini-web queue. |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model to use. Run `python -m dm_assistant models` to see which models your key can access. |
| `GEMINI_FALLBACK_MODELS` | *(empty)* | Comma-separated backups, tried in order if the main model is retired or unavailable. |
| `GEMINI_TEMPERATURE` | *(empty)* | Leave empty to use the model's default. |
| `LLM_MIN_INTERVAL_SECONDS` | `7` | Minimum gap between API calls, to stay under free-tier per-minute limits. |

### Schedule & UI
| Variable | Default | Description |
|---|---|---|
| `RUN_TIMES` | `09:00,13:00,17:00` | Scan times, 24-hour format. |
| `TIMEZONE` | *(system)* | For example `America/New_York`. |
| `HOST` / `PORT` | `127.0.0.1` / `5055` | Address of the local web UI. |
| `OPEN_BROWSER_ON_NEW_DRAFTS` | `true` | Open the UI after a scheduled scan that produced drafts. |
| `DATA_DIR` | `./data` | Where the database and browser session are stored (relative to the working directory). |

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
| `MEETING_LINK` | *(empty)* | Booking link the assistant can offer when proposing meetings. |

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
| `python -m dm_assistant scan` | Runs one scan now and prints a summary. |
| `python -m dm_assistant context show` | Prints your current context. |
| `python -m dm_assistant context set <key> --text "..."` | Updates a context field. Keys: `company`, `phase`, `focus`, `goals`, `followup`, `email`, `outreach`, `style`. |
| `python -m dm_assistant context set <key> --file notes.md` | Updates a context field from a file. |
| `python -m dm_assistant email-test` | Checks IMAP and SMTP login and shows the detected Sent folder. |
| `python -m dm_assistant models` | Lists the Gemini models available to your API key. |
| `python -m dm_assistant llm-test` | Sends one small test request to the configured model. |

Example: update your current focus from the terminal.

```bash
python -m dm_assistant context set focus --text "Closing 3 pilot customers in logistics this month"
```

### Keep it running in the background

The app must be running at the scheduled times. Always start it **from the project folder**, because `.env` and `data/` are found relative to it.

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
</details>

<details>
<summary><b>Windows (Task Scheduler)</b></summary>

Create a task with trigger **At log on** and action **Start a program**:
- Program: `C:\path\to\linkedin-dm-assistant\.venv\Scripts\python.exe`
- Arguments: `-m dm_assistant serve`
- Start in: `C:\path\to\linkedin-dm-assistant`
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

A background service usually has no display, so either set `HEADLESS=true` or set `Environment=DISPLAY=:0` in the unit.
</details>

---

## Using the app

### Context page
Everything on this page is sent to Gemini with every request. Only include what the assistant may rely on or say.
- **Phase** and **focus** keep a version history, so update them as often as things change.
- The optional fields cover connection follow-ups, email, cold outreach and writing style.

### Drafts page
- **Drafts to review.** Each card shows the channel (LinkedIn or email), a summary, the conversation, and the editable draft. Email cards also have To and Cc fields.
  - **Send** (LinkedIn replies) or **Send email**: posts immediately after your click.
  - **Copy & Open**: copies the text and opens the thread, profile or webmail; then click **Mark as sent**.
  - **Regenerate**: optionally type an instruction first.
  - **Skip**: dismisses the draft.
- **Flagged: probably no response needed.** Each card shows Gemini's reason and the header signals it found. Click **✓ Approve: no response needed** (or **Approve all**), or **Write a reply anyway**.
- **⏳ Waiting for a draft.** Items the API couldn't draft; see [below](#when-the-gemini-api-quota-runs-out).
- **History.** Sent, skipped, approved and superseded items. Anything except sent items can be restored.

Before sending, search the draft for `[square brackets]`. The assistant uses them as placeholders for facts it doesn't know, such as `[confirm pricing]` or `[Tue 2pm]`.

### New outreach
1. Click **🚀 New outreach**.
2. Fill in the name and why you want to talk. Optionally add their LinkedIn URL, email, role and company, and who they are.
3. Tick one or more of **connect note**, **LinkedIn DM** and **email**, then click **Draft message(s)**.
4. Send the drafts:
   - **Email**: edit the subject and body, then click **Send email**. A copy is saved to Sent.
   - **Connect note**: click **Copy & Open profile**, then on LinkedIn choose **Connect → Add a note**, paste, and send. When they accept, the follow-up is drafted automatically on a later scan.
   - **LinkedIn DM**: click **Copy & Open profile**, then **Message**, paste, and send.

The model only knows what you type; it does not research the person. One specific, true detail in "Who are they?" improves the draft more than anything else.

### When the Gemini API quota runs out
1. During a scan, remaining items go to **⏳ Waiting for a draft** automatically.
2. Click **⤴ Export to Gemini web**. A JSON file downloads, instructions are copied, and Gemini opens.
3. In Gemini, paste into the chat, attach the JSON file, and send.
4. Copy Gemini's JSON answer using the copy button on its code block.
5. Paste it into **⤵ Import results from Gemini web** and click **Import drafts**.

The New outreach page has the same option: **Export to Gemini web instead**. If you never import, the next scan that reaches the API drafts the queued items automatically.

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
- **Email safety.**
  - Emails are fetched without being marked as read.
  - A failure to save a copy to Sent is reported as a warning after the email has already gone out. This prevents accidental resends.
- **Prompt-injection guard.** Message and email content is passed to the model as untrusted data, and the model is told not to follow instructions inside it. Human review remains the final safeguard.
- **Local-only UI.** The server binds to `127.0.0.1` and rejects POST requests from other websites.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Not logged in to LinkedIn` | Run `python -m dm_assistant login` again. |
| "Conversation list not found", or no LinkedIn drafts even though you have DMs | LinkedIn changed its layout. Update `dm_assistant/selectors.py` (open linkedin.com/messaging, right-click → Inspect). |
| Your own messages are treated as theirs | Set `LINKEDIN_MY_NAME` exactly as LinkedIn shows it, adding aliases if needed. |
| Non-English LinkedIn interface | Update `YOU_PREFIXES` in `selectors.py`. |
| Gemini `404 … model no longer available` | Run `python -m dm_assistant models`, update `GEMINI_MODEL`, and set `GEMINI_FALLBACK_MODELS`. Also run `pip install -U google-genai`. |
| Gemini `429` / quota exhausted | Items are queued automatically. Use **Export to Gemini web**, or wait for the quota to reset. Lower `MAX_THREADS_PER_RUN` / `EMAIL_MAX_PER_RUN` if it happens often. Free-tier limits are listed at https://ai.google.dev/gemini-api/docs/rate-limits. |
| Email login fails | Check the address and password. If your mailbox uses two-factor authentication, you may need an application-specific password from the webmail security settings. |
| Sent copies go to the wrong folder | Set `EMAIL_SENT_FOLDER` (see the folder shown by `email-test`). |
| Export didn't open Gemini or copy the text | Your browser blocked the pop-up or clipboard. The status message shows a link and the text to copy manually. |
| Gemini won't accept the `.json` file | Rename it to `.txt`, or paste its contents into the chat. |
| A scan finished with problems | Expand **Recent runs** on the Drafts page for per-item errors. |

---

## Important caveats

- **LinkedIn terms of service.** LinkedIn has no messaging API for individual accounts, so this tool automates a browser with your own session. LinkedIn's User Agreement prohibits automated access, and accounts can be restricted. Use it at your own risk. For zero automated *sending*, set `ENABLE_DIRECT_SEND=false`.
- **Threads get marked as read.** Opening a LinkedIn thread to read it marks it as read. This does not happen for emails.
- **Privacy.** Your DMs, emails and contact notes are sent to Google.
  - On the **free API tier**, Google may use this content to improve its products.
  - The **Gemini web app** may retain it, depending on your Gemini Apps Activity setting.
  - These are other people's messages, so consider whether that is acceptable. A paid API tier has different data terms.
- **Drafts may be imperfect.** The model may misread a conversation. It is told not to invent facts and to use `[placeholders]` instead, but always review before sending.

---

## Your data & resetting

| Item | What it holds | Reset |
|---|---|---|
| `data/assistant.db` | Context history, drafts, queue and run log | Delete it to start fresh (your Context is lost too). |
| `data/browser-profile/` | Your logged-in LinkedIn session | Delete it and run `login` again. **Treat it like a password.** |
| `.env` | API key and mailbox password | Keep it private (`chmod 600 .env`) and never commit it. |

Back up `data/assistant.db` if you want to keep your context history and sent-message records.