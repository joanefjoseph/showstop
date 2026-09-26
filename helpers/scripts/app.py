import math
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from flask import Flask, jsonify, render_template, request
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "all_people.db"
SCRAPER = BASE / "linkedin_people_scraper.py"
PER_PAGE = 25
app = Flask(__name__)
# Single shared scrape job (only one browser session at a time).
job = {"proc": None, "running": False, "log": [], "returncode": None}
job_lock = threading.Lock()
# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def fetch_page(page: int) -> tuple[list[dict], int, int]:
    """Return (rows, total_rows, clamped_page) for the requested page."""
    if not DB_PATH.exists():
        return [], 0, 1
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        total = con.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    except sqlite3.OperationalError:  # table not created yet
        con.close()
        return [], 0, 1
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, page), pages)
    offset = (page - 1) * PER_PAGE
    rows = con.execute(
        """SELECT company_name, first_name, last_name, job_title,
                  linkedin_profile_url, email
           FROM employees
           ORDER BY company_name, last_name, first_name
           LIMIT ? OFFSET ?""",
        (PER_PAGE, offset),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows], total, page
# --------------------------------------------------------------------------- #
# Scrape job management
# --------------------------------------------------------------------------- #
def _reader(proc: subprocess.Popen) -> None:
    """Stream the scraper's output into the job log."""
    for line in proc.stdout:
        with job_lock:
            job["log"].append(line.rstrip())
    proc.wait()
    with job_lock:
        job["running"] = False
        job["returncode"] = proc.returncode
@app.route("/")
def index():
    page = request.args.get("page", 1, type=int)
    rows, total, page = fetch_page(page)
    pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "index.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        per_page=PER_PAGE,
    )
@app.route("/scrape", methods=["POST"])
def scrape():
    url = request.form.get("url", "").strip()
    max_people = request.form.get("max_people", "100").strip()
    if "linkedin.com/company/" not in url:
        return jsonify(error="Please enter a LinkedIn company People page URL."), 400
    if not max_people.isdigit() or int(max_people) < 1:
        return jsonify(error="Max people must be a positive number."), 400
    with job_lock:
        if job["running"]:
            return jsonify(error="A scrape is already running."), 409
        cmd = [
            sys.executable, "-u", str(SCRAPER), url,
            "--sqlite", str(DB_PATH),
            "--max-people", max_people,
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=BASE,
            stdin=subprocess.PIPE,      # lets the UI answer the login prompt
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        job.update(proc=proc, running=True, log=[], returncode=None)
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return jsonify(ok=True)
@app.route("/status")
def status():
    with job_lock:
        return jsonify(
            running=job["running"],
            returncode=job["returncode"],
            log=job["log"][-200:],
        )
@app.route("/continue", methods=["POST"])
def continue_scrape():
    """Send Enter to the scraper after the user has logged in to LinkedIn."""
    with job_lock:
        proc = job["proc"]
        if not job["running"] or proc is None:
            return jsonify(error="No scrape is running."), 400
        proc.stdin.write("\n")
        proc.stdin.flush()
    return jsonify(ok=True)
if __name__ == "__main__":
    app.run(debug=False, port=5000)