#!/usr/bin/env python3
"""
Send personalized outreach emails from people_data.csv via Namecheap Private Email.
Usage:
  python send_emails.py                         # dry run: preview only, sends nothing
  python send_emails.py --test-to me@mydomain.com   # send first 3 rows to YOURSELF
  python send_emails.py --send                  # send for real
"""
import argparse
import csv
import getpass
import html
import imaplib
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
# ------------------------- CONFIG -------------------------
SENDER_NAME = "Joane Joseph"
SENDER_EMAIL = "joane@showstop.io"      # <-- your Private Email address
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 465                               # SSL. (587 + STARTTLS also works)
IMAP_HOST = "mail.privateemail.com"
IMAP_PORT = 993
SAVE_TO_SENT_FOLDER = True                    # SMTP sends don't show up in "Sent" otherwise
SENT_FOLDER = "Sent"
CSV_PATH = "people_data.csv"
CSV_DELIMITER = ","                           # change to "|" if your file is pipe-delimited
LOG_PATH = "sent_log.csv"                     # prevents double-sending on re-runs
MIN_DELAY_SEC = 45                            # random pause between emails
MAX_DELAY_SEC = 90
# ----------------------------------------------------------
SUBJECT_TEMPLATE = "Direct-to-fan tour ticketing for {company_name}'s artists"
INTRO = [
    "My name is Joane Joseph, Founder and CEO of Show Stop.",
    "We are building a white-label software platform designed to give artists and "
    "music labels direct control over their tour ticketing experience. Our software "
    "allows your artists to host primary ticket sales directly on their own "
    "websites—connecting natively to primary ticketing APIs like Ticketmaster and AXS.",
    "Instead of losing fans to third-party portals where drop-off is high and "
    "post-sale revenue is lost, Show Stop enables:",
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
SIGNATURE = ["Best Regards,", "----", "Joane Joseph", "Founder & CEO | Show Stop"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
def build_plain(first_name: str) -> str:
    parts = [f"Hello {first_name},", ""]
    for p in INTRO:
        parts += [p, ""]
    for title, text in BULLETS:
        parts += [f"• {title}: {text}", ""]
    for p in OUTRO:
        parts += [p, ""]
    parts += ["", *SIGNATURE]
    return "\n".join(parts)
def build_html(first_name: str) -> str:
    e = html.escape
    out = [f"<p>Hello {e(first_name)},</p>"]
    out += [f"<p>{e(p)}</p>" for p in INTRO]
    out.append("<ul>")
    out += [f"<li><b>{e(t)}:</b> {e(x)}</li>" for t, x in BULLETS]
    out.append("</ul>")
    out += [f"<p>{e(p)}</p>" for p in OUTRO]
    out.append("<p>&nbsp;</p><p>" + "<br>".join(e(s) for s in SIGNATURE) + "</p>")
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
def load_rows(path: str) -> list[dict]:
    required = {"company_name", "first_name", "last_name", "email_address"}
    rows, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER, skipinitialspace=True)
        reader.fieldnames = [h.strip().strip('"') for h in (reader.fieldnames or [])]
        missing = required - set(reader.fieldnames)
        if missing:
            sys.exit(f"CSV is missing columns: {missing}. Found: {reader.fieldnames}")
        for n, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip().strip('"').strip() for k, v in raw.items() if k}
            email = row["email_address"].lower()
            if not EMAIL_RE.match(email):
                print(f"  [skip] line {n}: invalid email {row['email_address']!r}")
            elif not row["first_name"] or not row["company_name"]:
                print(f"  [skip] line {n}: missing first_name/company_name")
            elif email in seen:
                print(f"  [skip] line {n}: duplicate {email}")
            else:
                seen.add(email)
                rows.append(row)
    return rows
def load_already_sent() -> set[str]:
    if not os.path.exists(LOG_PATH):
        return set()
    with open(LOG_PATH, newline="", encoding="utf-8") as f:
        return {r["email_address"].lower() for r in csv.DictReader(f) if r["status"] == "sent"}
def log_result(email: str, status: str, detail: str = "") -> None:
    new = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "email_address", "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), email, status, detail])
def smtp_send(msg: EmailMessage, password: str) -> None:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
        s.login(SENDER_EMAIL, password)
        s.send_message(msg)
def save_to_sent(msg: EmailMessage, password: str) -> None:
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as imap:
            imap.login(SENDER_EMAIL, password)
            imap.append(SENT_FOLDER, "\\Seen", imaplib.Time2Internaldate(time.time()),
                        msg.as_bytes())
    except Exception as exc:  # non-fatal
        print(f"    (couldn't copy to Sent folder: {exc})")
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually send to recipients")
    ap.add_argument("--test-to", metavar="EMAIL",
                    help="send the first few rows to this address instead of the real recipients")
    ap.add_argument("--limit", type=int, default=None, help="max emails this run")
    args = ap.parse_args()
    rows = load_rows(CSV_PATH)
    already = load_already_sent()
    if not args.test_to:
        rows = [r for r in rows if r["email_address"].lower() not in already]
    if args.test_to:
        rows = rows[: args.limit or 3]
    elif args.limit:
        rows = rows[: args.limit]
    print(f"{len(rows)} email(s) queued ({len(already)} already sent previously).")
    if not rows:
        return
    # ---------- DRY RUN ----------
    if not args.send and not args.test_to:
        sample = build_message(rows[0], rows[0]["email_address"])
        print("\n--- DRY RUN: preview of first email ---")
        print(f"To:      {sample['To']}\nSubject: {sample['Subject']}\n")
        print(build_plain(rows[0]["first_name"]))
        print("\n--- all recipients ---")
        for r in rows:
            print(f"  {r['email_address']:<40} {SUBJECT_TEMPLATE.format(**r)}")
        print("\nNothing was sent. Use --test-to you@domain.com, then --send.")
        return
    password = os.environ.get("EMAIL_PASSWORD") or getpass.getpass(f"Password for {SENDER_EMAIL}: ")
    if args.send and not args.test_to:
        if input(f"Send {len(rows)} real emails? Type 'yes': ").strip().lower() != "yes":
            return
    for i, row in enumerate(rows, 1):
        to_addr = args.test_to or row["email_address"]
        msg = build_message(row, to_addr)
        try:
            smtp_send(msg, password)
            print(f"[{i}/{len(rows)}] sent -> {to_addr}  ({row['company_name']})")
            if not args.test_to:
                log_result(row["email_address"], "sent")
                if SAVE_TO_SENT_FOLDER:
                    save_to_sent(msg, password)
        except smtplib.SMTPAuthenticationError:
            sys.exit("Login failed. Check SENDER_EMAIL / password.")
        except smtplib.SMTPRecipientsRefused as exc:
            print(f"[{i}/{len(rows)}] REFUSED {to_addr}: {exc}")
            log_result(row["email_address"], "refused", str(exc))
        except Exception as exc:
            print(f"[{i}/{len(rows)}] ERROR {to_addr}: {exc}")
            log_result(row["email_address"], "error", str(exc))
            if "limit" in str(exc).lower() or "rate" in str(exc).lower():
                sys.exit("Looks like a sending limit. Stop and re-run later; progress is saved.")
        if i < len(rows):
            time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC) if not args.test_to else 2)
    print("Done.")
if __name__ == "__main__":
    main()