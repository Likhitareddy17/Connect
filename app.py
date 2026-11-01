"""
CONNECT — Flask application entry point.

Wellness app with three modules: spiritual (religion-aware daily guidance),
mental (grounding practices) and emotional (LLM-backed chat with a crisis
safety screen).
"""
from __future__ import annotations

import hashlib
import hmac
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

from services import email_sender
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
VERIFY_TOKEN_HOURS = 24
RESET_TOKEN_HOURS = 1
DELETE_CONFIRM_WORD = "DELETE"
GENERIC_RESEND_NOTICE = (
    "If an account with that email needs verifying, we've sent a new link. "
    "Check your inbox (and spam folder)."
)
GENERIC_RESET_NOTICE = (
    "If an account with that email exists, we've sent a link to reset its password. "
    "Check your inbox (and spam folder)."
)


def _password_fingerprint(password_hash: str) -> str:
    key = app.config["SECRET_KEY"]
    key = key.encode() if isinstance(key, str) else key
    return hmac.new(key, password_hash.encode(), hashlib.sha256).hexdigest()[:32]


class User(UserMixin):
    def __init__(self, data: dict):
        self._data = data

    def get_id(self) -> str:
        """``<id>:<fingerprint of the current password hash>``.

        The fingerprint is baked into the session and "remember me" cookies, so changing the password
        (e.g. via reset) makes every previously issued cookie fail in ``load_user``.
        """
        return f"{self._data['id']}:{_password_fingerprint(self._data['password_hash'])}"

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
def load_user(session_id: str) -> User | None:
    uid, _, fingerprint = str(session_id).partition(":")
    if not uid.isdigit() or not fingerprint:
        return None  # malformed or pre-upgrade cookie: sign in again
    data = store.get_user_by_id(int(uid))
    if not data or not hmac.compare_digest(fingerprint, _password_fingerprint(data["password_hash"])):
        return None  # unknown user, or the password changed since this cookie was issued
    return User(data)


@app.after_request
def add_security_headers(response):
    if request.endpoint in NO_STORE_ENDPOINTS:
        response.headers["Cache-Control"] = "no-store"  # pages that carry one-time tokens
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if app.config["SESSION_COOKIE_SECURE"]:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


NO_STORE_ENDPOINTS = {"verify_email", "reset_password"}


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
        "tone": daily.get("calendar_label") or daily.get("weekday") or "",
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

# ── Shared pages for the account flows (templates: auth_message.html, auth_form.html) ──

EMAIL_FIELD = {"name": "email", "label": "Email", "type": "email", "autocomplete": "email",
               "placeholder": "you@example.com"}
NEW_PASSWORD_FIELDS = [
    {"name": "password", "label": "New password", "type": "password", "autocomplete": "new-password",
     "placeholder": "At least 8 characters"},
    {"name": "confirm_password", "label": "Confirm new password", "type": "password",
     "autocomplete": "new-password", "placeholder": "••••••••"},
]


def auth_message(title: str, message: str, status: int = 200, **context):
    return render_template("auth_message.html", title=title, message=message, **context), status


def auth_form(title: str, intro: str, fields: list, button: str, status: int = 200, **context):
    return render_template("auth_form.html", title=title, intro=intro, fields=fields, button=button,
                           **context), status


def _check_email_page(email: str, sent: bool):
    message = (
        f"We sent a confirmation link to {email}. It works for {VERIFY_TOKEN_HOURS} hours. "
        "Open it to finish creating your account, then sign in."
        if sent else
        f"Your account was created, but we couldn’t send the confirmation email to {email} just now. "
        "Use the button below to try again in a moment."
    )
    return auth_message(
        "Check your email", message, post_url=url_for("resend_verification"), hidden={"email": email},
        button="Send the link again", note="Didn’t get it? Check your spam folder, or",
        link_url=url_for("login"), link_label="Back to sign in",
    )


def _password_error(password: str, confirm: str) -> str | None:
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return "Password must be at most 72 bytes (about 72 characters)."
    if password != confirm:
        return "Passwords do not match."
    return None


