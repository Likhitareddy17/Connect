"""
CONNECT — Flask application entry point.
State-tracked multi-turn local conversational pipeline.
"""
from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from services.grounding import ALLOWED_MOODS, recommend_practices
from services.journal_analysis import generate_chat_response
from services.religion_providers import ALLOWED_RELIGIONS, RELIGION_LABELS, get_provider
from services.spiritual_api import spiritual_api
from store import ConnectStore

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_mapping(
    SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=64 * 1024,
)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "60 per hour"],
    storage_uri=os.getenv("LIMITER_STORAGE_URI", "memory://"),
)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."

store = ConnectStore()

MOOD_OPTIONS = ALLOWED_MOODS
MAX_JOURNAL_LENGTH = 2000


class User(UserMixin):
    def __init__(self, data: dict):
        self._data = data

    def get_id(self) -> str:
        return str(self._data["id"])

    @property
    def id(self) -> int:
        return self._data["id"]

    @property
    def email(self) -> str:
        return self._data["email"]

    @property
    def display_name(self) -> str:
        return self._data["display_name"]

    @property
    def daily_goal(self) -> str:
        return self._data["daily_goal"]

    @property
    def religion(self) -> str:
        return self._data.get("religion") or "atheist"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    data = store.get_user_by_id(int(user_id))
    return User(data) if data else None


# ── Helpers ────────────────────────────────────────────────────────────────

def build_guidance(user: User) -> dict:
    mood = store.latest_mood(user.id) or "calm"
    daily = spiritual_api.today()
    practices = recommend_practices(mood=mood, goal=user.daily_goal, limit=3)
    return {
        "focus": daily["focus"],
        "tone": daily["tone"],
        "reflection": daily["reflection"],
        "action": practices[0] if practices else "Take a mindful pause.",
        "mood": mood,
    }


