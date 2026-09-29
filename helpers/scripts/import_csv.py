import csv
from pathlib import Path
from send_emails import init_sent_table, _connect, BASE
from config import SENT_TABLE_NAME
from contextlib import closing
def import_sent_csv(csv_path: Path = BASE / "sent_log.csv") -> int:
    if not csv_path.exists():
        return 0
    init_sent_table()
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [(r["timestamp"], r["email"].lower(), r["status"], r.get("detail", ""))
                for r in csv.DictReader(f)]
    with closing(_connect()) as conn:
        conn.executemany(
            f"INSERT INTO {SENT_TABLE_NAME} (timestamp, email, status, detail) "
            f"VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    return len(rows)
print(import_sent_csv(), "rows imported")
