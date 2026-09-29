from pathlib import Path
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "clients.db"
TABLE_NAME = "employees"
SENT_TABLE_NAME = "sent_emails"