# ── Auth Routes ────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        display_name = request.form.get("display_name", "").strip() or "Friend"
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        religion = request.form.get("religion", "atheist")
        if not email or "@" not in email:
            error = "A valid email is required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            user_data = store.create_user(email, display_name, password, religion=religion)
            if user_data is None:
                error = "An account with that email already exists."
            else:
                login_user(User(user_data))
                return redirect(url_for("home"))
    return render_template("register.html", error=error, religion_options=RELIGION_LABELS)


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user_data = store.get_user_by_email(email)
        if user_data and store.verify_password(user_data, password):
            login_user(User(user_data), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("home"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ── Main Module Views ──────────────────────────────────────────────────────

@app.route("/")
@login_required
def home():
    mood = store.latest_mood(current_user.id) or "calm"
    practices = recommend_practices(mood=mood, goal=current_user.daily_goal, limit=3)
    return render_template(
        "home.html",
        profile=current_user,
        guidance=build_guidance(current_user),
        practices=practices,
    )


@app.route("/spiritual")
@login_required
def spiritual():
    provider = get_provider(current_user.religion)
    today = date.today()
    daily = provider.get_day(today)
    upcoming = provider.upcoming(today, limit=5)

    jaap_count = 0
    if daily.get("jaap_allowed"):
        jaap_count = store.get_jaap_count(current_user.id, today, daily["extra"]["mantra"])

    return render_template(
        "spiritual.html",
        daily=daily,
        upcoming=upcoming,
        jaap_count=jaap_count,
        religion=current_user.religion,
        religion_label=RELIGION_LABELS.get(current_user.religion, "Atheist / no religion"),
    )


@app.get("/api/spiritual/day")
@login_required
def api_spiritual_day():
    """Returns the day-detail payload for a clicked calendar date, for the given user's religion."""
    date_str = request.args.get("date", "")
    try:
        target = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Invalid date, expected YYYY-MM-DD."}), 400

    provider = get_provider(current_user.religion)
    daily = provider.get_day(target)

    jaap_count = 0
    if daily.get("jaap_allowed"):
        jaap_count = store.get_jaap_count(current_user.id, target, daily["extra"]["mantra"])

    return jsonify({"daily": daily, "jaap_count": jaap_count})


@app.post("/spiritual/jaap")
@login_required
@limiter.limit("120 per minute")
def spiritual_jaap():
    if current_user.religion != "hindu":
        return jsonify({"error": "Jaap counter is only available for the Hindu daily practice."}), 400
    daily = spiritual_api.today()
    payload = request.get_json(silent=True) or {}
    count = store.save_jaap_count(
        user_id=current_user.id,
        practice_date=date.today(),
        deity=daily["deity"],
        mantra=daily["mantra"],
        count=payload.get("count", 0),
    )
    return jsonify({"count": count})


@app.route("/mental")
@login_required
def mental():
    mood = store.latest_mood(current_user.id) or "calm"
    practices = recommend_practices(mood=mood, goal="clarity", limit=4)
    return render_template("mental.html", practices=practices)

@app.route("/emotional", methods=["GET"])
@login_required
def emotional():
    """Renders workspace and loads active or previous session threads.

    Pass ?new=1 to force-start a brand-new thread, even if one is already
    active — otherwise a plain visit just keeps resuming whatever thread
    was last used.
    """
    start_new = request.args.get("new") == "1"
    session_id = None if start_new else (request.args.get("session_id") or session.get("chat_session_active"))

    if not session_id:
        session_id = str(uuid.uuid4())
        session["chat_session_active"] = session_id
        store.create_chat_session(current_user.id, session_id)
    else:
        session["chat_session_active"] = session_id

    active_messages = store.get_session_messages(session_id)
    all_sessions = store.list_user_sessions(current_user.id)
    entries = store.recent_journals(current_user.id)

    return render_template(
        "emotional.html",
        active_session_id=session_id,
        active_messages=active_messages,
        all_sessions=all_sessions,
        entries=entries,
    )


@app.post("/api/chat/message")
@login_required
@limiter.limit("15 per minute")
def api_chat_message():
    payload = request.get_json(silent=True) or {}
    user_message = payload.get("message", "").strip()[:MAX_JOURNAL_LENGTH]
    session_id = session.get("chat_session_active") or str(uuid.uuid4())

    if not user_message:
        return jsonify({"error": "Reflection text cannot be empty."}), 400

    # Persist user message to DB
    store.create_chat_session(current_user.id, session_id)
    store.save_chat_message(session_id, "user", user_message)

    # Fetch context history for LLM — its length also tells us, unambiguously,
    # whether this is the first message in THIS thread (no flag needed).
    db_history = store.get_session_messages(session_id, limit=6)
    is_first_message = len(db_history) == 1

    # Rolling summary must be scoped per-thread, or continuing an old chat
    # picks up whatever summary was last left behind by a *different* chat.
    thread_summaries = session.get("chat_summaries", {})
    rolling_summary = thread_summaries.get(session_id, "")

    try:
        ai_reply, detected_mood, new_summary = generate_chat_response(
            db_history, rolling_summary=rolling_summary
        )
    except Exception as llm_err:
        logger.error("Chat generation failed: %s", llm_err)
        return jsonify({"error": "Failed to process reflection."}), 500

    # Save Assistant response & mood
    store.save_chat_message(session_id, "assistant", ai_reply)
    store.save_mood(current_user.id, detected_mood)

    thread_summaries[session_id] = new_summary
    session["chat_summaries"] = thread_summaries

    # Save initial entry to reflection history list with explicit session_id
    if is_first_message:
        store.save_journal(
            user_id=current_user.id,
            mood=detected_mood,
            detected_state=detected_mood,
            entry_text=user_message,
            suggestion=ai_reply,
            session_id=session_id,
        )

    recommended_practices = recommend_practices(
        mood=detected_mood, goal=current_user.daily_goal, limit=3
    )

    return jsonify({
        "reply": ai_reply,
        "detected_mood": detected_mood,
        "session_id": session_id,
        "practices": recommended_practices,
        "is_first_message": is_first_message,
    })


@app.delete("/api/chat/session/<session_id>")
@login_required
def delete_chat_session_endpoint(session_id: str):
    """Deletes a specific chat thread permanently."""
    success = store.delete_chat_session(current_user.id, session_id)
    if success:
        thread_summaries = session.get("chat_summaries", {})
        if thread_summaries.pop(session_id, None) is not None:
            session["chat_summaries"] = thread_summaries
        if session.get("chat_session_active") == session_id:
            session.pop("chat_session_active", None)
        return jsonify({"success": True})
    return jsonify({"error": "Session not found or unauthorized."}), 404

# ── Configuration & Analytics ──────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        goal = request.form.get("daily_goal", "grounding")
        store.update_goal(current_user.id, goal)
        religion = request.form.get("religion")
        if religion:
            store.update_religion(current_user.id, religion)
        name = request.form.get("display_name", "").strip()
        if name:
            store.update_display_name(current_user.id, name)
        return redirect(request.referrer or url_for("home"))
    
    return redirect(url_for("dashboard"))

@app.route("/dashboard")
@login_required
def dashboard():
    # Fetch 7-day mood history for weekly tracking
    history = store.mood_history(current_user.id, days=7)
    return render_template("dashboard.html", history=history)


# ── API Metrics ────────────────────────────────────────────────────────────

@app.get("/api/guidance/today")
@login_required
def api_guidance_today():
    daily = spiritual_api.today()
    guidance = build_guidance(current_user)
    return jsonify({
        "daily": daily,
        "guidance": guidance,
        "recommended_practices": recommend_practices(
            mood=guidance["mood"], goal=current_user.daily_goal, limit=3
        ),
    })


@app.get("/api/mood/history")
@login_required
def api_mood_history():
    days = min(int(request.args.get("days", 30)), 90)
    return jsonify(store.mood_history(current_user.id, days=days))


# ── Error handlers ─────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", page_title="Page not found",
                           message="This page has drifted out of view."), 404


@app.errorhandler(429)
def rate_limited(_error):
    return render_template("error.html", page_title="Slow down",
                           message="Too many requests. Please wait a moment."), 429


@app.errorhandler(500)
def server_error(_error):
    logger.exception("Internal server error")
    return render_template("error.html", page_title="Something went quiet",
                           message="CONNECT needs a moment. Please try again."), 500


if __name__ == "__main__":
    app.run(debug=True, threaded=True)