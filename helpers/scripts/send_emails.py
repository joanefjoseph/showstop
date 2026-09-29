#!/usr/bin/env python3
"""
Send personalized outreach emails via Namecheap Private Email.
Recipients are read from the `employees` table of clients.db.
Importable by app.py; the Flask UI is the entry point.
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
from config import BASE, DB_PATH, TABLE_NAME, SENT_TABLE_NAME
# ------------------------- LOAD .env -------------------------
load_dotenv(BASE / ".env")
def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in your .env file.")
    return value
SENDER_NAME = require_env("SENDER_NAME")
SENDER_EMAIL = require_env("SENDER_EMAIL")
COMPANY_NAME = require_env("COMPANY_NAME")
COMPANY_URL = require_env("COMPANY_URL")                 # e.g. www.thenewcompany.com
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "").strip() # falls back to a prompt if empty
# Link target: add https:// if the URL in .env doesn't include a scheme
COMPANY_HREF = COMPANY_URL if re.match(r"^https?://", COMPANY_URL, re.I) else f"https://{COMPANY_URL}"
# ------------------------- CONFIG -------------------------
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 465                     # SSL. (587 + STARTTLS also works)
IMAP_HOST = "mail.privateemail.com"
IMAP_PORT = 993
SAVE_TO_SENT_FOLDER = True          # SMTP sends don't show up in "Sent" otherwise
SENT_FOLDER = "Sent"
MIN_DELAY_SEC = 45                  # random pause between emails
MAX_DELAY_SEC = 90
REQUIRED_COLUMNS = {"company_name", "first_name", "last_name", "email"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# ------------------------- EMAIL CONTENT -------------------------
# Placeholders: {company} -> company name (bold in HTML), {sender} -> your name
SUBJECT_TEMPLATE = "Direct-to-fan tour ticketing for {company_name}'s artists"
INTRO = [
    "My name is {sender}, Founder and CEO of {company}.",
    "We are building a white-label software platform designed to give artists and "
    "music labels direct control over their tour ticketing experience. Our software "
    "allows your artists to host primary ticket sales directly on their own "
    "websites—connecting natively to primary ticketing APIs like Ticketmaster and AXS.",
    "Instead of losing fans to third-party portals where drop-off is high and "
    "post-sale revenue is lost, {company} enables:",
]
BULLETS = [
    ("Direct Ecosystem Monetization",
     "Keep fans on your artist's website through the entire ticket purchase. "
     "Immediately post-checkout (when buying intent peaks), route fans into tour "
     "apparel, album pre-orders, and VIP upgrades (averaging 15–30% upsell conversion)."),
    ("Seamless Fan Club & Presale Gating",
     "Give loyal fans the friction-free, secure buying experience they expect by "
     "authenticating official memberships natively at checkout—eliminating redirects, "
     "bots, and leaked promo codes."),
    ("Unified Multi-Vendor Experience",
     "Deliver a single, consistent checkout flow across your entire tour, regardless "
     "of whether individual venues use Ticketmaster, AXS, or another primary ticket vendor."),
]
OUTRO = [
    "Whenever tour ticketing is on your roadmap next, we are onboarding a select group "
    "of tour partners for our Early Access Program ($0 platform fee) as we finalize "
    "our product.",
    "Would you be open to a 10-minute introductory call in the next week to see a "
    "brief visual preview?",
]
SIGNATURE = [
    "Best Regards,",
    "----",
    "{sender}",
    "Founder & CEO | {company}",
]
def render(text: str, html_mode: bool) -> str:
    """Fill in {sender} and {company}. In HTML, the company name is bolded."""
    if html_mode:
        text = html.escape(text)
        return (text.replace("{sender}", html.escape(SENDER_NAME))
                    .replace("{company}", f"<b>{html.escape(COMPANY_NAME)}</b>"))
    return text.replace("{sender}", SENDER_NAME).replace("{company}", COMPANY_NAME)
def build_plain(first_name: str) -> str:
    r = lambda t: render(t, False)
    parts = [f"Hello {first_name},", ""]
    for p in INTRO:
        parts += [r(p), ""]
    for title, text in BULLETS:
        parts += [f"• {title}: {text}", ""]
    for p in OUTRO:
        parts += [p, ""]
    parts += ["", *[r(s) for s in SIGNATURE], COMPANY_URL]
    return "\n".join(parts)
def build_html(first_name: str) -> str:
    r = lambda t: render(t, True)
    e = html.escape
    out = [f"<p>Hello {e(first_name)},</p>"]
    out += [f"<p>{r(p)}</p>" for p in INTRO]
    out.append("<ul>")
    out += [f"<li><b>{e(t)}:</b> {e(x)}</li>" for t, x in BULLETS]
    out.append("</ul>")
    out += [f"<p>{e(p)}</p>" for p in OUTRO]
    sig_lines = [r(s) for s in SIGNATURE]
    sig_lines.append(f'<a href="{e(COMPANY_HREF, quote=True)}">{e(COMPANY_URL)}</a>')
    out.append("<p>&nbsp;</p><p>" + "<br>".join(sig_lines) + "</p>")
    return ('<html><body style="font-family:Arial,sans-serif;font-size:14px;'
            'line-height:1.5;color:#222">' + "".join(out) + "</body></html>")
def build_message(row: dict, to_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = SUBJECT_TEMPLATE.format(company_name=row["company_name"])
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL))
    msg["To"] = formataddr((f'{row["first_name"]} {row["last_name"]}'.strip(), to_addr))
    msg["Reply-To"] = SENDER_EMAIL
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=SENDER_EMAIL.split("@")[1])
    msg.set_content(build_plain(row["first_name"]))
    msg.add_alternative(build_html(row["first_name"]), subtype="html")
    return msg
# ------------------------- DATA SOURCE -------------------------
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
        records = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE_NAME}")]
    except sqlite3.Error as exc:
        raise RuntimeError(f"Database error: {exc}") from exc
    finally:
        conn.close()
    return clean_records(records, log)
# ------------------------- SEND LOG -------------------------
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
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT NOT NULL,
                email         TEXT NOT NULL,
                status        TEXT NOT NULL,
                detail        TEXT
            )
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{SENT_TABLE_NAME}_email_status
            ON {SENT_TABLE_NAME} (email, status)
        """)
        conn.commit()
    finally:
        conn.close()