def _email_link(endpoint: str, token: str) -> str:
    """Absolute link for an e-mail. Refuses a malformed base URL, and plain http outside debug mode."""
    base = email_sender.public_base_url(request.url_root)
    parsed = urlparse(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        logger.error("PUBLIC_BASE_URL is invalid; e-mail not sent.")
        raise email_sender.EmailDeliveryError("invalid base URL")
    if not DEBUG and parsed.scheme != "https":
        logger.error("E-mailed links must use HTTPS outside debug mode (check PUBLIC_BASE_URL); e-mail not sent.")
        raise email_sender.EmailDeliveryError("insecure base URL")
    return f"{base}{url_for(endpoint, token=token)}"


def _send_verification(user_id: int, email: str) -> bool:
    """Issue a fresh verification link and mail it. Never raises; False means it wasn't delivered."""
    try:
        token = store.create_email_verification_token(user_id, expires_hours=VERIFY_TOKEN_HOURS)
        email_sender.send_verification_email(email, _email_link("verify_email", token), VERIFY_TOKEN_HOURS)
        return True
    except email_sender.EmailDeliveryError:
        return False  # already logged without the token or SMTP details
    except Exception:  # noqa: BLE001 - e.g. database error: don't turn it into a 500 / leak details
        logger.exception("Could not create or send a verification e-mail for user %s", user_id)
        return False


def _send_password_reset(user_id: int, email: str) -> bool:
    try:
        token = store.create_password_reset_token(user_id, expires_hours=RESET_TOKEN_HOURS)
        email_sender.send_password_reset_email(email, _email_link("reset_password", token), RESET_TOKEN_HOURS)
        return True
    except email_sender.EmailDeliveryError:
        return False
    except Exception:  # noqa: BLE001
        logger.exception("Could not create or send a password-reset e-mail for user %s", user_id)
        return False


def _email_rate_key() -> str:
    """Rate-limit by the e-mail being targeted, so one inbox can't be flooded from many IPs."""
    return "email:" + request.form.get("email", "").strip().lower()[:255]


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
        else:
            error = _password_error(password, confirm)
        if error is None:
            user_data = store.create_user(email, display_name, password, religion=religion, email_verified=False)
            if user_data is None:
                error = "An account with that email already exists."
            else:
                # No login until the address is confirmed.
                sent = _send_verification(user_data["id"], user_data["email"])
                return _check_email_page(user_data["email"], sent)
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
            if not user_data.get("email_verified"):
                # Only reached with the correct password, so this reveals nothing to a stranger.
                return render_template(
                    "login.html",
                    error="Please confirm your email address before signing in.",
                    unverified_email=user_data["email"],
                ), 403
            login_user(User(user_data), remember=True)
            next_page = request.args.get("next")
            return redirect(next_page if is_safe_redirect(next_page) else url_for("home"))
        error = "Incorrect email or password."
    return render_template("login.html", error=error)


def _sign_out() -> None:
    """Clear the session AND the "remember me" cookie.

    Order matters: logout_user() leaves a marker in the session that tells Flask-Login to delete the
    remember cookie. Calling session.clear() *after* it erases that marker, and the next request would
    silently sign the person back in.
    """
    session.clear()
    logout_user()


@app.post("/logout")
@login_required
def logout():
    """POST-only (and CSRF-protected) so a third-party page can't log users out via <img src=...>."""
    _sign_out()
    return redirect(url_for("login"))


# ── E-mail verification ────────────────────────────────────────────────────
# The e-mailed link (GET) only *shows* a confirm button; the token is spent by the POST. Mail scanners
# and link previews fetch GET links automatically and would otherwise burn the single-use token.

@app.get("/verify-email")
def verify_email():
    token = request.args.get("token", "")
    if not token:
        return _verify_failed()
    return auth_message(
        "Confirm your email", "One last step to finish creating your account.",
        post_url=url_for("verify_email_confirm"), hidden={"token": token}, button="Confirm my email",
    )


def _verify_failed():
    return auth_message(
        "That link didn’t work", "It may have expired or already been used. You can request a new one.",
        status=400, link_url=url_for("resend_verification"), link_label="Send a new link",
    )


@app.post("/verify-email")
@limiter.limit("20 per hour")
def verify_email_confirm():
    if not store.verify_email_token(request.form.get("token", "")):
        return _verify_failed()
    return auth_message("Email confirmed", "Thank you. Your account is ready.",
                        link_url=url_for("login"), link_label="Sign in")


@app.route("/resend-verification", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
@limiter.limit("3 per hour", methods=["POST"], key_func=_email_rate_key)
def resend_verification():
    notice = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:255]
        user_data = store.get_user_by_email(email) if EMAIL_RE.match(email) else None
        if user_data and not user_data.get("email_verified"):
            _send_verification(user_data["id"], user_data["email"])
        # Identical answer whether or not the account exists, is verified, or the mail went out.
        notice = GENERIC_RESEND_NOTICE
    return auth_form(
        "Resend confirmation", "Enter the email you signed up with and we’ll send a fresh link.",
        [EMAIL_FIELD], "Send link", notice=notice, links=[(url_for("login"), "Back to sign in")],
    )


# ── Password reset ─────────────────────────────────────────────────────────

@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
@limiter.limit("3 per hour", methods=["POST"], key_func=_email_rate_key)
def forgot_password():
    notice = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:255]
        user_data = store.get_user_by_email(email) if EMAIL_RE.match(email) else None
        if user_data:
            _send_password_reset(user_data["id"], user_data["email"])
        notice = GENERIC_RESET_NOTICE
    return auth_form(
        "Forgot your password", "Enter your email and we’ll send a link to choose a new one.",
        [EMAIL_FIELD], "Send reset link", notice=notice, heading="Forgot your password?",
        links=[(url_for("login"), "Back to sign in")],
    )


