"""
CONNECT — Flask application entry point.

Wellness app with three modules: spiritual (religion-aware daily guidance),
mental (grounding practices) and emotional (LLM-backed chat with a crisis
safety screen).
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date
from urllib.parse import urlparse

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
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from services.grounding import ALLOWED_MOODS, recommend_practices
from services.journal_analysis import FALLBACK_MOOD, HISTORY_WINDOW, SOURCE_FALLBACK, generate_chat_response
from services.religion_providers import RELIGION_LABELS, get_provider
from services.spiritual_api import spiritual_api
from store import ConnectStore

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = _env_flag("FLASK_DEBUG", False)


def _load_secret_key() -> str:
    """Fail fast in production: a per-process random key silently breaks logins
    as soon as there is more than one gunicorn worker (or a restart)."""
    key = os.getenv("FLASK_SECRET_KEY")
    if key:
        return key
    if DEBUG:
        logger.warning("FLASK_SECRET_KEY not set; using an ephemeral key (debug mode only).")
        return uuid.uuid4().hex + uuid.uuid4().hex
    raise RuntimeError("FLASK_SECRET_KEY must be set (generate one with: python -c 'import secrets; print(secrets.token_hex(32))').")


app = Flask(__name__)
app.config.from_mapping(
    SECRET_KEY=_load_secret_key(),
    MAX_CONTENT_LENGTH=64 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_env_flag("COOKIE_SECURE", not DEBUG),
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_SECURE=_env_flag("COOKIE_SECURE", not DEBUG),
    WTF_CSRF_TIME_LIMIT=None,  # token lives as long as the session (chat pages stay open for hours)
)

if _env_flag("TRUST_PROXY", False):
    # Behind Render/Heroku-style proxies: otherwise every client shares the
    # proxy's IP and therefore a single rate-limit bucket.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

csrf = CSRFProtect(app)

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
MAX_DISPLAY_NAME_LENGTH = 120
MAX_PASSWORD_BYTES = 72  # bcrypt only uses the first 72 bytes; reject rather than silently truncate
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


# ── Helpers ────────────────────────────────────────────────────────────────

def is_safe_redirect(target: str | None) -> bool:
    """Allow only same-site relative paths (blocks //evil.com, https://evil.com, /\\evil.com)."""
    if not target:
        return False
    parsed = urlparse(target)
    return (
        not parsed.scheme
        and not parsed.netloc
        and target.startswith("/")
        and not target.startswith(("//", "/\\"))
    )


def is_same_host_url(url: str | None) -> bool:
    return bool(url) and urlparse(url).netloc == request.host


def parse_uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError):
        return None


def build_guidance(user: User) -> dict:
    mood = store.latest_mood(user.id) or "calm"
    provider = get_provider(user.religion)
    daily = provider.get_day(date.today())
    practices = recommend_practices(mood=mood, goal=user.daily_goal, limit=3)
    return {
        "focus": daily["headline"],
        "tone": daily.get("calendar_label") or "",
        "reflection": daily["guidance_steps"][0] if daily["guidance_steps"] else "",
        "action": practices[0] if practices else "Take a mindful pause.",
        "mood": mood,
    }


# ── Health ─────────────────────────────────────────────────────────────────

@app.get("/healthz")
@limiter.exempt
def healthz():
    """Liveness: the process is up (no DB access)."""
    return jsonify({"status": "ok"})


@app.get("/readyz")
@limiter.exempt
def readyz():
    """Readiness: the database is reachable."""
    try:
        store.ping()
    except Exception:  # noqa: BLE001
        logger.exception("Readiness check failed")
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ready"})


