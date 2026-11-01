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


class TestEmailTokens:
    """Real-MySQL checks of the token SQL: hashing, expiry, single use, concurrency, cascade."""

    def rows(self, store, table, user_id):
        with store.connection() as conn:
            cur = conn.cursor(dictionary=True)
            cur.execute(f"SELECT * FROM {table} WHERE user_id = %s", (user_id,))
            return cur.fetchall()

    def unverified(self, store):
        return store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "T", "password123", email_verified=False)

    def test_new_user_is_unverified_and_verify_flips_the_flag(self, real_store):
        u = self.unverified(real_store)
        assert real_store.get_user_by_id(u["id"])["email_verified"] == 0
        token = real_store.create_email_verification_token(u["id"])
        assert real_store.verify_email_token(token) is True
        assert real_store.get_user_by_id(u["id"])["email_verified"] == 1

    def test_only_a_sha256_of_the_token_is_stored(self, real_store):
        u = self.unverified(real_store)
        token = real_store.create_email_verification_token(u["id"])
        (row,) = self.rows(real_store, "email_verification_tokens", u["id"])
        from store import hash_token
        assert row["token_hash"] == hash_token(token) and token not in row["token_hash"] and len(row["token_hash"]) == 64

    def test_single_use(self, real_store):
        u = self.unverified(real_store)
        token = real_store.create_email_verification_token(u["id"])
        assert real_store.verify_email_token(token) is True
        assert real_store.verify_email_token(token) is False

    def test_expired_token_is_refused_and_leaves_user_unverified(self, real_store):
        u = self.unverified(real_store)
        token = real_store.create_email_verification_token(u["id"])
        with real_store.connection() as conn:
            conn.cursor().execute(
                "UPDATE email_verification_tokens SET expires_at = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 SECOND) "
                "WHERE user_id = %s", (u["id"],))
        assert real_store.verify_email_token(token) is False
        assert real_store.get_user_by_id(u["id"])["email_verified"] == 0

    def test_expiry_is_about_24_hours_for_verification_and_1_hour_for_reset(self, real_store):
        u = self.unverified(real_store)
        real_store.create_email_verification_token(u["id"])
        real_store.create_password_reset_token(u["id"])
        with real_store.connection() as conn:
            cur = conn.cursor()
            for table, hours in (("email_verification_tokens", 24), ("password_reset_tokens", 1)):
                cur.execute(
                    f"SELECT TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), expires_at) FROM {table} WHERE user_id = %s",
                    (u["id"],))
                assert hours * 60 - 2 <= cur.fetchone()[0] <= hours * 60

    def test_issuing_a_new_token_retires_the_previous_one(self, real_store):
        u = self.unverified(real_store)
        first = real_store.create_email_verification_token(u["id"])
        second = real_store.create_email_verification_token(u["id"])
        assert real_store.verify_email_token(first) is False
        assert real_store.verify_email_token(second) is True

    def test_unknown_user_id_is_refused_by_the_foreign_key(self, real_store):
        with pytest.raises(mysql.connector.IntegrityError):
            real_store.create_email_verification_token(2_000_000_000)

    def test_garbage_and_oversized_tokens(self, real_store):
        assert real_store.verify_email_token("") is False
        assert real_store.verify_email_token("nope") is False
        assert real_store.verify_email_token("x" * 5000) is False
        assert real_store.reset_password_with_token("' OR 1=1 --", "password123") is False

    def test_concurrent_use_succeeds_exactly_once(self, real_store):
        from concurrent.futures import ThreadPoolExecutor
        u = self.unverified(real_store)
        token = real_store.create_email_verification_token(u["id"])
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: real_store.verify_email_token(token), range(4)))
        assert results.count(True) == 1

    def test_concurrent_password_reset_succeeds_exactly_once(self, real_store):
        from concurrent.futures import ThreadPoolExecutor
        u = self.unverified(real_store)
        token = real_store.create_password_reset_token(u["id"])
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda i: real_store.reset_password_with_token(token, f"new-password-{i}"), range(4)))
        assert results.count(True) == 1

    def test_password_reset_changes_hash_verifies_email_and_voids_other_links(self, real_store):
        u = self.unverified(real_store)
        old_hash = real_store.get_user_by_id(u["id"])["password_hash"]
        stale = real_store.create_password_reset_token(u["id"])
        live = real_store.create_password_reset_token(u["id"])  # retires `stale`
        assert real_store.reset_password_with_token(stale, "new-password-1") is False
        assert real_store.reset_password_with_token(live, "new-password-1") is True
        row = real_store.get_user_by_id(u["id"])
        assert row["password_hash"] != old_hash and real_store.verify_password(row, "new-password-1")
        assert not real_store.verify_password(row, "password123")
        assert row["email_verified"] == 1
        assert all(r["used_at"] is not None for r in self.rows(real_store, "password_reset_tokens", u["id"]))

    def test_a_token_of_one_kind_is_useless_for_the_other(self, real_store):
        u = self.unverified(real_store)
        assert real_store.reset_password_with_token(real_store.create_email_verification_token(u["id"]), "new-password-1") is False
        assert real_store.verify_email_token(real_store.create_password_reset_token(u["id"])) is False

    def test_delete_user_cascades_everywhere_and_spares_other_users(self, real_store):
        u = real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "D", "password123")
        other = real_store.create_user(f"{uuid.uuid4().hex[:10]}@example.com", "O", "password123")
        sid, osid = new_thread(real_store, u), new_thread(real_store, other)
        real_store.save_chat_message(sid, "user", "bye")
        real_store.save_chat_message(osid, "user", "stay")
        real_store.save_mood(u["id"], "calm")
        real_store.save_journal(u["id"], "calm", "calm", "text", "hint", session_id=sid)
        real_store.save_jaap_count(u["id"], "2026-01-01", "Shiva", "Om", 5)
        real_store.create_email_verification_token(u["id"])
        real_store.create_password_reset_token(u["id"])
        real_store.create_password_reset_token(other["id"])

        assert real_store.delete_user(u["id"]) is True
        assert real_store.delete_user(u["id"]) is False  # already gone
        assert real_store.get_user_by_id(u["id"]) is None
        with real_store.connection() as conn:
            cur = conn.cursor()
            for table, col in (("mood_checkins", "user_id"), ("chat_sessions", "user_id"), ("journal_entries", "user_id"),
                               ("jaap_sessions", "user_id"), ("email_verification_tokens", "user_id"),
                               ("password_reset_tokens", "user_id")):
                cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = %s", (u["id"],))
                assert cur.fetchone()[0] == 0, table
            cur.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = %s", (sid,))
            assert cur.fetchone()[0] == 0
        assert [m["content"] for m in real_store.get_session_messages(osid)] == ["stay"]
        assert len(self.rows(real_store, "password_reset_tokens", other["id"])) == 1


