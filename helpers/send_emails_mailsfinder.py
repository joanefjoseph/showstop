#!/usr/bin/env python3
"""
Send personalized outreach emails via Namecheap Private Email (max 1 email per person).
People with a blank email_address are NOT emailed blindly. Their address is looked up
through the Mailsfinder API. A Valid address is saved back to people_data and used;
everyone still blank (nothing valid found, or daily quota used up) is skipped.
Usage:
  python send_emails.py                               # dry run: no emails, no API calls
  python send_emails.py --find-test John Park weversecompany.com   # test Email-Find (1 credit)
  python send_emails.py --verify-test a@b.com         # test Email-Verify (1 credit)
  python send_emails.py --verify-only                 # look up blanks + update people_data, send nothing
  python send_emails.py --test-to me@mydomain.com     # send first 3 emails to YOURSELF
  python send_emails.py --send --limit 20             # look up blanks, then send for real (max 20)
  python send_emails.py --send --no-guess             # known addresses only, no API calls
  python send_emails.py --send --strategy verify      # guess 4 patterns + Email-Verify instead of Email-Find
  (add --source sqlite to use <COMPANY_NAME>.db instead of the CSV files)
"""
import argparse
import csv
import getpass
import html
import imaplib
import json
import os
import random
import re
import shutil
import smtplib
import sqlite3
import ssl
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
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
MF_API_KEY = os.getenv("MAILSFINDER_API_KEY", "").strip()
MF_BASE_URL = os.getenv("MAILSFINDER_BASE_URL",
                        "https://server.mailsfinder.com/api/access-key").strip().rstrip("/")
MF_DAILY_LIMIT = int(os.getenv("MAILSFINDER_DAILY_LIMIT", "100"))
MF_MIN_CONFIDENCE = float(os.getenv("MAILSFINDER_MIN_CONFIDENCE", "0"))
MF_DEFAULT_STRATEGY = os.getenv("MAILSFINDER_STRATEGY", "find").strip().lower()
if MF_DEFAULT_STRATEGY not in ("find", "verify"):
    MF_DEFAULT_STRATEGY = "find"
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
CHECKS_CSV_PATH = "mailsfinder_checks.csv"    # API lookup history (CSV mode)
CSV_DELIMITER = ","                           # change to "|" if your files are pipe-delimited
PEOPLE_TABLE = "people_data"
COMPANIES_TABLE = "companies"
CHECKS_TABLE = "mailsfinder_checks"           # API lookup history (SQLite mode)
LOG_PATH = "sent_log.csv"                     # prevents double-sending on re-runs
MIN_DELAY_SEC = 45                            # random pause between emails
MAX_DELAY_SEC = 90
API_DELAY_SEC = 1.0                           # pause between Mailsfinder calls
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
CHECK_FIELDS = ["lookup_key", "kind", "email", "status", "confidence", "checked_at",
                "first_name", "last_name", "company_name", "raw"]
