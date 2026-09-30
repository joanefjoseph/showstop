import csv
import io
import math
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from flask import Flask, Response, jsonify, render_template, request, url_for
from linkedin_people_scraper import clean_profile_url, init_sqlite
BASE = Path(__file__).resolve().parent
from config import DB_PATH
from hunter import fill_missing_emails, require_key
from send_emails import (
    CAMPAIGN_INITIAL,
    CAMPAIGN_FOLLOWUP,
    EMAIL_RE,
    get_recipients,
    preview_first,
    require_password,
    send_campaign,
)
SCRAPER = BASE / "linkedin_people_scraper.py"
PER_PAGE = 25
CSV_FIELDS = ("first_name", "last_name", "job_title", "email")
REQUIRED_CSV_FIELDS = ("company_name", "linkedin_profile_url")
app = Flask(__name__)
# Single shared scrape job (only one browser session at a time).
job = {"proc": None, "running": False, "log": [], "returncode": None}
job_lock = threading.Lock()
# --------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------- #
def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so the search is a literal substring match."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
def fetch_page(page: int, company_filter: str = "") -> tuple[list[dict], int, int]:
    """
    Return (rows, total_matching_rows, clamped_page) for the requested page,
    optionally limited to rows whose company_name contains `company_filter`.
    """
    if not DB_PATH.exists():
        return [], 0, 1
    where = ""
    params: list = []
    if company_filter:
        where = "WHERE company_name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(company_filter)}%")
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        total = con.execute(
            f"SELECT COUNT(*) FROM employees {where}", params
        ).fetchone()[0]
    except sqlite3.OperationalError:  # table not created yet
        con.close()
        return [], 0, 1
    pages = max(1, math.ceil(total / PER_PAGE))
    page = min(max(1, page), pages)
    offset = (page - 1) * PER_PAGE
    rows = con.execute(
        f"""SELECT company_name, first_name, last_name, job_title,
                   linkedin_profile_url, email
            FROM employees
            {where}
            ORDER BY company_name, last_name, first_name
            LIMIT ? OFFSET ?""",
        [*params, PER_PAGE, offset],
    ).fetchall()
    con.close()
    return [dict(r) for r in rows], total, page
# --------------------------------------------------------------------- #
# Scrape job management
# --------------------------------------------------------------------- #
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
    q = request.args.get("q", "").strip()
    rows, total, page = fetch_page(page, q)
    pages = max(1, math.ceil(total / PER_PAGE))
    return render_template(
        "index.html",
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        q=q,
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
            stdin=subprocess.PIPE,       # lets the UI answer the login prompt
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
# --------------------------------------------------------------------- #
# CSV upload / download
# --------------------------------------------------------------------- #
@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    file = request.files.get("csv_file")
    if not file or not file.filename:
        return jsonify(error="Please choose a CSV file."), 400
    try:
        text = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify(error="Could not read the file as UTF-8 text."), 400
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return jsonify(error="The CSV file appears to be empty."), 400
    header_map = {h.strip().lower() for h in reader.fieldnames if h}
    missing = [f for f in REQUIRED_CSV_FIELDS if f not in header_map]
    if missing:
        return jsonify(
            error=f"CSV is missing required column(s): {', '.join(missing)}"
        ), 400
    valid_rows = []
    skipped = 0
    for raw in reader:
        values = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        company_name = values.get("company_name", "")
        profile_url = clean_profile_url(values.get("linkedin_profile_url", ""))
        if not company_name or not profile_url:
            skipped += 1
            continue
        row = {
            "company_name": company_name,
            "linkedin_profile_url": profile_url,
        }
        for field in CSV_FIELDS:
            row[field] = values.get(field) or None  # blank/missing -> None
        valid_rows.append(row)
    if not valid_rows:
        return jsonify(
            error="No valid rows found (each row needs company_name and linkedin_profile_url)."
        ), 400
    init_sqlite(DB_PATH)  # make sure the table exists
    con = sqlite3.connect(DB_PATH)
    existing_pairs = {
        (c, u) for c, u in
        con.execute("SELECT company_name, linkedin_profile_url FROM employees")
    }
    inserted = updated = 0
    for row in valid_rows:
        key = (row["company_name"], row["linkedin_profile_url"])
        if key in existing_pairs:
            updated += 1
        else:
            inserted += 1
            existing_pairs.add(key)
    con.executemany(
        """INSERT INTO employees
               (company_name, first_name, last_name, job_title,
                linkedin_profile_url, email)
           VALUES (:company_name, :first_name, :last_name, :job_title,
                   :linkedin_profile_url, :email)
           ON CONFLICT(company_name, linkedin_profile_url) DO UPDATE SET
               first_name = COALESCE(excluded.first_name, employees.first_name),
               last_name  = COALESCE(excluded.last_name, employees.last_name),
               job_title  = COALESCE(excluded.job_title, employees.job_title),
               email      = COALESCE(excluded.email, employees.email)""",
        valid_rows,
    )
    con.commit()
    con.close()
    return jsonify(ok=True, inserted=inserted, updated=updated, skipped=skipped)
@app.route("/download_csv")
def download_csv():
    q = request.args.get("q", "").strip()
    where = ""
    params: list = []
    if q:
        where = "WHERE company_name LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(q)}%")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["company_name", "first_name", "last_name", "job_title",
                      "linkedin_profile_url", "email"])
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        try:
            rows = con.execute(
                f"""SELECT company_name, first_name, last_name, job_title,
                           linkedin_profile_url, email
                    FROM employees {where}
                    ORDER BY company_name, last_name, first_name""",
                params,
            ).fetchall()
            writer.writerows(rows)
        except sqlite3.OperationalError:
            pass
        con.close()
    filename = "employees_filtered.csv" if q else "employees.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
