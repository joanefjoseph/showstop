"""
Work email over IMAP/SMTP (defaults are for Namecheap Private Email).

Reading never marks messages as read (BODY.PEEK). Sending happens ONLY from the web UI
when the user clicks "Send email"; a copy is then saved to the Sent folder.
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Callable, Iterator

from .config import settings

log = logging.getLogger(__name__)

MAX_REFS = 5            # how many earlier thread messages to look up
QUOTED_MAX_CHARS = 4000
ORIG_TEXT_MAX_CHARS = 15000
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


class EmailError(RuntimeError):
    pass


@dataclass
class EmailItem:
    message_id: str
    thread_id: str
    sender_display: str
    messages: list[dict]     # [{sender, text, time, is_me}] oldest -> newest
    meta: dict               # subject, recipients, headers needed to reply, signals...


@dataclass
class SendResult:
    ok: bool
    saved_to_sent: bool = False
    warning: str | None = None
    error: str | None = None


# ------------------------------------------------------------------ connections
@contextmanager
def _imap() -> Iterator[imaplib.IMAP4_SSL]:
    if not settings.email_enabled:
        raise EmailError("EMAIL_ADDRESS / EMAIL_PASSWORD are not set.")
    conn = imaplib.IMAP4_SSL(settings.email_imap_host, settings.email_imap_port,
                             ssl_context=ssl.create_default_context(), timeout=60)
    try:
        conn.login(settings.email_address, settings.email_password)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


@contextmanager
def _smtp() -> Iterator[smtplib.SMTP]:
    ctx = ssl.create_default_context()
    host, port = settings.email_smtp_host, settings.email_smtp_port
    server = (smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) if port == 465
              else smtplib.SMTP(host, port, timeout=30))
    with server:
        if port != 465:
            server.starttls(context=ctx)
        server.login(settings.email_address, settings.email_password)
        yield server


def _q(s: str) -> str:
    """IMAP quoted string (folder names with spaces, Message-IDs in SEARCH)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _imap_date(d: date) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _select(conn: imaplib.IMAP4_SSL, folder: str, readonly: bool = True) -> None:
    typ, _ = conn.select(_q(folder), readonly=readonly)
    if typ != "OK":
        raise EmailError(f"Cannot open mail folder '{folder}'.")


def _search(conn: imaplib.IMAP4_SSL, *criteria: str) -> list[bytes]:
    typ, data = conn.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _sent_folder(conn: imaplib.IMAP4_SSL) -> str:
    if settings.email_sent_folder:
        return settings.email_sent_folder
    fallback = None
    typ, data = conn.list()
    for raw in data or []:
        if not isinstance(raw, bytes):
            continue
        line = raw.decode("utf-8", "replace")
        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if not m:
            continue
        name = m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        if "\\Sent" in m.group("flags"):
            return name
        if name.lower() in ("sent", "sent items", "sent messages", "inbox.sent"):
            fallback = fallback or name
    return fallback or "Sent"


# ------------------------------------------------------------------ fetching & parsing
def _fetch_message_ids(conn: imaplib.IMAP4_SSL, uids: list[bytes]) -> dict[bytes, str]:
    out: dict[bytes, str] = {}
    for i in range(0, len(uids), 200):
        chunk = b",".join(uids[i:i + 200]).decode()
        typ, data = conn.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        items = data or []
        for j, part in enumerate(items):
            if not isinstance(part, tuple):
                continue
            m = re.search(rb"UID (\d+)", part[0])
            if not m and j + 1 < len(items) and isinstance(items[j + 1], bytes):
                m = re.search(rb"UID (\d+)", items[j + 1])
            if not m:
                continue
            mid = (email.message_from_bytes(part[1]).get("Message-ID") or "").strip()
            out[m.group(1)] = mid or f"<uid-{m.group(1).decode()}@no-message-id>"
    return out


def _fetch_raw(conn: imaplib.IMAP4_SSL, uid: bytes) -> bytes | None:
    typ, data = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
    for part in data or []:
        if isinstance(part, tuple):
            return part[1]
    return None


def _fetch_by_message_id(conn: imaplib.IMAP4_SSL, message_id: str) -> bytes | None:
    uids = _search(conn, "HEADER", "Message-ID", _q(message_id))
    return _fetch_raw(conn, uids[-1]) if uids else None


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t\xa0]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def _body_text(msg: EmailMessage) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        if part.get_content_type() == "text/html":
            content = _html_to_text(content)
        return str(content).replace("\r\n", "\n").strip()
    except Exception as e:  # odd charsets, broken MIME
        log.debug("Could not decode body: %s", e)
        return ""


