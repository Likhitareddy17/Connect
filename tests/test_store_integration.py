"""Integration tests against a REAL MySQL (skipped unless MYSQL_TEST_HOST is set).

Run locally:
    MYSQL_TEST_HOST=127.0.0.1 MYSQL_TEST_PASSWORD=... pytest tests/test_store_integration.py

They apply schema.sql to a throw-away database, so they also prove that a
fresh install (the README's setup steps) actually works.
"""
import os
import uuid
from unittest.mock import MagicMock, patch

import mysql.connector
import pytest

from init_db import initialize

pytestmark = pytest.mark.skipif(
    not os.getenv("MYSQL_TEST_HOST"), reason="set MYSQL_TEST_HOST to run database integration tests"
)

TEST_DB = "connect_integration_test"


@pytest.fixture(scope="module")
def real_store():
    conn_args = {
        "host": os.environ["MYSQL_TEST_HOST"],
        "port": int(os.getenv("MYSQL_TEST_PORT", "3306")),
        "user": os.getenv("MYSQL_TEST_USER", "root"),
        "password": os.getenv("MYSQL_TEST_PASSWORD", ""),
    }
    with mysql.connector.connect(**conn_args) as conn:
        conn.cursor().execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        conn.commit()
    initialize(conn_args, TEST_DB)  # the real init path: creates the database, tables and upgrades

    for key, value in {"HOST": conn_args["host"], "PORT": str(conn_args["port"]),
                       "USER": conn_args["user"], "PASSWORD": conn_args["password"],
                       "DATABASE": TEST_DB}.items():
        os.environ[f"MYSQL_{key}"] = value

    from store import ConnectStore
    yield ConnectStore()

    with mysql.connector.connect(**conn_args) as conn:
        conn.cursor().execute(f"DROP DATABASE IF EXISTS {TEST_DB}")


@pytest.fixture()
def user(real_store):
    return real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "Tester", "password123", religion="sikh")


def new_thread(store, user):
    sid = str(uuid.uuid4())
    assert store.ensure_chat_session(user["id"], sid) is True
    return sid


class TestUsers:
    def test_create_and_login_roundtrip(self, real_store, user):
        row = real_store.get_user_by_email(user["email"].upper())  # case-insensitive lookup
        assert row and row["religion"] == "sikh"
        assert real_store.verify_password(row, "password123")
        assert not real_store.verify_password(row, "wrong")

    def test_duplicate_email_returns_none(self, real_store, user):
        assert real_store.create_user(user["email"], "Dup", "password123") is None

    def test_unknown_religion_becomes_atheist(self, real_store):
        u = real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "X", "password123", religion="nonsense")
        assert u["religion"] == "atheist"


class TestChatThreads:
    def test_recent_messages_are_the_latest_in_chronological_order(self, real_store, user):
        sid = new_thread(real_store, user)
        for i in range(10):
            real_store.save_chat_message(sid, "user", f"m{i}")
        assert [m["content"] for m in real_store.get_recent_messages(sid, limit=4)] == ["m6", "m7", "m8", "m9"]
        full = [m["content"] for m in real_store.get_session_messages(sid)]
        assert full == [f"m{i}" for i in range(10)]
        assert real_store.count_session_messages(sid) == 10

    def test_ownership(self, real_store, user):
        other = real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "O", "password123")
        sid = new_thread(real_store, user)
        assert real_store.owns_chat_session(user["id"], sid)
        assert not real_store.owns_chat_session(other["id"], sid)
        assert real_store.ensure_chat_session(other["id"], sid) is False  # cannot claim someone else's id

    def test_summary_roundtrip(self, real_store, user):
        sid = new_thread(real_store, user)
        assert real_store.get_session_summary(sid) == ""
        real_store.set_session_summary(sid, "feeling stretched thin")
        assert real_store.get_session_summary(sid) == "feeling stretched thin"

    def test_delete_cascades_to_messages_and_reflection_card(self, real_store, user):
        sid = new_thread(real_store, user)
        real_store.save_chat_message(sid, "user", "hello")
        real_store.save_journal(user["id"], "calm", "calm", "hello", "hi", session_id=sid)
        assert real_store.delete_chat_session(user["id"], sid) is True
        assert real_store.get_session_messages(sid) == []
        assert all(j.get("session_id") != sid for j in real_store.recent_journals(user["id"]))

    def test_delete_by_non_owner_touches_nothing(self, real_store, user):
        other = real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "O", "password123")
        sid = new_thread(real_store, user)
        real_store.save_chat_message(sid, "user", "keep me")
        assert real_store.delete_chat_session(other["id"], sid) is False
        assert [m["content"] for m in real_store.get_session_messages(sid)] == ["keep me"]

    def test_ping(self, real_store):
        assert real_store.ping() is True


