#!/usr/bin/env python3
"""
app.py — Flask backend that:
  1. Launches linkedin_people_scraper.py as a background subprocess when the
     user submits a People-page URL.
  2. Lets the frontend poll the job's status/log output.
  3. Serves paginated rows from the `employees` table in all_people.db.
"""
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from flask import Flask, jsonify, render_template, request
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "all_people.db"
SCRAPER_SCRIPT = BASE_DIR / "linkedin_people_scraper.py"
PAGE_SIZE = 25
app = Flask(__name__)
# In-memory job tracker: {job_id: {"status": ..., "log": [...], "returncode": ...}}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
def _run_scraper(job_id: str, people_url: str, max_people: int) -> None:
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"
    cmd = [
        sys.executable, str(SCRAPER_SCRIPT),
        people_url,
        "--sqlite", str(DB_PATH),
        "--max-people", str(max_people),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            with JOBS_LOCK:
                JOBS[job_id]["log"].append(line.rstrip())
                # Cap stored log length so memory doesn't grow unbounded
                if len(JOBS[job_id]["log"]) > 2000:
                    JOBS[job_id]["log"] = JOBS[job_id]["log"][-2000:]
        proc.wait()
        with JOBS_LOCK:
            JOBS[job_id]["returncode"] = proc.returncode
            JOBS[job_id]["status"] = "finished" if proc.returncode == 0 else "error"
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]["log"].append(f"!! Failed to launch scraper: {e}")
            JOBS[job_id]["status"] = "error"
@app.route("/")
def index():
    return render_template("index.html")
@app.route("/api/scrape", methods=["POST"])
def start_scrape():
    data = request.get_json(force=True)
    people_url = (data.get("people_url") or "").strip()
    max_people = int(data.get("max_people") or 100)
    if not people_url:
        return jsonify({"error": "people_url is required"}), 400
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "log": [], "returncode": None}
    thread = threading.Thread(
        target=_run_scraper, args=(job_id, people_url, max_people), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})
@app.route("/api/scrape/<job_id>/status")
def scrape_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job_id"}), 404
        return jsonify({
            "status": job["status"],
            "log": job["log"],
            "returncode": job["returncode"],
        })
@app.route("/api/people")
def get_people():
    page = max(int(request.args.get("page", 1)), 1)
    company = (request.args.get("company") or "").strip()
    if not DB_PATH.exists():
        return jsonify({"rows": [], "total": 0, "page": page, "page_size": PAGE_SIZE})
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    where_clause = ""
    params: list = []
    if company:
        where_clause = "WHERE company_name LIKE ?"
        params.append(f"%{company}%")
    total = con.execute(
        f"SELECT COUNT(*) FROM employees {where_clause}", params
    ).fetchone()[0]
    offset = (page - 1) * PAGE_SIZE
    rows = con.execute(
        f"""SELECT company_name, first_name, last_name, job_title,
                   linkedin_profile_url, email
            FROM employees
            {where_clause}
            ORDER BY company_name, last_name, first_name
            LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, offset],
    ).fetchall()
    con.close()
    return jsonify({
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
    })
if __name__ == "__main__":
    app.run(debug=True, port=5001)