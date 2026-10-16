"""Route tests for the chat API, including regressions for bugs found in review."""
from unittest.mock import MagicMock, patch

import pytest


def llm_ok(reply="I hear you.", mood="calm"):
    res = MagicMock(status_code=200)
    res.raise_for_status = MagicMock()
    res.json.return_value = {"choices": [{"message": {"content": f'{{"reply": "{reply}", "mood": "{mood}"}}'}}]}
    return res


@pytest.fixture(autouse=True)
def groq_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


def send(client, text):
    return client.post("/api/chat/message", json={"message": text})


class TestChatBasics:
    def test_requires_login(self, client):
        assert send(client, "hi").status_code == 302

    def test_empty_message_rejected(self, logged_in):
        client, _ = logged_in
        assert send(client, "   ").status_code == 400

    def test_first_message_creates_reflection_card(self, logged_in, fake_store):
        client, user = logged_in
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            data = send(client, "rough day").get_json()
        assert data["is_first_message"] is True
        assert len(fake_store.journals) == 1
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            assert send(client, "still rough").get_json()["is_first_message"] is False
        assert len(fake_store.journals) == 1  # only the first message makes a card

    def test_mood_is_recorded_for_real_replies(self, logged_in, fake_store):
        client, user = logged_in
        with patch("services.journal_analysis.requests.post", return_value=llm_ok(mood="stressed")):
            send(client, "so much work")
        assert fake_store.latest_mood(user["id"]) == "stressed"

    def test_fallback_reply_does_not_pollute_mood_analytics(self, logged_in, fake_store, monkeypatch):
        client, user = logged_in
        monkeypatch.setenv("GROQ_API_KEY", "")  # LLM unavailable
        data = send(client, "I feel flat").get_json()
        assert data["degraded"] is True and data["detected_mood"] is None
        assert fake_store.moods == []  # no fabricated "calm" check-in


class TestCrisisHandling:
    def test_crisis_flag_returned_and_llm_not_called(self, logged_in):
        client, _ = logged_in
        with patch("services.journal_analysis.requests.post") as post:
            data = send(client, "I want to kill myself").get_json()
        post.assert_not_called()
        assert data["crisis"] is True
        assert "112" in data["reply"]

    def test_regression_crisis_still_detected_after_long_conversation(self, logged_in):
        """Original bug: history was fetched ORDER BY ASC LIMIT 6 (= the *first* six
        messages), so from message 7 on the crisis screen inspected stale text."""
        client, _ = logged_in
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            for i in range(8):
                send(client, f"just chatting {i}")
        with patch("services.journal_analysis.requests.post") as post:
            data = send(client, "I don't want to be here anymore").get_json()
        post.assert_not_called()
        assert data["crisis"] is True

    def test_regression_model_sees_latest_messages_in_long_thread(self, logged_in):
        client, _ = logged_in
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            for i in range(10):
                send(client, f"message number {i}")
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()) as post:
            send(client, "the newest message")
        sent = [m["content"] for m in post.call_args_list[0].kwargs["json"]["messages"] if m["role"] == "user"]
        assert sent[-1] == "the newest message"
        assert "message number 0" not in sent  # oldest messages fall out of the window


class TestThreadOwnership:
    """Original bug: ?session_id= was trusted blindly, letting user B read/write A's thread."""

    def _make_user_with_thread(self, fake_store):
        owner = fake_store.create_user("owner@example.com", "Owner", "password123")
        fake_store.ensure_chat_session(owner["id"], "11111111-1111-4111-8111-111111111111")
        fake_store.save_chat_message("11111111-1111-4111-8111-111111111111", "user", "my private secret")
        return owner

    def test_cannot_view_another_users_thread(self, logged_in, fake_store):
        client, _ = logged_in
        self._make_user_with_thread(fake_store)
        page = client.get("/emotional?session_id=11111111-1111-4111-8111-111111111111").get_data(as_text=True)
        assert "my private secret" not in page

    def test_cannot_write_into_another_users_thread(self, logged_in, fake_store):
        client, _ = logged_in
        self._make_user_with_thread(fake_store)
        client.get("/emotional?session_id=11111111-1111-4111-8111-111111111111")
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            data = send(client, "intruder message").get_json()
        assert data["session_id"] != "11111111-1111-4111-8111-111111111111"
        victim = fake_store.messages["11111111-1111-4111-8111-111111111111"]
        assert [m["content"] for m in victim] == ["my private secret"]

    def test_cannot_delete_another_users_thread(self, logged_in, fake_store):
        client, _ = logged_in
        self._make_user_with_thread(fake_store)
        res = client.delete("/api/chat/session/11111111-1111-4111-8111-111111111111")
        assert res.status_code == 404
        assert fake_store.messages["11111111-1111-4111-8111-111111111111"]  # untouched

    def test_owner_can_delete_own_thread(self, logged_in, fake_store):
        client, user = logged_in
        with patch("services.journal_analysis.requests.post", return_value=llm_ok()):
            sid = send(client, "hello").get_json()["session_id"]
        assert client.delete(f"/api/chat/session/{sid}").status_code == 200
        assert sid not in fake_store.sessions
        assert fake_store.journals == []  # reflection card removed with the thread

    def test_garbage_session_id_is_ignored(self, logged_in):
        client, _ = logged_in
        assert client.get("/emotional?session_id=../../etc/passwd").status_code == 200