# Patterns for --strategy verify. Default order = most common corporate formats first.
# If a domain's pattern is already known from other people, that pattern is tried first.
PATTERNS = {
    "first.last": lambda f, l: f"{f}.{l}",        # john.park@
    "flast":      lambda f, l: f[0] + l,          # jpark@
    "first":      lambda f, l: f,                 # john@
    "firstlast":  lambda f, l: f + l,             # johnpark@
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
# ------------------------- DATA STORES (CSV / SQLite) -------------------------
def clean_record(raw: dict) -> dict:
    return {k: ("" if v is None else str(v)).strip().strip('"').strip()
            for k, v in raw.items() if k}
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
def check_row(key, kind, email, status, confidence, person: dict, raw: str) -> list:
    return [key, kind, email, status, "" if confidence is None else confidence, now_iso(),
            person.get("first_name", ""), person.get("last_name", ""),
            person.get("company_name", ""), raw]
class CsvStore:
    """people_data.csv + companies.csv + mailsfinder_checks.csv"""
    def __init__(self, people_path: str, companies_path: str, checks_path: str):
        self.people_path, self.companies_path, self.checks_path = people_path, companies_path, checks_path
        self._people: list[dict] = []
        self._people_fields: list[str] = []
        self._backed_up = False
    def _read(self, path: str, required: set, optional: bool = False):
        if not Path(path).is_file():
            if optional:
                print(f"  [warn] {path} not found.")
                return [], []
            sys.exit(f"CSV file not found: {path}")
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=CSV_DELIMITER, skipinitialspace=True)
            reader.fieldnames = [h.strip().strip('"') for h in (reader.fieldnames or [])]
            missing = required - set(reader.fieldnames)
            if missing:
                sys.exit(f"{path} is missing columns: {missing}. Found: {reader.fieldnames}")
            return [clean_record(r) for r in reader], list(reader.fieldnames)
    def load_people(self) -> list[dict]:
        self._people, self._people_fields = self._read(self.people_path, PEOPLE_COLUMNS)
        return [{**row, "_id": i} for i, row in enumerate(self._people)]
    def load_companies(self) -> list[dict]:
        return self._read(self.companies_path, COMPANY_COLUMNS, optional=True)[0]
    def load_checks(self) -> dict[str, dict]:
        if not Path(self.checks_path).is_file():
            return {}
        with open(self.checks_path, newline="", encoding="utf-8") as f:
            return {r["lookup_key"]: r for r in csv.DictReader(f)}
    def record_check(self, key, kind, email, status, confidence, person: dict, raw: str) -> None:
        new = not Path(self.checks_path).is_file()
        with open(self.checks_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CHECK_FIELDS)
            w.writerow(check_row(key, kind, email, status, confidence, person, raw))
    def set_email(self, person_id, email: str) -> None:
        """Write a verified address back into people_data.csv (backup made on first write)."""
        if not self._backed_up:
            shutil.copy2(self.people_path, self.people_path + ".bak")
            self._backed_up = True
        self._people[int(person_id)]["email_address"] = email
        tmp = self.people_path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self._people_fields, delimiter=CSV_DELIMITER,
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(self._people)
        os.replace(tmp, self.people_path)
    def close(self) -> None:
        pass
class SqliteStore:
    """Tables people_data + companies + mailsfinder_checks inside <COMPANY_NAME>.db"""
    def __init__(self, db_path: str):
        path = Path(db_path).expanduser().resolve()
        # Check first: sqlite3 would silently create an empty DB if the file didn't exist.
        if not path.is_file():
            sys.exit(f"SQLite database not found: {path}\n"
                     f"(Expected '<COMPANY_NAME>.db'. Set DB_PATH in .env or use --db to override.)")
        self.name = path.name
        try:
            self.conn = sqlite3.connect(str(path), timeout=30)
        except sqlite3.Error as exc:
            sys.exit(f"Could not open database {path}: {exc}")
        self.conn.row_factory = sqlite3.Row
    def _table_exists(self, table: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                 (table,)).fetchone() is not None
    def _read(self, table: str, required: set, select: str = "*", optional: bool = False) -> list[dict]:
        if not self._table_exists(table):
            if optional:
                print(f"  [warn] table '{table}' not found in {self.name}.")
                return []
            sys.exit(f"Table '{table}' not found in {self.name}.")
        cols = {r["name"] for r in self.conn.execute(f'PRAGMA table_info("{table}")')}
        missing = required - cols
        if missing:
            sys.exit(f"Table '{table}' is missing columns: {missing}. Found: {sorted(cols)}")
        return [clean_record(dict(r)) for r in self.conn.execute(f'SELECT {select} FROM "{table}"')]
    def load_people(self) -> list[dict]:
        return self._read(PEOPLE_TABLE, PEOPLE_COLUMNS, select="rowid AS _id, *")
    def load_companies(self) -> list[dict]:
        return self._read(COMPANIES_TABLE, COMPANY_COLUMNS, optional=True)
    def load_checks(self) -> dict[str, dict]:
        if not self._table_exists(CHECKS_TABLE):
            return {}
        return {r["lookup_key"]: dict(r)
                for r in self.conn.execute(f'SELECT * FROM "{CHECKS_TABLE}"')}
    def record_check(self, key, kind, email, status, confidence, person: dict, raw: str) -> None:
        self.conn.execute(f'''CREATE TABLE IF NOT EXISTS "{CHECKS_TABLE}" (
            lookup_key TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT, status TEXT NOT NULL,
            confidence REAL, checked_at TEXT NOT NULL,
            first_name TEXT, last_name TEXT, company_name TEXT, raw TEXT)''')
        row = check_row(key, kind, email, status, confidence, person, raw)
        row[4] = confidence                                   # keep NULL instead of ""
        self.conn.execute(f'INSERT OR REPLACE INTO "{CHECKS_TABLE}" VALUES (?,?,?,?,?,?,?,?,?,?)', row)
        self.conn.commit()
    def set_email(self, person_id, email: str) -> None:
        self.conn.execute(f'UPDATE "{PEOPLE_TABLE}" SET email_address = ? WHERE rowid = ?',
                          (email, int(person_id)))
        self.conn.commit()
    def close(self) -> None:
        self.conn.close()
