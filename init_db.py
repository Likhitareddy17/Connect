"""Create (or upgrade) the CONNECT MySQL schema.

Safe to run repeatedly, and safe on managed MySQL hosts:
  * The database named by MYSQL_DATABASE is created if the user is allowed to; if it already exists
    (typical on managed hosts, where you get one fixed database) that is fine.
  * ``schema.sql`` creates every table with ``IF NOT EXISTS``.
  * ``UPGRADES`` then adds any column that older databases are missing (MySQL 8 has no
    ``ADD COLUMN IF NOT EXISTS``, so ``information_schema`` is checked first).

TLS options (MYSQL_SSL_CA, MYSQL_SSL_VERIFY, ...) are shared with the app: see store.ssl_options.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv

from store import connection_config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# (table, column, DDL fragment): columns added after the first release.
UPGRADES = [
    ("users", "religion", "VARCHAR(20) NOT NULL DEFAULT 'atheist'"),
    ("chat_sessions", "summary", "TEXT NULL"),
    ("journal_entries", "session_id", "VARCHAR(36) NULL"),
]

_DB_NAME_RE = re.compile(r"^[A-Za-z0-9_$]{1,64}$")


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


def select_database(cursor, database: str) -> None:
    """Create the database if we may, then switch to it (clear error if it is unusable)."""
    if not _DB_NAME_RE.match(database):
        raise ValueError(f"Invalid MYSQL_DATABASE name: {database!r}")
    try:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    except mysql.connector.Error as exc:
        print(f"Note: could not create database '{database}' ({exc.msg}); assuming it already exists.")
    try:
        cursor.execute(f"USE `{database}`")
    except mysql.connector.Error as exc:
        raise RuntimeError(
            f"Database '{database}' is not usable ({exc.msg}). Create it first (or set MYSQL_DATABASE to "
            "the database your provider gave you) and make sure this user has privileges on it."
        ) from exc


def initialize(config: dict, database: str, schema_path: Path = SCHEMA_PATH) -> None:
    """Connect with ``config`` (no database selected), then create/upgrade the schema in ``database``."""
    statements = split_sql(schema_path.read_text(encoding="utf-8"))
    with mysql.connector.connect(**config) as conn:
        cursor = conn.cursor()
        select_database(cursor, database)
        for statement in statements:
            print("Running:", " ".join(statement.split())[:60], "...")
            cursor.execute(statement)
        apply_upgrades(cursor, database)
        conn.commit()


def main() -> None:
    load_dotenv()

    if os.getenv("MYSQL_PASSWORD", "") == "" and sys.stdin.isatty():
        os.environ["MYSQL_PASSWORD"] = getpass.getpass("MySQL password (leave blank if none): ")

    config = connection_config()
    database = os.getenv("MYSQL_DATABASE", "connect_mvp")
    print(f"Connecting to {config['host']}:{config['port']} as {config['user']} (database: {database})...")
    initialize(config, database)
    print("CONNECT MySQL schema is ready.")


if __name__ == "__main__":
    main()
