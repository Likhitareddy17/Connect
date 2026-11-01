"""In-memory stand-in for ConnectStore, so route tests need no database.

Behaviour that matters (latest-N history, ownership, cascade delete) mirrors
the real MySQL implementation; tests/test_store_integration.py runs the same
scenarios against a real database to keep the two from drifting apart.
"""
from __future__ import annotations

import itertools
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from store import MAX_TOKEN_HOURS, MAX_TOKEN_LENGTH, hash_token

ALLOWED_RELIGIONS = {"hindu", "islam", "christianity", "sikh", "atheist"}


class FakeStore:
    def __init__(self):
        self._ids = itertools.count(1)
        self.users: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {}      # id -> {user_id, summary}
        self.messages: dict[str, list[dict]] = {}  # session_id -> [{role, content}]
        self.moods: list[tuple[int, str]] = []
        self.journals: list[dict] = []
        # kind ("verify" / "reset") -> {token_hash: {"user_id", "expires_at", "used"}}
        self.tokens: dict[str, dict[str, dict]] = {"verify": {}, "reset": {}}

    # users
    def create_user(self, email, display_name, password, religion="atheist", email_verified=True):
        email = email.lower().strip()
        if any(u["email"] == email for u in self.users.values()):
            return None
        uid = next(self._ids)
        user = {
            "id": uid, "email": email, "display_name": display_name.strip() or "Friend",
            "password_hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode(),
            "daily_goal": "grounding",
            "religion": religion if religion in ALLOWED_RELIGIONS else "atheist",
            "email_verified": bool(email_verified),
        }
        self.users[uid] = user
        return {k: v for k, v in user.items() if k != "password_hash"}

    def get_user_by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email.lower().strip()), None)

    def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    def verify_password(self, user, password):
        return bcrypt.checkpw(password.encode(), user["password_hash"].encode())

    # e-mail tokens (same contract as ConnectStore: hashed at rest, single use, expiring, newest wins)
    def _issue(self, kind, user_id, hours):
        if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_TOKEN_HOURS:
            raise ValueError("expires_hours out of range")
        token = secrets.token_urlsafe(32)
        for rec in self.tokens[kind].values():
            if rec["user_id"] == user_id:
                rec["used"] = True
        self.tokens[kind][hash_token(token)] = {
            "user_id": user_id, "used": False,
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=hours),
        }
        return token

    def _claim(self, kind, token):
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        rec = self.tokens[kind].get(hash_token(token))
        if not rec or rec["used"] or rec["expires_at"] <= datetime.now(timezone.utc):
            return None
        rec["used"] = True
        return rec["user_id"]

    def create_email_verification_token(self, user_id, expires_hours=24):
        return self._issue("verify", user_id, expires_hours)

    def verify_email_token(self, token):
        user_id = self._claim("verify", token)
        if user_id is None:
            return False
        self.users[user_id]["email_verified"] = True
        return True

    def create_password_reset_token(self, user_id, expires_hours=1):
        return self._issue("reset", user_id, expires_hours)

    def reset_password_with_token(self, token, new_password):
        user_id = self._claim("reset", token)
        if user_id is None:
            return False
        self.users[user_id]["password_hash"] = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt(4)).decode()
        self.users[user_id]["email_verified"] = True
        for rec in self.tokens["reset"].values():
            if rec["user_id"] == user_id:
                rec["used"] = True
        return True

    def delete_user(self, user_id):
        """Mirrors ON DELETE CASCADE: everything the user owns goes with them."""
        if user_id not in self.users:
            return False
        del self.users[user_id]
        for sid in [sid for sid, s in self.sessions.items() if s["user_id"] == user_id]:
            del self.sessions[sid]
            self.messages.pop(sid, None)
        self.journals = [j for j in self.journals if j["user_id"] != user_id]
        self.moods = [(uid, m) for uid, m in self.moods if uid != user_id]
        for records in self.tokens.values():
            for digest in [d for d, rec in records.items() if rec["user_id"] == user_id]:
                del records[digest]
        return True

    def update_goal(self, user_id, goal):
        self.users[user_id]["daily_goal"] = goal

    def update_religion(self, user_id, religion):
        self.users[user_id]["religion"] = religion

    def update_display_name(self, user_id, name):
        self.users[user_id]["display_name"] = name

    # moods
    def save_mood(self, user_id, mood):
        self.moods.append((user_id, mood))

    def latest_mood(self, user_id):
        mine = [m for uid, m in self.moods if uid == user_id]
        return mine[-1] if mine else None

    def mood_history(self, user_id, days=7):
        return [{"mood": m, "created_at": datetime.now()} for uid, m in self.moods if uid == user_id]

    # chat
    def ensure_chat_session(self, user_id, session_id):
        self.sessions.setdefault(session_id, {"user_id": user_id, "summary": ""})
        self.messages.setdefault(session_id, [])
        return self.sessions[session_id]["user_id"] == user_id

    def owns_chat_session(self, user_id, session_id):
        return session_id in self.sessions and self.sessions[session_id]["user_id"] == user_id

    def save_chat_message(self, session_id, role, content):
        self.messages[session_id].append({"role": role, "content": content})

    def get_session_messages(self, session_id, limit=200):
        return list(self.messages.get(session_id, []))[:limit]

    def get_recent_messages(self, session_id, limit=6):
        return list(self.messages.get(session_id, []))[-limit:]

    def count_session_messages(self, session_id):
        return len(self.messages.get(session_id, []))

    def get_session_summary(self, session_id):
        return self.sessions.get(session_id, {}).get("summary", "")

    def set_session_summary(self, session_id, summary):
        self.sessions[session_id]["summary"] = summary

    def list_user_sessions(self, user_id):
        return [{"id": sid, "created_at": datetime.now(), "snippet": ""}
                for sid, s in self.sessions.items() if s["user_id"] == user_id]

    def delete_chat_session(self, user_id, session_id):
        if not self.owns_chat_session(user_id, session_id):
            return False
        del self.sessions[session_id]
        del self.messages[session_id]
        self.journals = [j for j in self.journals if j["session_id"] != session_id]
        return True

    def ping(self):
        return True

    # journals
    def save_journal(self, user_id, mood, detected_state, entry_text, suggestion, session_id=None):
        self.journals.append({
            "user_id": user_id, "mood": mood, "detected_state": detected_state,
            "entry_text": entry_text, "suggestion": suggestion, "session_id": session_id,
            "created_at": datetime.now(),
        })

    def recent_journals(self, user_id, limit=5):
        return [j for j in reversed(self.journals) if j["user_id"] == user_id][:limit]

    # jaap
    def get_jaap_count(self, user_id, practice_date, mantra):
        return 0

    def save_jaap_count(self, user_id, practice_date, deity, mantra, count):
        return int(count)
