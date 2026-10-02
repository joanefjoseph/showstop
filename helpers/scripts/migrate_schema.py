#!/usr/bin/env python3
"""
migrate_schema.py - one-off schema migration for clients.db
  * client_metadata : `id` becomes INTEGER PRIMARY KEY AUTOINCREMENT
                      (added if missing; any previous primary key becomes UNIQUE)
  * sent_emails.timestamp, sent_dms.timestamp, hunter_lookups.queried_at :
                      DEFAULT CURRENT_TIMESTAMP  (UTC, 'YYYY-MM-DD HH:MM:SS')
SQLite cannot alter columns in place, so every affected table is rebuilt:
CREATE new -> INSERT ... SELECT -> DROP old -> RENAME -> re-create indexes.
A timestamped backup of the database file is written before anything changes.
Run once:   python migrate_schema.py
"""
import shutil
import sqlite3
import sys
from datetime import datetime
from config import DB_PATH, METADATA_TABLE_NAME, SENT_TABLE_NAME, SENT_DMS_TABLE_NAME
# table -> {column: SQL default expression}
TIMESTAMP_DEFAULTS = {
    SENT_TABLE_NAME:     {"timestamp":  "CURRENT_TIMESTAMP"},
    SENT_DMS_TABLE_NAME: {"timestamp":  "CURRENT_TIMESTAMP"},
    "hunter_lookups":    {"queried_at": "CURRENT_TIMESTAMP"},
}
AUTOINCREMENT_ID_TABLES = [METADATA_TABLE_NAME]
def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
def table_sql(conn, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else None
def columns(conn, table: str) -> list[sqlite3.Row]:
    return conn.execute(f"PRAGMA table_info({q(table)})").fetchall()
def column_def(col: sqlite3.Row, default: str | None = None) -> str:
    parts = [q(col["name"]), col["type"] or ""]
    if col["notnull"]:
        parts.append("NOT NULL")
    dflt = default if default is not None else col["dflt_value"]
    if dflt is not None:
        parts.append(f"DEFAULT {dflt}")
    return " ".join(p for p in parts if p)
def rebuild(conn, table: str, defs: list[str], constraints: list[str], copy_cols: list[str]) -> None:
    tmp = f"{table}__new"
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,),
    )]
    conn.execute(f"DROP TABLE IF EXISTS {q(tmp)}")
    conn.execute(f"CREATE TABLE {q(tmp)} ({', '.join(defs + constraints)})")
    cols = ", ".join(q(c) for c in copy_cols)
    conn.execute(f"INSERT INTO {q(tmp)} ({cols}) SELECT {cols} FROM {q(table)} ORDER BY rowid")
    conn.execute(f"DROP TABLE {q(table)}")
    conn.execute(f"ALTER TABLE {q(tmp)} RENAME TO {q(table)}")
    for sql in indexes:
        conn.execute(sql)
def migrate_table(conn, table: str, defaults: dict[str, str] | None = None,
                  autoinc_id: bool = False) -> None:
    sql = table_sql(conn, table)
    if sql is None:
        print(f"- {table}: does not exist yet, skipping (the app creates it with the new schema).")
        return
    cols = columns(conn, table)
    names = {c["name"].lower(): c for c in cols}
    defaults = defaults or {}
    # --- Is there anything to do? ---
    has_autoinc = "AUTOINCREMENT" in sql.upper()
    needs_defaults = any(
        c in names and (names[c]["dflt_value"] or "").upper() != d.upper()
        for c, d in defaults.items()
    )
    needs_id = autoinc_id and not ("id" in names and names["id"]["pk"] and has_autoinc)
    if not needs_defaults and not needs_id:
        print(f"- {table}: already up to date.")
        return
    pk_cols = [c["name"] for c in sorted((c for c in cols if c["pk"]), key=lambda c: c["pk"])]
    defs, constraints, copy_cols = [], [], []
    if autoinc_id and "id" not in names:
        defs.append('"id" INTEGER PRIMARY KEY AUTOINCREMENT')
    for c in cols:
        name = c["name"]
        if autoinc_id and name.lower() == "id":
            defs.append(f"{q(name)} INTEGER PRIMARY KEY AUTOINCREMENT")
            if (c["type"] or "").upper() == "INTEGER":
                copy_cols.append(name)
            else:
                print(f"  ! {table}.id was {c['type'] or 'untyped'}; rows will be renumbered.")
            continue
        if pk_cols == [name] and (c["type"] or "").upper() == "INTEGER" and has_autoinc:
            defs.append(f"{q(name)} INTEGER PRIMARY KEY AUTOINCREMENT")   # keep existing autoinc pk
            copy_cols.append(name)
            continue
        defs.append(column_def(c, defaults.get(name)))
        copy_cols.append(name)
    # Table-level key constraints
    if autoinc_id:
        other_pk = [p for p in pk_cols if p.lower() != "id"]
        if other_pk:                      # old key is still unique, id is now the primary key
            constraints.append(f"UNIQUE ({', '.join(q(p) for p in other_pk)})")
    elif pk_cols and not (len(pk_cols) == 1 and has_autoinc):
        constraints.append(f"PRIMARY KEY ({', '.join(q(p) for p in pk_cols)})")
    rebuild(conn, table, defs, constraints, copy_cols)
    print(f"- {table}: rebuilt "
          f"({'id AUTOINCREMENT; ' if autoinc_id else ''}"
          f"{', '.join(f'{c} DEFAULT {d}' for c, d in defaults.items() if c in names) or 'no defaults changed'}).")
def main() -> None:
    if not DB_PATH.is_file():
        sys.exit(f"Database not found: {DB_PATH}")
    backup = DB_PATH.with_name(f"{DB_PATH.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{DB_PATH.suffix}")
    shutil.copy2(DB_PATH, backup)
    print(f"Backup written to {backup}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        for table in AUTOINCREMENT_ID_TABLES:
            migrate_table(conn, table, defaults=TIMESTAMP_DEFAULTS.get(table), autoinc_id=True)
        for table, defaults in TIMESTAMP_DEFAULTS.items():
            if table not in AUTOINCREMENT_ID_TABLES:
                migrate_table(conn, table, defaults=defaults)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    conn = sqlite3.connect(DB_PATH)
    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
    print(f"Integrity check: {check}")
    for table in AUTOINCREMENT_ID_TABLES + list(TIMESTAMP_DEFAULTS):
        sql = table_sql(conn, table)
        if sql:
            print(f"\n{sql}")
    conn.close()
if __name__ == "__main__":
    main()