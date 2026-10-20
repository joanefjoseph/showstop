from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from . import db, linkedin, llm, pipeline, scheduler
from .config import settings

log = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.urandom(24)


@app.before_request
def _block_cross_site_posts():
    # The UI can post to LinkedIn on your behalf; refuse POSTs from other websites.
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)


# ------------------------------------------------------------------ pages
@app.get("/")
def index():
    ctx = db.get_context()
    missing = [label for key, (label, _) in db.CONTEXT_FIELDS.items()
            if key not in db.OPTIONAL_CONTEXT and key not in ctx]
    return render_template(
        "index.html",
        drafts=db.list_drafts(["pending"]),
        recent=db.list_drafts(["sent", "skipped", "superseded"], limit=25),
        runs=db.recent_runs(5),
        running=pipeline.is_running(),
        next_runs=scheduler.next_run_times(),
        direct_send=settings.enable_direct_send,
        missing=missing,
    )


@app.post("/run")
def run_now():
    if pipeline.is_running():
        flash("A scan is already running.")
    else:
        threading.Thread(target=pipeline.run_scan, kwargs={"trigger": "manual"}, daemon=True).start()
        flash("Scan started - drafts will appear here when it finishes.")
    return redirect(url_for("index"))


@app.route("/context", methods=["GET", "POST"])
def context_page():
    if request.method == "POST":
        key = request.form.get("key", "")
        if key not in db.CONTEXT_FIELDS:
            abort(400)
        db.set_context(key, request.form.get("value", ""))
        flash(f"Saved: {db.CONTEXT_FIELDS[key][0]}")
        return redirect(url_for("context_page"))
    return render_template(
        "context.html",
        fields=db.CONTEXT_FIELDS,
        ctx=db.get_context(),
        history={k: db.context_history(k) for k in db.CONTEXT_FIELDS},
    )


# ------------------------------------------------------------------ JSON API (used by the UI)
def _draft_or_404(draft_id: int) -> dict:
    d = db.get_draft(draft_id)
    if not d:
        abort(404)
    return d


@app.get("/api/status")
def api_status():
    return jsonify(running=pipeline.is_running())


@app.post("/api/drafts/<int:draft_id>/save")
def api_save(draft_id: int):
    _draft_or_404(draft_id)
    db.update_draft(draft_id, draft=(request.get_json() or {}).get("text", ""))
    return jsonify(ok=True)


@app.post("/api/drafts/<int:draft_id>/regenerate")
def api_regenerate(draft_id: int):
    d = _draft_or_404(draft_id)
    data = request.get_json() or {}
    try:
        r = llm.draft_reply(db.get_context(), d["participant"], d["messages"],
                            kind=d["kind"],
                            instruction=(data.get("instruction") or "").strip() or None,
                            previous_draft=data.get("text") or d["draft"])
    except Exception as e:
        log.exception("Regenerate failed")
        return jsonify(error=f"Gemini error: {e}"), 502
    db.update_draft(draft_id, draft=r.reply, summary=r.summary, rationale=r.rationale,
                    category=r.category, needs_reply=int(r.needs_reply), error=None)
    return jsonify(draft=r.reply, rationale=r.rationale, summary=r.summary)


@app.post("/api/drafts/<int:draft_id>/send")
def api_send(draft_id: int):
    if not settings.enable_direct_send:
        return jsonify(error="Direct sending is disabled (ENABLE_DIRECT_SEND=false). Use Copy & Open."), 403
    d = _draft_or_404(draft_id)
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Message is empty."), 400
    db.update_draft(draft_id, draft=text)
    if not db.claim_for_sending(draft_id):
        return jsonify(error="This draft is already being sent or is no longer pending."), 409

    try:
        outcome = linkedin.send_message(d["thread_url"], text, d["state_hash"],
                                        kind=d["kind"], force=bool(data.get("force")))
    except linkedin.NotLoggedInError as e:
        db.update_draft(draft_id, status="pending", error=str(e))
        return jsonify(error=str(e)), 401
    except Exception as e:
        log.exception("Send failed")
        db.update_draft(draft_id, status="pending",
                        error=f"Send failed: {e}. Check LinkedIn before retrying.")
        return jsonify(error=f"Send failed: {e}. Check LinkedIn before retrying."), 500

    if outcome.conflict:
        db.update_draft(draft_id, status="pending")
        return jsonify(conflict=outcome.conflict), 409
    if not outcome.ok:
        db.update_draft(draft_id, status="pending", error=outcome.error)
        return jsonify(error=outcome.error), 500

    db.update_draft(draft_id, status="sent", sent_at=db.now(),
                    error="Clicked Send but couldn't confirm delivery - check LinkedIn." if outcome.unconfirmed else None)
    return jsonify(ok=True, unconfirmed=outcome.unconfirmed)


@app.post("/api/drafts/<int:draft_id>/mark-sent")
def api_mark_sent(draft_id: int):
    _draft_or_404(draft_id)
    text = (request.get_json() or {}).get("text")
    fields = {"status": "sent", "sent_at": db.now(), "error": None}
    if text:
        fields["draft"] = text
    db.update_draft(draft_id, **fields)
    return jsonify(ok=True)


@app.post("/api/drafts/<int:draft_id>/skip")
def api_skip(draft_id: int):
    _draft_or_404(draft_id)
    db.update_draft(draft_id, status="skipped")
    return jsonify(ok=True)


@app.post("/api/drafts/<int:draft_id>/restore")
def api_restore(draft_id: int):
    _draft_or_404(draft_id)
    db.update_draft(draft_id, status="pending", error=None)
    return jsonify(ok=True)