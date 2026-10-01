#!/usr/bin/env python3
"""
db_tool.py: update clients.db from the command line.
Examples:
  # Delete rows where one column matches
  python db_tool.py --remove --table employees --column company_name --key "Acme"
  # Delete rows where several columns all match
  python db_tool.py --remove --table employees \
      --column company_name --key "Acme" \
      --column linkedin_profile_url --key "https://www.linkedin.com/in/jane-doe/"
  # Upsert one row (insert, or update if the primary key already exists)
  python db_tool.py --upsert --table hunter_lookups \
      --set domain=reddit.com --set first_name=alexis --set last_name=ohanian \
      --set email=alexis.ohanian@reddit.com --set score=81
  # Upsert every row from a CSV file
  python db_tool.py --upsert-csv rows.csv --table employees
"""
import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from config import DB_PATH
# Columns whose values are stored lowercase (matches how hunter.py writes them)
LOWERCASE_COLUMNS = {
    "hunter_lookups": {"domain", "first_name", "last_name"},
}
# Timestamps filled in automatically when not supplied
DEFAULT_VALUES = {
    "hunter_lookups": {"queried_at": lambda: _utc_now()},
    "sent_emails": {"timestamp": lambda: datetime.now().isoformat(timespec="seconds")},
}
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
# ------------------------- HELPERS -------------------------
def quote(identifier: str) -> str:
    """Quote a table/column name so it's safe to put into SQL."""
    return '"' + identifier.replace('"', '""') + '"'
def connect(db_path: Path) -> sqlite3.Connection:
    if not Path(db_path).is_file():
        sys.exit(f"Database not found: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
def table_info(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    info = conn.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    if not info:
        sys.exit(f"Table '{table}' not found.")
    return info
def check_columns(info: list[sqlite3.Row], columns: list[str], table: str) -> None:
    names = {r["name"] for r in info}
    unknown = [c for c in columns if c not in names]
    if unknown:
        sys.exit(f"Table '{table}' has no column(s): {unknown}. Columns: {sorted(names)}")
def primary_key(info: list[sqlite3.Row], table: str) -> list[str]:
    pk_rows = sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])
    if not pk_rows:
        sys.exit(f"Table '{table}' has no primary key, so upsert can't tell which row to update.")
    return [r["name"] for r in pk_rows]
def parse_set(items: list[str]) -> dict:
    out = {}
    for item in items:
        if "=" not in item:
            sys.exit(f"--set expects COL=VALUE, got {item!r}")
        col, value = item.split("=", 1)
        out[col.strip()] = value
    return out
def normalize(table: str, row: dict) -> dict:
    """Blank values become NULL, lowercase keys where needed, and defaults are filled in."""
    lower = LOWERCASE_COLUMNS.get(table, set())
    out = {}
    for col, val in row.items():
        if val is not None:
            val = val.strip()
        if not val:
            val = None
        elif col in lower:
            val = val.lower()
        out[col] = val
    for col, factory in DEFAULT_VALUES.get(table, {}).items():
        if out.get(col) is None:
            out[col] = factory()
    return out