def test_upgrade_keeps_existing_users_verified(tmp_path):
    """An old database (users without email_verified) must come out of init_db with those users verified."""
    args = {"host": os.environ["MYSQL_TEST_HOST"], "port": int(os.getenv("MYSQL_TEST_PORT", "3306")),
            "user": os.getenv("MYSQL_TEST_USER", "root"), "password": os.getenv("MYSQL_TEST_PASSWORD", "")}
    db = "connect_upgrade_test"
    with mysql.connector.connect(**args) as conn:
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS {db}")
        cur.execute(f"CREATE DATABASE {db}")
        cur.execute(f"USE {db}")
        cur.execute("""CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE,
                       display_name VARCHAR(120) NOT NULL DEFAULT 'Friend', password_hash VARCHAR(255) NOT NULL,
                       daily_goal VARCHAR(40) NOT NULL DEFAULT 'grounding',
                       created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                       updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)""")
        cur.execute("INSERT INTO users (email, password_hash) VALUES ('legacy@example.com', 'x')")
        conn.commit()
    try:
        initialize(args, db)
        initialize(args, db)  # idempotent
        with mysql.connector.connect(**args, database=db) as conn:
            cur = conn.cursor()
            cur.execute("SELECT email_verified FROM users WHERE email = 'legacy@example.com'")
            assert cur.fetchone()[0] == 1
            cur.execute("INSERT INTO users (email, password_hash) VALUES ('fresh@example.com', 'x')")
            cur.execute("SELECT email_verified FROM users WHERE email = 'fresh@example.com'")
            # (After the upgrade the column default is TRUE; app code always inserts the flag explicitly.)
            assert cur.fetchone()[0] in (0, 1)
    finally:
        with mysql.connector.connect(**args) as conn:
            conn.cursor().execute(f"DROP DATABASE IF EXISTS {db}")


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
    assert res.status_code == 200  # "check your email": no session yet
    assert client.get("/").status_code == 302
    user = real_store.get_user_by_email(email)
    assert user["email_verified"] == 0
    assert real_store.verify_email_token(real_store.create_email_verification_token(user["id"]))
    assert client.post("/login", data={"email": email, "password": "password123"}).status_code == 302

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
            assert cur.fetchone()[0] == 8

    def test_unusable_database_gives_a_clear_error(self, admin):
        with pytest.raises(RuntimeError, match="not usable"):
            initialize(self.limited(admin), "connect_does_not_exist_test")
