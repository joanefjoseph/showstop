from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from urllib.parse import quote_plus, urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from . import db, email_client, linkedin, llm, pipeline, scheduler
from .config import settings

log = logging.getLogger(__name__)
app = Flask(__name__)
app.secret_key = os.urandom(24)


@app.before_request
def _block_cross_site_posts():
    # The UI can send messages on your behalf; refuse POSTs coming from other websites.
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
    pending = db.list_drafts(["pending"], limit=300)
    return render_template(
        "index.html",
        drafts=[d for d in pending if d["needs_reply"]],
        flagged=[d for d in pending if not d["needs_reply"]],
        recent=db.list_drafts(["sent", "skipped", "superseded", "no_reply"], limit=30),
        runs=db.recent_runs(5),
        running=pipeline.is_running(),
        next_runs=scheduler.next_run_times(),
        linkedin_send=settings.enable_direct_send,
        email_send=settings.email_enabled,
        outreach_labels=llm.OUTREACH_KINDS,
        note_limit=settings.connect_note_limit,
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


# ------------------------------------------------------------------ new outreach
def _render_outreach(form, selected, status: int = 200):
    return render_template("outreach.html", form=form, selected=selected,
                           note_limit=settings.connect_note_limit,
                           email_enabled=settings.email_enabled), status


@app.route("/outreach", methods=["GET", "POST"])
def outreach_page():
    if request.method == "GET":
        return _render_outreach({}, [])

    f = request.form
    kinds = [k for k in f.getlist("kinds") if k in llm.OUTREACH_KINDS]
    person = {k: f.get(k, "").strip() for k in ("name", "email", "linkedin_url", "role", "about", "reason")}
    if person["linkedin_url"] and not person["linkedin_url"].startswith(("http://", "https://")):
        person["linkedin_url"] = "https://" + person["linkedin_url"]

    problems = []
    if not person["name"]:
        problems.append("Name is required.")
    if not person["reason"]:
        problems.append("Say why you want to start this conversation.")
    if not kinds:
        problems.append("Pick at least one message type.")
    if llm.OUTREACH_EMAIL in kinds and "@" not in person["email"]:
        problems.append("An email address is required to draft an email.")
    if person["linkedin_url"] and "linkedin.com/" not in person["linkedin_url"]:
        problems.append("That doesn't look like a LinkedIn profile URL.")
    if problems:
        for p in problems:
            flash(p)
        return _render_outreach(f, kinds, 400)

    context = db.get_context()
    created = 0
    for kind in kinds:
        label = llm.OUTREACH_KINDS[kind]
        try:
            result, subject = llm.draft_outreach(context, person, kind,
                                                 instruction=f.get("instruction", "").strip() or None)
        except Exception as e:
            log.exception("Outreach drafting failed")
            flash(f"Could not draft the {label}: {e}")
            continue
        _insert_outreach(person, kind, result, subject)
        created += 1

    if not created:
        return _render_outreach(f, kinds, 502)
    flash(f"Drafted {created} message(s) for {person['name']} - review below.")
    return redirect(url_for("index"))


def _insert_outreach(person: dict, kind: str, result: llm.DraftResult, subject: str) -> None:
    uid = uuid.uuid4().hex[:12]
    is_email = kind == llm.OUTREACH_EMAIL
    meta = dict(person)
    if is_email:
        url = settings.email_webmail_url
        meta.update(subject=subject, to=f"{person['name']} <{person['email']}>", cc="")
    else:
        # No profile URL? Open a LinkedIn people search for them instead.
        url = person["linkedin_url"] or (
            "https://www.linkedin.com/search/results/people/?keywords="
            + quote_plus(f"{person['name']} {person['role']}".strip()))
    db.insert_draft(
        thread_id=f"outreach:{uid}", thread_url=url, participant=person["name"],
        preview_key=f"outreach:{uid}", state_hash="", messages=[], kind=kind,
        channel="email" if is_email else "linkedin", meta=meta,
        needs_reply=True, category=result.category, summary=result.summary,
        rationale=result.rationale, draft=result.reply,
    )


# ------------------------------------------------------------------ JSON API (used by the UI)
def _draft_or_404(draft_id: int) -> dict:
    d = db.get_draft(draft_id)
    if not d:
        abort(404)
    return d


def _save_fields(d: dict, data: dict) -> None:
    """Persist edited To / Cc / Subject for email drafts."""
    if d["channel"] != "email" or not ({"to", "cc", "subject"} & data.keys()):
        return
    meta = dict(d["meta"])
    for key in ("to", "cc", "subject"):
        if key in data:
            meta[key] = (data.get(key) or "").strip()
    db.update_draft(d["id"], meta_json=json.dumps(meta, ensure_ascii=False))
    d["meta"] = meta


@app.get("/api/status")
def api_status():
    return jsonify(running=pipeline.is_running())


@app.post("/api/drafts/<int:draft_id>/save")
def api_save(draft_id: int):
    d = _draft_or_404(draft_id)
    data = request.get_json() or {}
    db.update_draft(draft_id, draft=data.get("text", ""))
    _save_fields(d, data)
    return jsonify(ok=True)


@app.post("/api/drafts/<int:draft_id>/regenerate")
def api_regenerate(draft_id: int):
    d = _draft_or_404(draft_id)
    data = request.get_json() or {}
    instruction = (data.get("instruction") or "").strip() or None
    previous = (data.get("text") or d["draft"]) or None
    subject = None
    try:
        if d["kind"] in llm.OUTREACH_KINDS:
            r, subject = llm.draft_outreach(
                db.get_context(), d["meta"], d["kind"], instruction=instruction, previous_draft=previous,
                previous_subject=(data.get("subject") or d["meta"].get("subject") or None),
            )
        else:
            r = llm.draft_reply(
                db.get_context(), d["participant"], d["messages"],
                kind=d["kind"], channel=d["channel"], meta=d["meta"],
                instruction=instruction, previous_draft=previous,
                force_reply=bool(data.get("force_reply")),
            )
    except Exception as e:
        log.exception("Regenerate failed")
        return jsonify(error=f"Gemini error: {e}"), 502
    db.update_draft(draft_id, draft=r.reply, summary=r.summary, rationale=r.rationale,
                    category=r.category, needs_reply=int(r.needs_reply), error=None)
    if subject:
        _save_fields(d, {"subject": subject})
    return jsonify(draft=r.reply, rationale=r.rationale, summary=r.summary,
                   needs_reply=r.needs_reply, subject=subject)


@app.post("/api/drafts/<int:draft_id>/send")
def api_send(draft_id: int):
    d = _draft_or_404(draft_id)
    data = request.get_json() or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(error="Message is empty."), 400
    if d["channel"] == "email":
        return _send_email(d, text, data)
    return _send_linkedin(d, text, data)


def _send_linkedin(d: dict, text: str, data: dict):
    draft_id = d["id"]
    if d["kind"] in llm.OUTREACH_KINDS:
        return jsonify(error="New LinkedIn outreach is sent by you: use Copy & Open, then paste it "
                             "into the Connect note or Message box."), 400
    if not settings.enable_direct_send:
        return jsonify(error="Direct sending is disabled (ENABLE_DIRECT_SEND=false). Use Copy & Open."), 403
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
        log.exception("LinkedIn send failed")
        msg = f"Send failed: {e}. Check LinkedIn before retrying."
        db.update_draft(draft_id, status="pending", error=msg)
        return jsonify(error=msg), 500

    if outcome.conflict:
        db.update_draft(draft_id, status="pending")
        return jsonify(conflict=outcome.conflict), 409
    if not outcome.ok:
        db.update_draft(draft_id, status="pending", error=outcome.error)
        return jsonify(error=outcome.error), 500
    warning = "Clicked Send but couldn't confirm delivery - check LinkedIn." if outcome.unconfirmed else None
    db.update_draft(draft_id, status="sent", sent_at=db.now(), error=warning)
    return jsonify(ok=True, warning=warning)


def _send_email(d: dict, text: str, data: dict):
    draft_id = d["id"]
    if not settings.email_enabled:
        return jsonify(error="Email is not configured (EMAIL_ADDRESS / EMAIL_PASSWORD)."), 403
    _save_fields(d, data)
    to, cc = d["meta"].get("to", ""), d["meta"].get("cc", "")
    is_new = d["kind"] == llm.OUTREACH_EMAIL
    if not to:
        return jsonify(error="Add at least one recipient in 'To'."), 400
    if is_new and not d["meta"].get("subject", "").strip():
        return jsonify(error="Add a subject line."), 400
    db.update_draft(draft_id, draft=text)
    if not db.claim_for_sending(draft_id):
        return jsonify(error="This draft is already being sent or is no longer pending."), 409
    try:
        if is_new:
            res = email_client.send_new(to, d["meta"]["subject"], text, cc)
        else:
            res = email_client.send_reply(d["meta"], text, to, cc)
    except Exception as e:
        log.exception("Email send failed")
        msg = f"Email was NOT sent: {e}"
        db.update_draft(draft_id, status="pending", error=msg)
        return jsonify(error=msg), 500
    if not res.ok:
        db.update_draft(draft_id, status="pending", error=res.error)
        return jsonify(error=res.error), 400
    db.update_draft(draft_id, status="sent", sent_at=db.now(), error=res.warning)
    return jsonify(ok=True, saved_to_sent=res.saved_to_sent, warning=res.warning)


@app.post("/api/drafts/<int:draft_id>/no-reply")
def api_no_reply(draft_id: int):
    """The user approves the assistant's 'no response needed' flag."""
    _draft_or_404(draft_id)
    db.update_draft(draft_id, status="no_reply", error=None)
    return jsonify(ok=True)


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