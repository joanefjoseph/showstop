"""
Hand-off between the Gemini API and the Gemini web app (gemini.google.com).

Every message that needs a draft becomes a *payload*. If the API can draft it, the draft is saved
right away. If not (daily quota used up, model unavailable, no API key...), the payload is queued.
Queued payloads can be exported as a JSON file for the Gemini web app, and Gemini's answer can be
imported back to create normal drafts (so Send, Sent-folder saving etc. keep working).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from email.utils import parseaddr
from urllib.parse import quote_plus

from google.genai import errors as genai_errors

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)

TASK_LABELS = {
    llm.KIND_REPLY: "Reply to this conversation",
    llm.KIND_FOLLOWUP: "Follow-up after they accepted my LinkedIn connection request",
    llm.OUTREACH_CONNECT: "New LinkedIn connection-request note",
    llm.OUTREACH_DM: "New LinkedIn DM (first contact)",
    llm.OUTREACH_EMAIL: "New cold email (first contact)",
}


# ------------------------------------------------------------------ payloads
def payload_from_thread(t) -> dict:
    """A LinkedIn thread (linkedin.Thread) that needs a draft."""
    contact = db.find_outreach_contact(name=t.participant)
    meta = {"person_notes": llm.format_person_notes(contact)} if contact else {}
    h = t.state_hash()
    return {
        "key": f"li:{t.thread_id}:{h}", "channel": "linkedin", "kind": t.kind,
        "participant": t.participant, "messages": [asdict(m) for m in t.messages], "meta": meta,
        "thread_id": t.thread_id, "thread_url": t.url, "preview_key": t.preview_key, "state_hash": h,
    }


def payload_from_email(it) -> dict:
    """An inbox email (email_client.EmailItem) that needs a draft."""
    meta = dict(it.meta)
    contact = db.find_outreach_contact(email=parseaddr(it.sender_display)[1])
    if contact:
        meta["person_notes"] = llm.format_person_notes(contact)
    import hashlib
    h = hashlib.sha256(it.message_id.encode()).hexdigest()[:20]
    return {
        "key": f"em:{it.message_id}", "channel": "email", "kind": llm.KIND_REPLY,
        "participant": it.sender_display, "messages": it.messages, "meta": meta,
        "thread_id": it.thread_id, "thread_url": settings.email_webmail_url,
        "preview_key": f"email:{h}", "state_hash": h, "email_message_id": it.message_id,
    }


def payload_from_outreach(person: dict, kind: str) -> dict:
    """A first-contact message requested on the New outreach page."""
    uid = uuid.uuid4().hex[:12]
    is_email = kind == llm.OUTREACH_EMAIL
    meta = dict(person)
    if is_email:
        url = settings.email_webmail_url
        meta.update(to=f"{person['name']} <{person['email']}>", cc="", subject="")
    else:
        url = person.get("linkedin_url") or (
            "https://www.linkedin.com/search/results/people/?keywords="
            + quote_plus(f"{person['name']} {person.get('role', '')}".strip()))
    return {
        "key": f"out:{uid}", "channel": "email" if is_email else "linkedin", "kind": kind,
        "participant": person["name"], "messages": [], "meta": meta,
        "thread_id": f"outreach:{uid}", "thread_url": url, "preview_key": f"outreach:{uid}", "state_hash": "",
    }


# ------------------------------------------------------------------ drafting
def draft_with_api(context: dict, p: dict) -> tuple[llm.DraftResult, str]:
    if p["kind"] in llm.OUTREACH_KINDS:
        return llm.draft_outreach(context, p["meta"], p["kind"])
    r = llm.draft_reply(context, p["participant"], p["messages"],
                        kind=p["kind"], channel=p["channel"], meta=p["meta"])
    return r, ""


def save_draft(p: dict, result: llm.DraftResult, subject: str = "") -> int:
    meta = dict(p["meta"])
    if subject:
        meta["subject"] = subject
    draft_id = db.insert_draft(
        thread_id=p["thread_id"], thread_url=p["thread_url"], participant=p["participant"],
        preview_key=p["preview_key"], state_hash=p["state_hash"], messages=p["messages"],
        kind=p["kind"], channel=p["channel"], meta=meta,
        needs_reply=result.needs_reply, category=result.category, summary=result.summary,
        rationale=result.rationale, draft=result.reply,
    )
    if p.get("email_message_id"):
        db.mark_email_seen(p["email_message_id"], "drafted" if result.needs_reply else "flagged no-reply")
    db.resolve_queue(p["key"], p["thread_id"])
    return draft_id


def is_llm_unavailable(e: Exception) -> bool:
    """Errors where retrying other messages with the API right now is pointless."""
    if isinstance(e, llm.ModelUnavailableError):
        return True
    if isinstance(e, genai_errors.APIError):
        return e.code in (401, 403, 429) or "api key" in str(e).lower()
    return isinstance(e, RuntimeError) and "GEMINI_API_KEY" in str(e)


def _short(e: Exception) -> str:
    text = getattr(e, "message", None) or str(e)
    return text.strip().splitlines()[0][:200] if text.strip() else e.__class__.__name__


def _server_errors_piling_up(e: Exception, state: dict) -> bool:
    if isinstance(e, genai_errors.APIError) and (e.code or 0) >= 500:
        state["server_errors"] = state.get("server_errors", 0) + 1
        return state["server_errors"] >= 2
    return False


def draft_or_queue(context: dict, p: dict, state: dict, problems: list[str]) -> str:
    """Draft with the API, or queue for the Gemini web app. Returns 'drafted' or 'queued'."""
    tried = state.setdefault("tried", set())
    if p["key"] in tried:
        return "queued"      # already attempted earlier in this run (queue drain) - don't call Gemini twice
    tried.add(p["key"])
    if state.get("llm_down"):
        db.enqueue(p["key"], p["thread_id"], p, state["llm_down"])
        return "queued"
    try:
        result, subject = draft_with_api(context, p)
        state["server_errors"] = 0
    except Exception as e:
        if is_llm_unavailable(e) or _server_errors_piling_up(e, state):
            state["llm_down"] = f"Gemini API unavailable: {_short(e)}"
            log.warning("%s - queuing the rest for manual drafting", state["llm_down"])
            db.enqueue(p["key"], p["thread_id"], p, state["llm_down"])
        else:
            if isinstance(e, genai_errors.APIError):
                log.warning("Drafting failed for %s: %s %s", p["participant"], e.code, _short(e))
            else:
                log.exception("Drafting failed for %s", p["participant"])
            problems.append(f"{p['participant']}: {_short(e)}")
            db.enqueue(p["key"], p["thread_id"], p, f"Drafting failed: {_short(e)}")
        return "queued"
    save_draft(p, result, subject)
    return "drafted"


# ------------------------------------------------------------------ export for gemini.google.com
HOW_TO_PROCESS = [
    "Act as the drafting assistant described in `system_prompt` - treat it as your system instructions.",
    "`context` is the owner's company, product, current phase, current focus and goals in structured form "
    "(system_prompt already contains the same information).",
    "For EACH entry in `items`, carry out its `instructions` - this is the exact request the app would have "
    "sent to the Gemini API. `data` repeats the conversation or contact details in structured form.",
    "Where an item's instructions say to start `reply` with a 'Subject:' line, put the subject in the "
    "`subject` field instead.",
    "Answer with ONE ```json code block that follows `response_format`: one result per item, using the "
    "item's `id`. The owner pastes that block back into the app.",
]

RESPONSE_FORMAT = {
    "instructions": "Return a single ```json code block containing an object with a `results` array - "
                    "exactly one object per item, with the same ids.",
    "fields": {
        "id": "The item's id, e.g. 'q12'. Required.",
        "needs_reply": "true/false. false ONLY when no response is warranted (rule 8 in system_prompt). "
                       "Always true for new outreach and connection follow-ups.",
        "category": "prospect, customer, partner, investor, recruiter, vendor_pitch, networking, personal, "
                    "automated_report, auto_reply, notification, newsletter, spam or other.",
        "summary": "One sentence: who this is / what they want, or the angle chosen for outreach.",
        "subject": "ONLY for 'New cold email' items: the subject line. Otherwise an empty string.",
        "reply": "The message text, ready to send. Empty string if needs_reply is false and nothing "
                 "sensible can be said.",
        "rationale": "1-2 sentences for the owner: the approach taken, or why no reply is needed.",
    },
    "example": {"results": [{
        "id": "q1", "needs_reply": True, "category": "prospect",
        "summary": "Ops lead asking about pricing for 3 warehouses.", "subject": "",
        "reply": "Hi Jane, thanks for the quick reply! ...", "rationale": "Answers the pricing question directly.",
    }]},
}

CLIPBOARD_TEMPLATE = """I've attached {filename}, exported from my LinkedIn + email drafting assistant (its Gemini API quota ran out). The file contains:
- system_prompt: your role and the rules for this task - follow it as your instructions
- context: my company, product, current phase, current focus and conversation goals
- items: {count} message(s) to draft ({what}); each has the exact task instructions plus the conversation or contact details
- response_format: the JSON structure I need back

