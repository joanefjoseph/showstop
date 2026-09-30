#!/usr/bin/env python3
"""
Send personalized outreach emails via Namecheap Private Email.
Recipients are read from the `employees` table of clients.db.
Importable by app.py; the Flask UI is the entry point.
Every email (the initial cold email, the follow-up, and any future
follow-ups) is defined once in EMAILS below and rendered by the same
build_subject/build_plain/build_html functions -- no per-campaign
functions needed. See the "EMAIL CONTENT" section for the formatting
rules shared by every part of every email.
"""
import argparse
import getpass
import html
import imaplib
import os
import random
import re
import smtplib
import sqlite3
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from dotenv import load_dotenv
from config import BASE, DB_PATH, TABLE_NAME, SENT_TABLE_NAME, METADATA_TABLE_NAME
# -------------------------- LOAD .env -------------------------------
load_dotenv(BASE / ".env")
def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in your .env file.")
    return value
SENDER_NAME = require_env("SENDER_NAME")
SENDER_EMAIL = require_env("SENDER_EMAIL")
COMPANY_NAME = require_env("COMPANY_NAME")
COMPANY_URL = require_env("COMPANY_URL")                  # e.g. www.thenewcompany.com
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip()  # falls back to a prompt if empty
# Link target: add https:// if the URL in .env doesn't include a scheme
COMPANY_HREF = COMPANY_URL if re.match(r"^https?://", COMPANY_URL, re.I) else f"https://{COMPANY_URL}"
# -------------------------- CONFIG -----------------------------------
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 465                      # SSL. (587 + STARTTLS also works)
IMAP_HOST = "mail.privateemail.com"
IMAP_PORT = 993
SAVE_TO_SENT_FOLDER = True           # SMTP sends don't show up in "Sent" otherwise
SENT_FOLDER = "Sent"
MIN_DELAY_SEC = 45                   # random pause between emails
MAX_DELAY_SEC = 90
REQUIRED_COLUMNS = {"company_name", "first_name", "last_name", "email"}
REQUIRED_METADATA_COLUMNS = {"company_name", "email_name"}   # client_metadata
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# -------------------------- EMAIL CONTENT -----------------------------
# Formatting cheatsheet -- this is the ONE convention used everywhere
# (subject, greeting, intro/body/outro, bullets, numbering, signature):
#
#   *word*            -> bold in the HTML version (asterisks are simply
#                         dropped in the plain-text version)
#   {sender}          -> your name                          (SENDER_NAME)
#   {company}         -> your own company                   (COMPANY_NAME)
#   {email_name}     -> the recipient's company, shortened  (client_metadata.email_name)
#   {first_name}      -> the recipient's first name         (row["first_name"])
#
# Each campaign below is a dict with:
#   subject  : str  -- may use any of the placeholders above
#   greeting : str  -- e.g. "Hello {first_name}," or "Hi {first_name},"
#   blocks   : ordered list of (kind, lines) tuples, kind is one of:
#       "intro" / "body" / "outro"  -> each line is its own paragraph
#       "bullets"                   -> rendered as a bulleted list
#       "numbering"                 -> rendered as a numbered list
#
# To add a 3rd/4th/Nth follow-up in the future: add a new key here with
# its own subject/greeting/blocks, then append that key to
# CAMPAIGN_SEQUENCE below. No other code in this file needs to change.
CAMPAIGN_INITIAL = "initial"
CAMPAIGN_FOLLOWUP = "followup"
EMAILS = {
    CAMPAIGN_INITIAL: {
        "subject": "Direct-to-fan tour ticketing for {email_name}'s artists",
        "greeting": "Hello {first_name},",
        "blocks": [
            ("intro", [
                "My name is {sender}, Founder and CEO of *{company}*.",
                "We are building a white-label software platform designed to give artists and "
                "music labels direct control over their tour ticketing experience. Our software "
                "allows your artists to host primary ticket sales directly on their own "
                "websites—connecting natively to primary ticketing APIs like Ticketmaster and AXS.",
                "Instead of losing fans to third-party portals where drop-off is high and "
                "post-sale revenue is lost, {company} enables:",
            ]),
            ("bullets", [
                "*Direct Ecosystem Monetization:* Keep fans on your artist's website through the "
                "entire ticket purchase. Immediately post-checkout (when buying intent peaks), route "
                "fans into tour apparel, album pre-orders, and VIP upgrades (averaging 15–30% upsell "
                "conversion).",
                "*Seamless Fan Club & Presale Gating:* Give loyal fans the friction-free, secure "
                "buying experience they expect by authenticating official memberships natively at "
                "checkout—eliminating redirects, bots, and leaked promo codes.",
                "*Unified Multi-Vendor Experience:* Deliver a single, consistent checkout flow across "
                "your entire tour, regardless of whether individual venues use Ticketmaster, AXS, or "
                "another primary ticket vendor.",
            ]),
            ("outro", [
                "Whenever tour ticketing is on your roadmap next, we are onboarding a select group "
                "of tour partners for our Early Access Program ($0 platform fee) as we finalize "
                "our product.",
                "Would you be open to a 10-minute introductory call in the next week to see a "
                "brief visual preview?",
            ]),
        ],
    },
    CAMPAIGN_FOLLOWUP: {
        "subject": "Concert Sales Directly On Your Platform",
        "greeting": "Hi {first_name},",
        "blocks": [
            ("body", [
                "I'm following up on my previous message about *{company}* - the software product "
                "that will *revolutionize* the way that you manage ticket sales for all of your artists.",
                "Our company is called {company} because we are changing the game for how music "
                "labels and fan platforms envision what it takes to put on a show, and we aim to "
                "be a one-stop shop for all of your concert ticketing needs.",
                "We are finalizing our software architecture and are looking for 2-3 design partners "
                "in the music space to test the beta. Given {email_name}'s focus on fan experience, "
                "I'd love to show you the prototype and get your feedback.",
                "Are you open to a brief 10-minute intro call this week?",
            ]),
        ],
    },
}
# Campaigns are sent in this order; campaign[i] is only offered to someone
# who has already received campaign[i-1] (and hasn't received campaign[i]
# yet). CAMPAIGN_INITIAL (index 0) has no prerequisite.
CAMPAIGN_SEQUENCE = [CAMPAIGN_INITIAL, CAMPAIGN_FOLLOWUP]
SIGNATURE = [
    "Best Regards,",
    "----",
    "{sender}",
    "Founder & CEO | *{company}*",
]
def render(text: str, html_mode: bool, **fields) -> str:
    """
    The single formatting function used by every part of every email.
    Fills in {sender}/{company} plus any extra placeholders passed in
    (e.g. company_name=..., first_name=...), and in HTML mode turns
    *word* into <b>word</b> (asterisks are simply dropped in plain text).
    """
    values = {"sender": SENDER_NAME, "company": COMPANY_NAME, **fields}
    if html_mode:
        text = html.escape(text)
        text = re.sub(r"\*(.+?)\*", r"<b>\1</b>", text)
        for key, val in values.items():
            text = text.replace(f"{{{key}}}", html.escape(str(val)))
        return text
    text = text.replace("*", "")
    for key, val in values.items():
        text = text.replace(f"{{{key}}}", str(val))
    return text
