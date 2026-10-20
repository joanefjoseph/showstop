from __future__ import annotations

import logging
import threading
import webbrowser
from dataclasses import asdict

from . import db, linkedin, llm
from .config import settings

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def run_scan(trigger: str = "manual") -> dict:
    """Read inbox -> draft replies + connection follow-ups -> store for review. Never sends anything."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running"}
    run_id = db.start_run(trigger)
    found = created = followups = 0
    problems: list[str] = []
    try:
        context = db.get_context()
        if "company" not in context:
            log.warning("No company context set yet - drafts will be generic. Fill in the Context page.")

        # 1) Browser phase: collect threads, then close the browser before slow LLM calls.
        threads, you_last = linkedin.fetch_candidate_threads(
            reply_limit=settings.max_threads_per_run,
            followup_limit=settings.max_followup_checks_per_run,
            already_drafted=db.preview_seen,
            already_checked=db.was_checked,
            mark_checked=db.mark_checked,
        )
        found = len(threads)
        for name in set(you_last):
            db.supersede_pending_for_participant(name, "You sent a newer message in this conversation")

        # 2) LLM phase
        for t in threads:
            h = t.state_hash()
            if db.is_handled(t.thread_id, h):
                continue
            msgs = [asdict(m) for m in t.messages]
            try:
                r = llm.draft_reply(context, t.participant, msgs, kind=t.kind)
            except Exception as e:  # keep going; this thread is retried next run
                log.exception("Drafting failed for %s", t.participant)
                problems.append(f"{t.participant}: {e}")
                continue
            db.insert_draft(
                thread_id=t.thread_id, thread_url=t.url, participant=t.participant,
                preview_key=t.preview_key, state_hash=h, messages=msgs, kind=t.kind,
                needs_reply=r.needs_reply, category=r.category, summary=r.summary,
                rationale=r.rationale, draft=r.reply,
            )
            created += 1
            followups += t.kind == linkedin.KIND_FOLLOWUP

        status = "ok" if not problems else "partial"
        db.finish_run(run_id, status, found, created, "; ".join(problems) or None)
        log.info("Scan done: %d candidate threads, %d new drafts (%d connection follow-ups)",
                 found, created, followups)
        if created and trigger == "scheduled" and settings.open_browser_on_new_drafts:
            webbrowser.open(settings.ui_url)
        return {"status": status, "threads": found, "drafts_created": created,
                "connection_followups": followups, "problems": problems}
    except Exception as e:
        log.exception("Scan failed")
        db.finish_run(run_id, "error", found, created, str(e))
        return {"status": "error", "error": str(e)}
    finally:
        _run_lock.release()