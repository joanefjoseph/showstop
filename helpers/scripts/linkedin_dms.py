#!/usr/bin/env python3
"""
linkedin_dms.py: LinkedIn DM templates for each employee row.
LinkedIn messages are plain text only, so formatting is "baked in" as Unicode:
  *word*         -> Mathematical Sans-Serif Bold letters/digits (𝗯𝗼𝗹𝗱)
  - line         -> "• line"  (bulleted list)
Placeholders are the same ones used by send_emails.py:
  {first_name} {job_title}      -> from the employees row
  {email_name}                  -> client_metadata.email_name (falls back to company_name)
  {sender} {company} {company_url} -> SENDER_NAME / COMPANY_NAME / COMPANY_URL from .env
"""
import re
from send_emails import SENDER_NAME, COMPANY_NAME, COMPANY_URL
import sqlite3
from datetime import datetime, timezone
from config import DB_PATH, SENT_DMS_TABLE_NAME
from db import connect as _connect, init_db
DM_SEQUENCE = ["dm1", "dm2", "dm3"]
DMS = {
    "dm1": {
        "label": "DM 1 · Intro",
        "text": """*Native ticketing checkout + membership API integration*
Hi {first_name},

I noticed the upcoming North American tour dates for several artists on {email_name}'s roster.

Right now, when fans click through your official site or tour portal to buy tickets, they get redirected off-site to external vendors. That handoff causes three major bottlenecks:
 *1.* Data drop-off: Loss of first-party session telemetry and pixel tracking at checkout.
 *2.* Presale leaks: Static codes get shared or scraped, inviting bot attacks during ticket drops.
 *3.* Lost upsells: Zero ability to cross-sell merch, album pre-orders, or membership packages on the confirmation screen.

At {company}, we're building a middleware engine that embeds the primary ticketing vendor's checkout API directly inside your native web stack. Fans purchase directly on the artist's site while our system handles the real-time seat lock, payment processing, and native membership authentication in the background.

Are you open to a 10-minute chat next week to see if this fits upcoming tour rollouts?

Best,
{sender}
Founder, {company}
{company_url}""",
    },
    "dm2": {
        "label": "DM 2 · Connected",
        "text": """Hi {first_name}, thanks for connecting!

As I mentioned, I've been building a company called *{company}* ({company_url}) that is centered around building a ticketing software for platforms like {email_name} to deepen the direct connection between fans and music artists by re-writing the entire concert ticket sales process.

Instead of losing fans to third-party portals where drop-off is high and post-sale revenue is lost, {company} enables:

- *Direct Ecosystem Monetization*: Keep fans on your artist's website through the entire ticket purchase. Immediately post-checkout (when buying intent peaks), route fans into tour apparel, album pre-orders, and VIP upgrades (averaging 15–30% upsell conversion).
- *Seamless Fan Club & Presale Gating*: Give loyal fans the friction-free, secure buying experience they expect by authenticating official memberships natively at checkout—eliminating redirects, bots, and leaked promo codes.
- *Unified Multi-Vendor Experience*: Deliver a single, consistent checkout flow across the entire tour, regardless of whether individual venues use Ticketmaster, AXS, or another primary ticket vendor.

I see that you've been heading {job_title} at {email_name} for some time now, and I'm sure that a lot of your work has revolved around building a better experience for the fans of the artists at {email_name} while also increasing margins.

If what we are building at {company} is of any interest, would you be open to having a meeting in the next week to discuss more of what a collaboration with your team and {company} might look like?""",
    },
    "dm3": {
        "label": "DM 3 · Follow-up",
        "text": """Hi there!
I'm following up on my previous message about {company} - the software product that will *revolutionize* the way that you manage ticket sales for all of your tours.

Our company is called {company} because we are changing the game for how music labels and tour organizers envision what it takes to put on a show, and we aim to be a one-stop shop for all of your concert ticketing needs.

We are finalizing our architecture and are looking for 2-3 design partners in the music space to test the beta. Given {email_name}'s focus on fan experience and potential upcoming North American tour schedules, I'd love to show you the prototype and get your feedback.

Are you open to a brief 10-minute intro call in the next week?""",
    },
}
# ------------------------- Unicode pseudo-font -------------------------
# Mathematical Sans-Serif Bold: A-Z at U+1D5D4, a-z at U+1D5EE, 0-9 at U+1D7EC.
# Anything else (punctuation, accents, spaces) is left untouched.
_BOLD_UPPER = 0x1D5D4
_BOLD_LOWER = 0x1D5EE
_BOLD_DIGIT = 0x1D7EC
BULLET = "\u2022"   # •
def _bold_char(ch: str) -> str:
    if "A" <= ch <= "Z":
        return chr(_BOLD_UPPER + ord(ch) - ord("A"))
    if "a" <= ch <= "z":
        return chr(_BOLD_LOWER + ord(ch) - ord("a"))
    if "0" <= ch <= "9":
        return chr(_BOLD_DIGIT + ord(ch) - ord("0"))
    return ch
