from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import db
from .config import settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="python -m dm_assistant")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("login", help="Log in to LinkedIn once in a real browser window")
    sub.add_parser("serve", help="Run the review UI + the 9/1/5 scheduler (default)")
    sub.add_parser("scan", help="Run one scan now and exit")
    c = sub.add_parser("context", help="View/update your context from the terminal")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("show")
    s = csub.add_parser("set")
    s.add_argument("key", choices=list(db.CONTEXT_FIELDS))
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--text")
    g.add_argument("--file", type=Path)
    args = p.parse_args()

    db.init_db()
    cmd = args.cmd or "serve"

    if cmd == "login":
        from .linkedin import interactive_login
        ok = interactive_login()
        print("✅ Logged in - session saved." if ok else "❌ Could not confirm login; try again.")
        sys.exit(0 if ok else 1)

    if cmd == "scan":
        from .pipeline import run_scan
        print(json.dumps(run_scan(trigger="cli"), indent=2))
        return

    if cmd == "context":
        if args.action == "show":
            ctx = db.get_context()
            for key, (label, _) in db.CONTEXT_FIELDS.items():
                e = ctx.get(key)
                print(f"\n== {label} ({e['updated_at'] if e else 'not set'}) ==\n{e['value'] if e else ''}")
        else:
            value = args.text if args.text is not None else args.file.read_text(encoding="utf-8")
            db.set_context(args.key, value)
            print(f"Saved {args.key}.")
        return

    # serve
    if not settings.gemini_api_key:
        print("⚠️  GEMINI_API_KEY is not set - drafting will fail.")
    if not settings.my_name:
        print("⚠️  LINKEDIN_MY_NAME is not set - the assistant can't tell your messages from theirs.")
    from . import scheduler
    from .web import app
    scheduler.start()
    print(f"\nDM Assistant running at {settings.ui_url}\n")
    app.run(host=settings.host, port=settings.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()