from pathlib import Path
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "clients.db"
SCHEMA_PATH = BASE / "schema.sql"
TABLE_NAME = "employees"
SENT_TABLE_NAME = "sent_emails"
METADATA_TABLE_NAME = "client_metadata"
SENT_DMS_TABLE_NAME = "sent_dms"
HUNTER_TABLE_NAME = "hunter_lookups"