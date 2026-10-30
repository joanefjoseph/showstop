"""
Drafting through the Gemini web app (gemini.google.com) with YOUR signed-in Google account.

A dedicated Chrome/Edge profile (data/gemini-chrome) is started as a normal browser with a local
DevTools port and Playwright attaches to it. Queued items are exported exactly like the manual
"Export to Gemini web" flow, typed into a new Gemini chat, and Gemini's ```json answer is read from
the page and imported as normal drafts. Nothing is ever sent to LinkedIn or email from here.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from . import db, handoff, llm
from . import gemini_selectors as G
from .config import settings

log = logging.getLogger(__name__)

_browser_lock = threading.Lock()   # one Gemini browser session at a time
_jobs = 0                          # background drafting jobs submitted but not finished
_jobs_lock = threading.Lock()

_SIGN_IN_MSG = "Not signed in to Gemini. Run: python -m dm_assistant gemini-login"


class GeminiWebError(RuntimeError):
    pass


class NotSignedInError(GeminiWebError):
    pass


def is_busy() -> bool:
    return _jobs > 0 or _browser_lock.locked()


# ------------------------------------------------------------------ browser
_BROWSER_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)


def _browser_exe() -> str:
    if settings.gemini_web_browser:
        return settings.gemini_web_browser
    for candidate in _BROWSER_CANDIDATES:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    for name in ("chrome", "google-chrome", "msedge", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    raise GeminiWebError("Couldn't find Chrome or Edge. Set GEMINI_WEB_BROWSER to the full path of chrome.exe.")


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", settings.gemini_web_port)) == 0


def _launch(url: str, debug: bool) -> subprocess.Popen:
    settings.gemini_profile_dir.mkdir(parents=True, exist_ok=True)
    args = [_browser_exe(), f"--user-data-dir={settings.gemini_profile_dir}",
            "--no-first-run", "--no-default-browser-check"]
    if debug:
        args += [f"--remote-debugging-port={settings.gemini_web_port}",
                 # keep Gemini streaming at full speed even when the window is in the background
                 "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                 "--disable-backgrounding-occluded-windows"]
        if settings.gemini_web_headless:
            args.append("--headless=new")
    return subprocess.Popen([*args, url])


@contextmanager
def _gemini_page() -> Iterator[Page]:
    with _browser_lock, sync_playwright() as p:
        proc = None
        if not _port_open():
            proc = _launch("about:blank", debug=True)
            deadline = time.time() + 30
            while not _port_open():
                if proc.poll() is not None:
                    raise GeminiWebError(
                        "The Gemini browser closed right after starting - the profile is probably still "
                        "open in another window (e.g. from gemini-login). Close it and try again.")
                if time.time() > deadline:
                    proc.kill()
                    raise GeminiWebError("The Gemini browser didn't open its debugging port.")
                time.sleep(0.3)
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{settings.gemini_web_port}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30_000)
        try:
            yield page
        finally:
            try:
                page.close()
            except Exception:
                pass
            if proc is not None:  # we started it -> close it cleanly
                try:
                    browser.new_browser_cdp_session().send("Browser.close")
                except Exception:
                    pass
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            try:
                browser.close()  # for a CDP connection this just disconnects
            except Exception:
                pass


# ------------------------------------------------------------------ page interaction
def _sel() -> dict:
    return {"response": G.RESPONSE, "text": G.RESPONSE_TEXT, "code": G.CODE_BLOCK, "stop": G.STOP_BUTTON}


_STATE_JS = r"""(s) => {
  const rs = document.querySelectorAll(s.response);
  const last = rs[rs.length - 1];
  const body = last ? (last.querySelector(s.text) || last) : null;
  return {count: rs.length, len: body ? body.innerText.length : 0, busy: !!document.querySelector(s.stop)};
}"""

_ANSWER_JS = r"""(s) => {
  const rs = document.querySelectorAll(s.response);
  const last = rs[rs.length - 1];
  if (!last) return '';
  const body = last.querySelector(s.text) || last;
  let blocks = [...body.querySelectorAll(s.code)].map(b => (b.querySelector('code') || b).innerText);
  if (!blocks.length) blocks = [...body.querySelectorAll('pre')].map(p => p.innerText);
  return blocks.length ? blocks.map(b => '```json\n' + b + '\n```').join('\n') : body.innerText;
}"""


def _pick_model(page: Page, label: str) -> None:
    try:
        page.locator(G.MODEL_MENU_BUTTON).first.click(timeout=5_000)
        page.locator(G.MODEL_MENU_ITEM).filter(has_text=label).first.click(timeout=5_000)
        time.sleep(0.5)
    except Exception as e:
        log.warning("Couldn't select Gemini model %r (%s) - using the current one", label, e)
        page.keyboard.press("Escape")


def _open_chat(page: Page) -> None:
    page.goto(settings.gemini_web_url, wait_until="domcontentloaded")
    if "accounts.google.com" in page.url:
        raise NotSignedInError(_SIGN_IN_MSG)
    try:
        page.wait_for_selector(G.INPUT, timeout=30_000)
    except PWTimeout:
        raise GeminiWebError("Gemini's chat box wasn't found - Google may have changed the page "
                             "(update dm_assistant/gemini_selectors.py).") from None
    time.sleep(1.5)  # let the account menu / banners render
    if page.locator(G.SIGNED_OUT).count():
        raise NotSignedInError(_SIGN_IN_MSG)
    page.keyboard.press("Escape")  # dismiss "what's new" style pop-ups, if any
    if settings.gemini_web_model:
        _pick_model(page, settings.gemini_web_model)


def _send(page: Page, text: str) -> None:
    box = page.locator(G.INPUT).first
    box.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if i < len(lines) - 1:
            page.keyboard.press("Shift+Enter")  # newline without sending
    time.sleep(1)
    typed = len(box.inner_text())
    if typed < 0.8 * len(text):
        raise GeminiWebError(f"Only {typed} of {len(text)} characters reached Gemini's input box - "
                             "try a smaller GEMINI_WEB_BATCH_SIZE.")
    try:
        page.locator(G.SEND_BUTTON).first.click(timeout=10_000)
    except PWTimeout:
        box.press("Enter")


def _wait_for_answer(page: Page, before: int) -> None:
    """Done = a new answer exists, the Stop button is gone and the text has stopped growing."""
    deadline = time.time() + settings.gemini_web_timeout_s
    last_len, stable = -1, 0
    while time.time() < deadline:
        time.sleep(2)
        s = page.evaluate(_STATE_JS, _sel())
        if s["count"] <= before or s["busy"] or s["len"] == 0 or s["len"] != last_len:
            last_len, stable = s["len"], 0
            continue
        stable += 1
        if stable >= 2:
            return
    raise GeminiWebError(f"Gemini didn't finish answering within {settings.gemini_web_timeout_s}s.")


PROMPT_TEMPLATE = """I'm sending you an export from my LinkedIn + email drafting assistant. The JSON at the end contains:
- system_prompt: your role and the rules for this task - follow it as your instructions
- context: my company, product, current phase, current focus and conversation goals
- items: {count} message(s) to draft ({what}); each has the exact task instructions plus the conversation or contact details
- response_format: the JSON structure I need back