def to_bold(text: str) -> str:
    """'Hello 123' -> '𝗛𝗲𝗹𝗹𝗼 𝟭𝟮𝟯'."""
    return "".join(_bold_char(c) for c in text)
def format_plain_unicode(text: str) -> str:
    """Apply *bold* and '- bullet' formatting as LinkedIn-safe Unicode."""
    text = re.sub(r"\*(.+?)\*", lambda m: to_bold(m.group(1)), text)
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        m = re.match(r"^-\s*(.*)$", stripped)
        if m:                                   # "- item" -> "• item"
            out.append(f"{BULLET} {m.group(1)}")
        elif re.match(r"^\d+\.\s", stripped):   # " 1. item" -> "1. item"
            out.append(stripped)
        else:
            out.append(line.rstrip())
    return "\n".join(out)
# ------------------------- Rendering -------------------------
def _fields_for(row: dict) -> dict:
    first = (row.get("first_name") or "").strip() or "there"
    company_name = (row.get("company_name") or "").strip()
    email_name = (row.get("email_name") or "").strip() or company_name
    job_title = (row.get("job_title") or "").strip() or "your team"
    return {
        "first_name": first,
        "email_name": email_name,
        "job_title": job_title,
        "sender": SENDER_NAME,
        "company": COMPANY_NAME,
        "company_url": COMPANY_URL,
    }
def render_dm(key: str, row: dict) -> str:
    """Fully rendered, clipboard-ready text for DM `key` and one employee row."""
    text = DMS[key]["text"]
    # Fill placeholders first so that e.g. *{company}* ends up bold.
    for name, value in _fields_for(row).items():
        text = text.replace(f"{{{name}}}", str(value))
    return format_plain_unicode(text)
def render_all(row: dict) -> dict:
    return {key: render_dm(key, row) for key in DM_SEQUENCE}
# ------------------------- SEND LOG (sent_dms) -------------------------
# Mirrors sent_emails, but keyed on linkedin_profile_url instead of email.
def init_sent_dms_table() -> None:
    """Table is defined in schema.sql."""
    init_db()
def load_sent_dms(urls: list[str] | None = None,
                  status: str = "sent") -> dict[str, set[str]]:
    """
    {linkedin_profile_url: {"dm1", "dm3", ...}} for profiles that have a log
    entry with `status`. Pass `urls` to limit the lookup to the rows on screen.
    """
    init_sent_dms_table()
    conn = _connect()
    try:
        sql = (f"SELECT linkedin_profile_url AS url, campaign "
               f"FROM {SENT_DMS_TABLE_NAME} WHERE status = ?")
        params: list = [status]
        if urls is not None:
            if not urls:
                return {}
            sql += f" AND linkedin_profile_url IN ({','.join('?' * len(urls))})"
            params += list(urls)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["url"], set()).add(r["campaign"])
    return out
def log_dm_result(linkedin_profile_url: str, campaign: str,
                  status: str = "sent", detail: str = "") -> None:
    """Record that DM `campaign` was sent to (copied for) a LinkedIn profile."""
    if campaign not in DMS:
        raise ValueError(f"Unknown DM {campaign!r}; must be one of {DM_SEQUENCE}")
    init_sent_dms_table()
    conn = _connect()
    try:
        conn.execute(
            f"INSERT INTO {SENT_DMS_TABLE_NAME} "
            f"(timestamp, linkedin_profile_url, status, detail, campaign) "
            f"VALUES (?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             linkedin_profile_url, status, detail, campaign),
        )
        conn.commit()
    finally:
        conn.close()
if __name__ == "__main__":   # quick visual check
    demo = {"first_name": "Jane", "job_title": "Partnerships", "company_name": "Acme", "email_name": "Acme Music"}
    for key in DM_SEQUENCE:
        print(f"===== {DMS[key]['label']} =====\n{render_dm(key, demo)}\n")