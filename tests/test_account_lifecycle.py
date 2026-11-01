"""Email verification, password reset and account deletion: routes, FakeStore contract and mail sender."""
import re
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from services import email_sender
from tests import conftest
from tests.conftest import sign_in

PASSWORD = "password123"


def link_in(message) -> str:
    """The one https link in a captured e-mail."""
    return re.search(r"https://\S+", message.get_content()).group(0)


def link_in_http(message) -> str:
    return re.search(r"https?://\S+", message.get_content()).group(0)


def token_in(message) -> str:
    return re.search(r"token=([\w-]+)", link_in(message)).group(1)


def register(client, email="new@example.com", **overrides):
    data = {"email": email, "display_name": "New", "password": PASSWORD,
            "confirm_password": PASSWORD, "religion": "hindu"}
    data.update(overrides)
    return client.post("/register", data=data)


def login(client, email="new@example.com", password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


# ── Email verification ─────────────────────────────────────────────────────

class TestVerificationFlow:
    def test_full_happy_path(self, client, fake_store, outbox):
        register(client)
        assert login(client).status_code == 403  # blocked until verified
        link = link_in(outbox[0])
        assert link.startswith("https://connect.test/verify-email?token=")  # PUBLIC_BASE_URL, https
        token = token_in(outbox[0])

        page = client.get(f"/verify-email?token={token}")  # GET only shows a button...
        assert page.status_code == 200 and b"Confirm my email" in page.data
        assert fake_store.get_user_by_email("new@example.com")["email_verified"] is False  # ...and changes nothing

        done = client.post("/verify-email", data={"token": token})
        assert done.status_code == 200 and b"Email confirmed" in done.data
        assert fake_store.get_user_by_email("new@example.com")["email_verified"] is True
        with client.session_transaction() as sess:
            assert "_user_id" not in sess  # verifying does not log anyone in

        assert login(client).status_code == 302

    def test_token_is_single_use(self, client, outbox):
        register(client)
        token = token_in(outbox[0])
        assert client.post("/verify-email", data={"token": token}).status_code == 200
        again = client.post("/verify-email", data={"token": token})
        assert again.status_code == 400 and "didn’t work" in again.get_data(as_text=True)

    def test_expired_token_rejected(self, client, fake_store, outbox):
        register(client)
        token = token_in(outbox[0])
        for rec in fake_store.tokens["verify"].values():
            rec["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert client.post("/verify-email", data={"token": token}).status_code == 400
        assert fake_store.get_user_by_email("new@example.com")["email_verified"] is False

    @pytest.mark.parametrize("token", ["", "garbage", "a" * 500, "' OR 1=1 --"])
    def test_bad_tokens_never_verify(self, client, fake_store, outbox, token):
        register(client)
        assert client.post("/verify-email", data={"token": token}).status_code == 400
        assert fake_store.get_user_by_email("new@example.com")["email_verified"] is False

    def test_a_reset_token_cannot_verify_email(self, client, fake_store, outbox):
        register(client)
        user = fake_store.get_user_by_email("new@example.com")
        reset = fake_store.create_password_reset_token(user["id"])
        assert client.post("/verify-email", data={"token": reset}).status_code == 400

    def test_tokens_are_stored_hashed_only(self, client, fake_store, outbox):
        register(client)
        token = token_in(outbox[0])
        assert token not in fake_store.tokens["verify"]  # keyed by hash, not by the raw token
        assert all(len(h) == 64 for h in fake_store.tokens["verify"])

    def test_new_link_invalidates_older_one(self, client, fake_store, outbox):
        register(client)
        first = token_in(outbox[0])
        client.post("/resend-verification", data={"email": "new@example.com"})
        second = token_in(outbox[1])
        assert first != second
        assert client.post("/verify-email", data={"token": first}).status_code == 400
        assert client.post("/verify-email", data={"token": second}).status_code == 200

    def test_verify_page_is_not_cached(self, client):
        assert client.get("/verify-email?token=x").headers["Cache-Control"] == "no-store"


class TestRegistrationEmailHandling:
    def test_smtp_failure_still_creates_account_and_hides_details(self, client, fake_store, outbox, caplog):
        conftest.FakeSMTP.fail_with = smtplib.SMTPException("535 bad credentials for smtp-user / smtp-pass")
        res = register(client)
        body = res.get_data(as_text=True)
        assert res.status_code == 200 and "couldn’t send" in body
        assert "smtp-pass" not in body and "535" not in body and "smtp.test.invalid" not in body
        assert "smtp-pass" not in caplog.text and "535" not in caplog.text
        assert fake_store.get_user_by_email("new@example.com") is not None
        assert "resend-verification" in body  # a way out of the confusing state

    @pytest.mark.parametrize("base", ["http://connect.example.com", "https://user:pw@connect.example.com",
                                      "https://connect.example.com/?x=1", "ftp://connect.example.com"])
    def test_links_must_be_https_and_well_formed_outside_debug_mode(self, client, monkeypatch, outbox, base):
        import app as app_module

        monkeypatch.setattr(app_module, "DEBUG", False)
        monkeypatch.setenv("PUBLIC_BASE_URL", base)
        res = register(client)
        assert "couldn’t send" in res.get_data(as_text=True) and outbox == []  # nothing mailed, account still created

    def test_http_links_are_allowed_in_debug_mode_only(self, client, monkeypatch, outbox):
        import app as app_module

        monkeypatch.setattr(app_module, "DEBUG", True)
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:5000")
        register(client)
        assert link_in_http(outbox[0]).startswith("http://127.0.0.1:5000/verify-email?token=")

    def test_password_reset_links_get_the_same_https_rule(self, client, fake_store, monkeypatch, outbox):
        import app as app_module

        fake_store.create_user("me@example.com", "Me", PASSWORD)
        monkeypatch.setattr(app_module, "DEBUG", False)
        monkeypatch.setenv("PUBLIC_BASE_URL", "http://connect.example.com")
        res = client.post("/forgot-password", data={"email": "me@example.com"})
        assert res.status_code == 200 and outbox == []

    def test_smtp_unconfigured_is_handled(self, client, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "")
        assert "couldn’t send" in register(client).get_data(as_text=True)

    def test_raw_token_is_never_logged(self, client, outbox, caplog):
        caplog.set_level("DEBUG")
        register(client)
        client.post("/resend-verification", data={"email": "new@example.com"})
        for message in outbox:
            assert token_in(message) not in caplog.text

    def test_registration_existing_behaviour_unchanged_for_duplicates(self, client):
        register(client)
        assert b"already exists" in register(client, email="NEW@example.com").data


class TestLoginRestrictions:
    def test_unverified_correct_password_gets_message_and_resend_option(self, client, fake_store):
        fake_store.create_user("u@example.com", "U", PASSWORD, email_verified=False)
        res = login(client, "u@example.com")
        assert res.status_code == 403
        assert b"confirm your email" in res.data and b"/resend-verification" in res.data
        with client.session_transaction() as sess:
            assert "_user_id" not in sess

    def test_unverified_wrong_password_looks_like_any_wrong_password(self, client, fake_store):
        fake_store.create_user("u@example.com", "U", PASSWORD, email_verified=False)
        res = login(client, "u@example.com", "wrong-password")
        assert res.status_code == 200 and b"Incorrect email or password" in res.data
        assert b"confirm your email" not in res.data

    def test_legacy_users_remain_able_to_log_in(self, client, fake_store):
        fake_store.create_user("old@example.com", "Old", PASSWORD)  # default: verified
        assert login(client, "old@example.com").status_code == 302

    def test_row_without_the_column_fails_closed(self, client, fake_store):
        fake_store.create_user("u@example.com", "U", PASSWORD)
        del fake_store.get_user_by_email("u@example.com")["email_verified"]
        assert login(client, "u@example.com").status_code == 403


class TestResendVerification:
    def test_identical_response_for_unknown_unverified_and_verified(self, client, fake_store, outbox):
        fake_store.create_user("unverified@example.com", "A", PASSWORD, email_verified=False)
        fake_store.create_user("verified@example.com", "B", PASSWORD)
        bodies = []
        for email in ("unverified@example.com", "verified@example.com", "nobody@example.com", "not-an-email"):
            res = client.post("/resend-verification", data={"email": email})
            assert res.status_code == 200
            bodies.append(re.sub(r'csrf[^"]*"[^"]*"', "", res.get_data(as_text=True)))
        assert len(set(bodies)) == 1
        assert [m["To"] for m in outbox] == ["unverified@example.com"]  # mail only where it makes sense

    def test_requires_csrf(self, csrf_client):
        assert csrf_client.post("/resend-verification", data={"email": "a@b.co"}).status_code == 400

    def test_is_rate_limited_per_ip_and_per_address(self, client, fake_store):
        import app as app_module

        fake_store.create_user("u@example.com", "U", PASSWORD, email_verified=False)
        app_module.limiter.enabled = True
        try:
            app_module.limiter.reset()
            codes = [client.post("/resend-verification", data={"email": "u@example.com"}).status_code
                     for _ in range(4)]
            assert codes == [200, 200, 200, 429]  # 3 per hour per address
            other_ip = [client.post("/resend-verification", data={"email": "u@example.com"},
                                    environ_overrides={"REMOTE_ADDR": "9.9.9.9"}).status_code]
            assert other_ip == [429]  # still blocked: the *address* is limited, not just the IP
        finally:
            app_module.limiter.reset()
            app_module.limiter.enabled = False


# ── Password reset ─────────────────────────────────────────────────────────

class TestPasswordReset:
    @pytest.fixture()
    def user(self, fake_store):
        return fake_store.create_user("me@example.com", "Me", PASSWORD)

    def test_full_happy_path_and_no_auto_login(self, client, fake_store, outbox, user):
        assert b"Forgot your password" in client.get("/login").data  # link exists on the login page
        client.post("/forgot-password", data={"email": "me@example.com"})
        assert len(outbox) == 1 and outbox[0]["To"] == "me@example.com"
        token = token_in(outbox[0])
        assert link_in(outbox[0]).startswith("https://connect.test/reset-password?token=")

        form = client.get(f"/reset-password?token={token}")
        assert form.status_code == 200 and token.encode() in form.data
        assert fake_store.verify_password(fake_store.get_user_by_id(user["id"]), PASSWORD)  # GET changed nothing

        res = client.post("/reset-password", data={"token": token, "password": "brand-new-pass",
                                                   "confirm_password": "brand-new-pass"})
        assert res.status_code == 200 and b"password has been updated" in res.data
        with client.session_transaction() as sess:
            assert "_user_id" not in sess
        assert login(client, "me@example.com", PASSWORD).status_code == 200  # old password dead
        assert login(client, "me@example.com", "brand-new-pass").status_code == 302

    def test_token_single_use(self, client, outbox, user):
        client.post("/forgot-password", data={"email": "me@example.com"})
        token = token_in(outbox[0])
        data = {"token": token, "password": "brand-new-pass", "confirm_password": "brand-new-pass"}
        assert client.post("/reset-password", data=data).status_code == 200
        again = client.post("/reset-password", data=dict(data, password="another-pass1", confirm_password="another-pass1"))
        assert again.status_code == 400 and b"invalid or has expired" in again.data

    def test_expired_token_rejected_and_password_unchanged(self, client, fake_store, outbox, user):
        client.post("/forgot-password", data={"email": "me@example.com"})
        token = token_in(outbox[0])
        for rec in fake_store.tokens["reset"].values():
            rec["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        res = client.post("/reset-password", data={"token": token, "password": "brand-new-pass",
                                                   "confirm_password": "brand-new-pass"})
        assert res.status_code == 400
        assert fake_store.verify_password(fake_store.get_user_by_id(user["id"]), PASSWORD)

    def test_requesting_again_voids_the_earlier_link(self, client, outbox, user):
        client.post("/forgot-password", data={"email": "me@example.com"})
        client.post("/forgot-password", data={"email": "me@example.com"})
        first, second = token_in(outbox[0]), token_in(outbox[1])
        data = {"password": "brand-new-pass", "confirm_password": "brand-new-pass"}
        assert client.post("/reset-password", data=dict(data, token=first)).status_code == 400
        assert client.post("/reset-password", data=dict(data, token=second)).status_code == 200

    def test_successful_reset_voids_other_outstanding_links(self, client, fake_store, user):
        a = fake_store.create_password_reset_token(user["id"])
        assert fake_store.reset_password_with_token(a, "brand-new-pass")
        assert not fake_store.reset_password_with_token(a, "x" * 12)
        assert all(rec["used"] for rec in fake_store.tokens["reset"].values())

    @pytest.mark.parametrize("password, confirm, message", [
        ("short", "short", b"at least 8"), ("a" * 73, "a" * 73, b"at most 72"),
        ("brand-new-pass", "different-pass", b"do not match"),
    ])
    def test_new_password_is_validated_and_token_survives_a_typo(self, client, fake_store, outbox, user,
                                                                  password, confirm, message):
        client.post("/forgot-password", data={"email": "me@example.com"})
        token = token_in(outbox[0])
        res = client.post("/reset-password", data={"token": token, "password": password, "confirm_password": confirm})
        assert res.status_code == 400 and message in res.data and token.encode() in res.data
        assert client.post("/reset-password", data={"token": token, "password": "brand-new-pass",
                                                    "confirm_password": "brand-new-pass"}).status_code == 200

    def test_identical_response_whether_or_not_the_account_exists(self, client, outbox, user):
        bodies = [re.sub(r'csrf[^"]*"[^"]*"', "", client.post("/forgot-password", data={"email": e}).get_data(as_text=True))
                  for e in ("me@example.com", "nobody@example.com", "garbage")]
        assert len(set(bodies)) == 1
        assert len(outbox) == 1

    def test_smtp_failure_gives_the_same_generic_answer(self, client, user, outbox):
        conftest.FakeSMTP.fail_with = smtplib.SMTPException("boom")
        res = client.post("/forgot-password", data={"email": "me@example.com"})
        assert res.status_code == 200 and b"If an account" in res.data

    def test_reset_also_confirms_an_unverified_address(self, client, fake_store, outbox):
        fake_store.create_user("u@example.com", "U", PASSWORD, email_verified=False)
        client.post("/forgot-password", data={"email": "u@example.com"})
        client.post("/reset-password", data={"token": token_in(outbox[0]), "password": "brand-new-pass",
                                             "confirm_password": "brand-new-pass"})
        assert fake_store.get_user_by_email("u@example.com")["email_verified"] is True

    def test_reset_token_cannot_be_used_as_verification_token_and_vice_versa(self, client, fake_store, user):
        verify = fake_store.create_email_verification_token(user["id"])
        assert not fake_store.reset_password_with_token(verify, "brand-new-pass")

    def test_existing_sessions_are_invalidated_by_a_password_change(self, fake_store, app, outbox, user):
        a, b = app.test_client(), app.test_client()
        sign_in(a, fake_store, user)
        sign_in(b, fake_store, user)
        assert a.get("/").status_code == 200 and b.get("/").status_code == 200
        fake_store.reset_password_with_token(fake_store.create_password_reset_token(user["id"]), "brand-new-pass")
        assert a.get("/").status_code == 302 and b.get("/").status_code == 302

    def test_legacy_plain_id_cookies_are_rejected(self, client, user):
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user["id"])
        assert client.get("/").status_code == 302

    def test_forged_fingerprint_is_rejected(self, client, user):
        with client.session_transaction() as sess:
            sess["_user_id"] = f"{user['id']}:{'0' * 32}"
        assert client.get("/").status_code == 302

    def test_reset_pages_need_csrf_and_are_not_cached(self, csrf_client):
        assert csrf_client.post("/forgot-password", data={"email": "a@b.co"}).status_code == 400
        assert csrf_client.post("/reset-password", data={"token": "x", "password": "p" * 9}).status_code == 400
        assert csrf_client.get("/reset-password?token=x").headers["Cache-Control"] == "no-store"

    def test_forgot_password_is_rate_limited(self, client, user):
        import app as app_module

        app_module.limiter.enabled = True
        try:
            app_module.limiter.reset()
            codes = [client.post("/forgot-password", data={"email": "me@example.com"}).status_code for _ in range(4)]
            assert codes == [200, 200, 200, 429]
        finally:
            app_module.limiter.reset()
            app_module.limiter.enabled = False


