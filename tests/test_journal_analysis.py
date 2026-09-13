"""
Unit tests for journal_analysis.py.
No real network calls are made — the Groq request is mocked.
"""
import os

from services.journal_analysis import (
    CRISIS_RESPONSE,
    generate_chat_response,
    is_crisis_message,
)


class TestIsCrisisMessage:
    def test_detects_suicide_mention(self):
        assert is_crisis_message("I don't want to be here, thinking about suicide")

    def test_detects_self_harm_mention(self):
        assert is_crisis_message("I keep wanting to hurt myself")

    def test_case_insensitive(self):
        assert is_crisis_message("I WANT TO DIE")

    def test_ordinary_message_is_not_crisis(self):
        assert not is_crisis_message("I had a stressful day at college")


class TestGenerateChatResponse:
    def test_crisis_message_skips_the_llm_entirely(self, mocker):
        mock_post = mocker.patch("services.journal_analysis.requests.post")
        history = [{"role": "user", "content": "I want to end my life"}]

        reply, mood = generate_chat_response(history)

        assert reply == CRISIS_RESPONSE
        assert mood == "overwhelmed"
        mock_post.assert_not_called()

    def test_missing_api_key_returns_safe_fallback(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": ""})
        history = [{"role": "user", "content": "I had a long day"}]

        reply, mood = generate_chat_response(history)

        assert "not able to respond fully" in reply
        assert mood == "calm"

    def test_successful_groq_call_returns_reply_and_mood(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mock_response = mocker.Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"reply": "That sounds really tough.", "mood": "stressed"}'}}]
        }
        mocker.patch("services.journal_analysis.requests.post", return_value=mock_response)
        history = [{"role": "user", "content": "I have so much pressure at work"}]

        reply, mood = generate_chat_response(history)

        assert reply == "That sounds really tough."
        assert mood == "stressed"

    def test_handles_negation_via_the_model_not_keywords(self, mocker):
        # The model, not a keyword list, decides the mood — so it can
        # correctly read "not stressed" as NOT stressed. This test just
        # confirms we trust whatever valid mood the model returns.
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mock_response = mocker.Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"reply": "Glad to hear that.", "mood": "calm"}'}}]
        }
        mocker.patch("services.journal_analysis.requests.post", return_value=mock_response)
        history = [{"role": "user", "content": "Honestly I'm not stressed about the exam anymore"}]

        reply, mood = generate_chat_response(history)

        assert mood == "calm"

    def test_invalid_mood_from_model_falls_back_to_calm(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mock_response = mocker.Mock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"reply": "I see.", "mood": "confused"}'}}]
        }
        mocker.patch("services.journal_analysis.requests.post", return_value=mock_response)
        history = [{"role": "user", "content": "I had a long day"}]

        reply, mood = generate_chat_response(history)

        assert mood == "calm"

    def test_groq_failure_returns_safe_fallback(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mocker.patch("services.journal_analysis.requests.post", side_effect=Exception("timeout"))
        history = [{"role": "user", "content": "I had a long day"}]

        reply, mood = generate_chat_response(history)

        assert "right here with you" in reply
        assert mood == "calm"