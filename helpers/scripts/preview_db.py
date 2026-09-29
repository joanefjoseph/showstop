import sqlite3

DB_PATH = 'clients.db'
TABLE_NAME = 'client_metadata'
LIMIT = 1000

con = sqlite3.connect(DB_PATH)
# cur = con.execute(
#     "DELETE FROM employees WHERE company_name = ?",
#     ('WeVerse',))
# print(cur.rowcount, 'row(s) deleted')
# con.commit()
# con.close()

print(f"Previewing up to {LIMIT} rows from {TABLE_NAME}...")
rows = con.execute(f'SELECT * FROM {TABLE_NAME} LIMIT {LIMIT}').fetchall()
[print(r) for r in rows]
con.close()


# def _connect() -> sqlite3.Connection:
#     conn = sqlite3.connect(DB_PATH)
#     conn.row_factory = sqlite3.Row
#     return conn
# def init_client_metadata_table() -> None:
#     """Create the client_metadata table if it doesn't exist yet (idempotent)."""
#     conn = _connect()
#     try:
#         conn.execute(f"""
#             CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
#                 id            INTEGER PRIMARY KEY AUTOINCREMENT,
#                 company_name  TEXT NOT NULL,
#                 email         TEXT NOT NULL
#             )
#         """)
#         conn.commit()
#         print(f"Initialized table: {TABLE_NAME}")
#     finally:
#         conn.close()

# init_client_metadata_table()