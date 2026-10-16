"""Shared pytest fixtures and configuration."""
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

import pytest  # noqa: E402

from tests.fakes import FakeStore  # noqa: E402


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
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user["id"])
        sess["_fresh"] = True
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