def _render_blocks(blocks: list[tuple[str, list[str]]], html_mode: bool, **fields) -> list[str]:
    """
    Turn an email's ordered (kind, lines) blocks into a flat list of
    ready-to-join chunks for either the plain-text or HTML body. This is
    the one place that understands intro/body/outro vs. bullets vs.
    numbering -- every campaign in EMAILS reuses it as-is.
    """
    r = lambda t: render(t, html_mode, **fields)
    chunks: list[str] = []
    for kind, lines in blocks:
        if kind in ("intro", "body", "outro"):
            chunks += [f"<p>{r(line)}</p>" if html_mode else r(line) for line in lines]
        elif kind == "bullets":
            if html_mode:
                chunks.append("<ul>" + "".join(f"<li>{r(line)}</li>" for line in lines) + "</ul>")
            else:
                chunks += [f"• {r(line)}" for line in lines]
        elif kind == "numbering":
            if html_mode:
                chunks.append("<ol>" + "".join(f"<li>{r(line)}</li>" for line in lines) + "</ol>")
            else:
                chunks += [f"{i}. {r(line)}" for i, line in enumerate(lines, 1)]
        else:
            raise ValueError(f"Unknown email block kind: {kind!r}")
    return chunks
def _fields_for(row: dict) -> dict:
    return {"first_name": row["first_name"], "email_name": row["email_name"]}
