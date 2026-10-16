"""In-memory stand-in for ConnectStore, so route tests need no database.

Behaviour that matters (latest-N history, ownership, cascade delete) mirrors
the real MySQL implementation; tests/test_store_integration.py runs the same
scenarios against a real database to keep the two from drifting apart.
"""
from __future__ import annotations

import itertools
from datetime import datetime

import bcrypt

ALLOWED_RELIGIONS = {"hindu", "islam", "christianity", "sikh", "atheist"}


class FakeStore:
    def __init__(self):
        self._ids = itertools.count(1)
        self.users: dict[int, dict] = {}
        self.sessions: dict[str, dict] = {}      # id -> {user_id, summary}
        self.messages: dict[str, list[dict]] = {}  # session_id -> [{role, content}]
        self.moods: list[tuple[int, str]] = []
        self.journals: list[dict] = []

    # users
    def create_user(self, email, display_name, password, religion="atheist"):
        email = email.lower().strip()
        if any(u["email"] == email for u in self.users.values()):
            return None
        uid = next(self._ids)
        user = {
            "id": uid, "email": email, "display_name": display_name.strip() or "Friend",
            "password_hash": bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode(),
            "daily_goal": "grounding",
            "religion": religion if religion in ALLOWED_RELIGIONS else "atheist",
        }
        self.users[uid] = user
        return {k: v for k, v in user.items() if k != "password_hash"}

    def get_user_by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email.lower().strip()), None)

    def get_user_by_id(self, user_id):
        return self.users.get(user_id)

    def verify_password(self, user, password):
        return bcrypt.checkpw(password.encode(), user["password_hash"].encode())

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