def load_already_sent() -> set[str]:
    init_sent_table()
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT LOWER(email) AS email "
            f"FROM {SENT_TABLE_NAME} WHERE status = 'sent'"
        ).fetchall()
    finally:
        conn.close()
    return {r["email"] for r in rows}
def log_result(email: str, status: str, detail: str = "") -> None:
    init_sent_table()
    conn = _connect()
    try:
        conn.execute(
            f"INSERT INTO {SENT_TABLE_NAME} (timestamp, email, status, detail) "
            f"VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(timespec="seconds"), email, status, detail),
        )
        conn.commit()
    finally:
        conn.close()
# ------------------------- SENDING -------------------------
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
# ------------------------- PUBLIC API -------------------------
def get_recipients(limit: int | None = None, test_to: str | None = None, log=print) -> list[dict]:
    """
    Mirrors the original CLI behavior:
      - test mode: first `limit` (default 3) rows, ignoring the sent log
      - send mode: rows not yet sent, optionally capped at `limit`
    """
    rows = load_rows_sqlite(DB_PATH, log)
    if not test_to:
        already = load_already_sent()
        rows = [r for r in rows if r["email"].lower() not in already]
    if test_to:
        rows = rows[: limit or 3]
    elif limit:
        rows = rows[:limit]
    log(f"{len(rows)} email(s) queued.")
    return rows
def preview_first() -> dict | None:
    """Return the rendered email that would go to the first queued recipient."""
    rows = get_recipients(log=lambda _m: None)
    if not rows:
        return None
    first = rows[0]
    return {
        "to": first["email"],
        "subject": SUBJECT_TEMPLATE.format(company_name=first["company_name"]),
        "plain": build_plain(first["first_name"]),
        "html": build_html(first["first_name"]),
        "count": len(rows),
    }
def send_campaign(rows: list[dict], test_to: str | None = None, log=print) -> None:
    password = require_password()
    for i, row in enumerate(rows, 1):
        to_addr = test_to or row["email"]
        msg = build_message(row, to_addr)
        try:
            smtp_send(msg, password)
            log(f"[{i}/{len(rows)}] sent -> {to_addr}  ({row['company_name']})")
            if not test_to:
                log_result(row["email"], "sent")
                if SAVE_TO_SENT_FOLDER:
                    save_to_sent(msg, password, log)
        except smtplib.SMTPAuthenticationError:
            log("Login failed. Check SENDER_EMAIL / EMAIL_PASSWORD in .env.")
            return
        except smtplib.SMTPRecipientsRefused as exc:
            log(f"[{i}/{len(rows)}] REFUSED {to_addr}: {exc}")
            log_result(row["email"], "refused", str(exc))
        except Exception as exc:
            log(f"[{i}/{len(rows)}] ERROR {to_addr}: {exc}")
            log_result(row["email"], "error", str(exc))
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
    args = ap.parse_args()
    print(f"Reading recipients from SQLite: {DB_PATH} (table '{TABLE_NAME}')")
    rows = get_recipients(limit=args.limit, test_to=args.test_to)
    if not rows:
        return
    # ---------- DRY RUN ----------
    if not args.send and not args.test_to:
        print("\n--- DRY RUN: preview of first email (plain-text version) ---")
        sample = preview_first()
        if sample is None:
            print("No eligible recipients in clients.db.")
            return
        print(f"To:      {sample['to']}\nSubject: {sample['subject']}\n")
        print(sample['plain'])
        print("\nNothing was sent. Use --test-to you@domain.com, then --send.")
        return
    password = EMAIL_PASSWORD or getpass.getpass(f"Password for {SENDER_EMAIL}: ")
    if args.send and not args.test_to:
        if input(f"Send {len(rows)} real emails? Type 'yes': ").strip().lower() != "yes":
            return
    send_campaign(rows=rows, test_to=args.test_to)
    print("Done.")
if __name__ == "__main__":
    main()