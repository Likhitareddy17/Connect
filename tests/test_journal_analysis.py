"""Unit tests for the chat pipeline: crisis screening, LLM parsing, fallbacks."""
from unittest.mock import MagicMock, patch

import pytest

from services.journal_analysis import (
    SOURCE_CRISIS,
    SOURCE_FALLBACK,
    SOURCE_LLM,
    check_regex_crisis,
    generate_chat_response,
)


def history(*texts):
    return [{"role": "user", "content": t} for t in texts]


def llm_reply(content: str, status: int = 200):
    res = MagicMock(status_code=status)
    res.raise_for_status = MagicMock()
    res.json.return_value = {"choices": [{"message": {"content": content}}]}
    return res


@pytest.fixture(autouse=True)
def groq_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")


class TestCrisisScreen:
    @pytest.mark.parametrize("text", [
        "I want to kill myself",
        "I've been thinking about suicide",
        "i just want to die",
        "I don't want to be here anymore",
        "everyone would be better off without me",
        "I keep hurting myself",
        "I can't go on like this",
        "I'm going to end it all",
    ])
    def test_self_harm_detected(self, text):
        reply, mood = check_regex_crisis(text)
        assert reply is not None and mood == "overwhelmed"

    @pytest.mark.parametrize("text", [
        "my husband hits me when he's drunk",
        "she beats me every night",
        "I was sexually assaulted last year",
        "I'm being abused at home",
    ])
    def test_abuse_detected(self, text):
        reply, _ = check_regex_crisis(text)
        assert reply is not None

    @pytest.mark.parametrize("text", [
        "it hit me that I'm just tired",            # 'hit me' idiom
        "the deadline is killing me",                # figurative
        "I'm dying to see my friends",               # figurative
        "work was exhausting but I'm okay",
        "I feel anxious about my exam",
        "I cut my finger while cooking",
    ])
    def test_everyday_language_not_flagged(self, text):
        assert check_regex_crisis(text) == (None, None)


class TestGenerateChatResponse:
    def test_crisis_skips_llm_entirely(self):
        with patch("services.journal_analysis.requests.post") as post:
            result = generate_chat_response(history("I want to kill myself"))
        post.assert_not_called()
        assert result.source == SOURCE_CRISIS and result.is_crisis
        assert result.mood == "overwhelmed"

    def test_crisis_checks_the_latest_message_not_the_first(self):
        with patch("services.journal_analysis.requests.post") as post:
            result = generate_chat_response(history("hi", "ok thanks", "I want to die"))
        post.assert_not_called()
        assert result.is_crisis

    def test_missing_api_key_gives_fallback_without_mood(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "")
        result = generate_chat_response(history("I feel low"))
        assert result.source == SOURCE_FALLBACK
        assert result.mood is None  # must not be recorded as a real mood

    def test_llm_success(self):
        with patch("services.journal_analysis.requests.post",
                   return_value=llm_reply('{"reply": "I hear you.", "mood": "stressed"}')):
            result = generate_chat_response(history("I'm nervous"))
        assert (result.reply, result.mood, result.source) == ("I hear you.", "stressed", SOURCE_LLM)

    def test_invalid_mood_from_model_is_normalised(self):
        with patch("services.journal_analysis.requests.post",
                   return_value=llm_reply('{"reply": "Okay.", "mood": "ecstatic-banana"}')):
            result = generate_chat_response(history("hello"))
        assert result.mood == "calm" and result.source == SOURCE_LLM

    def test_json_wrapped_in_prose_is_extracted(self):
        raw = 'Sure! {"reply": "Take a breath.", "mood": "calm"} hope that helps'
        with patch("services.journal_analysis.requests.post", return_value=llm_reply(raw)):
            assert generate_chat_response(history("hi")).reply == "Take a breath."

    def test_garbage_model_output_falls_back(self):
        with patch("services.journal_analysis.requests.post", return_value=llm_reply("not json at all")):
            result = generate_chat_response(history("hi"))
        assert result.source == SOURCE_FALLBACK and result.mood is None

    def test_empty_reply_falls_back(self):
        with patch("services.journal_analysis.requests.post",
                   return_value=llm_reply('{"reply": "  ", "mood": "calm"}')):
            assert generate_chat_response(history("hi")).source == SOURCE_FALLBACK

    def test_network_error_falls_back(self):
        with patch("services.journal_analysis.requests.post", side_effect=TimeoutError("slow")):
            result = generate_chat_response(history("hi"))
        assert result.source == SOURCE_FALLBACK

    def test_only_recent_window_is_sent_and_truncated(self):
        long_history = history(*[f"message {i} " + "x" * 600 for i in range(10)])
        with patch("services.journal_analysis.requests.post",
                   return_value=llm_reply('{"reply": "ok", "mood": "calm"}')) as post:
            generate_chat_response(long_history, total_messages=10)
        sent = post.call_args.kwargs["json"]["messages"]
        user_msgs = [m for m in sent if m["role"] == "user"]
        assert len(user_msgs) == 6
        assert all(len(m["content"]) <= 500 for m in user_msgs)

    def test_summary_refreshes_on_real_thread_length_not_window_size(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs["json"].get("max_tokens"))
            return llm_reply('{"reply": "ok", "mood": "calm"}') if kwargs["json"].get("max_tokens") == 400 \
                else llm_reply("new summary")

        with patch("services.journal_analysis.requests.post", side_effect=fake_post):
            # window is always 6 long (6 % 3 == 0) but the thread really has 7 messages -> no refresh
            generate_chat_response(history(*["m"] * 6), "old", total_messages=7)
            assert calls == [400]
            calls.clear()
            result = generate_chat_response(history(*["m"] * 6), "old", total_messages=9)
            assert 150 in calls and result.summary == "new summary"