# --------------------------------------------------------------------- #
# Email outreach (initial + follow-up campaigns)
# --------------------------------------------------------------------- #
EMAIL_JOB = {"running": False, "log": [], "returncode": None}
email_lock = threading.Lock()
FOLLOWUP_JOB = {"running": False, "log": [], "returncode": None}
followup_lock = threading.Lock()
def _email_log(line: str) -> None:
    with email_lock:
        EMAIL_JOB["log"].append(line)
def _followup_log(line: str) -> None:
    with followup_lock:
        FOLLOWUP_JOB["log"].append(line)
def _email_worker(rows: list, test_to: str | None, campaign: str,
                   job: dict, lock: threading.Lock, log) -> None:
    rc = 0
    try:
        send_campaign(rows, test_to=test_to, campaign=campaign, log=log)
    except Exception as exc:
        log(f"ERROR: {exc}")
        rc = 1
    with lock:
        job["running"] = False
        job["returncode"] = rc
def _start_email(test_to: str | None, limit: int | None, campaign: str,
                  job: dict, lock: threading.Lock, log):
    with lock:
        if job["running"]:
            return jsonify(error="An email job is already running."), 409
        job.update(running=True, log=[], returncode=None)
    try:
        require_password()
        rows = get_recipients(limit=limit, test_to=test_to, campaign=campaign, log=log)
    except Exception as exc:
        with lock:
            job["running"] = False
        return jsonify(error=str(exc)), 400
    if not rows:
        with lock:
            job.update(running=False, returncode=0)
        no_rows_msg = (
            "No recipients to email."
            if campaign == CAMPAIGN_INITIAL
            else "No one is eligible for a follow-up yet (they must have received the first email already)."
        )
        return jsonify(error=no_rows_msg), 400
    threading.Thread(target=_email_worker, args=(rows, test_to, campaign, job, lock, log),
                      daemon=True).start()
    return jsonify(ok=True, count=len(rows))
@app.route("/email/preview")
def email_preview():
    try:
        info = preview_first(campaign=CAMPAIGN_INITIAL)
    except Exception as exc:
        return jsonify(error=str(exc)), 400
    if info is None:
        return jsonify(error="No eligible recipients in clients.db."), 400
    return jsonify(ok=True, **info)