# ── Account deletion ───────────────────────────────────────────────────────

class TestAccountDeletion:
    def post(self, client, **overrides):
        data = {"password": PASSWORD, "confirm_text": "DELETE"}
        data.update(overrides)
        return client.post("/account/delete", data=data)

    def test_requires_login(self, client):
        assert client.get("/account/delete").status_code == 302
        assert client.post("/account/delete", data={"password": PASSWORD, "confirm_text": "DELETE"}).status_code == 302

    def test_confirmation_page_explains_what_is_deleted(self, logged_in):
        client, _ = logged_in
        page = client.get("/account/delete").get_data(as_text=True)
        assert "cannot be undone" in page and "Type DELETE" in page and 'type="password"' in page

    def test_deletes_account_and_all_owned_data_and_logs_out(self, logged_in, fake_store):
        client, user = logged_in
        fake_store.save_mood(user["id"], "calm")
        sid = "11111111-1111-1111-1111-111111111111"
        fake_store.ensure_chat_session(user["id"], sid)
        fake_store.save_chat_message(sid, "user", "hello")
        fake_store.save_journal(user["id"], "calm", "calm", "text", "hint", session_id=sid)
        token = fake_store.create_password_reset_token(user["id"])

        res = self.post(client)
        assert res.status_code == 200 and b"deleted" in res.data
        assert fake_store.get_user_by_id(user["id"]) is None
        assert fake_store.get_user_by_email("me@example.com") is None
        assert user["id"] not in {uid for uid, _ in fake_store.moods}
        assert sid not in fake_store.sessions and sid not in fake_store.messages
        assert fake_store.journals == []
        assert not fake_store.reset_password_with_token(token, "brand-new-pass")
        with client.session_transaction() as sess:
            assert "_user_id" not in sess
        assert client.get("/").status_code == 302
        assert login(client, "me@example.com").status_code == 200  # cannot sign in any more

    def test_wrong_password_deletes_nothing(self, logged_in, fake_store):
        client, user = logged_in
        res = self.post(client, password="wrong-password")
        assert res.status_code == 400 and b"not correct" in res.data
        assert fake_store.get_user_by_id(user["id"]) is not None
        assert client.get("/").status_code == 200  # still signed in

    @pytest.mark.parametrize("typed", ["", "delete", "DELET", "yes"])
    def test_must_type_the_confirmation_word(self, logged_in, fake_store, typed):
        client, user = logged_in
        assert self.post(client, confirm_text=typed).status_code == 400
        assert fake_store.get_user_by_id(user["id"]) is not None

    def test_only_the_signed_in_users_data_is_touched(self, logged_in, fake_store):
        client, user = logged_in
        other = fake_store.create_user("other@example.com", "Other", PASSWORD)
        fake_store.save_mood(other["id"], "happy")
        fake_store.ensure_chat_session(other["id"], "22222222-2222-2222-2222-222222222222")
        fake_store.save_journal(other["id"], "calm", "calm", "mine", "hint")
        # A forged user id in the form must be ignored: the account comes from the session.
        self.post(client, user_id=str(other["id"]), id=str(other["id"]))
        assert fake_store.get_user_by_id(user["id"]) is None
        assert fake_store.get_user_by_id(other["id"]) is not None
        assert "happy" in [m for uid, m in fake_store.moods if uid == other["id"]]
        assert "22222222-2222-2222-2222-222222222222" in fake_store.sessions
        assert len(fake_store.journals) == 1

    def test_get_cannot_delete(self, logged_in, fake_store):
        client, user = logged_in
        client.get("/account/delete?password=password123&confirm_text=DELETE")
        assert fake_store.get_user_by_id(user["id"]) is not None

    def test_requires_csrf_token(self, csrf_client, fake_store):
        user = fake_store.create_user("me@example.com", "Me", PASSWORD)
        sign_in(csrf_client, fake_store, user)
        assert csrf_client.post("/account/delete", data={"password": PASSWORD, "confirm_text": "DELETE"}).status_code == 400
        assert fake_store.get_user_by_id(user["id"]) is not None

    def test_is_rate_limited(self, logged_in):
        import app as app_module

        client, _ = logged_in
        app_module.limiter.enabled = True
        try:
            app_module.limiter.reset()
            codes = [self.post(client, password="wrong-password").status_code for _ in range(6)]
            assert codes == [400] * 5 + [429]
        finally:
            app_module.limiter.reset()
            app_module.limiter.enabled = False


