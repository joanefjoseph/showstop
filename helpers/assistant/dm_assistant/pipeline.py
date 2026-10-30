from __future__ import annotations

import logging
import threading
import webbrowser

from . import db, email_client, handoff, linkedin
from .config import settings

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


# ------------------------------------------------------------------ queue
def _drain_queue(context: dict, state: dict, problems: list[str]) -> int:
    """Retry items waiting for a draft (e.g. the quota has reset since)."""
    drafted = 0
    for q in db.list_queue():
        if state.get("llm_down"):
            break
        if handoff.draft_or_queue(context, q["payload"], state, problems) == "drafted":
            drafted += 1
    return drafted


# ------------------------------------------------------------------ LinkedIn
def _scan_linkedin(context: dict, state: dict, problems: list[str]) -> tuple[int, int, int]:
    threads, you_last = linkedin.fetch_candidate_threads(
        reply_limit=settings.max_threads_per_run,
        followup_limit=settings.max_followup_checks_per_run,
        already_drafted=db.preview_seen,
        already_checked=db.was_checked,
        mark_checked=db.mark_checked,
    )
    for name in set(you_last):
        db.supersede_pending_for_participant(name, "You sent a newer message in this conversation")

    drafted = queued = 0
    for t in threads:
        if db.is_handled(t.thread_id, t.state_hash()):
            continue
        outcome = handoff.draft_or_queue(context, handoff.payload_from_thread(t), state, problems)
        drafted += outcome == "drafted"
        queued += outcome == "queued"
    return len(threads), drafted, queued


# ------------------------------------------------------------------ Email
def _scan_email(context: dict, state: dict, problems: list[str]) -> tuple[int, int, int]:
    # Retire drafts / queued items for emails you've since answered from your normal mail client.
    pending = [d for d in db.list_drafts(["pending"], limit=500) if d["channel"] == "email"]
    queued_emails = [q for q in db.list_queue() if q["payload"].get("email_message_id")]
    ids = [d["meta"].get("message_id", "") for d in pending] + \
          [q["payload"]["email_message_id"] for q in queued_emails]
    try:
        answered = email_client.answered_elsewhere([i for i in ids if i])
    except Exception as e:
        log.warning("Could not check for emails answered elsewhere: %s", e)
        answered = set()
    for d in pending:
        if d["meta"].get("message_id") in answered:
            db.update_draft(d["id"], status="superseded", error="You already replied to this email")
    for q in queued_emails:
        mid = q["payload"]["email_message_id"]
        if mid in answered:
            db.set_queue_status(q["id"], "discarded", "You already replied to this email")
            db.mark_email_seen(mid, "already replied")

    items = email_client.fetch_emails_needing_reply(
        limit=settings.email_max_per_run,
        already_seen=db.email_seen,
        mark_seen=db.mark_email_seen,
    )
    drafted = queued = 0
    for it in items:  # oldest first
        outcome = handoff.draft_or_queue(context, handoff.payload_from_email(it), state, problems)
        drafted += outcome == "drafted"
        queued += outcome == "queued"
    return len(items), drafted, queued


# ------------------------------------------------------------------ entry point
def run_scan(trigger: str = "manual") -> dict:
    """Read LinkedIn + email -> draft (or queue for the Gemini web app). Never sends anything."""
    if not _run_lock.acquire(blocking=False):
        log.warning("Scan (%s) skipped - a previous scan is still running", trigger)
        return {"status": "already_running"}
    run_id = db.start_run(trigger)
    found = drafted = queued = 0
    problems: list[str] = []
    state: dict = {}  # shared across the run: once the API is down, stop calling it
    channels = [
        ("LinkedIn", settings.linkedin_enabled, _scan_linkedin),
        ("Email", settings.email_enabled, _scan_email),
    ]
    enabled = [c for c in channels if c[1]]
    failed = 0
    try:
        context = db.get_context()
        if "company" not in context:
            log.warning("No company context set yet - drafts will be generic. Fill in the Context page.")
        drafted += _drain_queue(context, state, problems)
        for name, _, scan in enabled:
            try:
                f, d, q = scan(context, state, problems)
                found, drafted, queued = found + f, drafted + d, queued + q
                log.info("%s scan: %d candidates, %d drafted, %d queued", name, f, d, q)
            except Exception as e:  # one channel failing must not block the other
                log.exception("%s scan failed", name)
                problems.append(f"{name} scan failed: {e}")
                failed += 1

        notes = list(problems)
        waiting = len(db.list_queue())
        if waiting:
            notes.append(f"{waiting} item(s) waiting for a draft"
                         + (f" ({state['llm_down']})" if state.get("llm_down") else "")
                         + " - use 'Export to Gemini web'")
        status = "error" if enabled and failed == len(enabled) else ("partial" if notes else "ok")
        db.finish_run(run_id, status, found, drafted, "; ".join(notes) or None)
        if (drafted or queued) and trigger == "scheduled" and settings.open_browser_on_new_drafts:
            webbrowser.open(settings.ui_url)
        return {"status": status, "candidates": found, "drafted": drafted, "queued": queued,
                "waiting_total": waiting, "problems": problems}
    except Exception as e:
        log.exception("Scan failed")
        db.finish_run(run_id, "error", found, drafted, str(e))
        return {"status": "error", "error": str(e)}
    finally:
        _run_lock.release()