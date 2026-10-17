"""
ConnectStore — MySQL data layer for CONNECT.
All writes use parameterised queries. Connection pooling via mysql-connector.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

import bcrypt
import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool

logger = logging.getLogger(__name__)

ALLOWED_GOALS = {"grounding", "clarity", "energy", "gratitude"}
ALLOWED_RELIGIONS = {"hindu", "islam", "christianity", "sikh", "atheist"}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def ssl_options() -> dict:
    """TLS settings for managed MySQL hosts, most of which require encrypted connections.

    By default the driver negotiates TLS when the server offers it (encrypted, but the server
    certificate is not verified). For verified TLS set MYSQL_SSL_CA to the provider's CA
    certificate file and MYSQL_SSL_VERIFY=1.
    """
    options: dict = {}
    ca = os.getenv("MYSQL_SSL_CA")
    if ca:
        options["ssl_ca"] = ca
    if _env_flag("MYSQL_SSL_VERIFY"):
        if not ca:
            raise ValueError("MYSQL_SSL_VERIFY=1 needs MYSQL_SSL_CA (path to the CA certificate)")
        options["ssl_verify_cert"] = True
    if _env_flag("MYSQL_SSL_DISABLED"):
        options["ssl_disabled"] = True
    return options


def connection_config(database: str | None = None) -> dict:
    """Connection settings from the environment; pass ``database=None`` to connect without one."""
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", ""),
        **ssl_options(),
    }
    if database:
        config["database"] = database
    return config


class ConnectStore:
    def __init__(self):
        self._pool = None

    def _get_pool(self):
        """Lazy-initialise the connection pool on first use, not at import time."""
        if self._pool is None:
            self._pool = MySQLConnectionPool(
                pool_name="connect_pool",
                pool_size=int(os.getenv("MYSQL_POOL_SIZE", "5")),
                **connection_config(os.getenv("MYSQL_DATABASE", "connect_mvp")),
            )
        return self._pool

    @contextmanager
    def connection(self):
        conn = self._get_pool().get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Auth ────────────────────────────────────────────────────────────────

    def create_user(self, email: str, display_name: str, password: str, religion: str = "atheist") -> dict | None:
        """Hash password and insert a new user. Returns user dict or None on duplicate email."""
        email = email.lower().strip()
        display_name = display_name.strip()[:120] or "Friend"
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        safe_religion = religion if religion in ALLOWED_RELIGIONS else "atheist"
        try:
            with self.connection() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    """INSERT INTO users (email, display_name, password_hash, daily_goal, religion)
                       VALUES (%s, %s, %s, 'grounding', %s)""",
                    (email, display_name, pw_hash, safe_religion),
                )
                return {
                    "id": cursor.lastrowid, "email": email, "display_name": display_name,
                    "daily_goal": "grounding", "religion": safe_religion,
                }
        except mysql.connector.IntegrityError:
            return None

    def get_user_by_email(self, email: str) -> dict | None:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE email = %s", (email.lower().strip(),))
            return cursor.fetchone()

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            return cursor.fetchone()

    def verify_password(self, user: dict, password: str) -> bool:
        return bcrypt.checkpw(password.encode(), user["password_hash"].encode())

    # ── Profile ─────────────────────────────────────────────────────────────

    def update_goal(self, user_id: int, goal: str) -> None:
        safe_goal = goal if goal in ALLOWED_GOALS else "grounding"
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET daily_goal = %s WHERE id = %s",
                (safe_goal, user_id),
            )

    def update_religion(self, user_id: int, religion: str) -> None:
        safe_religion = religion if religion in ALLOWED_RELIGIONS else "atheist"
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET religion = %s WHERE id = %s",
                (safe_religion, user_id),
            )

    def update_display_name(self, user_id: int, name: str) -> None:
        safe_name = name.strip()[:120] or "Friend"
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET display_name = %s WHERE id = %s",
                (safe_name, user_id),
            )

    # ── Mood ────────────────────────────────────────────────────────────────

    def save_mood(self, user_id: int, mood: str) -> None:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO mood_checkins (user_id, mood) VALUES (%s, %s)",
                (user_id, mood),
            )

    def latest_mood(self, user_id: int) -> str | None:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT mood FROM mood_checkins
                   WHERE user_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (user_id,),
            )
            row = cursor.fetchone()
            return row["mood"] if row else None

    def mood_history(self, user_id: int, days: int = 7) -> list[dict]:
        """Daily mood counts for the past N days (defaults to 7 for weekly dashboard view)."""
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT DATE(created_at) AS day, mood, COUNT(*) AS count
                   FROM mood_checkins
                   WHERE user_id = %s AND created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                   GROUP BY day, mood
                   ORDER BY day ASC""",
                (user_id, days),
            )
            return cursor.fetchall()


    # ── Chat threads ────────────────────────────────────────────────────────

    def ensure_chat_session(self, user_id: int, session_id: str) -> bool:
        """Create the thread if new. Returns True only if it belongs to ``user_id``.

        ``INSERT IGNORE`` alone would silently "succeed" for a thread that
        already belongs to someone else, so ownership is verified explicitly.
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT IGNORE INTO chat_sessions (id, user_id) VALUES (%s, %s)",
                (session_id, user_id),
            )
            cursor.execute("SELECT user_id FROM chat_sessions WHERE id = %s", (session_id,))
            row = cursor.fetchone()
            return bool(row) and row[0] == user_id

    def owns_chat_session(self, user_id: int, session_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM chat_sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            return cursor.fetchone() is not None

    def save_chat_message(self, session_id: str, role: str, content: str) -> None:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO chat_messages (session_id, role, content) VALUES (%s, %s, %s)",
                (session_id, role, content),
            )

    def get_session_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        """Full thread for display, oldest first."""
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT role, content FROM chat_messages
                   WHERE session_id = %s ORDER BY id ASC LIMIT %s""",
                (session_id, limit),
            )
            return cursor.fetchall()

    def get_recent_messages(self, session_id: str, limit: int = 6) -> list[dict]:
        """The *latest* ``limit`` messages, returned oldest-first.

        (Selecting ``ORDER BY ... ASC LIMIT n`` would return the *first* n
        messages of the thread instead — see tests/test_chat_api.py.)
        """
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT role, content FROM (
                       SELECT id, role, content FROM chat_messages
                       WHERE session_id = %s ORDER BY id DESC LIMIT %s
                   ) latest ORDER BY id ASC""",
                (session_id, limit),
            )
            return cursor.fetchall()

    def count_session_messages(self, session_id: str) -> int:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = %s", (session_id,))
            return cursor.fetchone()[0]

    def get_session_summary(self, session_id: str) -> str:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT summary FROM chat_sessions WHERE id = %s", (session_id,))
            row = cursor.fetchone()
            return (row[0] or "") if row else ""

    def set_session_summary(self, session_id: str, summary: str) -> None:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE chat_sessions SET summary = %s WHERE id = %s", (summary, session_id))

    def list_user_sessions(self, user_id: int) -> list[dict]:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT s.id, s.created_at,
                          (SELECT content FROM chat_messages WHERE session_id = s.id
                           ORDER BY id ASC LIMIT 1) AS snippet
                   FROM chat_sessions s
                   WHERE s.user_id = %s ORDER BY s.created_at DESC""",
                (user_id,),
            )
            return cursor.fetchall()

    def delete_chat_session(self, user_id: int, session_id: str) -> bool:
        """Delete a thread the user owns. Messages and its reflection card go via ON DELETE CASCADE.

        The ``user_id`` condition is the only thing deleted-by — nothing is
        removed for threads that belong to someone else.
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM chat_sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            return cursor.rowcount > 0

    def ping(self) -> bool:
        """Cheap liveness check for /readyz."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)

    # ── Journal ─────────────────────────────────────────────────────────────

    def save_journal(
        self,
        user_id: int,
        mood: str,
        detected_state: str,
        entry_text: str,
        suggestion: str,
        session_id: str | None = None,
    ) -> None:
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO journal_entries
                     (user_id, mood, detected_state, entry_text, suggestion, session_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (user_id, mood, detected_state, entry_text[:2000], suggestion, session_id),
            )

    def recent_journals(self, user_id: int, limit: int = 5) -> list[dict]:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT mood, detected_state, entry_text, suggestion, created_at, session_id
                   FROM journal_entries
                   WHERE user_id = %s
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (user_id, limit),
            )
            return cursor.fetchall()

    # ── Jaap ────────────────────────────────────────────────────────────────

    def get_jaap_count(self, user_id: int, practice_date, mantra: str) -> int:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """SELECT count FROM jaap_sessions
                   WHERE user_id = %s AND practice_date = %s AND mantra = %s""",
                (user_id, practice_date, mantra),
            )
            row = cursor.fetchone()
            return row["count"] if row else 0

    def save_jaap_count(
        self, user_id: int, practice_date, deity: str, mantra: str, count: int
    ) -> int:
        try:
            safe_count = int(count)
        except (TypeError, ValueError):
            safe_count = 0
        safe_count = max(0, min(safe_count, 100_000))
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO jaap_sessions
                     (user_id, practice_date, deity, mantra, count)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE deity = VALUES(deity), count = VALUES(count)""",
                (user_id, practice_date, deity, mantra, safe_count),
            )
        return safe_count