# ── Auth Routes ────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        display_name = request.form.get("display_name", "").strip()[:MAX_DISPLAY_NAME_LENGTH] or "Friend"
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        religion = request.form.get("religion", "atheist")
        if len(email) > 255 or not EMAIL_RE.match(email):
            error = "A valid email is required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif len(password.encode()) > MAX_PASSWORD_BYTES:
            error = "Password must be at most 72 bytes (about 72 characters)."
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
@limiter.limit("20 per hour", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user_data = store.get_user_by_email(email)
        if user_data and store.verify_password(user_data, password):
            login_user(User(user_data), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page if is_safe_redirect(next_page) else url_for("home"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error)


@app.post("/logout")
@login_required
def logout():
    """POST-only (and CSRF-protected) so a third-party page can't log users out via <img src=...>."""
    logout_user()
    session.clear()
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
        religion=current_user.religion,
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


def _resolve_active_session(start_new: bool, requested_id: str | None) -> str:
    """Pick the chat thread to show — only ever one the current user owns."""
    candidates = [] if start_new else [parse_uuid(requested_id), parse_uuid(session.get("chat_session_active"))]
    for candidate in candidates:
        if candidate and store.owns_chat_session(current_user.id, candidate):
            return candidate
    new_id = str(uuid.uuid4())
    store.ensure_chat_session(current_user.id, new_id)
    return new_id


@app.route("/emotional", methods=["GET"])
@login_required
def emotional():
    """Renders workspace and loads active or previous session threads.

    ``?new=1`` forces a brand-new thread; ``?session_id=`` resumes a thread,
    but only if the signed-in user owns it (otherwise a fresh thread starts).
    """
    session_id = _resolve_active_session(
        start_new=request.args.get("new") == "1",
        requested_id=request.args.get("session_id"),
    )
    session["chat_session_active"] = session_id

    return render_template(
        "emotional.html",
        active_session_id=session_id,
        active_messages=store.get_session_messages(session_id),
        all_sessions=store.list_user_sessions(current_user.id),
        entries=store.recent_journals(current_user.id),
    )


@app.post("/api/chat/message")
@login_required
@limiter.limit("15 per minute")
def api_chat_message():
    payload = request.get_json(silent=True) or {}
    user_message = str(payload.get("message", "")).strip()[:MAX_JOURNAL_LENGTH]
    if not user_message:
        return jsonify({"error": "Reflection text cannot be empty."}), 400

    session_id = parse_uuid(session.get("chat_session_active")) or str(uuid.uuid4())
    if not store.ensure_chat_session(current_user.id, session_id):
        # The id in the cookie belongs to someone else: never read or write it.
        session_id = str(uuid.uuid4())
        store.ensure_chat_session(current_user.id, session_id)
    session["chat_session_active"] = session_id

    store.save_chat_message(session_id, "user", user_message)

    # The model sees only the LATEST few messages; the thread length decides
    # "first message" and when to refresh the summary.
    history = store.get_recent_messages(session_id, limit=HISTORY_WINDOW)
    total_messages = store.count_session_messages(session_id)
    is_first_message = total_messages == 1
    rolling_summary = store.get_session_summary(session_id)

    try:
        result = generate_chat_response(history, rolling_summary, total_messages=total_messages)
    except Exception:  # noqa: BLE001
        logger.exception("Chat generation failed")
        return jsonify({"error": "Failed to process reflection."}), 500

    store.save_chat_message(session_id, "assistant", result.reply)
    if result.mood:  # fallback replies carry no real mood signal — don't pollute analytics
        store.save_mood(current_user.id, result.mood)
    if result.summary != rolling_summary:
        store.set_session_summary(session_id, result.summary)

    mood = result.mood or FALLBACK_MOOD
    if is_first_message:
        store.save_journal(
            user_id=current_user.id,
            mood=mood,
            detected_state=mood,
            entry_text=user_message,
            suggestion=result.reply,
            session_id=session_id,
        )

    return jsonify({
        "reply": result.reply,
        "detected_mood": result.mood,
        "session_id": session_id,
        "practices": recommend_practices(mood=mood, goal=current_user.daily_goal, limit=3),
        "is_first_message": is_first_message,
        "crisis": result.is_crisis,
        "degraded": result.source == SOURCE_FALLBACK,
    })


@app.delete("/api/chat/session/<session_id>")
@login_required
def delete_chat_session_endpoint(session_id: str):
    """Deletes a chat thread (and its messages) — only if it belongs to the user."""
    valid_id = parse_uuid(session_id)
    if valid_id and store.delete_chat_session(current_user.id, valid_id):
        if session.get("chat_session_active") == valid_id:
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
        name = request.form.get("display_name", "").strip()[:MAX_DISPLAY_NAME_LENGTH]
        if name:
            store.update_display_name(current_user.id, name)
        # Return to the page the form was on, but only if it is on our own host.
        referrer = request.referrer
        return redirect(referrer if is_same_host_url(referrer) else url_for("home"))

    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
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
    days = request.args.get("days", default=30, type=int)  # junk like ?days=abc falls back to 30
    days = max(1, min(days, 90))
    return jsonify(store.mood_history(current_user.id, days=days))


# ── Error handlers ─────────────────────────────────────────────────────────

@app.errorhandler(CSRFError)
def csrf_failed(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Session expired. Please refresh the page."}), 400
    return render_template("error.html", page_title="Session expired",
                           message="Please go back, refresh the page and try again."), 400


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", page_title="Page not found",
                           message="This page has drifted out of view."), 404


@app.errorhandler(429)
def rate_limited(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Too many requests. Please wait a moment."}), 429
    return render_template("error.html", page_title="Slow down",
                           message="Too many requests. Please wait a moment."), 429


@app.errorhandler(500)
def server_error(_error):
    logger.exception("Internal server error")
    return render_template("error.html", page_title="Something went quiet",
                           message="CONNECT needs a moment. Please try again."), 500


if __name__ == "__main__":
    app.run(debug=DEBUG, threaded=True)