def split_quoted(text: str) -> tuple[str, str]:
    """Split an email body into (what the sender wrote, quoted earlier messages)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_quote = (
            s.startswith(">")
            or (s.startswith("On ") and (s.endswith("wrote:") or nxt.endswith("wrote:")))
            or re.match(r"^-{2,}\s*(Original|Forwarded) Message\s*-{2,}$", s, re.I)
            or (s.lower().startswith("from:") and nxt.lower().startswith(("sent:", "date:", "to:")))
        )
        if is_quote:
            head = "\n".join(lines[:i]).rstrip()
            if head:
                return head, "\n".join(lines[i:]).strip()
            return text.strip(), ""  # starts with a quote (inline reply) -> keep everything
    return text.strip(), ""


_AUTO_SENDER = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|notification|bounce|dmarc|alert|newsletter)",
    re.I,
)


def _fmt_addr(name: str, addr: str) -> str:
    if not name:
        return addr
    return f'"{name}" <{addr}>' if re.search(r'[,;<>"]', name) is None and "," in name else f"{name} <{addr}>"


def _parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=policy.default)

    def h(name: str) -> str:
        try:
            return str(msg.get(name, "") or "").replace("\r", " ").replace("\n", " ").strip()
        except Exception:
            return ""

    from_name, from_addr = parseaddr(h("From"))
    to_pairs = [(n, a) for n, a in getaddresses([h("To")]) if a]
    cc_pairs = [(n, a) for n, a in getaddresses([h("Cc")]) if a]
    reply_to = [(n, a) for n, a in getaddresses([h("Reply-To")]) if a] or [(from_name, from_addr)]
    try:
        attachments = [p.get_filename() or "unnamed" for p in msg.iter_attachments()]
    except Exception:
        attachments = []

    ts, when = 0.0, h("Date")
    try:
        dt = parsedate_to_datetime(h("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts, when = dt.timestamp(), dt.astimezone().strftime("%a %b %d, %Y %H:%M")
    except Exception:
        pass

    body = _body_text(msg)
    own, quoted = split_quoted(body)
    refs: list[str] = []
    for r in re.findall(r"<[^<>\s]+>", h("References") + " " + h("In-Reply-To")):
        if r not in refs:
            refs.append(r)

    # Signals that suggest a machine wrote this / nobody expects an answer (hints for the LLM).
    signals: list[str] = []
    me = settings.email_addresses
    auto = h("Auto-Submitted")
    if auto and auto.lower() != "no":
        signals.append(f"Auto-Submitted: {auto}")
    if h("Precedence").lower() in ("bulk", "junk", "list", "auto_reply"):
        signals.append(f"Precedence: {h('Precedence')}")
    if h("List-Unsubscribe") or h("List-Id"):
        signals.append("mailing-list headers (List-Unsubscribe / List-Id)")
    if h("X-Auto-Response-Suppress") or h("X-Autoreply") or h("X-Autorespond"):
        signals.append("auto-responder headers")
    if msg.get_content_type() == "multipart/report":
        signals.append("multipart/report (delivery status or machine-generated report)")
    if _AUTO_SENDER.search(from_addr.split("@")[0]):
        signals.append(f"sender address looks automated ({from_addr})")
    if "report" in h("Subject").lower() and any(
            a.lower().endswith((".xml", ".gz", ".zip")) for a in attachments):
        signals.append("report-style attachment (e.g. a DMARC aggregate report)")
    to_set = {a.lower() for _, a in to_pairs}
    if me and not (me & to_set):
        in_cc = me & {a.lower() for _, a in cc_pairs}
        signals.append("you are only in Cc" if in_cc else "you are not in To/Cc (Bcc or a list)")

    return {
        "subject": h("Subject") or "(no subject)",
        "from_name": from_name, "from_addr": from_addr,
        "to_pairs": to_pairs, "cc_pairs": cc_pairs, "reply_to": reply_to,
        "ts": ts, "when": when,
        "body": own or "(no text content)", "quoted": quoted, "full_text": body,
        "references": refs, "attachments": attachments, "signals": signals,
        "message_id": (h("Message-ID") or "").strip(),
    }


def _as_message(p: dict) -> dict:
    me = settings.email_addresses
    text = p["body"]
    if len(text) > settings.email_body_max_chars:
        text = text[:settings.email_body_max_chars] + "\n[... truncated ...]"
    return {
        "sender": p["from_name"] or p["from_addr"] or "Unknown",
        "text": text,
        "time": p["when"],
        "is_me": p["from_addr"].lower() in me,
    }


def _reply_recipients(p: dict) -> tuple[str, str]:
    """Reply-all by default: To = Reply-To/From, Cc = everyone else on the original (minus you)."""
    me = settings.email_addresses
    to_pairs = [(n, a) for n, a in p["reply_to"] if a.lower() not in me]
    seen = {a.lower() for _, a in to_pairs} | me
    cc_pairs = []
    for n, a in p["to_pairs"] + p["cc_pairs"]:
        if a.lower() not in seen:
            seen.add(a.lower())
            cc_pairs.append((n, a))
    fmt = lambda pairs: ", ".join(_fmt_addr(n, a) for n, a in pairs)  # noqa: E731
    return fmt(to_pairs), fmt(cc_pairs)


def _is_real_id(message_id: str) -> bool:
    return bool(message_id) and not message_id.startswith("<uid-")


# ------------------------------------------------------------------ public API
def fetch_emails_needing_reply(
    *, limit: int,
    already_seen: Callable[[str], bool],
    mark_seen: Callable[[str, str], None],
) -> list[EmailItem]:
    """Unanswered inbox emails (oldest first) that the assistant hasn't processed yet."""
    me = settings.email_addresses
    with _imap() as conn:
        sent = _sent_folder(conn)

        # ---- INBOX: candidates + their earlier thread messages
        _select(conn, "INBOX")
        since = _imap_date(date.today() - timedelta(days=settings.email_lookback_days))
        uids = _search(conn, "SINCE", since, "UNANSWERED")
        ids = _fetch_message_ids(conn, uids)
        fresh = [(u, ids[u]) for u in sorted(uids, key=int, reverse=True)
                 if u in ids and not already_seen(ids[u])][:limit]
        log.info("Email: %d unanswered in window, %d new to process", len(uids), len(fresh))

        parsed: list[dict] = []
        for uid, mid in fresh:
            raw = _fetch_raw(conn, uid)
            if not raw:
                continue
            p = _parse(raw)
            p["message_id"] = mid
            if p["from_addr"].lower() in me:
                mark_seen(mid, "sent by me")
                continue
            parsed.append(p)

        wanted = {ref for p in parsed for ref in p["references"][-MAX_REFS:]}
        ancestors: dict[str, dict] = {}
        for ref in wanted:
            raw = _fetch_by_message_id(conn, ref)
            if raw:
                ancestors[ref] = _parse(raw)

        # ---- Sent: did I already reply from another client? + my earlier messages in the thread
        _select(conn, sent)
        todo: list[dict] = []
        for p in parsed:
            if _is_real_id(p["message_id"]) and _search(conn, "HEADER", "In-Reply-To", _q(p["message_id"])):
                mark_seen(p["message_id"], "already replied")
                continue
            todo.append(p)
        for ref in wanted - ancestors.keys():
            raw = _fetch_by_message_id(conn, ref)
            if raw:
                ancestors[ref] = _parse(raw)

    # ---- one draft per thread: keep only the newest unanswered email of each thread
    todo.sort(key=lambda p: p["ts"])
    newest: dict[str, dict] = {}
    for p in todo:
        tid = p["references"][0] if p["references"] else p["message_id"]
        p["thread_id"] = tid
        if tid in newest:
            mark_seen(newest[tid]["message_id"], "older email in the same thread")
        newest[tid] = p

    items: list[EmailItem] = []
    for p in sorted(newest.values(), key=lambda p: p["ts"]):
        earlier = sorted((ancestors[r] for r in p["references"][-MAX_REFS:] if r in ancestors),
                         key=lambda a: a["ts"])
        messages = [_as_message(a) for a in earlier] + [_as_message(p)]
        to, cc = _reply_recipients(p)
        subject = p["subject"]
        meta = {
            "subject": subject,
            "reply_subject": subject if re.match(r"(?i)^\s*re:", subject) else f"Re: {subject}",
            "to": to, "cc": cc,
            "message_id": p["message_id"],
            "references": p["references"],
            "orig_from": _fmt_addr(p["from_name"], p["from_addr"]),
            "orig_date": p["when"],
            "orig_text": p["full_text"][:ORIG_TEXT_MAX_CHARS],
            "signals": p["signals"],
            "attachments": p["attachments"],
            # If we couldn't find earlier messages, the quoted history is the only thread context.
            "quoted": "" if earlier else p["quoted"][:QUOTED_MAX_CHARS],
        }
        items.append(EmailItem(p["message_id"], p["thread_id"],
                               _fmt_addr(p["from_name"], p["from_addr"]), messages, meta))
    return items


