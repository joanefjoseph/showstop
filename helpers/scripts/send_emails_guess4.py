#!/usr/bin/env python3
"""
Send personalized outreach emails via Namecheap Private Email.
Recipients come from people_data (CSV or SQLite). If a person's email_address is
empty, the script guesses up to 4 addresses using the company's email_domain from
the companies table (companies.csv or SQLite table `companies`).
Usage:
  python send_emails.py                               # dry run (nothing is sent)
  python send_emails.py --source sqlite               # dry run from <COMPANY_NAME>.db
  python send_emails.py --infer-patterns              # learn each domain's pattern from known emails
  python send_emails.py --no-guess                    # only email known addresses
  python send_emails.py --test-to me@mydomain.com     # send first 3 people to YOURSELF
  python send_emails.py --send --limit 20             # send for real, max 20 emails
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
import sqlite3
import ssl
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from dotenv import load_dotenv
# ------------------------- LOAD .env -------------------------
load_dotenv(Path(__file__).resolve().with_name(".env"))
def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in your .env file.")
    return value
SENDER_NAME = require_env("SENDER_NAME")
SENDER_EMAIL = require_env("SENDER_EMAIL")
COMPANY_NAME = require_env("COMPANY_NAME")
COMPANY_URL = require_env("COMPANY_URL")          # e.g. www.thenewcompany.com
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")  # falls back to a prompt if empty
DEFAULT_SOURCE = os.getenv("DATA_SOURCE", "csv").strip().lower()
DB_PATH = os.getenv("DB_PATH", "").strip() or f"{COMPANY_NAME}.db"
# Link target: add https:// if the URL in .env doesn't include a scheme
COMPANY_HREF = COMPANY_URL if re.match(r"^https?://", COMPANY_URL, re.I) else f"https://{COMPANY_URL}"
# ------------------------- CONFIG -------------------------
SMTP_HOST = "mail.privateemail.com"
SMTP_PORT = 465                               # SSL. (587 + STARTTLS also works)
IMAP_HOST = "mail.privateemail.com"
IMAP_PORT = 993
SAVE_TO_SENT_FOLDER = True                    # SMTP sends don't show up in "Sent" otherwise
SENT_FOLDER = "Sent"
PEOPLE_CSV_PATH = "people_data.csv"
COMPANIES_CSV_PATH = "companies.csv"
CSV_DELIMITER = ","                           # change to "|" if your files are pipe-delimited
PEOPLE_TABLE = "people_data"
COMPANIES_TABLE = "companies"
LOG_PATH = "sent_log.csv"                     # prevents double-sending on re-runs
MIN_DELAY_SEC = 45                            # random pause between emails
MAX_DELAY_SEC = 90
# ----------------------------------------------------------
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
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
PEOPLE_COLUMNS = {"company_name", "first_name", "last_name", "email_address"}
COMPANY_COLUMNS = {"company_name", "email_domain"}
# The email patterns to try, in order. f = first name, l = last name (both normalized).
PATTERNS = {
    "first":      lambda f, l: f,                 # john@
    "firstlast":  lambda f, l: f + l,             # johnpark@
    "first.last": lambda f, l: f"{f}.{l}",        # john.park@
    "flast":      lambda f, l: f[0] + l,          # jpark@
}
# ------------------------- EMAIL CONTENT -------------------------
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
# ------------------------- READING TABLES -------------------------
def clean_record(raw: dict) -> dict:
    return {k: ("" if v is None else str(v)).strip().strip('"').strip()
            for k, v in raw.items() if k}
def read_csv_table(path: str, required: set, optional: bool = False) -> list[dict]:
    if not Path(path).is_file():
        if optional:
            print(f"  [warn] {path} not found.")
            return []
        sys.exit(f"CSV file not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER, skipinitialspace=True)
        reader.fieldnames = [h.strip().strip('"') for h in (reader.fieldnames or [])]
        missing = required - set(reader.fieldnames)
        if missing:
            sys.exit(f"{path} is missing columns: {missing}. Found: {reader.fieldnames}")
        return [clean_record(r) for r in reader]
def read_sqlite_table(db_path: str, table: str, required: set, optional: bool = False) -> list[dict]:
    path = Path(db_path).expanduser().resolve()
    # Check first: sqlite3 would silently create an empty DB if the file didn't exist.
    if not path.is_file():
        sys.exit(f"SQLite database not found: {path}\n"
                 f"(Expected '<COMPANY_NAME>.db'. Set DB_PATH in .env or use --db to override.)")
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)   # read-only
    except sqlite3.Error as exc:
        sys.exit(f"Could not open database {path}: {exc}")
    try:
        conn.row_factory = sqlite3.Row
        cols = {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if not cols:
            if optional:
                print(f"  [warn] table '{table}' not found in {path.name}.")
                return []
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            sys.exit(f"Table '{table}' not found in {path.name}. Tables present: {tables}")
        missing = required - cols
        if missing:
            sys.exit(f"Table '{table}' is missing columns: {missing}. Found: {sorted(cols)}")
        return [clean_record(dict(r)) for r in conn.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error as exc:
        sys.exit(f"Database error: {exc}")
    finally:
        conn.close()
# ------------------------- PEOPLE / COMPANIES / GUESSING -------------------------
def norm_company(name: str) -> str:
    return " ".join(name.casefold().split())
def slug(name: str) -> str:
    """'José' -> 'jose', "O'Brien" -> 'obrien', 'Mary-Jane' -> 'maryjane'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())