# ------------------------- MAILSFINDER API -------------------------
# Docs: https://mailsfinder.com/docs/api
#   POST {base}/email/findEmail    {"first_name", "last_name", "domain"}
#        -> {"email", "status": Valid|Invalid|Unknown, "confidence": 0-100, "domain", "time"}
#   POST {base}/email/verifyEmail  {"email"}
#        -> {"email", "status": Valid|Invalid|Unknown, "domain", "time"}
#   Auth: "Authorization: Bearer <API key>"
class QuotaExhausted(Exception):
    pass
class VerifierError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code
class StopVerification(Exception):
    pass
def _mf_post(endpoint: str, payload: dict) -> tuple[dict, str]:
    """POST JSON to Mailsfinder. Returns (response fields, raw response text)."""
    req = Request(
        f"{MF_BASE_URL}/{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {MF_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; outreach-script/1.0)",
        },
    )
    try:
        with urlopen(req, timeout=90) as resp:
            body = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        if exc.code in (401, 403):
            sys.exit(f"Mailsfinder rejected the API key (HTTP {exc.code}): {body}\n"
                     f"Check MAILSFINDER_API_KEY in .env.")
        if exc.code in (402, 429):
            raise QuotaExhausted(body)
        raise VerifierError(f"HTTP {exc.code}: {body}", code=exc.code)
    except (URLError, OSError) as exc:
        raise VerifierError(str(exc))
    try:
        data = json.loads(body)
    except ValueError:
        raise VerifierError(f"non-JSON response: {body[:200]}")
    if not isinstance(data, dict):
        raise VerifierError(f"unexpected response: {body[:200]}")
    # The docs show flat fields; tolerate a {"data": {...}} / {"result": {...}} wrapper too.
    for wrapper in ("data", "result"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and ("status" in inner or "email" in inner):
            data = inner
            break
    if "status" not in data:
        # e.g. an error/limit message with HTTP 200. Don't cache it as a result.
        raise VerifierError(f"response has no 'status' field: {body[:200]}")
    return data, body[:1000]
def _norm_status(value) -> str:
    """Valid | Invalid | Unknown  ->  valid | invalid | unknown"""
    s = str(value or "").strip().lower()
    return s if s in ("valid", "invalid") else "unknown"
def mailsfinder_verify(email: str) -> tuple[str, str]:
    """Email-Verify. Returns (status, raw_response)."""
    data, raw = _mf_post("email/verifyEmail", {"email": email})
    return _norm_status(data.get("status")), raw
def mailsfinder_find(first_name: str, last_name: str, domain: str):
    """Email-Find. Returns (status, email, confidence, raw_response)."""
    try:
        data, raw = _mf_post("email/findEmail",
                             {"first_name": first_name, "last_name": last_name, "domain": domain})
    except VerifierError as exc:
        if exc.code == 404:                      # treat "404" as "nothing found" for this person
            return "not_found", "", None, str(exc)
        raise
    email = str(data.get("email") or "").strip().lower()
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    status = _norm_status(data.get("status"))
    if status == "valid" and not EMAIL_RE.match(email):
        status = "not_found"
    return status, email, confidence, raw
def checks_today(checks: dict[str, dict]) -> int:
    today = date.today().isoformat()
    return sum(1 for c in checks.values() if str(c.get("checked_at", "")).startswith(today))
# ------------------------- PEOPLE / COMPANIES / PATTERNS -------------------------
def norm_company(name: str) -> str:
    return " ".join(name.casefold().split())
def slug(name: str) -> str:
    """'José' -> 'jose', "O'Brien" -> 'obrien', 'Mary-Jane' -> 'maryjane'."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_name.lower())
def who(p: dict) -> str:
    return f'{p["first_name"]} {p["last_name"]} ({p["company_name"]})'
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
def match_pattern(first: str, last: str, local: str) -> str | None:
    f, l = slug(first), slug(last)
    if not f:
        return None
    for name, fn in PATTERNS.items():
        if (l or name == "first") and fn(f, l) == local:
            return name
    return None
def learn_patterns(people: list[dict]) -> dict[str, Counter]:
    """Count which pattern each domain uses, based on people whose email we already know."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for p in people:
        email = p["email_address"].lower()
        if email:
            local, domain = email.rsplit("@", 1)
            pattern = match_pattern(p["first_name"], p["last_name"], local)
            if pattern:
                votes[domain][pattern] += 1
    return votes
def guess_emails(first: str, last: str, domain: str, votes: Counter) -> list[str]:
    """Candidate addresses, most likely first."""
    f, l = slug(first), slug(last)
    if not f:
        return []                                  # e.g. name in a non-Latin script
    names = sorted(PATTERNS, key=lambda n: -votes.get(n, 0))   # stable: ties keep default order
    out = []
    for name in names:
        if name != "first" and not l:
            continue                               # pattern needs a last name
        addr = f"{PATTERNS[name](f, l)}@{domain}"
        if addr not in out:
            out.append(addr)
    return out
# ------------------------- RESOLVING BLANK EMAILS -------------------------
class Resolver:
    """Fills in blank email addresses via Mailsfinder, within the daily quota."""
    def __init__(self, people: list[dict], domains: dict[str, str], store, strategy: str):
        self.people, self.domains, self.store, self.strategy = people, domains, store, strategy
        self.checks = store.load_checks()
        self.remaining = max(0, MF_DAILY_LIMIT - checks_today(self.checks))
        self.votes = learn_patterns(people)
        self.known = {p["email_address"].lower() for p in people if p["email_address"]}
        self.domain_stats: dict[str, Counter] = defaultdict(Counter)   # verify results per domain
        for c in self.checks.values():
            if c.get("kind") == "verify" and "@" in str(c.get("email", "")):
                self.domain_stats[c["email"].rsplit("@", 1)[1]][c["status"]] += 1
        self.found = self.calls = self.errors = 0
    # ----- helpers -----
    def _call(self, fn, *args):
        """Run one API call. Returns its result, or None on a (non-cached) API error."""
        if self.remaining <= 0:
            raise StopVerification("daily Mailsfinder quota used up")
        try:
            result = fn(*args)
        except QuotaExhausted:
            self.remaining = 0
            raise StopVerification("Mailsfinder reports the quota/rate limit is exhausted")
        except VerifierError as exc:
            self.errors += 1
            print(f"  [api error] {exc}")
            if self.errors >= 3:
                raise StopVerification("3 API errors in this run")
            return None
        self.remaining -= 1
        self.calls += 1
        time.sleep(API_DELAY_SEC)
        return result
    def _record(self, key, kind, email, status, confidence, p, raw) -> None:
        self.store.record_check(key, kind, email, status, confidence, p, raw)
        self.checks[key] = {"kind": kind, "email": email, "status": status, "checked_at": now_iso()}
        if kind == "verify" and "@" in email:
            self.domain_stats[email.rsplit("@", 1)[1]][status] += 1
    def _accept(self, p: dict, addr: str, note: str = "") -> None:
        p["email_address"] = addr
        p["_verified"] = True
        self.store.set_email(p["_id"], addr)
        self.known.add(addr)
        local, domain = addr.rsplit("@", 1)
        pattern = match_pattern(p["first_name"], p["last_name"], local)
        if pattern:
            self.votes[domain][pattern] += 1       # next person at this domain tries this first
        self.found += 1
        print(f"  [found] {who(p)} -> {addr} {note}")
    def _unverifiable(self, domain: str) -> bool:
        """Likely a catch-all domain: only 'unknown' answers so far."""
        s = self.domain_stats[domain]
        return s["unknown"] >= 2 and s["valid"] + s["invalid"] == 0
    # ----- strategy: Email-Find (1 call per person) -----
    def _find(self, p: dict, domain: str) -> None:
        key = f'find:{p["first_name"].casefold()}|{p["last_name"].casefold()}|{domain}'
        cached = self.checks.get(key)
        if cached:
            addr = str(cached.get("email") or "").lower()
            if cached["status"] == "valid" and addr and addr not in self.known:
                self._accept(p, addr, "(from earlier lookup)")
            else:
                print(f"  [skip] {who(p)}: already looked up, no valid address")
            return
        if not p["last_name"]:
            print(f"  [skip] {who(p)}: Email-Find needs a last name")
            return
        result = self._call(mailsfinder_find, p["first_name"], p["last_name"], domain)
        if result is None:
            return
        status, addr, confidence, raw = result
        self._record(key, "find", addr, status, confidence, p, raw)
        conf_txt = "" if confidence is None else f", confidence {confidence:g}"
        print(f"  [find]  {who(p)}: {status}{' ' + addr if addr else ''}{conf_txt} "
              f"({self.remaining} credits left today)")
        if status != "valid":
            return
        if confidence is not None and confidence < MF_MIN_CONFIDENCE:
            print(f"  [skip] {who(p)}: confidence below MAILSFINDER_MIN_CONFIDENCE")
        elif addr in self.known:
            print(f"  [skip] {who(p)}: {addr} already belongs to someone else in your list")
        else:
            self._accept(p, addr)
    # ----- strategy: guess patterns + Email-Verify (up to 4 calls per person) -----
    def _verify(self, p: dict, domain: str) -> None:
        candidates = [a for a in guess_emails(p["first_name"], p["last_name"], domain,
                                              self.votes[domain]) if a not in self.known]
        if not candidates:
            print(f"  [skip] {who(p)}: can't build an email address from this name")
            return
        cached = next((a for a in candidates if self.checks.get(a, {}).get("status") == "valid"), None)
        if cached:
            self._accept(p, cached, "(from earlier check)")
            return
        if self._unverifiable(domain):
            print(f"  [skip] {who(p)}: {domain} only returns 'Unknown' (likely catch-all)")
            return
        todo = [a for a in candidates if a not in self.checks]
        if not todo:
            print(f"  [skip] {who(p)}: all patterns already checked, none valid")
            return
        for addr in todo:
            result = self._call(mailsfinder_verify, addr)
            if result is None:
                return
            status, raw = result
            self._record(addr, "verify", addr, status, None, p, raw)
            print(f"  [check] {addr:<42} {status:<8} ({self.remaining} credits left today)")
            if status == "valid":
                self._accept(p, addr)
                return
            if status == "unknown":
                return                             # don't burn more credits on this person now
    # ----- main loop -----
    def run(self) -> None:
        try:
            for p in self.people:
                if p["email_address"]:
                    continue
                domain = self.domains.get(norm_company(p["company_name"]))
                if not domain:
                    print(f"  [skip] {who(p)}: company not found in companies table")
                    continue
                if self.strategy == "find":
                    self._find(p, domain)
                else:
                    self._verify(p, domain)
        except StopVerification as exc:
            print(f"\nStopping lookups: {exc}.")
        still_blank = sum(1 for p in self.people if not p["email_address"])
        print(f"\nLookup summary: {self.calls} API call(s), {self.found} address(es) found and "
              f"saved, {still_blank} people still without an email (they will be skipped).")
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
        w.writerow([now_iso(), email, status, detail])
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
                    help="where to read/write data (default: DATA_SOURCE in .env, else csv)")
    ap.add_argument("--csv", default=PEOPLE_CSV_PATH, metavar="PATH", help="people CSV path")
    ap.add_argument("--companies-csv", default=COMPANIES_CSV_PATH, metavar="PATH")
    ap.add_argument("--db", default=DB_PATH, metavar="PATH",
                    help="SQLite path (default: <COMPANY_NAME>.db)")
    ap.add_argument("--strategy", choices=["find", "verify"], default=MF_DEFAULT_STRATEGY,
                    help="find = Email-Find, 1 call/person (default); "
                         "verify = guess 4 patterns + Email-Verify")
    ap.add_argument("--no-guess", action="store_true",
                    help="no Mailsfinder lookups; only email known addresses")
    ap.add_argument("--verify-only", action="store_true",
                    help="look up blank emails and update people_data, but send no emails")
    ap.add_argument("--verify-test", metavar="EMAIL",
                    help="call Email-Verify for ONE address and print the raw response (1 credit)")
    ap.add_argument("--find-test", nargs=3, metavar=("FIRST", "LAST", "DOMAIN"),
                    help="call Email-Find for ONE person and print the raw response (1 credit)")
    ap.add_argument("--send", action="store_true", help="actually send to recipients")
    ap.add_argument("--test-to", metavar="EMAIL",
                    help="send the first few emails to this address instead")
    ap.add_argument("--limit", type=int, default=None, help="max emails this run")
    ap.add_argument("--yes", action="store_true", help="skip the 'Type yes' confirmation")
    args = ap.parse_args()
    if args.source == "sqlite":
        print(f"Using SQLite: {args.db}")
        store = SqliteStore(args.db)
    else:
        print(f"Using CSV: {args.csv}")
        store = CsvStore(args.csv, args.companies_csv, CHECKS_CSV_PATH)
    try:
        run(args, store)
    finally:
        store.close()
