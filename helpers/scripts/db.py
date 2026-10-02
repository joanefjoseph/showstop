"""
db.py: open clients.db and make sure it matches schema.sql.

Every module that touches the database calls init_db() once at startup
(or before its first write); the per-file CREATE TABLE statements are gone.
"""
import sqlite3
from pathlib import Path
from config import DB_PATH, SCHEMA_PATH, SENT_TABLE_NAME


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    """Create any missing tables/indexes from schema.sql (idempotent)."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema)
        _apply_legacy_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def _apply_legacy_migrations(conn: sqlite3.Connection) -> None:
    """
    Small, safe ALTERs for databases created before a column existed.
    Anything bigger (changing a column) belongs in migrate_schema.py.
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({SENT_TABLE_NAME})")}
    if cols and "campaign" not in cols:
        conn.execute(
            f"ALTER TABLE {SENT_TABLE_NAME} ADD COLUMN campaign TEXT NOT NULL DEFAULT 'initial'"
        )

if __name__ == "__main__":      # `python db.py` creates/updates clients.db
    init_db()
    print(f"Schema applied to {DB_PATH}")