def build_subject(row: dict, campaign: str) -> str:
    return render(EMAILS[campaign]["subject"], False, **_fields_for(row))
def build_plain(row: dict, campaign: str) -> str:
    spec = EMAILS[campaign]
    fields = _fields_for(row)
    parts = [render(spec["greeting"], False, **fields), ""]
    for chunk in _render_blocks(spec["blocks"], False, **fields):
        parts += [chunk, ""]
    parts += ["", *[render(s, False, **fields) for s in SIGNATURE], COMPANY_URL]
    return "\n".join(parts)
def build_html(row: dict, campaign: str) -> str:
    spec = EMAILS[campaign]
    fields = _fields_for(row)
    out = [f"<p>{render(spec['greeting'], True, **fields)}</p>"]
    out += _render_blocks(spec["blocks"], True, **fields)
    sig_lines = [render(s, True, **fields) for s in SIGNATURE]
    sig_lines.append(
        f'<a href="{html.escape(COMPANY_HREF, quote=True)}">{html.escape(COMPANY_URL)}</a>'
    )
    out.append("<p>&nbsp;</p><p>" + "<br>".join(sig_lines) + "</p>")
    return ('<html><body style="font-family:Arial,sans-serif;font-size:14px;'
            'line-height:1.5;color:#222">' + "".join(out) + "</body></html>")
def build_message(row: dict, to_addr: str, campaign: str = CAMPAIGN_INITIAL) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = build_subject(row, campaign)
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = formataddr((f'{row["first_name"]} {row["last_name"]}'.strip(), to_addr))
    msg["Reply-To"] = SENDER_EMAIL
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@")[1])
    msg.set_content(build_plain(row, campaign))
    msg.add_alternative(build_html(row, campaign), subtype="html")
    return msg
# -------------------------- DATA SOURCE -------------------------------
def clean_records(records: list[dict], log=print) -> list[dict]:
    """Normalize, validate, and de-duplicate employee records."""
    rows, seen = [], set()
    for n, raw in enumerate(records, start=1):
        row = {k: ("" if v is None else str(v)).strip().strip('"').strip()
               for k, v in raw.items() if k}
        email = row.get("email", "").lower()
        if not EMAIL_RE.match(email):
            log(f"  [skip] record {n}: invalid email {row.get('email')!r}")
        elif not row.get("first_name") or not row.get("company_name"):
            log(f"  [skip] record {n}: missing first_name/company_name")
        elif not row.get("email_name"):
            log(f"  [skip] record {n}: no email_name in client_metadata for {row.get('company_name')!r}")
        elif email in seen:
            log(f"  [skip] record {n}: duplicate {email}")
        else:
            seen.add(email)
            rows.append(row)
    return rows
def load_rows_sqlite(db_path: Path = DB_PATH, log=print) -> list[dict]:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise RuntimeError(f"Database not found: {path}")
    try:
        # Read-only connection: this module never modifies the database.
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not open database {path}: {exc}") from exc
    try:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
        if not cols:
            raise RuntimeError(f"Table '{TABLE_NAME}' not found in {path.name}")
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise RuntimeError(f"Table '{TABLE_NAME}' is missing columns: {sorted(missing)}")
        meta_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({METADATA_TABLE_NAME})")}
        if not meta_cols:
            raise RuntimeError(f"Table '{METADATA_TABLE_NAME}' not found in {path.name}")
        meta_missing = REQUIRED_METADATA_COLUMNS - meta_cols
        if meta_missing:
            raise RuntimeError(
                f"Table '{METADATA_TABLE_NAME}' is missing columns: {sorted(meta_missing)}"
            )
        records = [dict(r) for r in conn.execute(
            f"""SELECT e.*, m.email_name AS email_name
                FROM {TABLE_NAME} e
                LEFT JOIN {METADATA_TABLE_NAME} m ON m.company_name = e.company_name"""
        )]
    except sqlite3.Error as exc:
        raise RuntimeError(f"Database error: {exc}") from exc
    finally:
        conn.close()
    return clean_records(records, log)
