"""
Reads LinkedIn conversations and posts a reply only when the user clicks Send.
It drives a real browser logged in with YOUR session (saved in data/browser-profile).
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterator
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from . import selectors as S
from .config import settings

log = logging.getLogger(__name__)
_browser_lock = threading.Lock()  # one browser session at a time (shared profile dir)

KIND_REPLY = "reply"
KIND_FOLLOWUP = "connection_followup"


class LinkedInError(RuntimeError):
    pass


class NotLoggedInError(LinkedInError):
    pass


@dataclass
class Message:
    sender: str
    text: str
    time: str = ""
    is_me: bool = False


@dataclass
class Thread:
    thread_id: str
    url: str
    participant: str
    preview_key: str
    messages: list[Message] = field(default_factory=list)
    kind: str = KIND_REPLY

    @property
    def awaiting_reply(self) -> bool:
        return bool(self.messages) and not self.messages[-1].is_me

    def state_hash(self) -> str:
        return state_hash(self.messages)


@dataclass
class SendOutcome:
    ok: bool
    unconfirmed: bool = False
    conflict: str | None = None
    error: str | None = None


# ------------------------------------------------------------------ helpers
def state_hash(messages: list[Message]) -> str:
    tail = [[m.sender, m.text] for m in messages[-3:]]
    return hashlib.sha256(json.dumps(tail, ensure_ascii=False).encode()).hexdigest()[:20]


def preview_key(name: str, snippet: str) -> str:
    return hashlib.sha256(f"{name}\x00{snippet}".encode()).hexdigest()[:20]


def is_connection_note_thread(messages: list[Message]) -> bool:
    """Exactly one message, written by me, short enough to be an invitation note."""
    return (
        len(messages) == 1
        and messages[0].is_me
        and len(messages[0].text.strip()) <= settings.connection_note_max_chars
    )


def _pause(lo: float = 0.8, hi: float = 2.0) -> None:
    time.sleep(random.uniform(lo, hi))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _is_me(name: str | None) -> bool:
    n = _norm(name or "")
    if not n:
        return False
    if n == "you":
        return True
    return any(n == a or n.startswith(a + " ") for a in settings.my_names)


def _thread_id(url: str, fallback_name: str) -> str:
    m = re.search(r"/messaging/thread/([^/?#]+)", url)
    if m:
        return m.group(1)
    return "name:" + hashlib.sha256(fallback_name.encode()).hexdigest()[:16]


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _age_days(label: str, today: date) -> int | None:
    """Parse the conversation-list timestamp ('10:32 AM', 'Mon', 'Jun 3', 'Jun 3, 2024').
    Returns None if unrecognised (then no age filter is applied)."""
    s = (label or "").strip()
    if not s:
        return None
    low = s.lower()
    if re.fullmatch(r"\d{1,2}:\d{2}\s*(am|pm)?", low) or low in ("now", "just now", "today"):
        return 0
    if low == "yesterday":
        return 1
    if s.isalpha() and low[:3] in _WEEKDAYS:
        delta = (today.weekday() - _WEEKDAYS.index(low[:3])) % 7
        return delta or 7
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%b %d", "%B %d"):
        try:
            dt = datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=today.year)
            if dt > today:
                dt = dt.replace(year=today.year - 1)
        return (today - dt).days
    return None


def _check_logged_in(page: Page) -> None:
    if any(marker in page.url for marker in S.LOGGED_OUT_MARKERS):
        raise NotLoggedInError("Not logged in to LinkedIn. Run: python -m dm_assistant login")


@contextmanager
def linkedin_page(headless: bool | None = None) -> Iterator[Page]:
    with _browser_lock, sync_playwright() as p:
        settings.profile_dir.mkdir(parents=True, exist_ok=True)
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(settings.profile_dir),
            channel=settings.browser_channel or None,
            headless=settings.headless if headless is None else headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(20_000)
            yield page
        finally:
            ctx.close()


# ------------------------------------------------------------------ DOM extraction
_LIST_JS = r"""
(s) => Array.from(document.querySelectorAll(s.item)).map((li, i) => {
  const q = (sel) => { const el = li.querySelector(sel); return el ? el.innerText.trim() : ''; };
  const a = li.querySelector(s.link);
  return {
    index: i,
    name: q(s.name).split('\n')[0].trim(),
    snippet: q(s.snippet).replace(/\s+/g, ' ').trim(),
    time: q(s.time),
    unread: !!li.querySelector(s.unread),
    href: a ? a.getAttribute('href') : null,
    sponsored: /\bSponsored\b/i.test(li.innerText),
  };
})
"""

_MESSAGES_JS = r"""
(s) => {
  const out = [];
  let sender = null, time = '', day = '';
  for (const li of document.querySelectorAll(s.event)) {
    const d = li.querySelector(s.day);     if (d && d.innerText.trim()) day = d.innerText.trim();
    const n = li.querySelector(s.sender);  if (n && n.innerText.trim()) sender = n.innerText.trim().split('\n')[0];
    const t = li.querySelector(s.time);    if (t && t.innerText.trim()) time = t.innerText.trim();
    for (const b of li.querySelectorAll(s.body)) {
      const text = b.innerText.trim();
      if (text) out.push({sender: sender || 'Unknown', text, time: [day, time].filter(Boolean).join(' ')});
    }
  }
  return out;
}
"""


def _list_conversations(page: Page, want: int) -> list[dict]:
    sel = {"item": S.CONVO_ITEM, "name": S.CONVO_NAME, "snippet": S.CONVO_SNIPPET,
           "time": S.CONVO_TIME, "unread": S.CONVO_UNREAD, "link": S.CONVO_LINK}
    items = page.evaluate(_LIST_JS, sel)
    for _ in range(5):  # scroll the list a little if we need more
        if len(items) >= want:
            break
        page.locator(S.CONVO_ITEM).last.scroll_into_view_if_needed()
        _pause(1.0, 2.0)
        more = page.evaluate(_LIST_JS, sel)
        if len(more) <= len(items):
            break
        items = more
    return items[:want]


def _extract_messages(page: Page) -> list[Message]:
    sel = {"event": S.MESSAGE_EVENT, "sender": S.MESSAGE_SENDER, "time": S.MESSAGE_TIME,
           "day": S.DAY_HEADING, "body": S.MESSAGE_BODY}
    raw = page.evaluate(_MESSAGES_JS, sel)
    return [Message(sender=m["sender"], text=m["text"], time=m["time"], is_me=_is_me(m["sender"]))
            for m in raw]


def _open_messaging(page: Page) -> None:
    page.goto(S.MESSAGING_URL, wait_until="domcontentloaded")
    _check_logged_in(page)
    try:
        page.wait_for_selector(S.CONVO_ITEM, timeout=25_000)
    except PWTimeout:
        _check_logged_in(page)
        raise LinkedInError("Conversation list not found - LinkedIn markup may have changed "
                            "(update dm_assistant/selectors.py).")
    _pause()


def _wait_for_title(page: Page, name: str) -> None:
    first = (name or "").split(",")[0].strip()
    if not first:
        return
    try:
        page.wait_for_function(
            "([sel, name]) => { const el = document.querySelector(sel); "
            "return !!el && el.innerText.includes(name); }",
            arg=[S.THREAD_TITLE, first], timeout=8_000,
        )
    except PWTimeout:
        log.debug("Thread title did not show %r; continuing", first)


def _open_conversation(page: Page, item: dict) -> str:
    if item.get("href"):
        page.goto(urljoin("https://www.linkedin.com", item["href"]), wait_until="domcontentloaded")
    else:
        prev = page.url
        page.locator(S.CONVO_ITEM).nth(item["index"]).click()
        try:
            page.wait_for_function("prev => location.href !== prev", arg=prev, timeout=8_000)
        except PWTimeout:
            pass  # it was probably the thread that was already open
    _check_logged_in(page)
    _wait_for_title(page, item.get("name", ""))
    page.wait_for_selector(S.MESSAGE_EVENT, timeout=15_000)
    _pause(1.0, 2.0)
    return page.url


def _read_thread(page: Page, item: dict, key: str) -> Thread | None:
    try:
        url = _open_conversation(page, item)
        msgs = _extract_messages(page)
    except PWTimeout:
        log.warning("Timed out opening conversation with %s; skipping", item["name"])
        return None
    finally:
        _pause(1.5, 4.0)  # human-ish pacing between threads
    return Thread(_thread_id(url, item["name"]), url, item["name"], key, msgs)


# ------------------------------------------------------------------ public API
def interactive_login() -> bool:
    with linkedin_page(headless=False) as page:
        page.goto("https://www.linkedin.com/login")
        input("\nA browser window has opened. Log in to LinkedIn there (complete any 2FA),\n"
              "then come back here and press Enter... ")
        page.goto(S.MESSAGING_URL, wait_until="domcontentloaded")
        try:
            _check_logged_in(page)
            page.wait_for_selector(S.CONVO_ITEM, timeout=20_000)
            return True
        except (LinkedInError, PWTimeout):
            return False


def fetch_candidate_threads(
    *,
    reply_limit: int,
    followup_limit: int,
    already_drafted: Callable[[str], bool] = lambda key: False,
    already_checked: Callable[[str], bool] = lambda key: False,
    mark_checked: Callable[[str], None] = lambda key: None,
) -> tuple[list[Thread], list[str]]:
    """
    Returns (threads to draft for, names of conversations where YOU sent the latest message).

    Threads are either:
      * KIND_REPLY     - the other person spoke last
      * KIND_FOLLOWUP  - exactly one message, from you, <= CONNECTION_NOTE_MAX_CHARS
                         (an accepted connection request with a note)
    """
    threads: list[Thread] = []
    you_last: list[str] = []
    today = date.today()
    replies_opened = followups_opened = 0

    with linkedin_page() as page:
        _open_messaging(page)
        items = _list_conversations(page, want=settings.conversation_scan_depth)
        log.info("Conversation list: %d items", len(items))

        for item in items:
            name, snippet = item["name"], item["snippet"]
            if item["sponsored"] or not name:
                continue
            key = preview_key(name, snippet)
            if already_drafted(key):
                continue  # unchanged since we last drafted for it

            if not snippet.startswith(S.YOU_PREFIXES):
                # --- The other person (probably) spoke last -> normal reply
                if replies_opened >= reply_limit:
                    continue
                t = _read_thread(page, item, key)
                if t is None:
                    continue
                replies_opened += 1
                if t.awaiting_reply:
                    threads.append(t)
                elif is_connection_note_thread(t.messages):
                    t.kind = KIND_FOLLOWUP
                    threads.append(t)
                elif t.messages:
                    you_last.append(name)
                continue

            # --- You sent the latest message: either you replied, or it's an accepted connection note.
            # Any older pending draft for this person is stale either way.
            you_last.append(name)
            if already_checked(key):
                continue
            age = _age_days(item.get("time", ""), today)
            if settings.followup_max_age_days and age is not None and age > settings.followup_max_age_days:
                continue
            if followups_opened >= followup_limit:
                continue  # budget used up; it'll be checked on a later run

            t = _read_thread(page, item, key)
            if t is None:
                continue
            followups_opened += 1
            # The preview says "You:", so a lone message is yours even if the name match failed.
            if len(t.messages) == 1 and not t.messages[0].is_me:
                t.messages[0].is_me = True
            if is_connection_note_thread(t.messages):
                t.kind = KIND_FOLLOWUP
                threads.append(t)
                log.info("Accepted connection note detected: %s", name)
            else:
                mark_checked(key)  # don't reopen this thread unless it changes

    return threads, you_last


def send_message(thread_url: str, text: str, expected_hash: str | None,
                 kind: str = KIND_REPLY, force: bool = False) -> SendOutcome:
    """Called ONLY from the web UI when the user clicks Send on an approved draft."""
    text = text.strip()
    if not text:
        return SendOutcome(False, error="Message is empty.")
    with linkedin_page() as page:
        page.goto(thread_url, wait_until="domcontentloaded")
        _check_logged_in(page)
        page.wait_for_selector(S.COMPOSE_BOX, timeout=25_000)
        try:
            page.wait_for_selector(S.MESSAGE_EVENT, timeout=10_000)
        except PWTimeout:
            pass
        _pause()

        msgs = _extract_messages(page)
        if not force and msgs:
            # For connection follow-ups the last message is *supposed* to be yours (the note).
            if kind != KIND_FOLLOWUP and msgs[-1].is_me:
                return SendOutcome(False, conflict="The last message in this thread is already from you "
                                                   "(you may have replied from another device).")
            if expected_hash and state_hash(msgs) != expected_hash:
                return SendOutcome(False, conflict="This conversation changed since the draft was written "
                                                   "(a new message from them or from you). Consider regenerating.")

        box = page.locator(S.COMPOSE_BOX).first
        box.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                page.keyboard.insert_text(line)
            if i < len(lines) - 1:
                page.keyboard.press("Shift+Enter")  # newline without sending
        _pause(0.5, 1.0)

        btn = page.locator(S.SEND_BUTTON).first
        btn.wait_for(state="visible", timeout=10_000)
        if not btn.is_enabled():
            return SendOutcome(False, error="LinkedIn's Send button is disabled - nothing was sent.")
        btn.click()

        needle = _norm(text)[:40]
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(1)
            if any(_norm(m.text).startswith(needle) for m in _extract_messages(page)[-3:]):
                return SendOutcome(True)
        return SendOutcome(True, unconfirmed=True)