def validate_people(records: list[dict]) -> list[dict]:
    people = []
    for n, row in enumerate(records, start=1):
        email = row["email_address"]
        if not row["first_name"] or not row["company_name"]:
            print(f"  [skip] record {n}: missing first_name/company_name")
        elif email and not EMAIL_RE.match(email):
            print(f"  [skip] record {n}: invalid email {email!r}")
        else:
            people.append(row)
    return people
def build_domain_map(records: list[dict]) -> dict[str, str]:
    domains = {}
    for n, row in enumerate(records, start=1):
        domain = row["email_domain"].lower().lstrip("@")
        if not row["company_name"] or not DOMAIN_RE.match(domain):
            print(f"  [skip] companies record {n}: bad company/domain "
                  f"({row['company_name']!r}, {row['email_domain']!r})")
            continue
        domains[norm_company(row["company_name"])] = domain
    return domains
def guess_emails(first: str, last: str, domain: str, only: str | None = None) -> list[str]:
    f, l = slug(first), slug(last)
    if not f:
        return []                      # e.g. name in a non-Latin script
    out = []
    for name, fn in PATTERNS.items():
        if only and name != only:
            continue
        if name != "first" and not l:
            continue                   # pattern needs a last name
        addr = f"{fn(f, l)}@{domain}"
        if addr not in out:
            out.append(addr)
    if not out and only:               # inferred pattern unusable for this person
        return guess_emails(first, last, domain)
    return out
