"""
Fill in missing employee emails using the Hunter.io Email Finder API.
Every (domain, first_name, last_name) lookup is recorded in `hunter_lookups`,
so the API is never called twice for the same person, even when Hunter
returned no email.
"""
import os
import sqlite3
import time
import requests
from dotenv import load_dotenv
from config import BASE, DB_PATH, TABLE_NAME, METADATA_TABLE_NAME
load_dotenv(BASE / ".env")
HUNTER_URL = "https://api.hunter.io/v2/email-finder"
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "").strip()
HUNTER_TABLE = "hunter_lookups"
DELAY_SEC = 1.0  # pause between API calls to stay under rate limits
class HunterStop(Exception):
    """Raised when Hunter rejects the key or credits, so the run must stop."""
def require_key() -> str:
    if not HUNTER_API_KEY:
        raise RuntimeError("HUNTER_API_KEY is not set in your .env file.")
    return HUNTER_API_KEY
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
def init_lookup_table() -> None:
    """Create the lookup cache table if it doesn't exist yet (idempotent)."""
    conn = _connect()
    try:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {HUNTER_TABLE} (
                domain      TEXT NOT NULL,
                first_name  TEXT NOT NULL,
                last_name   TEXT NOT NULL,
                email       TEXT,            -- NULL when Hunter found nothing
                score       INTEGER,
                queried_at  TEXT NOT NULL,
                PRIMARY KEY (domain, first_name, last_name)
            )
        """)
        conn.commit()
    finally:
        conn.close()
def _query_hunter(first: str, last: str, domain: str) -> tuple[bool, str | None, int | None]:
    """
    Returns (ok, email, score).
      ok=False  -> the call failed; don't cache, so it can be retried later.
      email=None -> Hunter answered but found no reliable address (cache it).
    Raises HunterStop for auth/credit/rate-limit responses.
    """
    try:
        resp = requests.get(
            HUNTER_URL,
            params={"domain": domain, "first_name": first,
                    "last_name": last, "api_key": HUNTER_API_KEY},
            timeout=30,
        )
    except requests.RequestException:
        return False, None, None
    if resp.status_code in (401, 403, 429):
        raise HunterStop(f"Hunter returned HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code != 200:
        return False, None, None
    data = resp.json().get("data") or {}
    return True, data.get("email") or None, data.get("score")
def fill_missing_emails(log=print) -> dict:
    """Look up emails for every employee whose email is empty. Returns summary stats."""
    require_key()
    init_lookup_table()
    stats = {"missing": 0, "no_domain": 0, "no_name": 0,
             "cached": 0, "queried": 0, "found": 0, "failed": 0}
    conn = _connect()
    try:
        # Cache of previous lookups: (domain, first, last) -> email or None
        cache = {
            (r["domain"], r["first_name"], r["last_name"]): r["email"]
            for r in conn.execute(
                f"SELECT domain, first_name, last_name, email FROM {HUNTER_TABLE}")
        }
        targets = conn.execute(f"""
            SELECT e.company_name, e.first_name, e.last_name,
                   e.linkedin_profile_url, m.domain
            FROM {TABLE_NAME} e
            LEFT JOIN {METADATA_TABLE_NAME} m ON m.company_name = e.company_name
            WHERE e.email IS NULL OR TRIM(e.email) = ''
        """).fetchall()
        stats["missing"] = len(targets)
        for t in targets:
            first_raw = (t["first_name"] or "").strip()
            last_raw = (t["last_name"] or "").strip()
            domain = (t["domain"] or "").strip().lower()
            if not domain:
                stats["no_domain"] += 1
                log(f"[skip] {t['company_name']}: no domain in {METADATA_TABLE_NAME}")
                continue
            if not first_raw or not last_raw:
                stats["no_name"] += 1
                continue
            key = (domain, first_raw.lower(), last_raw.lower())
            if key in cache:
                email = cache[key]
                stats["cached"] += 1
            else:
                ok, email, score = _query_hunter(first_raw, last_raw, domain)
                if not ok:
                    stats["failed"] += 1
                    log(f"[error] {first_raw} {last_raw} @ {domain}: request failed")
                    continue
                stats["queried"] += 1
                conn.execute(
                    f"INSERT OR REPLACE INTO {HUNTER_TABLE} "
                    f"(domain, first_name, last_name, email, score, queried_at) "
                    f"VALUES (?, ?, ?, ?, ?, datetime('now'))",
                    (domain, key[1], key[2], email, score),
                )
                cache[key] = email
                time.sleep(DELAY_SEC)
            if email:
                conn.execute(
                    f"UPDATE {TABLE_NAME} SET email = ? "
                    f"WHERE company_name = ? AND linkedin_profile_url = ? "
                    f"AND (email IS NULL OR TRIM(email) = '')",
                    (email, t["company_name"], t["linkedin_profile_url"]),
                )
                stats["found"] += 1
                log(f"[found] {first_raw} {last_raw} -> {email}")
            else:
                log(f"[none]  {first_raw} {last_raw} @ {domain}")
            conn.commit()  # commit per row so progress survives a crash
    finally:
        conn.close()
    return stats