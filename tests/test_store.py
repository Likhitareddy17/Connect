"""
Unit tests for ConnectStore — uses a mock DB connection pool to avoid MySQL dependency.
"""
import pytest
from unittest.mock import MagicMock, patch, call


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
