"""
ConnectStore — MySQL data layer for CONNECT.
All writes use parameterised queries. Connection pooling via mysql-connector.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
from contextlib import contextmanager

import bcrypt
import mysql.connector
from mysql.connector.pooling import MySQLConnectionPool

logger = logging.getLogger(__name__)

ALLOWED_GOALS = {"grounding", "clarity", "energy", "gratitude"}
ALLOWED_RELIGIONS = {"hindu", "islam", "christianity", "sikh", "atheist"}

# Single-use e-mail tokens. Table names are interpolated into SQL, so they come ONLY from this fixed map.
_TOKEN_TABLES = {"verify": "email_verification_tokens", "reset": "password_reset_tokens"}
MAX_TOKEN_HOURS = 168  # a week: the longest any e-mailed link may live
MAX_TOKEN_LENGTH = 200  # real tokens are 43 characters; refuse absurd input before hashing it


def hash_token(token: str) -> str:
    """SHA-256 hex digest. Only this is stored; the raw token exists in the e-mailed link alone."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


    def create_user(
        self,
        email: str,
        display_name: str,
        password: str,
        religion: str = "atheist",
        email_verified: bool = True,
    ) -> dict | None:
        email = email.lower().strip()
        display_name = display_name.strip()[:120] or "Friend"
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        safe_religion = religion if religion in ALLOWED_RELIGIONS else "atheist"

        try:
            with self.connection() as conn:
                cursor = conn.cursor(dictionary=True)
                cursor.execute(
                    """INSERT INTO users
                       (email, display_name, password_hash, daily_goal, religion, email_verified)
                       VALUES (%s, %s, %s, 'grounding', %s, %s)""",
                    (email, display_name, pw_hash, safe_religion, bool(email_verified)),
                )

                return {
                    "id": cursor.lastrowid,
                    "email": email,
                    "display_name": display_name,
                    "daily_goal": "grounding",
                    "religion": safe_religion,
                    "email_verified": bool(email_verified),
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

    # ── E-mail verification / password reset tokens ─────────────────────────
    #
    # Tokens are 256-bit random values; only their SHA-256 is stored. Expiry is computed by MySQL
    # (UTC_TIMESTAMP) so the app server's clock never matters. A token is *claimed* with one atomic
    # UPDATE ... WHERE used_at IS NULL AND expires_at > now: two concurrent requests cannot both win,
    # because the second UPDATE blocks on the row lock and then matches zero rows.

    def _issue_token(self, kind: str, user_id: int, hours: int) -> str:
        table = _TOKEN_TABLES[kind]
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_TOKEN_HOURS:
            raise ValueError(f"expires_hours must be a whole number between 1 and {MAX_TOKEN_HOURS}")
        token = secrets.token_urlsafe(32)
        with self.connection() as conn:
            cursor = conn.cursor()
            # Only the newest link works: retire any earlier unused ones.
            cursor.execute(
                f"UPDATE {table} SET used_at = UTC_TIMESTAMP() WHERE user_id = %s AND used_at IS NULL",
                (user_id,),
            )
            cursor.execute(
                f"""INSERT INTO {table} (user_id, token_hash, expires_at)
                    VALUES (%s, %s, DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s HOUR))""",
                (user_id, hash_token(token), hours),
            )
        return token

    @staticmethod
    def _claim_token(cursor, kind: str, token: str) -> int | None:
        """Atomically spend a valid token inside the caller's transaction; returns its user_id or None."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        table = _TOKEN_TABLES[kind]
        digest = hash_token(token)
        cursor.execute(
            f"""UPDATE {table} SET used_at = UTC_TIMESTAMP()
                WHERE token_hash = %s AND used_at IS NULL AND expires_at > UTC_TIMESTAMP()""",
            (digest,),
        )
        if cursor.rowcount != 1:
            return None
        cursor.execute(f"SELECT user_id FROM {table} WHERE token_hash = %s", (digest,))
        row = cursor.fetchone()
        return row["user_id"] if row else None

    def create_email_verification_token(self, user_id: int, expires_hours: int = 24) -> str:
        return self._issue_token("verify", user_id, expires_hours)

    def verify_email_token(self, token: str) -> bool:
        """Spend a valid verification token and mark its user verified, in one transaction."""
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            user_id = self._claim_token(cursor, "verify", token)
            if user_id is None:
                return False
            cursor.execute("UPDATE users SET email_verified = TRUE WHERE id = %s", (user_id,))
            return True

    def create_password_reset_token(self, user_id: int, expires_hours: int = 1) -> str:
        return self._issue_token("reset", user_id, expires_hours)

    def reset_password_with_token(self, token: str, new_password: str) -> bool:
        """Spend a valid reset token and set the new password.

        Receiving the token proves control of the mailbox, so the address is also marked verified.
        Changing password_hash signs out every existing session (see app.User.get_id).
        """
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()  # slow: before the lock
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=True)
            user_id = self._claim_token(cursor, "reset", token)
            if user_id is None:
                return False
            cursor.execute(
                "UPDATE users SET password_hash = %s, email_verified = TRUE WHERE id = %s",
                (pw_hash, user_id),
            )
            # Any other outstanding reset links for this user are now void.
            cursor.execute(
                "UPDATE password_reset_tokens SET used_at = UTC_TIMESTAMP() WHERE user_id = %s AND used_at IS NULL",
                (user_id,),
            )
            return True

    def delete_user(self, user_id: int) -> bool:
        """Delete the account. Every owned row goes via ON DELETE CASCADE (see schema.sql)."""
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE id = %s", (user_id,))
            return cursor.rowcount > 0

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