Complete every item following system_prompt and the item's own instructions. Answer with ONLY one ```json code block in the response_format (one result per item id) and no other text. These are drafts for me to review; nothing is sent automatically.

```json
{payload}
```"""


def _web_prompt(export: dict) -> str:
    counts = Counter(i["task"] for i in export["items"])
    what = "; ".join(f"{n} x {task}" for task, n in counts.items())
    payload = json.dumps(export, ensure_ascii=False, separators=(",", ":"))  # one line = fast to type
    return PROMPT_TEMPLATE.format(count=len(export["items"]), what=what, payload=payload)


def _ask_raw(page: Page, prompt: str) -> str:
    _open_chat(page)  # a fresh chat for every batch
    before = page.locator(G.RESPONSE).count()
    _send(page, prompt)
    _wait_for_answer(page, before)
    answer = page.evaluate(_ANSWER_JS, _sel())
    if not answer.strip():
        raise GeminiWebError("Gemini returned an empty answer.")
    return answer


def _ask(page: Page, export: dict) -> str:
    return _ask_raw(page, _web_prompt(export))


# ------------------------------------------------------------------ public API
def _queued(ids: list[int] | None) -> list[dict]:
    items = db.get_queue_items(ids) if ids is not None else db.list_queue()
    return [q for q in items if q["status"] == "queued"]


def _note(items: list[dict], reason: str) -> None:
    """Record why still-queued items weren't drafted (shown under 'Waiting for a draft')."""
    for q in items:
        cur = db.get_queue_item(q["id"])
        if cur and cur["status"] == "queued":
            db.set_queue_status(q["id"], "queued", str(reason)[:300])