def answered_elsewhere(message_ids: list[str]) -> set[str]:
    """Which of these emails have been answered since (flagged \\Answered, or a reply sits in Sent)?"""
    ids = [m for m in message_ids if _is_real_id(m)]
    found: set[str] = set()
    if not ids:
        return found
    with _imap() as conn:
        _select(conn, "INBOX")
        for mid in ids:
            if _search(conn, "HEADER", "Message-ID", _q(mid), "ANSWERED"):
                found.add(mid)
        _select(conn, _sent_folder(conn))
        for mid in ids:
            if mid not in found and _search(conn, "HEADER", "In-Reply-To", _q(mid)):
                found.add(mid)
    return found


def _compose(to: str, cc: str, subject: str) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    msg["From"] = _fmt_addr(settings.email_from_name, settings.email_address)
    msg["To"] = to
    if cc.strip():
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    new_id = make_msgid(domain=settings.email_address.split("@")[-1])
    msg["Message-ID"] = new_id
    return msg, new_id


def _deliver(msg: EmailMessage, new_id: str, answered_id: str = "") -> SendResult:
    # 1) Send. Any exception here means the message did not go out.
    with _smtp() as server:
        server.send_message(msg)

    # 2) Save a copy to Sent (+ flag the original as answered for replies). The email is already
    #    out, so problems here are reported as a warning, never as a failed send.
    saved, warning = False, None
    try:
        time.sleep(2)  # give the server a moment in case it files sent mail itself
        with _imap() as conn:
            sent = _sent_folder(conn)
            _select(conn, sent)
            if _search(conn, "HEADER", "Message-ID", _q(new_id)):
                saved = True  # server already stored it; don't create a duplicate
            else:
                typ, _ = conn.append(_q(sent), r"(\Seen)", imaplib.Time2Internaldate(time.time()),
                                     msg.as_bytes(policy=policy.SMTP))
                saved = typ == "OK"
                if not saved:
                    warning = f"Email was sent, but could not be saved to '{sent}'."
            if _is_real_id(answered_id):
                _select(conn, "INBOX", readonly=False)
                for uid in _search(conn, "HEADER", "Message-ID", _q(answered_id)):
                    conn.uid("STORE", uid.decode(), "+FLAGS", r"(\Answered)")
    except Exception as e:
        log.exception("Post-send bookkeeping failed")
        warning = f"Email was sent, but saving to Sent / flagging as answered failed: {e}"
    return SendResult(True, saved_to_sent=saved, warning=warning)


