from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .config import settings

# key -> (label, help text)
CONTEXT_FIELDS: dict[str, tuple[str, str]] = {
    "company": (
        "Company, product & your role",
        "Set once. What the company does, who it serves, the product, your title/responsibilities, "
        "differentiators, and any facts the assistant may state (links, customers you can name, etc.).",
    ),
    "phase": (
        "Current product phase",
        "Update regularly. E.g. 'Private beta with 6 design partners; self-serve launches in June; "
        "pricing not public yet.'",
    ),
    "focus": (
        "Current focus",
        "Update regularly. What you're working on now, e.g. 'Closing 3 pilot customers in logistics; "
        "hiring a founding engineer.'",
    ),
    "goals": (
        "Goals for DM conversations",
        "Who's in your DMs and what a good outcome is, e.g. 'Mostly B2B ops leaders I cold-contacted. "
        "Goal: book a 20-min discovery call. Secondary: get referrals to the right person.'",
    ),
    "followup": (
        "Connection follow-up guidance (optional)",
        "How to write the message after someone accepts your connection request: the one-line pitch "
        "you like, the meeting length/format (e.g. '20-min Zoom'), anything to always/never mention.",
    ),
    "email": (
        "Email guidance (optional)",
        "Anything specific to your work inbox: who typically emails you, how formal to be, "
        "what you never want to reply to, what always deserves a reply.",
    ),    
    "style": (
        "Writing style (optional)",
        "Tone, length, words to avoid, how you sign off, example phrases you use.",
    ),
}
OPTIONAL_CONTEXT = {"style", "followup", "email"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    thread_url TEXT NOT NULL,
    participant TEXT NOT NULL,
    preview_key TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reply',       -- reply | connection_followup
    conversation_json TEXT NOT NULL,
    needs_reply INTEGER NOT NULL DEFAULT 1,
    category TEXT,
    summary TEXT,
    rationale TEXT,
    draft TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sending | sent | skipped | superseded
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_thread ON drafts(thread_id, state_hash);
CREATE INDEX IF NOT EXISTS idx_drafts_preview ON drafts(preview_key);
CREATE TABLE IF NOT EXISTS checked_previews (
    preview_key TEXT PRIMARY KEY,   -- "You:" threads we opened and found NOT to be connection notes
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    threads_found INTEGER DEFAULT 0,
    drafts_created INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS email_seen (
    message_id TEXT PRIMARY KEY,   -- every inbox email the assistant has already dealt with
    outcome TEXT,
    seen_at TEXT NOT NULL
);
"""

_UPDATABLE = {"draft", "status", "error", "sent_at", "summary", "rationale", "category",
              "needs_reply", "meta_json"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        _ensure_column(c, "drafts", "kind", "TEXT NOT NULL DEFAULT 'reply'")  # migrate older DBs
        _ensure_column(c, "drafts", "channel", "TEXT NOT NULL DEFAULT 'linkedin'")
        _ensure_column(c, "drafts", "meta_json", "TEXT NOT NULL DEFAULT '{}'")
        # A crash mid-send leaves a draft in 'sending'; surface it instead of hiding it.
        c.execute(
            "UPDATE drafts SET status='pending', updated_at=?, "
            "error='Interrupted while sending - check LinkedIn before sending again.' "
            "WHERE status='sending'",
            (now(),),
        )


# ---------------------------------------------------------------- context
def set_context(key: str, value: str) -> None:
    if key not in CONTEXT_FIELDS:
        raise ValueError(f"Unknown context key: {key}")
    with connect() as c:
        c.execute(
            "INSERT INTO context_entries (key, value, created_at) VALUES (?, ?, ?)",
            (key, value.strip(), now()),
        )


def get_context() -> dict[str, dict[str, str]]:
    with connect() as c:
        rows = c.execute(
            """SELECT key, value, created_at FROM context_entries e
               WHERE id = (SELECT MAX(id) FROM context_entries WHERE key = e.key)"""
        ).fetchall()
    return {r["key"]: {"value": r["value"], "updated_at": r["created_at"]} for r in rows if r["value"]}


def context_history(key: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT value, created_at FROM context_entries WHERE key=? ORDER BY id DESC LIMIT ?",
            (key, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- drafts
def _to_draft(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["messages"] = json.loads(d.pop("conversation_json"))
    d["meta"] = json.loads(d.pop("meta_json", None) or "{}")
    d["needs_reply"] = bool(d["needs_reply"])
    return d


def is_handled(thread_id: str, state_hash: str) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM drafts WHERE thread_id=? AND state_hash=? LIMIT 1", (thread_id, state_hash)
        ).fetchone() is not None


def preview_seen(preview_key: str) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM drafts WHERE preview_key=? LIMIT 1", (preview_key,)
        ).fetchone() is not None


def was_checked(preview_key: str) -> bool:
    with connect() as c:
        return c.execute(
            "SELECT 1 FROM checked_previews WHERE preview_key=?", (preview_key,)
        ).fetchone() is not None


def mark_checked(preview_key: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO checked_previews (preview_key, checked_at) VALUES (?, ?)",
                  (preview_key, now()))


def insert_draft(*, thread_id: str, thread_url: str, participant: str, preview_key: str,
                 state_hash: str, messages: list[dict], needs_reply: bool, category: str,
                 summary: str, rationale: str, draft: str, kind: str = "reply",
                 channel: str = "linkedin", meta: dict | None = None) -> int:
    ts = now()
    with connect() as c:
        c.execute(
            "UPDATE drafts SET status='superseded', error='Replaced by a newer draft', updated_at=? "
            "WHERE thread_id=? AND channel=? AND status='pending'",
            (ts, thread_id, channel),
        )
        cur = c.execute(
            """INSERT INTO drafts (thread_id, thread_url, participant, preview_key, state_hash, kind,
                   channel, meta_json, conversation_json, needs_reply, category, summary, rationale,
                   draft, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (thread_id, thread_url, participant, preview_key, state_hash, kind, channel,
             json.dumps(meta or {}, ensure_ascii=False), json.dumps(messages, ensure_ascii=False),
             int(needs_reply), category, summary, rationale, draft, ts, ts),
        )
        return int(cur.lastrowid)


def supersede_pending_for_participant(participant: str, reason: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE drafts SET status='superseded', error=?, updated_at=? "
            "WHERE participant=? AND channel='linkedin' AND status='pending'",
            (reason, now(), participant),
        )


def list_drafts(statuses: list[str], limit: int = 100) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in statuses)
    order = "needs_reply DESC, id DESC" if statuses == ["pending"] else "updated_at DESC"
    with connect() as c:
        rows = c.execute(
            f"SELECT * FROM drafts WHERE status IN ({marks}) ORDER BY {order} LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [_to_draft(r) for r in rows]


def get_draft(draft_id: int) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    return _to_draft(row) if row else None


def update_draft(draft_id: int, **fields: Any) -> None:
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"Cannot update {bad}")
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE drafts SET {sets} WHERE id=?", (*fields.values(), draft_id))


def claim_for_sending(draft_id: int) -> bool:
    """Atomically move pending -> sending so a double click can't double-send."""
    with connect() as c:
        cur = c.execute(
            "UPDATE drafts SET status='sending', updated_at=? WHERE id=? AND status='pending'",
            (now(), draft_id),
        )
        return cur.rowcount == 1

# ---------------------------------------------------------------- email bookkeeping
def email_seen(message_id: str) -> bool:
    with connect() as c:
        return c.execute("SELECT 1 FROM email_seen WHERE message_id=?", (message_id,)).fetchone() is not None


def mark_email_seen(message_id: str, outcome: str) -> None:
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO email_seen (message_id, outcome, seen_at) VALUES (?, ?, ?)",
                  (message_id, outcome, now()))


# ---------------------------------------------------------------- runs
def start_run(trigger: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO runs (trigger, started_at, status) VALUES (?, ?, 'running')",
                        (trigger, now()))
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, found: int, created: int, error: str | None = None) -> None:
    with connect() as c:
        c.execute(
            "UPDATE runs SET finished_at=?, status=?, threads_found=?, drafts_created=?, error=? WHERE id=?",
            (now(), status, found, created, error, run_id),
        )


def recent_runs(limit: int = 5) -> list[dict[str, Any]]:
    with connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]