def infer_patterns(people: list[dict]) -> dict[str, str]:
    """Learn each domain's pattern from people whose email we already know."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for p in people:
        email = p["email_address"].lower()
        if not email:
            continue
        local, domain = email.rsplit("@", 1)
        f, l = slug(p["first_name"]), slug(p["last_name"])
        if not f:
            continue
        for name, fn in PATTERNS.items():
            if name != "first" and not l:
                continue
            if fn(f, l) == local:
                votes[domain][name] += 1
                break
    return {d: c.most_common(1)[0][0] for d, c in votes.items()}
def build_targets(people: list[dict], domains: dict[str, str],
                  guess: bool, infer: bool) -> list[list[dict]]:
    """Return one list of targets per person. Target = {row, to, guessed}."""
    inferred = infer_patterns(people) if infer else {}
    if inferred:
        print("Inferred patterns from known emails:")
        for d, p in sorted(inferred.items()):
            print(f"  {d:<35} {p}")
    known = {p["email_address"].lower() for p in people if p["email_address"]}
    seen: set[str] = set()
    per_person: list[list[dict]] = []
    for p in people:
        who = f'{p["first_name"]} {p["last_name"]} ({p["company_name"]})'
        email = p["email_address"].lower()
        if email:                                            # known address -> 1 email
            if email in seen:
                print(f"  [skip] duplicate {email}")
                continue
            seen.add(email)
            per_person.append([{"row": p, "to": email, "guessed": False}])
            continue
        if not guess:
            print(f"  [skip] {who}: no email (guessing disabled)")
            continue
        domain = domains.get(norm_company(p["company_name"]))
        if not domain:
            print(f"  [skip] {who}: no email and company not found in companies table")
            continue
        candidates = guess_emails(p["first_name"], p["last_name"], domain, inferred.get(domain))
        if not candidates:
            print(f"  [skip] {who}: can't build an email address from this name")
            continue
        targets = []
        for addr in candidates:
            if addr in seen or addr in known:                # belongs to someone else in the list
                continue
            seen.add(addr)
            targets.append({"row": p, "to": addr, "guessed": True})
        if targets:
            per_person.append(targets)
    return per_person
# ------------------------- SEND LOG -------------------------
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
# ------------------------- SENDING -------------------------
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
    ap.add_argument("--source", choices=["csv", "sqlite"], default=DEFAULT_SOURCE,
                    help="where to read people/companies from (default: DATA_SOURCE in .env, else csv)")
    ap.add_argument("--csv", default=PEOPLE_CSV_PATH, metavar="PATH", help="people CSV path")
    ap.add_argument("--companies-csv", default=COMPANIES_CSV_PATH, metavar="PATH",
                    help="companies CSV path")
    ap.add_argument("--db", default=DB_PATH, metavar="PATH",
                    help="SQLite path (default: <COMPANY_NAME>.db)")
    ap.add_argument("--no-guess", action="store_true",
                    help="skip people with no email instead of guessing addresses")
    ap.add_argument("--infer-patterns", action="store_true",
                    help="if a domain's pattern can be learned from known emails, "
                         "send only that pattern instead of all 4 (fewer bounces)")
    ap.add_argument("--send", action="store_true", help="actually send to recipients")
    ap.add_argument("--test-to", metavar="EMAIL",
                    help="send the first few people's email to this address instead")
    ap.add_argument("--limit", type=int, default=None, help="max EMAILS (not people) this run")
    args = ap.parse_args()
    # ---------- load data ----------
    guess = not args.no_guess
    if args.source == "sqlite":
        print(f"Reading from SQLite: {args.db}")
        people_raw = read_sqlite_table(args.db, PEOPLE_TABLE, PEOPLE_COLUMNS)
        companies_raw = read_sqlite_table(args.db, COMPANIES_TABLE, COMPANY_COLUMNS,
                                          optional=True) if guess else []
    else:
        print(f"Reading from CSV: {args.csv}")
        people_raw = read_csv_table(args.csv, PEOPLE_COLUMNS)
        companies_raw = read_csv_table(args.companies_csv, COMPANY_COLUMNS,
                                       optional=True) if guess else []
    people = validate_people(people_raw)
    domains = build_domain_map(companies_raw)
    per_person = build_targets(people, domains, guess=guess, infer=args.infer_patterns)
    # ---------- build the send queue ----------
    already = load_already_sent()
    if args.test_to:
        # one email per person is enough to check formatting
        targets = [ts[0] for ts in per_person][: args.limit or 3]
    else:
        targets = [t for ts in per_person for t in ts if t["to"] not in already]
        if args.limit:
            targets = targets[: args.limit]
    n_known = sum(not t["guessed"] for t in targets)
    n_guess = sum(t["guessed"] for t in targets)
    n_guess_people = len({id(t["row"]) for t in targets if t["guessed"]})
    print(f"\n{len(targets)} email(s) queued: {n_known} to known addresses, "
          f"{n_guess} guessed addresses for {n_guess_people} people. "
          f"({len(already)} already sent previously.)")
    if not targets:
        return
    # ---------- DRY RUN ----------
    if not args.send and not args.test_to:
        first = targets[0]
        sample = build_message(first["row"], first["to"])
        print("\n--- DRY RUN: preview of first email (plain-text version) ---")
        print(f"To:      {sample['To']}\nSubject: {sample['Subject']}\n")
        print(build_plain(first["row"]["first_name"]))
        print("\n--- all queued emails ---")
        for t in targets:
            tag = "[guess]" if t["guessed"] else "[known]"
            name = f'{t["row"]["first_name"]} {t["row"]["last_name"]}'
            print(f"  {tag} {t['to']:<42} {name} / {t['row']['company_name']}")
        print("\nNothing was sent. Use --test-to you@domain.com, then --send.")
        return
    password = EMAIL_PASSWORD or getpass.getpass(f"Password for {SENDER_EMAIL}: ")
    if args.send and not args.test_to:
        prompt = (f"Send {len(targets)} real emails ({n_known} known, {n_guess} guessed; "
                  f"expect most wrong guesses to bounce)? Type 'yes': ")
        if input(prompt).strip().lower() != "yes":
            return
    for i, t in enumerate(targets, 1):
        row = t["row"]
        to_addr = args.test_to or t["to"]
        tag = "guess" if t["guessed"] else "known"
        msg = build_message(row, to_addr)
        try:
            smtp_send(msg, password)
            print(f"[{i}/{len(targets)}] sent ({tag}) -> {to_addr}  "
                  f"({row['first_name']} {row['last_name']}, {row['company_name']})")
            if not args.test_to:
                log_result(t["to"], "sent", "guessed" if t["guessed"] else "")
                if SAVE_TO_SENT_FOLDER:
                    save_to_sent(msg, password)
        except smtplib.SMTPAuthenticationError:
            sys.exit("Login failed. Check SENDER_EMAIL / EMAIL_PASSWORD in .env.")
        except smtplib.SMTPRecipientsRefused as exc:
            print(f"[{i}/{len(targets)}] REFUSED {to_addr}: {exc}")
            log_result(t["to"], "refused", str(exc))
        except Exception as exc:
            print(f"[{i}/{len(targets)}] ERROR {to_addr}: {exc}")
            log_result(t["to"], "error", str(exc))
            if "limit" in str(exc).lower() or "rate" in str(exc).lower():
                sys.exit("Looks like a sending limit. Stop and re-run later; progress is saved.")
        if i < len(targets):
            time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC) if not args.test_to else 2)
    print("Done.")
if __name__ == "__main__":
    main()