Please complete every item following system_prompt and the item's instructions, then answer with ONE ```json code block in the response_format (one result per item id) so I can import the drafts back into my app. These are drafts for me to review - nothing is sent automatically."""


def prompt_for(context: dict, p: dict, instruction: str | None = None,
               previous_draft: str | None = None, force_reply: bool = False) -> str:
    """The exact user prompt the API call would have used for this payload."""
    kind, meta = p["kind"], p["meta"]
    if kind in llm.OUTREACH_KINDS:
        return llm.build_outreach_prompt(context, meta, kind, instruction, previous_draft,
                                         meta.get("subject") or None)
    notes = llm._notes_block(meta.get("person_notes"))
    if p["channel"] == "email":
        return llm.build_email_prompt(p["participant"], p["messages"], meta, instruction,
                                      previous_draft, force_reply) + notes
    if kind == llm.KIND_FOLLOWUP:
        return llm.build_followup_prompt(p["participant"], p["messages"], instruction, previous_draft) + notes
    return llm.build_reply_prompt(p["participant"], p["messages"], instruction,
                                  previous_draft, force_reply) + notes


_EMAIL_FIELDS = ("subject", "to", "cc", "attachments", "signals", "quoted")
_PERSON_FIELDS = ("name", "role", "email", "linkedin_url", "about", "reason")