def _reset_form(token: str, error: str | None = None, status: int = 200):
    return auth_form(
        "Choose a new password", "You’ll be signed out everywhere and asked to sign in with the new one.",
        NEW_PASSWORD_FIELDS, "Update password", status=status, hidden={"token": token}, error=error,
    )


def _reset_link_invalid():
    return auth_message(
        "That link didn’t work", "This reset link is invalid or has expired. Please request a new one.",
        status=400, link_url=url_for("forgot_password"), link_label="Request a new link",
    )


@app.get("/reset-password")
def reset_password():
    token = request.args.get("token", "")
    return _reset_form(token) if token else _reset_link_invalid()


@app.post("/reset-password")
@limiter.limit("10 per hour")
def reset_password_confirm():
    token = request.form.get("token", "")
    error = _password_error(request.form.get("password", ""), request.form.get("confirm_password", ""))
    if error:
        return _reset_form(token, error, status=400)  # the token survives a typo
    if not store.reset_password_with_token(token, request.form.get("password", "")):
        return _reset_link_invalid()
    # No automatic login: the person signs in with the new password (all old sessions are now void).
    return render_template("login.html", error=None, notice="Your password has been updated. Please sign in."), 200


# ── Account deletion ───────────────────────────────────────────────────────

def _delete_form(error: str | None = None, status: int = 200):
    return auth_form(
        "Delete your account",
        "This permanently deletes your account and everything in it: mood check-ins, chat conversations, "
        "reflection cards, journal entries and jaap counts. It cannot be undone.",
        [
            {"name": "password", "label": "Your password", "type": "password", "autocomplete": "current-password"},
            {"name": "confirm_text", "label": f"Type {DELETE_CONFIRM_WORD} to confirm"},
        ],
        "Permanently delete my account", status=status, error=error,
        links=[(url_for("dashboard"), "No, keep my account")],
    )


@app.get("/account/delete")
@login_required
def delete_account():
    return _delete_form()


@app.post("/account/delete")
@login_required
@limiter.limit("5 per hour")
def delete_account_confirm():
    user_data = store.get_user_by_id(current_user.id)  # fresh read: never trust cached session data
    typed = request.form.get("confirm_text", "").strip()
    if not user_data or not store.verify_password(user_data, request.form.get("password", "")):
        return _delete_form("That password is not correct.", status=400)
    if typed != DELETE_CONFIRM_WORD:
        return _delete_form(f"Type {DELETE_CONFIRM_WORD} in capitals to confirm.", status=400)

    # Only ever the signed-in user's own id; the id is never taken from the request.
    store.delete_user(current_user.id)
    _sign_out()
    return render_template("login.html", error=None, notice="Your account and all its data have been deleted."), 200


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
        "support_note": result.needs_support_note,
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