# ── FakeStore contract (keeps the fake honest about the real store's behaviour) ──

class TestFakeStoreTokens:
    def test_expired_and_used_tokens_fail(self, fake_store):
        user = fake_store.create_user("a@example.com", "A", PASSWORD, email_verified=False)
        with pytest.raises(ValueError):
            fake_store.create_email_verification_token(user["id"], expires_hours=0)
        token = fake_store.create_email_verification_token(user["id"])
        assert fake_store.verify_email_token(token) is True
        assert fake_store.verify_email_token(token) is False


# ── The SMTP sender ────────────────────────────────────────────────────────

class TestEmailSender:
    def test_starttls_happens_before_login_and_message_is_well_formed(self, outbox):
        email_sender.send_verification_email("to@example.com", "https://connect.test/verify-email?token=abc")
        events = [e[0] for e in conftest.FakeSMTP.log]
        assert events == ["connect", "starttls", "login"]
        msg = outbox[0]
        assert msg["To"] == "to@example.com" and msg["From"] == "noreply@connect.test"
        assert "token=abc" in msg.get_content() and "24 hours" in msg.get_content()

    def test_port_465_uses_implicit_tls_and_no_starttls(self, monkeypatch, outbox):
        monkeypatch.setenv("SMTP_PORT", "465")
        email_sender.send_email("to@example.com", "s", "b")
        assert conftest.FakeSMTP.log[0][1] == "FakeSMTPSSL"
        assert ("starttls",) not in conftest.FakeSMTP.log

    def test_failure_raises_generic_error_without_leaking_details(self, caplog):
        conftest.FakeSMTP.fail_with = ConnectionRefusedError("secret-host:25 refused for smtp-user")
        with pytest.raises(email_sender.EmailDeliveryError) as info:
            email_sender.send_email("to@example.com", "s", "b")
        assert "secret-host" not in str(info.value) and "secret-host" not in caplog.text

    def test_unconfigured_raises(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "")
        with pytest.raises(email_sender.EmailDeliveryError):
            email_sender.send_email("to@example.com", "s", "b")

    def test_public_base_url_prefers_the_configured_https_origin(self, monkeypatch):
        assert email_sender.public_base_url("http://127.0.0.1:5000") == "https://connect.test"
        monkeypatch.setenv("PUBLIC_BASE_URL", "")
        assert email_sender.public_base_url("http://127.0.0.1:5000/") == "http://127.0.0.1:5000"


# ── Signing out must also forget the "remember me" cookie ──────────────────

class TestSignOut:
    def test_logout_really_signs_out_even_with_a_remember_cookie(self, client, fake_store):
        fake_store.create_user("me@example.com", "Me", PASSWORD)
        assert login(client, "me@example.com").status_code == 302  # also sets the remember cookie
        assert client.post("/logout").status_code == 302
        assert client.get("/login").status_code == 200  # not bounced home
        assert client.get("/").status_code == 302

    def test_deleting_an_account_also_forgets_the_remember_cookie(self, client, fake_store):
        fake_store.create_user("me@example.com", "Me", PASSWORD)
        login(client, "me@example.com")
        client.post("/account/delete", data={"password": PASSWORD, "confirm_text": "DELETE"})
        assert client.get("/login").status_code == 200