def _check(body: str, to: str) -> str | None:
    if not body.strip():
        return "Message is empty."
    if not [a for _, a in getaddresses([to]) if "@" in a]:
        return "No valid recipient in 'To'."
    return None


def send_reply(meta: dict, body: str, to: str, cc: str = "") -> SendResult:
    """Called ONLY from the web UI when the user clicks 'Send email' on a reply draft."""
    problem = _check(body, to)
    if problem:
        return SendResult(False, error=problem)
    msg, new_id = _compose(to, cc, meta.get("reply_subject") or "Re:")
    orig_id = meta.get("message_id", "")
    if _is_real_id(orig_id):
        msg["In-Reply-To"] = orig_id
        msg["References"] = " ".join([*[r for r in meta.get("references", [])[-10:] if r != orig_id], orig_id])
    text = body.strip()
    if settings.email_quote_original and meta.get("orig_text"):
        quoted = "\n".join(("> " + line) if line else ">" for line in meta["orig_text"].splitlines())
        text += f"\n\nOn {meta.get('orig_date', '')}, {meta.get('orig_from', '')} wrote:\n{quoted}"
    msg.set_content(text)
    return _deliver(msg, new_id, answered_id=orig_id)


def send_new(to: str, subject: str, body: str, cc: str = "") -> SendResult:
    """Called ONLY from the web UI when the user clicks 'Send email' on a new-outreach draft."""
    problem = _check(body, to)
    if problem:
        return SendResult(False, error=problem)
    if not subject.strip():
        return SendResult(False, error="Add a subject line.")
    msg, new_id = _compose(to, cc, subject.strip())
    msg.set_content(body.strip())
    return _deliver(msg, new_id)


def self_test() -> list[str]:
    if not settings.email_enabled:
        return ["EMAIL_ADDRESS / EMAIL_PASSWORD are not set in .env"]
    out: list[str] = []
    with _imap() as conn:
        sent = _sent_folder(conn)
        _select(conn, "INBOX")
        since = _imap_date(date.today() - timedelta(days=settings.email_lookback_days))
        n = len(_search(conn, "SINCE", since, "UNANSWERED"))
        out.append(f"IMAP OK ({settings.email_imap_host}). Unanswered inbox emails in the last "
                   f"{settings.email_lookback_days} days: {n}")
        _select(conn, sent)
        out.append(f"Sent folder detected: '{sent}' (opens OK)")
    with _smtp():
        out.append(f"SMTP login OK ({settings.email_smtp_host}:{settings.email_smtp_port})")
    return out