def run_api_test(args, store) -> None:
    if not MF_API_KEY:
        sys.exit("Set MAILSFINDER_API_KEY in .env first.")
    try:
        if args.verify_test:
            email = args.verify_test.strip().lower()
            status, raw = mailsfinder_verify(email)
            store.record_check(email, "verify", email, status, None, {}, raw)
            print(f"\nRaw response:\n{raw}\n\nInterpreted as: {status.upper()}")
        else:
            first, last, domain = (s.strip() for s in args.find_test)
            domain = domain.lower().lstrip("@")
            status, email, confidence, raw = mailsfinder_find(first, last, domain)
            key = f"find:{first.casefold()}|{last.casefold()}|{domain}"
            store.record_check(key, "find", email, status, confidence,
                               {"first_name": first, "last_name": last}, raw)
            print(f"\nRaw response:\n{raw}\n\nInterpreted as: {status.upper()}"
                  f"{'  email=' + email if email else ''}"
                  f"{'' if confidence is None else f'  confidence={confidence:g}'}")
    except QuotaExhausted as exc:
        sys.exit(f"Mailsfinder says the quota/rate limit is exhausted: {exc}")
    except VerifierError as exc:
        sys.exit(f"API call failed: {exc}")
def run(args, store) -> None:
    if args.verify_test or args.find_test:
        run_api_test(args, store)
        return
    # ---------- load + look up blank emails ----------
    people = validate_people(store.load_people())
    blank = [p for p in people if not p["email_address"]]
    if blank:
        remaining = max(0, MF_DAILY_LIMIT - checks_today(store.load_checks()))
        print(f"\n{len(blank)} people have no email address. "
              f"Mailsfinder credits left today: {remaining}/{MF_DAILY_LIMIT} "
              f"(strategy: {args.strategy}).")
        wants_lookup = (args.send or args.verify_only) and not args.no_guess and not args.test_to
        if wants_lookup and not MF_API_KEY:
            print("  [warn] MAILSFINDER_API_KEY not set; skipping lookups.")
        elif wants_lookup and remaining <= 0:
            print("  No credits left today; these people will be skipped this run.")
        elif wants_lookup:
            domains = build_domain_map(store.load_companies())
            Resolver(people, domains, store, args.strategy).run()
        elif not args.no_guess and not args.test_to:
            print("  (Dry run: no API calls made. Use --verify-only or --send to look them up.)")
    if args.verify_only:
        return
    # ---------- build the send queue: 1 email per person, real addresses only ----------
    already = load_already_sent()
    seen: set[str] = set()
    targets = []
    for p in people:
        email = p["email_address"].lower()
        if not email:
            continue
        if email in seen:
            print(f"  [skip] duplicate {email}")
            continue
        seen.add(email)
        if args.test_to or email not in already:
            targets.append(p)
    targets = targets[: (args.limit or 3)] if args.test_to else targets[: args.limit]
    n_verified = sum(1 for p in targets if p.get("_verified"))
    n_blank = sum(1 for p in people if not p["email_address"])
    print(f"\n{len(targets)} email(s) queued ({n_verified} newly found). "
          f"{n_blank} people skipped (no email). {len(already)} already sent previously.")
    if not targets:
        return
    # ---------- DRY RUN ----------
    if not args.send and not args.test_to:
        first = targets[0]
        sample = build_message(first, first["email_address"])
        print("\n--- DRY RUN: preview of first email (plain-text version) ---")
        print(f"To:      {sample['To']}\nSubject: {sample['Subject']}\n")
        print(build_plain(first["first_name"]))
        print("\n--- all queued emails ---")
        for p in targets:
            print(f"  {p['email_address']:<42} {who(p)}")
        print("\nNothing was sent. Use --test-to you@domain.com, then --send.")
        return
    password = EMAIL_PASSWORD or getpass.getpass(f"Password for {SENDER_EMAIL}: ")
    if args.send and not args.test_to and not args.yes:
        if input(f"Send {len(targets)} real emails? Type 'yes': ").strip().lower() != "yes":
            return
    for i, p in enumerate(targets, 1):
        to_addr = args.test_to or p["email_address"]
        msg = build_message(p, to_addr)
        try:
            smtp_send(msg, password)
            print(f"[{i}/{len(targets)}] sent -> {to_addr}  {who(p)}")
            if not args.test_to:
                log_result(p["email_address"].lower(), "sent",
                           "verified" if p.get("_verified") else "")
                if SAVE_TO_SENT_FOLDER:
                    save_to_sent(msg, password)
        except smtplib.SMTPAuthenticationError:
            sys.exit("Login failed. Check SENDER_EMAIL / EMAIL_PASSWORD in .env.")
        except smtplib.SMTPRecipientsRefused as exc:
            print(f"[{i}/{len(targets)}] REFUSED {to_addr}: {exc}")
            log_result(p["email_address"].lower(), "refused", str(exc))
        except Exception as exc:
            print(f"[{i}/{len(targets)}] ERROR {to_addr}: {exc}")
            log_result(p["email_address"].lower(), "error", str(exc))
            if "limit" in str(exc).lower() or "rate" in str(exc).lower():
                sys.exit("Looks like a sending limit. Stop and re-run later; progress is saved.")
        if i < len(targets):
            time.sleep(random.uniform(MIN_DELAY_SEC, MAX_DELAY_SEC) if not args.test_to else 2)
    print("Done.")
if __name__ == "__main__":
    main()