def _item_data(p: dict) -> dict:
    meta = p["meta"]
    data: dict = {"participant": p["participant"]}
    if p["kind"] in llm.OUTREACH_KINDS:
        data["recipient"] = {k: meta[k] for k in _PERSON_FIELDS if meta.get(k)}
    else:
        data["conversation"] = p["messages"]
        if p["channel"] == "email":
            data["email"] = {k: meta[k] for k in _EMAIL_FIELDS if meta.get(k)}
    if meta.get("person_notes"):
        data["my_notes_about_this_person"] = meta["person_notes"]
    return data


def build_export(items: list[dict], export_type: str) -> tuple[str, dict, str]:
    """Returns (filename, export JSON object, clipboard text)."""
    context = db.get_context()
    now = datetime.now()
    entries = []
    for q in items:
        p = q["payload"]
        entries.append({
            "id": f"q{q['id']}",
            "task": TASK_LABELS.get(p["kind"], p["kind"]),
            "channel": p["channel"],
            "instructions": prompt_for(context, p, **q.get("revision", {})),
            "data": _item_data(p),
        })
    export = {
        "about": f"Export from {llm._me()}'s LinkedIn + email drafting assistant, for drafting in the "
                 f"Gemini web app while the API quota is used up.",
        "export_type": export_type,
        "generated_at": now.isoformat(timespec="seconds"),
        "how_to_process": HOW_TO_PROCESS,
        "system_prompt": llm.build_system_prompt(context),
        "context": {
            key: {"label": label, "value": context[key]["value"], "last_updated": context[key]["updated_at"]}
            for key, (label, _) in db.CONTEXT_FIELDS.items() if key in context
        },
        "items": entries,
        "response_format": RESPONSE_FORMAT,
    }
    counts = Counter(e["task"] for e in entries)
    what = "; ".join(f"{n} x {task}" for task, n in counts.items())
    filename = f"dm-assistant-{export_type.replace('_', '-')}-{now:%Y%m%d-%H%M}.json"
    clipboard = CLIPBOARD_TEMPLATE.format(filename=filename, count=len(entries), what=what)
    return filename, export, clipboard