# -------------------------- SEND LOG ----------------------------------
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_sent_table() -> None:
    """Create the sent_emails table if it doesn't exist yet (idempotent)."""
    conn = _connect()
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {SENT_TABLE_NAME} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                email       TEXT NOT NULL,
                status      TEXT NOT NULL,
                detail      TEXT,
                campaign    TEXT NOT NULL DEFAULT 'initial'
            )
        """)
        # Migration safety: older databases may already have this table
        # from before the `campaign` column existed.
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({SENT_TABLE_NAME})")}
        if "campaign" not in cols:
            conn.execute(
                f"ALTER TABLE {SENT_TABLE_NAME} ADD COLUMN campaign TEXT NOT NULL DEFAULT 'initial'"
            )
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{SENT_TABLE_NAME}_email_status
            ON {SENT_TABLE_NAME} (email, status, campaign)
        """)
        conn.commit()
    finally:
        conn.close()
def load_sent_emails(campaign: str, status: str = "sent") -> set[str]:
    """Emails with a given status for a given campaign (lowercased)."""
    init_sent_table()
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT LOWER(email) AS email FROM {SENT_TABLE_NAME} "
            f"WHERE campaign = ? AND status = ?",
            (campaign, status),
        ).fetchall()
    finally:
        conn.close()
    return {r["email"] for r in rows}
def log_result(email: str, status: str, detail: str = "", campaign: str = CAMPAIGN_INITIAL) -> None:
    init_sent_table()
    conn = _connect()
    try:
        conn.execute(
            f"INSERT INTO {SENT_TABLE_NAME} (timestamp, email, status, detail, campaign) "
            f"VALUES (?, ?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), email, status, detail, campaign),
        )
        conn.commit()
    finally:
        conn.close()
# -------------------------- SENDING -----------------------------------
def smtp_send(msg: EmailMessage, password: str) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(SENDER_EMAIL, password)
        s.send_message(msg)
def save_to_sent(msg: EmailMessage, password: str, log=print) -> None:
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(SENDER_EMAIL, password)
            imap.append(SENT_FOLDER, "\\Seen", imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes())
    except Exception as exc:  # non-fatal
        log(f"    (couldn't copy to Sent folder: {exc})")
def require_password() -> str:
    if not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_PASSWORD is not set in your .env file.")
    return EMAIL_PASSWORD
# -------------------------- PUBLIC API --------------------------------
def get_recipients(limit: int | None = None, test_to: str | None = None,
                    campaign: str = CAMPAIGN_INITIAL, log=print) -> list[dict]:
    """
    Mirrors the original CLI behavior, generalized to any number of
    sequential campaigns:
      - test mode: first `limit` (default 3) rows, ignoring the sent log.
      - send mode, first campaign in the sequence: rows not yet sent that email.
      - send mode, any later campaign: rows that completed the *previous*
        campaign in CAMPAIGN_SEQUENCE and have NOT yet gotten this one,
        optionally capped at `limit`.
    """
    if campaign not in CAMPAIGN_SEQUENCE:
        raise ValueError(f"Unknown campaign {campaign!r}; must be one of {CAMPAIGN_SEQUENCE}")
    rows = load_rows_sqlite(DB_PATH, log)
    if not test_to:
        idx = CAMPAIGN_SEQUENCE.index(campaign)
        not_yet_this_one = load_sent_emails(campaign, "sent")
        if idx == 0:
            rows = [r for r in rows if r["email"].lower() not in not_yet_this_one]
        else:
            prerequisite = CAMPAIGN_SEQUENCE[idx - 1]
            completed_prerequisite = load_sent_emails(prerequisite, "sent")
            rows = [r for r in rows
                    if r["email"].lower() in completed_prerequisite
                    and r["email"].lower() not in not_yet_this_one]
    if test_to:
        rows = rows[: limit or 3]
    elif limit:
        rows = rows[:limit]
    log(f"{len(rows)} email(s) queued.")
    return rows
