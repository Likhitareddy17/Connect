"""Shared pytest fixtures and configuration."""
from __future__ import annotations

import os
import sys

# Project root on the path so imports work from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set BEFORE `app` is imported (the app refuses to start without a secret key).
os.environ["FLASK_SECRET_KEY"] = "test-secret-key"
os.environ["COOKIE_SECURE"] = "0"
# Prevent any real external calls during tests.
os.environ["GROQ_API_KEY"] = ""
# The model-based safety classifier makes an extra API call per message. Most tests assert on the
# chat call alone, so it is off by default here; tests/test_safety.py enables it explicitly.
os.environ["SAFETY_CLASSIFIER_ENABLED"] = "0"
os.environ["PROKERALA_CLIENT_ID"] = ""
os.environ["PROKERALA_CLIENT_SECRET"] = ""
# E-mail: fixed, fake settings so a developer's real .env can never leak into tests. Nothing is ever sent:
# the autouse `outbox` fixture below replaces smtplib.SMTP / SMTP_SSL.
os.environ.update({
    "SMTP_HOST": "smtp.test.invalid", "SMTP_PORT": "587", "SMTP_USERNAME": "smtp-user",
    "SMTP_PASSWORD": "smtp-pass", "SMTP_FROM_EMAIL": "noreply@connect.test",
    "PUBLIC_BASE_URL": "https://connect.test",
})

import pytest  # noqa: E402

from tests.fakes import FakeStore  # noqa: E402


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL: records messages instead of sending them."""

    outbox: list = []          # EmailMessage objects "sent" in this test
    log: list = []             # ("starttls"|"login"|"connect", ...) events, to assert TLS ordering
    fail_with: Exception | None = None

    def __init__(self, host="", port=0, timeout=None, **kwargs):
        FakeSMTP.log.append(("connect", type(self).__name__, host, port))
        if FakeSMTP.fail_with is not None:
            raise FakeSMTP.fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, **kwargs):
        FakeSMTP.log.append(("starttls",))

    def login(self, user, password):
        FakeSMTP.log.append(("login", user))

    def send_message(self, message):
        FakeSMTP.outbox.append(message)


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """No test can reach a real mail server. Returns the list of messages 'sent'."""
    import smtplib

    FakeSMTP.outbox, FakeSMTP.log, FakeSMTP.fail_with = [], [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP.outbox


def sign_in(client, store, user):
    """Put a signed-in session on ``client`` for ``user`` (a dict from store.create_user)."""
    import app as app_module

    with client.session_transaction() as sess:
        sess["_user_id"] = app_module.User(store.get_user_by_id(user["id"])).get_id()
        sess["_fresh"] = True


@pytest.fixture()
def fake_store(monkeypatch):
    import app as app_module

    store = FakeStore()
    monkeypatch.setattr(app_module, "store", store)
    return store


@pytest.fixture()
def app(fake_store):
    import app as app_module

    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_module.limiter.enabled = False
    return app_module.app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def csrf_client(fake_store):
    """A client with CSRF protection ON (as in production)."""
    import app as app_module

    app_module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    app_module.limiter.enabled = False
    yield app_module.app.test_client()
    app_module.app.config.update(WTF_CSRF_ENABLED=False)


@pytest.fixture()
def logged_in(client, fake_store):
    """Client signed in as a fresh user; returns (client, user_dict)."""
    user = fake_store.create_user("me@example.com", "Me", "password123")
    sign_in(client, fake_store, user)
    return client, user


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Unit tests must never reach the internet (e.g. the Aladhan prayer-times API).

    Code under test handles the failure and uses local fallback data; tests that
    need a specific response patch ``requests`` themselves.
    """
    import requests

    def blocked(*args, **kwargs):
        raise requests.exceptions.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests, "get", blocked)
