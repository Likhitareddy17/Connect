import getpass
import os
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv


def split_sql(script):
    statements = []
    current = []

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


def main():
    load_dotenv()

    password = os.getenv("MYSQL_PASSWORD")
    if password is None:
        password = ""

    if password == "":
        entered = getpass.getpass("MySQL password for root, leave blank if none: ")
        password = entered

    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": password,
    }

    schema_path = Path(__file__).with_name("schema.sql")
    statements = split_sql(schema_path.read_text(encoding="utf-8"))
    print(f"Attempting to connect to {config['host']} as {config['user']}...")
    
    with mysql.connector.connect(**config) as conn:
        cursor = conn.cursor()
        print("Connected successfully! Executing schema...")
        for statement in statements:
            # Clean up whitespace to print nicely
            clean_statement = " ".join(statement.split())[:60]
            print(f"Running: {clean_statement}...")
            cursor.execute(statement)
        conn.commit()
    # with mysql.connector.connect(**config) as conn:
    #     cursor = conn.cursor()
    #     for statement in statements:
    #         cursor.execute(statement)
    #     conn.commit()

    print("CONNECT MySQL schema is ready.")


if __name__ == "__main__":
    main()
