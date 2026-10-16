"""Create (or upgrade) the CONNECT MySQL schema.

Safe to run repeatedly:
  * ``schema.sql`` creates every table with ``IF NOT EXISTS``.
  * ``UPGRADES`` then adds any column that older databases are missing
    (MySQL 8 has no ``ADD COLUMN IF NOT EXISTS``, so we check
    ``information_schema`` first).
"""
from __future__ import annotations

import getpass
import os
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

# (table, column, DDL fragment) — columns added after the first release.
UPGRADES = [
    ("users", "religion", "VARCHAR(20) NOT NULL DEFAULT 'atheist'"),
    ("chat_sessions", "summary", "TEXT NULL"),
    ("journal_entries", "session_id", "VARCHAR(36) NULL"),
]


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current).rstrip(";"))
            current = []
    if current:
        statements.append("\n".join(current))
    return statements


def column_exists(cursor, database: str, table: str, column: str) -> bool:
    cursor.execute(
        """SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s""",
        (database, table, column),
    )
    return cursor.fetchone()[0] > 0


def apply_upgrades(cursor, database: str) -> None:
    for table, column, ddl in UPGRADES:
        if not column_exists(cursor, database, table, column):
            print(f"Upgrading: adding {table}.{column}")
            cursor.execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {ddl}")


def main() -> None:
    load_dotenv()

    password = os.getenv("MYSQL_PASSWORD", "")
    if password == "":
        password = getpass.getpass("MySQL password (leave blank if none): ")

    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": password,
    }
    database = os.getenv("MYSQL_DATABASE", "connect_mvp")

    statements = split_sql(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    print(f"Connecting to {config['host']} as {config['user']}...")

    with mysql.connector.connect(**config) as conn:
        cursor = conn.cursor()
        for statement in statements:
            print("Running:", " ".join(statement.split())[:60], "...")
            cursor.execute(statement)
        cursor.execute(f"USE `{database}`")
        apply_upgrades(cursor, database)
        conn.commit()

    print("CONNECT MySQL schema is ready.")


if __name__ == "__main__":
    main()