# ------------------------------------------------------------------ import Gemini's answer
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_results(text: str) -> list[dict]:
    chunks = _FENCE.findall(text) or [text]
    results: list[dict] = []
    for chunk in chunks:
        chunk = chunk.strip()
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            starts = [i for i in (chunk.find("{"), chunk.find("[")) if i >= 0]
            end = max(chunk.rfind("}"), chunk.rfind("]"))
            if not starts or end <= min(starts):
                continue
            try:
                data = json.loads(chunk[min(starts):end + 1])
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            data = data.get("results", [data] if "id" in data else [])
        if isinstance(data, list):
            results += [r for r in data if isinstance(r, dict)]
    if not results:
        raise ValueError("Couldn't find any results in the pasted text. Paste Gemini's ```json code block "
                         "(an object with a `results` array).")
    return results


def _as_bool(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip().lower() not in ("false", "no", "0")
    return default if v is None or v == "" else bool(v)


def _result_from_dict(p: dict, r: dict) -> tuple[llm.DraftResult, str]:
    kind = p["kind"]
    reply = str(r.get("reply") or "").strip()
    subject = ""
    if kind == llm.OUTREACH_EMAIL:
        inline_subject, reply = llm.split_subject(reply)
        subject = str(r.get("subject") or "").strip() or inline_subject or "Quick introduction"
    forced = kind in llm.OUTREACH_KINDS or kind == llm.KIND_FOLLOWUP
    needs_reply = True if forced else _as_bool(r.get("needs_reply"))
    if needs_reply and not reply:
        raise ValueError("the reply is empty")
    sig = settings.email_signature.strip()
    if p["channel"] == "email" and sig and reply and not reply.rstrip().endswith(sig):
        reply = reply.rstrip() + "\n\n" + sig
    result = llm.DraftResult(
        needs_reply=needs_reply,
        category=str(r.get("category") or "other"),
        summary=str(r.get("summary") or ""),
        reply=reply,
        rationale=str(r.get("rationale") or "Drafted in the Gemini web app."),
    )
    return result, subject


def import_results(text: str) -> dict:
    created, skipped, errors = 0, 0, []
    for r in parse_results(text):
        rid = str(r.get("id", "")).strip()
        m = re.fullmatch(r"q?(\d+)", rid)
        if not m:
            errors.append(f"a result has no valid id ({rid or 'missing'})")
            continue
        q = db.get_queue_item(int(m.group(1)))
        if not q:
            errors.append(f"{rid}: not found in the queue")
            continue
        if q["status"] != "queued":
            skipped += 1  # already drafted (e.g. by the API) or discarded
            continue
        try:
            result, subject = _result_from_dict(q["payload"], r)
        except ValueError as e:
            errors.append(f"{rid}: {e}")
            continue
        save_draft(q["payload"], result, subject)
        created += 1
    return {"created": created, "skipped": skipped, "errors": errors}