def preview_first(campaign: str = CAMPAIGN_INITIAL) -> dict | None:
    """Return the rendered email that would go to the first queued recipient."""
    rows = get_recipients(campaign=campaign, log=lambda _m: None)
    if not rows:
        return None
    first = rows[0]
    return {
        "to": first["email"],
        "subject": build_subject(first, campaign),
        "plain": build_plain(first, campaign),
        "html": build_html(first, campaign),
        "count": len(rows),
    }
def send_campaign(rows: list[dict], test_to: str | None = None,
                   campaign: str = CAMPAIGN_INITIAL, log=print) -> None:
    password = require_password()
    for i, row in enumerate(rows, 1):
        to_addr = test_to or row["email"]
        msg = build_message(row, to_addr, campaign=campaign)
        try:
            smtp_send(msg, password)
            log(f"[{i}/{len(rows)}] sent -> {to_addr}  ({row['company_name']})")
            if not test_to:
                log_result(row["email"], "sent", campaign=campaign)
                if SAVE_TO_SENT_FOLDER:
                    save_to_sent(msg, password, log)
        except smtplib.SMTPAuthenticationError:
            log("Login failed. Check SENDER_EMAIL / EMAIL_PASSWORD in .env.")
            return
        except smtplib.SMTPRecipientsRefused as exc:
            log(f"[{i}/{len(rows)}] REFUSED {to_addr}: {exc}")
            log_result(row["email"], "refused", str(exc), campaign=campaign)
        except Exception as exc:
            log(f"[{i}/{len(rows)}] ERROR {to_addr}: {exc}")
            log_result(row["email"], "error", str(exc), campaign=campaign)
            if "limit" in str(exc).lower() or "rate" in str(exc).lower():
                log("Looks like a sending limit. Stop and re-run later; progress is saved.")
                return
        if i < len(rows):
            time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC) if not test_to else 2)
    log("Done.")
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send to recipients")
    ap.add_argument("--test-to", metavar="EMAIL",
                     help="send the first few rows to this address instead of the real recipients")
    ap.add_argument("--limit", type=int, default=None, help="max emails this run")
    ap.add_argument("--campaign", choices=CAMPAIGN_SEQUENCE, default=CAMPAIGN_INITIAL,
                     help="which email in the sequence to send (default: initial)")
    args = ap.parse_args()
    print(f"Reading recipients from SQLite: {DB_PATH} (table '{TABLE_NAME}'), campaign={args.campaign}")
    rows = get_recipients(limit=args.limit, test_to=args.test_to, campaign=args.campaign)
    if not rows:
        return
    # ---------- DRY RUN ----------
    if not args.send and not args.test_to:
        print("\n--- DRY RUN: preview of first email (plain-text version) ---")
        sample = preview_first(campaign=args.campaign)
        if sample is None:
            print("No eligible recipients for this campaign.")
            return
        print(f"To:      {sample['to']}\nSubject: {sample['subject']}\n")
        print(sample['plain'])
        print("\nNothing was sent. Use --test-to you@domain.com, then --send.")
        return
    password = EMAIL_PASSWORD or getpass.getpass(f"Password for {SENDER_EMAIL}: ")
    if args.send and not args.test_to:
        if input(f"Send {len(rows)} real emails? Type 'yes': ").strip().lower() != "yes":
            return
    send_campaign(rows=rows, test_to=args.test_to, campaign=args.campaign)
    print("Done.")
if __name__ == "__main__":
    main()