@app.route("/email/test", methods=["POST"])
def email_test():
    test_to = request.form.get("test_to", "").strip()
    if not EMAIL_RE.match(test_to):
        return jsonify(error="Enter a valid tester email address."), 400
    return _start_email(test_to=test_to, limit=None, campaign=CAMPAIGN_INITIAL,
                         job=EMAIL_JOB, lock=email_lock, log=_email_log)
@app.route("/email/send", methods=["POST"])
def email_send():
    raw = request.form.get("limit", "").strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        return jsonify(error="Limit must be a positive number."), 400
    return _start_email(test_to=None, limit=int(raw) if raw else None, campaign=CAMPAIGN_INITIAL,
                         job=EMAIL_JOB, lock=email_lock, log=_email_log)
@app.route("/email/status")
def email_status():
    with email_lock:
        return jsonify(
            running=EMAIL_JOB["running"],
            returncode=EMAIL_JOB["returncode"],
            log=EMAIL_JOB["log"][-300:],
        )
@app.route("/followup/preview")
def followup_preview():
    try:
        info = preview_first(campaign=CAMPAIGN_FOLLOWUP)
    except Exception as exc:
        return jsonify(error=str(exc)), 400
    if info is None:
        return jsonify(
            error="No one is eligible for a follow-up yet (they must have received the first email already)."
        ), 400
    return jsonify(ok=True, **info)
@app.route("/followup/test", methods=["POST"])
def followup_test():
    test_to = request.form.get("test_to", "").strip()
    if not EMAIL_RE.match(test_to):
        return jsonify(error="Enter a valid tester email address."), 400
    return _start_email(test_to=test_to, limit=None, campaign=CAMPAIGN_FOLLOWUP,
                         job=FOLLOWUP_JOB, lock=followup_lock, log=_followup_log)
@app.route("/followup/send", methods=["POST"])
def followup_send():
    raw = request.form.get("limit", "").strip()
    if raw and (not raw.isdigit() or int(raw) < 1):
        return jsonify(error="Limit must be a positive number."), 400
    return _start_email(test_to=None, limit=int(raw) if raw else None, campaign=CAMPAIGN_FOLLOWUP,
                         job=FOLLOWUP_JOB, lock=followup_lock, log=_followup_log)
@app.route("/followup/status")
def followup_status():
    with followup_lock:
        return jsonify(
            running=FOLLOWUP_JOB["running"],
            returncode=FOLLOWUP_JOB["returncode"],
            log=FOLLOWUP_JOB["log"][-300:],
        )
# --------------------------------------------------------------------- #
# Email search (Hunter.io)
# --------------------------------------------------------------------- #
HUNTER_JOB = {"running": False, "log": [], "returncode": None}
hunter_lock = threading.Lock()
def _hunter_log(line: str) -> None:
    with hunter_lock:
        HUNTER_JOB["log"].append(line)
def _hunter_worker() -> None:
    rc = 0
    try:
        stats = fill_missing_emails(log=_hunter_log)
        summary = ", ".join(f"{k}={v}" for k, v in stats.items())
        _hunter_log(f"Done. {summary}")
    except Exception as exc:
        _hunter_log(f"ERROR: {exc}")
        rc = 1
    with hunter_lock:
        HUNTER_JOB.update(running=False, returncode=rc)
@app.route("/hunter/start", methods=["POST"])
def hunter_start():
    try:
        require_key()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 400
    with hunter_lock:
        if HUNTER_JOB["running"]:
            return jsonify(error="An email search is already running."), 409
        HUNTER_JOB.update(running=True, log=[], returncode=None)
    threading.Thread(target=_hunter_worker, daemon=True).start()
    return jsonify(ok=True)
@app.route("/hunter/status")
def hunter_status():
    with hunter_lock:
        return jsonify(
            running=HUNTER_JOB["running"],
            returncode=HUNTER_JOB["returncode"],
            log=HUNTER_JOB["log"][-300:],
        )
if __name__ == "__main__":
    app.run(debug=False, port=5000)