def test_end_to_end_through_flask_with_real_database(real_store, monkeypatch):
    """Register -> chat past the history window -> crisis still caught -> delete."""
    import app as app_module

    monkeypatch.setattr(app_module, "store", real_store)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_module.limiter.enabled = False
    client = app_module.app.test_client()

    email = f"{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/register", data={"email": email, "display_name": "E2E", "password": "password123",
                                         "confirm_password": "password123", "religion": "hindu"})
    assert res.status_code == 302

    ok = MagicMock(status_code=200)
    ok.raise_for_status = MagicMock()
    ok.json.return_value = {"choices": [{"message": {"content": '{"reply": "I hear you.", "mood": "stressed"}'}}]}
    with patch("services.journal_analysis.requests.post", return_value=ok):
        first = client.post("/api/chat/message", json={"message": "long day"}).get_json()
        for i in range(8):
            client.post("/api/chat/message", json={"message": f"more {i}"})
    sid = first["session_id"]
    assert real_store.count_session_messages(sid) == 18  # 9 user + 9 assistant

    with patch("services.journal_analysis.requests.post") as post:
        crisis = client.post("/api/chat/message", json={"message": "I want to die"}).get_json()
    post.assert_not_called()
    assert crisis["crisis"] is True

    assert client.delete(f"/api/chat/session/{sid}").status_code == 200
    assert real_store.get_session_messages(sid) == []


class TestManagedHostSetup:
    """A managed MySQL host gives you ONE pre-created database and a user that can't create others."""

    DB, USER, PW = "connect_managed_test", "managed_user", "managed-pw-1"

    @pytest.fixture()
    def admin(self):
        args = {"host": os.environ["MYSQL_TEST_HOST"], "port": int(os.getenv("MYSQL_TEST_PORT", "3306")),
                "user": os.getenv("MYSQL_TEST_USER", "root"), "password": os.getenv("MYSQL_TEST_PASSWORD", "")}
        with mysql.connector.connect(**args) as conn:
            cur = conn.cursor()
            cur.execute(f"DROP DATABASE IF EXISTS {self.DB}")
            cur.execute(f"DROP USER IF EXISTS '{self.USER}'@'%'")
            cur.execute(f"CREATE DATABASE {self.DB}")
            cur.execute(f"CREATE USER '{self.USER}'@'%' IDENTIFIED BY '{self.PW}'")
            cur.execute(f"GRANT ALL PRIVILEGES ON {self.DB}.* TO '{self.USER}'@'%'")
            conn.commit()
            yield args
            cur.execute(f"DROP DATABASE IF EXISTS {self.DB}")
            cur.execute(f"DROP USER IF EXISTS '{self.USER}'@'%'")
            conn.commit()

    def limited(self, admin):
        return {"host": admin["host"], "port": admin["port"], "user": self.USER, "password": self.PW}

    def test_initialises_a_preexisting_database_without_create_privilege(self, admin):
        initialize(self.limited(admin), self.DB)
        initialize(self.limited(admin), self.DB)  # and again: idempotent
        with mysql.connector.connect(**admin) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", (self.DB,))
            assert cur.fetchone()[0] == 6

    def test_unusable_database_gives_a_clear_error(self, admin):
        with pytest.raises(RuntimeError, match="not usable"):
            initialize(self.limited(admin), "connect_does_not_exist_test")
