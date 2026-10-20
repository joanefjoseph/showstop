from __future__ import annotations

import hashlib
import logging
import threading
import webbrowser
from dataclasses import asdict
from email.utils import parseaddr

from . import db, email_client, linkedin, llm
from .config import settings

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


# ------------------------------------------------------------------ LinkedIn
def _scan_linkedin(context: dict) -> tuple[int, int, list[str]]:
    problems: list[str] = []
    created = 0
    # Browser phase first, then close the browser before the slow LLM calls.
    threads, you_last = linkedin.fetch_candidate_threads(
        reply_limit=settings.max_threads_per_run,
        followup_limit=settings.max_followup_checks_per_run,
        already_drafted=db.preview_seen,
        already_checked=db.was_checked,
        mark_checked=db.mark_checked,
    )
    for name in set(you_last):
        db.supersede_pending_for_participant(name, "You sent a newer message in this conversation")

    for t in threads:
        h = t.state_hash()
        if db.is_handled(t.thread_id, h):
            continue
        msgs = [asdict(m) for m in t.messages]
        contact = db.find_outreach_contact(name=t.participant)
        meta = {"person_notes": llm.format_person_notes(contact)} if contact else {}
        try:
            r = llm.draft_reply(context, t.participant, msgs, kind=t.kind, meta=meta)
        except llm.ModelUnavailableError:
            raise  # same problem for every message: abort this channel, show it in the run log
        except Exception as e:  # this thread is retried next run
            log.exception("Drafting failed for %s", t.participant)
            problems.append(f"LinkedIn / {t.participant}: {e}")
            continue
        db.insert_draft(
            thread_id=t.thread_id, thread_url=t.url, participant=t.participant,
            preview_key=t.preview_key, state_hash=h, messages=msgs, kind=t.kind, channel="linkedin",
            meta=meta,
            needs_reply=r.needs_reply, category=r.category, summary=r.summary,
            rationale=r.rationale, draft=r.reply,
        )
        created += 1
    return len(threads), created, problems


# ------------------------------------------------------------------ Email
def _scan_email(context: dict) -> tuple[int, int, list[str]]:
    problems: list[str] = []
    created = 0

    # Retire pending drafts for emails you've since answered from your normal mail client.
    pending = [d for d in db.list_drafts(["pending"], limit=500) if d["channel"] == "email"]
    try:
        answered = email_client.answered_elsewhere([d["meta"].get("message_id", "") for d in pending])
    except Exception as e:
        log.warning("Could not check for emails answered elsewhere: %s", e)
        answered = set()
    for d in pending:
        if d["meta"].get("message_id") in answered:
            db.update_draft(d["id"], status="superseded", error="You already replied to this email")

    items = email_client.fetch_emails_needing_reply(
        limit=settings.email_max_per_run,
        already_seen=db.email_seen,
        mark_seen=db.mark_email_seen,
    )
    for it in items:  # oldest first
        contact = db.find_outreach_contact(email=parseaddr(it.sender_display)[1])
        if contact:
            it.meta["person_notes"] = llm.format_person_notes(contact)
        try:
            r = llm.draft_reply(context, it.sender_display, it.messages, channel="email", meta=it.meta)
        except llm.ModelUnavailableError:
            raise  # same problem for every message: abort this channel, show it in the run log
        except Exception as e:  # not marked as seen -> retried next run
            log.exception("Drafting failed for email from %s", it.sender_display)
            problems.append(f"Email / {it.sender_display}: {e}")
            continue
        key = hashlib.sha256(it.message_id.encode()).hexdigest()[:20]
        db.insert_draft(
            thread_id=it.thread_id, thread_url=settings.email_webmail_url,
            participant=it.sender_display, preview_key=f"email:{key}", state_hash=key,
            messages=it.messages, kind="reply", channel="email", meta=it.meta,
            needs_reply=r.needs_reply, category=r.category, summary=r.summary,
            rationale=r.rationale, draft=r.reply,
        )
        db.mark_email_seen(it.message_id, "drafted" if r.needs_reply else "flagged no-reply")
        created += 1
    return len(items), created, problems


# ------------------------------------------------------------------ entry point
def run_scan(trigger: str = "manual") -> dict:
    """Read LinkedIn + email -> draft -> store for review. Never sends anything."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running"}
    run_id = db.start_run(trigger)
    found = created = 0
    problems: list[str] = []
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
        for name, _, scan in enabled:
            try:
                f, c, p = scan(context)
                found, created = found + f, created + c
                problems += p
                log.info("%s scan: %d candidates, %d new drafts", name, f, c)
            except Exception as e:  # one channel failing must not block the other
                log.exception("%s scan failed", name)
                problems.append(f"{name} scan failed: {e}")
                failed += 1
        status = "error" if enabled and failed == len(enabled) else ("partial" if problems else "ok")
        db.finish_run(run_id, status, found, created, "; ".join(problems) or None)
        if created and trigger == "scheduled" and settings.open_browser_on_new_drafts:
            webbrowser.open(settings.ui_url)
        return {"status": status, "candidates": found, "drafts_created": created, "problems": problems}
    except Exception as e:
        log.exception("Scan failed")
        db.finish_run(run_id, "error", found, created, str(e))
        return {"status": "error", "error": str(e)}
    finally:
        _run_lock.release()