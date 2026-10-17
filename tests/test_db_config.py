"""Database configuration, TLS options and init_db helpers (no database needed)."""
from pathlib import Path

import mysql.connector
import pytest

from init_db import select_database, split_sql
from store import connection_config, ssl_options

SSL_VARS = ("MYSQL_SSL_CA", "MYSQL_SSL_VERIFY", "MYSQL_SSL_DISABLED")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in SSL_VARS:
        monkeypatch.delenv(name, raising=False)


class TestSslOptions:
    def test_defaults_add_nothing(self):
        assert ssl_options() == {}

    def test_ca_only_encrypts_with_that_ca(self, monkeypatch):
        monkeypatch.setenv("MYSQL_SSL_CA", "/etc/ca.pem")
        assert ssl_options() == {"ssl_ca": "/etc/ca.pem"}

    def test_verify_requires_a_ca(self, monkeypatch):
        monkeypatch.setenv("MYSQL_SSL_VERIFY", "1")
        with pytest.raises(ValueError, match="MYSQL_SSL_CA"):
            ssl_options()

    def test_verify_with_ca(self, monkeypatch):
        monkeypatch.setenv("MYSQL_SSL_CA", "ca.pem")
        monkeypatch.setenv("MYSQL_SSL_VERIFY", "true")
        assert ssl_options() == {"ssl_ca": "ca.pem", "ssl_verify_cert": True}

    def test_can_disable_tls_for_local_dev(self, monkeypatch):
        monkeypatch.setenv("MYSQL_SSL_DISABLED", "yes")
        assert ssl_options() == {"ssl_disabled": True}


class TestConnectionConfig:
    def test_reads_environment_and_converts_port(self, monkeypatch):
        monkeypatch.setenv("MYSQL_HOST", "db.example.com")
        monkeypatch.setenv("MYSQL_PORT", "12345")
        monkeypatch.setenv("MYSQL_USER", "app")
        monkeypatch.setenv("MYSQL_PASSWORD", "pw")
        assert connection_config("mydb") == {
            "host": "db.example.com", "port": 12345, "user": "app", "password": "pw", "database": "mydb"}

    def test_database_is_omitted_when_not_given(self):
        assert "database" not in connection_config()

    def test_tls_options_are_merged_in(self, monkeypatch):
        monkeypatch.setenv("MYSQL_SSL_CA", "ca.pem")
        assert connection_config("db")["ssl_ca"] == "ca.pem"


class FakeCursor:
    def __init__(self, fail_on=None):
        self.fail_on, self.executed = fail_on or {}, []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        for marker, error in self.fail_on.items():
            if sql.startswith(marker):
                raise error


class TestSelectDatabase:
    def test_creates_then_uses(self):
        cur = FakeCursor()
        select_database(cur, "connect_mvp")
        assert cur.executed[0].startswith("CREATE DATABASE IF NOT EXISTS `connect_mvp`")
        assert cur.executed[1] == "USE `connect_mvp`"

    def test_missing_create_privilege_is_tolerated(self, capsys):
        denied = mysql.connector.Error(msg="Access denied", errno=1044)
        cur = FakeCursor(fail_on={"CREATE DATABASE": denied})
        select_database(cur, "defaultdb")
        assert cur.executed[-1] == "USE `defaultdb`" and "assuming it already exists" in capsys.readouterr().out

    def test_unusable_database_raises_a_helpful_error(self):
        cur = FakeCursor(fail_on={"CREATE DATABASE": mysql.connector.Error(msg="denied", errno=1044),
                                  "USE": mysql.connector.Error(msg="Unknown database", errno=1049)})
        with pytest.raises(RuntimeError, match="MYSQL_DATABASE"):
            select_database(cur, "nope")

    @pytest.mark.parametrize("name", ["bad name", "a;DROP DATABASE x", "`x`", "", "x" * 65])
    def test_rejects_unsafe_database_names(self, name):
        with pytest.raises(ValueError):
            select_database(FakeCursor(), name)


class TestSchemaFile:
    SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

    def test_schema_does_not_own_the_database_name(self):
        sql = self.SCHEMA.read_text(encoding="utf-8").upper()
        assert "CREATE DATABASE" not in sql and "\nUSE " not in sql

    def test_schema_creates_every_table_idempotently(self):
        statements = split_sql(self.SCHEMA.read_text(encoding="utf-8"))
        assert len(statements) == 6
        assert all(st.upper().startswith("CREATE TABLE IF NOT EXISTS") for st in statements)