# ------------------------- REMOVE -------------------------
def remove_rows(args: argparse.Namespace) -> None:
    if not args.column or len(args.column) != len(args.key):
        sys.exit("--remove needs at least one --column, with one --key for each --column.")
    conn = connect(Path(args.db))
    info = table_info(conn, args.table)
    check_columns(info, args.column, args.table)
    where = " AND ".join(f"{quote(c)} = ?" for c in args.column)
    from_clause = f"FROM {quote(args.table)} WHERE {where}"
    conditions = dict(zip(args.column, args.key))
    count = conn.execute(f"SELECT COUNT(*) {from_clause}", args.key).fetchone()[0]
    print(f"{count} row(s) in '{args.table}' match {conditions}.")
    if count == 0:
        return
    if args.dry_run:
        print("Dry run: nothing deleted.")
        return
    if not args.yes and input(f"Delete {count} row(s)? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return
    conn.execute(f"DELETE {from_clause}", args.key)
    conn.commit()
    conn.close()
    print(f"Deleted {count} row(s).")
# ------------------------- UPSERT -------------------------
def build_upsert_sql(table: str, columns: list[str], pk: list[str]) -> str:
    cols = ", ".join(quote(c) for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    conflict = ", ".join(quote(c) for c in pk)
    updates = [c for c in columns if c not in pk]
    if updates:
        # COALESCE: a blank value never overwrites an existing one
        set_clause = ", ".join(
            f"{quote(c)} = COALESCE(excluded.{quote(c)}, {quote(table)}.{quote(c)})"
            for c in updates
        )
        action = f"DO UPDATE SET {set_clause}"
    else:
        action = "DO NOTHING"
    return (f"INSERT INTO {quote(table)} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) {action}")
def upsert_rows(conn: sqlite3.Connection, table: str, rows: list[dict],
                info: list[sqlite3.Row], dry_run: bool = False) -> None:
    if not rows:
        print("No rows to upsert.")
        return
    columns = list(rows[0].keys())
    check_columns(info, columns, table)
    pk = primary_key(info, table)
    if not all(c in columns for c in pk):
        sys.exit(f"Upsert into '{table}' needs primary key column(s): {pk}")
    sql = build_upsert_sql(table, columns, pk)
    key_where = " AND ".join(f"{quote(c)} = ?" for c in pk)
    if dry_run:
        print(f"Dry run. SQL:\n  {sql}")
        print(f"Rows: {len(rows)}. First row: {rows[0]}")
        return
    inserted = updated = skipped = 0
    for row in rows:
        key_values = [row[c] for c in pk]
        if any(v is None for v in key_values):
            skipped += 1
            print(f"  [skip] primary key is blank: {dict(zip(pk, key_values))}")
            continue
        exists = conn.execute(
            f"SELECT 1 FROM {quote(table)} WHERE {key_where}", key_values
        ).fetchone()
        conn.execute(sql, [row[c] for c in columns])
        if exists:
            updated += 1
        else:
            inserted += 1
    conn.commit()
    print(f"Upserted into '{table}': inserted={inserted}, updated={updated}, skipped={skipped}.")
def upsert_one(args: argparse.Namespace) -> None:
    values = parse_set(args.set)
    if not values:
        sys.exit("--upsert needs at least one --set COL=VALUE.")
    conn = connect(Path(args.db))
    info = table_info(conn, args.table)
    row = normalize(args.table, values)
    upsert_rows(conn, args.table, [row], info, dry_run=args.dry_run)
    conn.close()
def upsert_csv(args: argparse.Namespace) -> None:
    csv_path = Path(args.upsert_csv)
    if not csv_path.is_file():
        sys.exit(f"CSV file not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        if not header:
            sys.exit("The CSV file appears to be empty.")
        rows = []
        for raw in reader:
            cleaned = {k.strip(): v for k, v in raw.items() if k and k.strip()}
            rows.append(normalize(args.table, cleaned))
    conn = connect(Path(args.db))
    info = table_info(conn, args.table)
    print(f"Read {len(rows)} row(s) from {csv_path.name}.")
    upsert_rows(conn, args.table, rows, info, dry_run=args.dry_run)
    conn.close()
# ------------------------- MAIN -------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Update clients.db from the command line.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--remove", action="store_true",
                      help="delete rows matching the --column/--key pairs")
    mode.add_argument("--upsert", action="store_true",
                      help="insert or update one row from --set pairs")
    mode.add_argument("--upsert-csv", metavar="PATH",
                      help="insert or update every row in a CSV file")
    ap.add_argument("--table", required=True, help="table name, e.g. employees")
    ap.add_argument("--db", default=str(DB_PATH), metavar="PATH",
                    help="database path (default: clients.db)")
    ap.add_argument("--column", action="append", default=[], metavar="COL",
                    help="column to match for --remove (repeatable)")
    ap.add_argument("--key", action="append", default=[], metavar="VALUE",
                    help="value to match for --remove; pairs with --column by position")
    ap.add_argument("--set", action="append", default=[], metavar="COL=VALUE",
                    help="column value for --upsert (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen without changing the database")
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompt for --remove")
    args = ap.parse_args()
    if args.remove:
        remove_rows(args)
    elif args.upsert:
        upsert_one(args)
    else:
        upsert_csv(args)
if __name__ == "__main__":
    main()