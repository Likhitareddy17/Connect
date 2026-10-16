"""
Unit tests for services/journal_analysis.py.

The Groq request is mocked, so these tests do not make real network calls.

``generate_chat_response`` returns a ``ChatResult`` (reply, mood, summary, source).
Replies produced when the LLM is unavailable carry ``mood=None`` and
``source="fallback"``: the app must not store a made-up mood in the user's history.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from services.journal_analysis import (
    CRISIS_RESPONSES,
    SOURCE_CRISIS,
    SOURCE_FALLBACK,
    SOURCE_LLM,
    check_regex_crisis,
    generate_chat_response,
)


def history(*texts):
    return [{"role": "user", "content": t} for t in texts]


def llm_reply(content: str):
    res = MagicMock(status_code=200)
    res.raise_for_status = MagicMock()
    res.json.return_value = {"choices": [{"message": {"content": content}}]}
    return res


# ── Crisis detection ────────────────────────────────────────────────────────


class TestCrisisDetection:

    def test_detects_suicide_mention(self):
        reply, mood = check_regex_crisis("I don't want to be here, thinking about suicide")
        assert reply == CRISIS_RESPONSES["suicide_self_harm"]
        assert mood == "overwhelmed"

    def test_detects_self_harm_mention(self):
        reply, mood = check_regex_crisis("I keep wanting to hurt myself")
        assert reply == CRISIS_RESPONSES["suicide_self_harm"]
        assert mood == "overwhelmed"

    def test_case_insensitive(self):
        reply, mood = check_regex_crisis("I WANT TO DIE")
        assert reply == CRISIS_RESPONSES["suicide_self_harm"]
        assert mood == "overwhelmed"

    def test_ordinary_message_is_not_crisis(self):
        assert check_regex_crisis("I had a stressful day at college") == (None, None)

    def test_detects_abuse(self):
        reply, mood = check_regex_crisis("I am being abused at home")
        assert reply == CRISIS_RESPONSES["abuse_violence"]
        assert mood == "overwhelmed"

    def test_detects_domestic_violence(self):
        reply, mood = check_regex_crisis("I am experiencing domestic violence")
        assert reply == CRISIS_RESPONSES["abuse_violence"]
        assert mood == "overwhelmed"

    def test_detects_sexual_assault(self):
        reply, mood = check_regex_crisis("I experienced sexual assault")
        assert reply == CRISIS_RESPONSES["abuse_violence"]
        assert mood == "overwhelmed"

    def test_non_crisis_similar_wording(self):
        assert check_regex_crisis("I want to live a better life") == (None, None)

    @pytest.mark.parametrize("text", [
        "I want to kill myself",
        "i just want to die",
        "I don't want to be here anymore",
        "everyone would be better off without me",
        "I can't go on like this",
        "I'm going to end it all",
        "I want to end my life",
    ])
    def test_more_self_harm_phrasings(self, text):
        reply, mood = check_regex_crisis(text)
        assert reply == CRISIS_RESPONSES["suicide_self_harm"] and mood == "overwhelmed"

    @pytest.mark.parametrize("text", [
        "my husband hits me when he's drunk",
        "she beats me every night",
        "Someone is hurting me",
        "they keep hitting me",
    ])
    def test_more_abuse_phrasings(self, text):
        assert check_regex_crisis(text)[0] == CRISIS_RESPONSES["abuse_violence"]

    @pytest.mark.parametrize("text", [
        "it hit me that I'm just tired",             # idiom
        "the deadline is killing me",                # figurative
        "I'm dying to see my friends",               # figurative
        "my boss is hurting my chances of promotion",
        "work was exhausting but I'm okay",
        "I cut my finger while cooking",
    ])
    def test_everyday_language_not_flagged(self, text):
        assert check_regex_crisis(text) == (None, None)


# ── generate_chat_response ──────────────────────────────────────────────────


class TestGenerateChatResponse:

    def test_crisis_message_skips_llm(self, mocker):
        mock_post = mocker.patch("services.journal_analysis.requests.post")

        result = generate_chat_response(history("I want to end my life"))

        assert result.reply == CRISIS_RESPONSES["suicide_self_harm"]
        assert result.mood == "overwhelmed"
        assert result.summary == ""
        assert result.source == SOURCE_CRISIS and result.is_crisis
        mock_post.assert_not_called()

    def test_abuse_message_skips_llm(self, mocker):
        mock_post = mocker.patch("services.journal_analysis.requests.post")

        result = generate_chat_response(history("Someone is hurting me"))

        assert result.reply == CRISIS_RESPONSES["abuse_violence"]
        assert result.mood == "overwhelmed"
        assert result.summary == ""
        mock_post.assert_not_called()

    def test_missing_api_key_returns_safe_fallback(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": ""})

        result = generate_chat_response(history("I had a long day"))

        assert "not able to respond fully" in result.reply
        assert result.mood is None  # no real signal -> never recorded as the user's mood
        assert result.source == SOURCE_FALLBACK
        assert result.summary == ""

    def test_successful_groq_call_returns_reply_and_mood(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mocker.patch("services.journal_analysis.requests.post", return_value=llm_reply(
            '{"reply": "That sounds really tough.", "mood": "stressed"}'))

        result = generate_chat_response(history("I have so much pressure at work"))

        assert result.reply == "That sounds really tough."
        assert result.mood == "stressed"
        assert result.summary == ""
        assert result.source == SOURCE_LLM

    def test_handles_negation_via_model(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mocker.patch("services.journal_analysis.requests.post", return_value=llm_reply(
            '{"reply": "Glad to hear that.", "mood": "calm"}'))

        result = generate_chat_response(history("Honestly I'm not stressed about the exam anymore"))

        assert result.reply == "Glad to hear that."
        assert result.mood == "calm"
        assert result.summary == ""

    def test_invalid_mood_falls_back_to_calm(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mocker.patch("services.journal_analysis.requests.post", return_value=llm_reply(
            '{"reply": "I see.", "mood": "confused"}'))

        result = generate_chat_response(history("I had a long day"))

        assert result.reply == "I see."
        assert result.mood == "calm"  # model replied fine; only its label was unusable
        assert result.source == SOURCE_LLM

    def test_groq_failure_returns_safe_fallback(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
        mocker.patch("services.journal_analysis.requests.post", side_effect=Exception("timeout"))

        result = generate_chat_response(history("I had a long day"))

        assert "right here with you" in result.reply
        assert result.mood is None
        assert result.source == SOURCE_FALLBACK
        assert result.summary == ""

    def test_existing_rolling_summary_is_preserved(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": ""})
        existing_summary = "The user has been discussing work pressure."

        result = generate_chat_response(history("I had a long day"), existing_summary)

        assert "not able to respond fully" in result.reply
        assert result.mood is None
        assert result.summary == existing_summary

    def test_rolling_summary_updates_on_third_turn(self, mocker):
        mocker.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})

        chat_response = mocker.Mock()
        chat_response.raise_for_status.return_value = None
        chat_response.json.return_value = {
            "choices": [{"message": {"content": '{"reply": "I understand.", "mood": "calm"}'}}]
        }
        summary_response = mocker.Mock()
        summary_response.status_code = 200
        summary_response.json.return_value = {
            "choices": [{"message": {"content": "The user is discussing their emotional state."}}]
        }
        post_mock = mocker.patch(
            "services.journal_analysis.requests.post",
            side_effect=[chat_response, summary_response],
        )

        result = generate_chat_response(history("Message one", "Message two", "Message three"))

        assert result.reply == "I understand."
        assert result.mood == "calm"
        assert result.summary == "The user is discussing their emotional state."
        assert post_mock.call_count == 2


class TestChatPipelineEdgeCases:
    """Behaviour added with the history-window fix."""

    @pytest.fixture(autouse=True)
    def groq_key(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key")

    def test_crisis_checks_the_latest_message_not_the_first(self):
        with patch("services.journal_analysis.requests.post") as post:
            result = generate_chat_response(history("hi", "ok thanks", "I want to die"))
        post.assert_not_called()
        assert result.is_crisis

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
            return (llm_reply('{"reply": "ok", "mood": "calm"}') if kwargs["json"].get("max_tokens") == 400
                    else llm_reply("new summary"))

        with patch("services.journal_analysis.requests.post", side_effect=fake_post):
            # the window is always 6 long (6 % 3 == 0) but the thread has 7 messages -> no refresh
            generate_chat_response(history(*["m"] * 6), "old", total_messages=7)
            assert calls == [400]
            calls.clear()
            result = generate_chat_response(history(*["m"] * 6), "old", total_messages=9)
            assert 150 in calls and result.summary == "new summary"
