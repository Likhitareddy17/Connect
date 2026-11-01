"""
Unit tests for ConnectStore — uses a mock DB connection pool to avoid MySQL dependency.
"""
import hashlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_pool(monkeypatch):
    """Replace MySQLConnectionPool so no real DB is needed."""
    pool = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    pool.get_connection.return_value = conn
    monkeypatch.setattr("mysql.connector.pooling.MySQLConnectionPool.__init__", lambda *a, **kw: None)
    monkeypatch.setattr("mysql.connector.pooling.MySQLConnectionPool.get_connection", lambda self: conn)
    return pool, conn, cursor


def _make_store(conn):
    """Helper: return a ConnectStore whose _get_pool() returns a mock pool with conn."""
    from store import ConnectStore
    s = ConnectStore.__new__(ConnectStore)
    s._pool = None
    pool_mock = MagicMock()
    pool_mock.get_connection.return_value = conn
    s._pool = pool_mock
    return s


class TestConnectStoreGoalValidation:
    def test_allowed_goal_is_saved(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)
        store.update_goal(1, "clarity")
        args = cursor.execute.call_args[0]
        assert "clarity" in args[1]

    def test_invalid_goal_falls_back_to_grounding(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)
        store.update_goal(1, "invalid-goal")
        args = cursor.execute.call_args[0]
        assert "grounding" in args[1]


class TestJaapCountBounds:
    def test_count_clamped_to_max(self, mock_pool):
        _, conn, _ = mock_pool
        store = _make_store(conn)
        result = store.save_jaap_count(1, "2025-01-01", "Shiva", "Om Namah Shivaya", 999_999)
        assert result == 100_000

    def test_count_clamped_to_zero(self, mock_pool):
        _, conn, _ = mock_pool
        store = _make_store(conn)
        result = store.save_jaap_count(1, "2025-01-01", "Shiva", "Om Namah Shivaya", -10)
        assert result == 0

    def test_invalid_count_type_becomes_zero(self, mock_pool):
        _, conn, _ = mock_pool
        store = _make_store(conn)
        result = store.save_jaap_count(1, "2025-01-01", "Shiva", "Om Namah Shivaya", "not-a-number")
        assert result == 0


class TestEmailVerificationTokens:

    def test_create_token_returns_raw_token_and_stores_hash(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        token = store.create_email_verification_token(1)

        assert isinstance(token, str)
        assert len(token) > 20

        # The final INSERT stores the hash, never the raw token.
        insert_args = cursor.execute.call_args_list[-1][0]
        stored_hash = insert_args[1][1]

        assert stored_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert stored_hash != token

    def test_create_token_rejects_non_positive_expiry(self, mock_pool):
        _, conn, _ = mock_pool
        store = _make_store(conn)

        with pytest.raises(ValueError):
            store.create_email_verification_token(1, expires_hours=0)

        with pytest.raises(ValueError):
            store.create_email_verification_token(1, expires_hours=-1)

    def test_create_token_invalidates_previous_unused_tokens(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        store.create_email_verification_token(1)

        # First query invalidates older unused tokens.
        first_query = cursor.execute.call_args_list[0][0]
        assert "UPDATE email_verification_tokens" in first_query[0]
        assert "used_at IS NULL" in first_query[0]

    def test_verify_valid_token_returns_true(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        cursor.fetchone.return_value = {"id": 10, "user_id": 1}
        cursor.rowcount = 1

        result = store.verify_email_token("valid-test-token")

        assert result is True

        queries = [call[0][0] for call in cursor.execute.call_args_list]

        assert any("SELECT" in query for query in queries)
        assert any("UPDATE users" in query and "email_verified" in query for query in queries)

    def test_verify_invalid_token_returns_false(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        cursor.fetchone.return_value = None

        assert store.verify_email_token("invalid-token") is False

    def test_verify_empty_token_returns_false(self, mock_pool):
        _, conn, _ = mock_pool
        store = _make_store(conn)

        assert store.verify_email_token("") is False
        assert store.verify_email_token(None) is False

    def test_verify_expired_token_returns_false(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        # The database query excludes expired tokens.
        cursor.fetchone.return_value = None

        result = store.verify_email_token("expired-token")

        assert result is False

        query = cursor.execute.call_args[0][0]
        assert "expires_at > UTC_TIMESTAMP()" in query

    def test_verify_reused_token_returns_false(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        # First use: token is valid.
        cursor.fetchone.return_value = {"id": 10, "user_id": 1}
        cursor.rowcount = 1

        assert store.verify_email_token("single-use-token") is True

        # Second use: the token is no longer available.
        cursor.fetchone.return_value = None

        assert store.verify_email_token("single-use-token") is False

    def test_verify_token_rejects_failed_conditional_update(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)

        cursor.fetchone.return_value = {"id": 10, "user_id": 1}
        cursor.rowcount = 0

        assert store.verify_email_token("already-used-token") is False

    # ── additional checks ──────────────────────────────────────────────────

    @pytest.mark.parametrize("hours", [169, 1.5, "24", None, True])
    def test_other_invalid_expiries_are_rejected_before_any_sql(self, mock_pool, hours):
        _, conn, cursor = mock_pool
        with pytest.raises(ValueError):
            _make_store(conn).create_email_verification_token(7, expires_hours=hours)
        cursor.execute.assert_not_called()

    def test_every_statement_is_parameterised(self, mock_pool):
        _, conn, cursor = mock_pool
        store = _make_store(conn)
        token = store.create_password_reset_token(7)
        cursor.rowcount = 1
        cursor.fetchone.return_value = {"user_id": 7}
        store.reset_password_with_token(token, "password123")
        store.delete_user(7)
        for call in cursor.execute.call_args_list:
            sql, params = call[0]
            assert token not in sql and "password123" not in sql and "%s" in sql

    def test_valid_token_marks_user_verified_in_one_transaction(self, mock_pool):
        _, conn, cursor = mock_pool
        cursor.rowcount = 1
        cursor.fetchone.return_value = {"user_id": 42}
        assert _make_store(conn).verify_email_token("some-token") is True
        sqls = [c[0][0] for c in cursor.execute.call_args_list]
        assert "used_at IS NULL" in sqls[0] and "expires_at > UTC_TIMESTAMP()" in sqls[0]
        assert "UPDATE users SET email_verified = TRUE" in sqls[-1]
        assert conn.commit.call_count == 1 and conn.rollback.call_count == 0

    def test_unknown_or_spent_token_never_touches_users(self, mock_pool):
        _, conn, cursor = mock_pool
        cursor.rowcount = 0
        assert _make_store(conn).verify_email_token("some-token") is False
        assert not any("UPDATE users" in c[0][0] for c in cursor.execute.call_args_list)

    def test_oversized_token_never_queries(self, mock_pool):
        _, conn, cursor = mock_pool
        assert _make_store(conn).verify_email_token("x" * 1000) is False
        cursor.execute.assert_not_called()

    def test_unknown_user_id_surfaces_as_a_database_error_and_rolls_back(self, mock_pool):
        import mysql.connector
        _, conn, cursor = mock_pool
        cursor.execute.side_effect = [None, mysql.connector.IntegrityError("FK fails")]
        with pytest.raises(mysql.connector.IntegrityError):
            _make_store(conn).create_email_verification_token(999_999)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