def process_queue(ids: list[int] | None = None) -> dict:
    """Draft queued items (all, or just `ids`) in Gemini web and import the answers as drafts."""
    out = {"created": 0, "skipped": 0, "errors": []}
    if not _queued(ids):
        return out
    try:
        with _gemini_page() as page:
            items = _queued(ids)  # re-read: another job may have drafted some while we waited
            size = max(1, settings.gemini_web_batch_size)
            for start in range(0, len(items), size):
                chunk = items[start:start + size]
                try:
                    _, export, _ = handoff.build_export(chunk, "gemini_web")
                    answer = _ask(page, export)
                    try:
                        res = handoff.import_results(answer)
                    except ValueError as e:
                        raise GeminiWebError(f"{e} Gemini said: {answer[:200]!r}") from None
                except NotSignedInError:
                    raise
                except Exception as e:
                    log.exception("Gemini web batch failed")
                    out["errors"].append(f"Gemini web: {e}")
                    _note(chunk, f"Gemini web: {e}")
                    continue
                out["created"] += res["created"]
                out["skipped"] += res["skipped"]
                out["errors"] += res["errors"]
                _note(chunk, "Gemini web didn't return a usable draft for this item")
    except Exception as e:
        log.exception("Gemini web drafting failed")
        out["errors"].append(f"Gemini web: {e}")
        _note(_queued(ids), f"Gemini web: {e}")
    log.info("Gemini web: %d drafted, %d skipped, %d problem(s)",
             out["created"], out["skipped"], len(out["errors"]))
    return out


def run_in_background(ids: list[int] | None = None) -> None:
    """For the UI: draft without blocking the request; the Drafts page polls until done."""
    global _jobs
    with _jobs_lock:
        _jobs += 1

    def work() -> None:
        global _jobs
        try:
            process_queue(ids)
        finally:
            with _jobs_lock:
                _jobs -= 1

    threading.Thread(target=work, name="gemini-web", daemon=True).start()


def draft_single(p: dict, *, instruction: str | None = None, previous_draft: str | None = None,
                 force_reply: bool = False) -> tuple[llm.DraftResult, str]:
    """Regenerate / 'reply anyway' for one existing draft. Returns (result, subject)."""
    item = {"id": 0, "payload": p,
            "revision": {"instruction": instruction, "previous_draft": previous_draft,
                         "force_reply": force_reply}}
    _, export, _ = handoff.build_export([item], "gemini_web_redraft")
    with _gemini_page() as page:
        answer = _ask(page, export)
    results = handoff.parse_results(answer)
    r = next((r for r in results if str(r.get("id", "")).strip().lstrip("q") == "0"), results[0])
    if force_reply:
        r = {**r, "needs_reply": True}
    return handoff._result_from_dict(p, r)


def interactive_login() -> bool:
    if _port_open():
        print("A Gemini browser session is already running - wait for it to finish, or close it.")
        return False
    proc = _launch(settings.gemini_web_url, debug=False)  # a plain browser: Google allows the sign-in
    input("\nA browser window has opened on Gemini. Sign in with the Google account that has Gemini Pro,\n"
          "check that the chat box appears (pick your preferred model while you're there), then CLOSE\n"
          "that browser window and press Enter here... ")
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
    try:
        with _gemini_page() as page:
            _open_chat(page)
        return True
    except GeminiWebError as e:
        print(e)
        return False


def self_test() -> str:
    with _gemini_page() as page:
        answer = _ask_raw(page, 'Connectivity test. Reply with only this code block:\n```json\n'
                                '{"results":[{"id":"q0","reply":"Hello!"}]}\n```')
    return f"OK - Gemini answered and the reply was parsed: {handoff.parse_results(answer)[0]}"