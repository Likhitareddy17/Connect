"""Auth flow + security-hardening tests (CSRF, open redirect, headers, startup)."""
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def csrf_token(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


class TestRegistration:
    def post(self, client, **overrides):
        data = {"email": "new@example.com", "display_name": "New", "password": "password123",
                "confirm_password": "password123", "religion": "hindu"}
        data.update(overrides)
        return client.post("/register", data=data)

    def test_success_logs_in_and_redirects_home(self, client, fake_store):
        res = self.post(client)
        assert res.status_code == 302 and res.headers["Location"].endswith("/")
        assert fake_store.get_user_by_email("new@example.com")["religion"] == "hindu"

    @pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "two@@example.com", "sp ace@example.com"])
    def test_bad_email_rejected(self, client, email):
        assert b"valid email" in self.post(client, email=email).data

    def test_short_password_rejected(self, client):
        assert b"at least 8" in self.post(client, password="short", confirm_password="short").data

    def test_password_over_bcrypt_limit_rejected(self, client):
        long_pw = "a" * 73
        assert b"at most 72" in self.post(client, password=long_pw, confirm_password=long_pw).data

    def test_password_mismatch_rejected(self, client):
        assert b"do not match" in self.post(client, confirm_password="different1").data

    def test_duplicate_email_rejected_case_insensitively(self, client):
        self.post(client)
        client.post("/logout")
        assert b"already exists" in self.post(client, email="NEW@example.com").data

    def test_unknown_religion_falls_back_to_atheist(self, client, fake_store):
        self.post(client, religion="<script>")
        assert fake_store.get_user_by_email("new@example.com")["religion"] == "atheist"


class TestLogin:
    @pytest.fixture(autouse=True)
    def user(self, fake_store):
        fake_store.create_user("me@example.com", "Me", "password123")

    def test_wrong_password(self, client):
        assert b"Incorrect" in client.post("/login", data={"email": "me@example.com", "password": "nope"}).data

    def test_email_is_case_insensitive(self, client):
        res = client.post("/login", data={"email": " ME@Example.com ", "password": "password123"})
        assert res.status_code == 302

    @pytest.mark.parametrize("evil", [
        "//evil.com", "https://evil.com/x", "/\\evil.com", "javascript:alert(1)", "evil.com", "///evil.com",
    ])
    def test_open_redirect_blocked(self, client, evil):
        res = client.post("/login?next=" + evil, data={"email": "me@example.com", "password": "password123"})
        assert res.status_code == 302
        assert "evil.com" not in res.headers["Location"] and "javascript" not in res.headers["Location"]

    def test_safe_next_is_honoured(self, client):
        res = client.post("/login?next=/spiritual", data={"email": "me@example.com", "password": "password123"})
        assert res.headers["Location"] == "/spiritual"


class TestCsrf:
    def test_login_without_token_rejected(self, csrf_client, fake_store):
        fake_store.create_user("me@example.com", "Me", "password123")
        res = csrf_client.post("/login", data={"email": "me@example.com", "password": "password123"})
        assert res.status_code == 400

    def test_login_with_token_accepted(self, csrf_client, fake_store):
        fake_store.create_user("me@example.com", "Me", "password123")
        token = csrf_token(csrf_client)
        res = csrf_client.post("/login", data={"email": "me@example.com", "password": "password123",
                                               "csrf_token": token})
        assert res.status_code == 302

    def test_chat_api_requires_csrf_header(self, csrf_client, fake_store):
        fake_store.create_user("me@example.com", "Me", "password123")
        token = csrf_token(csrf_client)
        csrf_client.post("/login", data={"email": "me@example.com", "password": "password123", "csrf_token": token})
        res = csrf_client.post("/api/chat/message", json={"message": "hi"})
        assert res.status_code == 400 and "refresh" in res.get_json()["error"]

    def test_logout_is_post_only(self, logged_in):
        client, _ = logged_in
        assert client.get("/logout").status_code == 405

    def test_every_post_form_carries_a_token(self, csrf_client):
        for path in ("/login", "/register"):
            assert 'name="csrf_token"' in csrf_client.get(path).get_data(as_text=True)


class TestHardening:
    def test_security_headers(self, client):
        res = client.get("/login")
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"

    def test_health_endpoints_need_no_login(self, client):
        assert client.get("/healthz").get_json() == {"status": "ok"}
        assert client.get("/readyz").get_json() == {"status": "ready"}

    def test_readyz_reports_db_outage(self, client, fake_store, monkeypatch):
        def boom():
            raise ConnectionError("db down")
        monkeypatch.setattr(fake_store, "ping", boom)
        assert client.get("/readyz").status_code == 503

    def test_mood_history_tolerates_junk_days(self, logged_in):
        client, _ = logged_in
        assert client.get("/api/mood/history?days=abc").status_code == 200
        assert client.get("/api/mood/history?days=-5").status_code == 200

    def test_app_refuses_to_start_without_secret_key(self):
        # app.py calls load_dotenv(), so a developer's own .env would silently supply the key and
        # defeat this test. Disable .env loading in the child process so the result never depends
        # on what happens to be in the working copy.
        env = {k: v for k, v in os.environ.items() if k not in {"FLASK_SECRET_KEY", "FLASK_DEBUG"}}
        code = "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import app"
        proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        assert proc.returncode != 0 and "FLASK_SECRET_KEY" in proc.stderr

    def test_app_starts_when_secret_key_comes_from_env(self):
        """The positive case: with a key present the app imports cleanly."""
        env = dict(os.environ, FLASK_SECRET_KEY="some-key")
        proc = subprocess.run([sys.executable, "-c", "import app"], cwd=ROOT, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
