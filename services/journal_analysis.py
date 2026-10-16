"""Chat companion for CONNECT's emotional module.

Pipeline for every user message:
  1. Safety assessment of the *latest user message* (services/safety.py): a local
     regex layer, then a model classifier. A crisis verdict returns a fixed safe
     reply and the chat model is never called; a "concern" verdict makes the chat
     model respond with extra care.
  2. Groq chat completion returning JSON ``{"reply", "mood"}``.
  3. Rolling summary refresh every few turns to keep prompts short.

``generate_chat_response`` returns a :class:`ChatResult` whose ``source``
tells the caller what happened, so degraded replies (LLM down / no key) are
never mistaken for real mood data.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import requests

from services.grounding import ALLOWED_MOODS
from services.safety import (  # noqa: F401 - several names re-exported for existing importers/tests
    CONCERN_ADDENDUM,
    CRISIS_RESPONSES,
    DEFAULT_GROQ_MODEL,
    GROQ_URL,
    LEVEL_NONE,
    SOURCE_REGEX_ONLY,
    SOURCE_UNAVAILABLE,
    SafetyAssessment,
    assess_message,
    check_regex_crisis,
    crisis_reply_for,
)

logger = logging.getLogger(__name__)

GROQ_MODEL = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

SOURCE_CRISIS = "crisis"
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "fallback"

# History window sent to the model, and how often the summary is refreshed.
HISTORY_WINDOW = 6
SUMMARY_EVERY_N_MESSAGES = 3

SYSTEM_PROMPT = (
    "You are CONNECT, an empathetic, grounded wellness companion. "
    "Listen and validate the user's feelings like a supportive peer, "
    "but NEVER claim to be a licensed therapist or medical professional, "
    "and never give medical, diagnostic or medication advice. "
    "NEVER encourage self-harm, suicide, or abuse under any circumstances.\n\n"
    "Respond ONLY with a single valid JSON object in this exact schema:\n"
    '{"reply": "<your 2-3 sentence grounded reply>", "mood": "<one_of_allowed_moods>"}\n\n'
    "Allowed moods: " + ", ".join(ALLOWED_MOODS) + ".\n"
    "Categorize the mood strictly from the user's latest statement, handling negation "
    "(e.g. 'not feeling calm' is NOT calm; 'devastated' is overwhelmed)."
)

SUMMARY_PROMPT = (
    "Summarize the core emotional state and main points of this conversation so far in "
    "2-3 concise sentences. Focus on underlying emotional themes for ongoing context."
)

FALLBACK_MOOD = "calm"
FALLBACK_NO_KEY = (
    "I'm right here with you, but I'm not able to respond fully right now. "
    "Let's take a quiet breath together."
)
FALLBACK_ERROR = (
    "I hear how much is going on right now. I'm right here with you — let's step back for a moment."
)


@dataclass(frozen=True)
class ChatResult:
    reply: str
    mood: str | None  # None => no reliable mood signal; do not record it
    summary: str
    source: str  # SOURCE_CRISIS | SOURCE_LLM | SOURCE_FALLBACK
    risk_level: str = LEVEL_NONE          # safety verdict: none | concern | crisis
    safety_source: str = SOURCE_REGEX_ONLY  # which layer decided (see services.safety)

    @property
    def is_crisis(self) -> bool:
        return self.source == SOURCE_CRISIS

    @property
    def needs_support_note(self) -> bool:
        """Classifier flagged 'concern': show the gentle resources note in the UI."""
        return self.risk_level == "concern"

    @property
    def safety_degraded(self) -> bool:
        return self.safety_source == SOURCE_UNAVAILABLE


def update_rolling_summary(history: list[dict[str, str]], existing_summary: str, api_key: str) -> str:
    """Refresh the conversation summary; keep the old one on any failure."""
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {
            "role": "user",
            "content": f"Previous summary: {existing_summary}\nRecent messages: {json.dumps(history[-4:])}",
        },
    ]
    try:
        res = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": 0.3, "max_tokens": 150},
            timeout=8,
        )
        if res.status_code == 200:
            return res.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001 - summary is best-effort
        logger.warning("Failed to update rolling summary: %s", exc)
    return existing_summary


def _parse_model_json(raw: str) -> dict:
    raw = raw.strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start : end + 1]
    return json.loads(raw)


def generate_chat_response(
    conversation_history: list[dict[str, str]],
    rolling_summary: str = "",
    total_messages: int | None = None,
) -> ChatResult:
    """Produce the companion's reply.

    ``conversation_history`` must be in chronological order and end with the
    user's *latest* message — that is the message screened for crisis language.
    ``total_messages`` is the real thread length (the history passed in is only
    a window); it decides when the rolling summary is refreshed.
    """
    api_key = os.getenv("GROQ_API_KEY")

    # Safety first: regex (local, instant) then the model classifier (if configured).
    assessment = assess_message(conversation_history, api_key)
    logger.info("Safety assessment: level=%s category=%s source=%s",
                assessment.level, assessment.category, assessment.source)
    safety = {"risk_level": assessment.level, "safety_source": assessment.source}

    if assessment.is_crisis:
        return ChatResult(crisis_reply_for(assessment.category), "overwhelmed", rolling_summary,
                          SOURCE_CRISIS, **safety)

    if not api_key:
        logger.warning("GROQ_API_KEY is missing; returning fallback reply.")
        return ChatResult(FALLBACK_NO_KEY, None, rolling_summary, SOURCE_FALLBACK, **safety)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if rolling_summary:
        messages.append({"role": "system", "content": f"Context summary of earlier chat: {rolling_summary}"})
    if assessment.is_concern:
        messages.append({"role": "system", "content": CONCERN_ADDENDUM})
    for msg in conversation_history[-HISTORY_WINDOW:]:
        messages.append(
            {
                "role": "assistant" if msg.get("role") == "assistant" else "user",
                "content": str(msg.get("content", ""))[:500],
            }
        )

    try:
        response = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0.5,
                "max_tokens": 400,
                "response_format": {"type": "json_object"},
                "reasoning_format": "hidden",
                "reasoning_effort": "low",
            },
            timeout=10,
        )
        response.raise_for_status()
        parsed = _parse_model_json(response.json()["choices"][0]["message"]["content"])

        reply = str(parsed.get("reply", "")).strip()
        if not reply:
            raise ValueError("model returned an empty reply")
        mood = str(parsed.get("mood", "")).strip().lower()
        if mood not in ALLOWED_MOODS:
            mood = FALLBACK_MOOD

        summary = rolling_summary
        count = total_messages if total_messages is not None else len(conversation_history)
        if count % SUMMARY_EVERY_N_MESSAGES == 0:
            summary = update_rolling_summary(conversation_history, rolling_summary, api_key)
        return ChatResult(reply, mood, summary, SOURCE_LLM, **safety)

    except Exception as exc:  # noqa: BLE001 - never surface LLM errors to the user
        logger.error("Groq generation failed: %s", exc)
        return ChatResult(FALLBACK_ERROR, None, rolling_summary, SOURCE_FALLBACK, **safety)
