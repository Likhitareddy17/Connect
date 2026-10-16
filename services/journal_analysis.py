"""Chat companion for CONNECT's emotional module.

Pipeline for every user message:
  1. Crisis screen (regex) on the *latest user message* — runs before any LLM
     call, so a crisis message never depends on a third-party service.
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
import re
from dataclasses import dataclass

import requests

from services.grounding import ALLOWED_MOODS

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

SOURCE_CRISIS = "crisis"
SOURCE_LLM = "llm"
SOURCE_FALLBACK = "fallback"

# History window sent to the model, and how often the summary is refreshed.
HISTORY_WINDOW = 6
SUMMARY_EVERY_N_MESSAGES = 3

# ── Crisis screening ────────────────────────────────────────────────────────
# This is a deliberately conservative *baseline* screen, not a clinical tool.
# It trades some recall for few false alarms on everyday language ("it hit
# me that..."). Tests in tests/test_crisis.py pin both directions.

_SELF_HARM_PATTERNS = [
    r"\b(kill|end|take)\s+(my\s*self|myself|my\s+(own\s+)?life)\b",
    r"\bsuicid(e|al)\b",
    r"\b(want|wish|wanna)\s+to\s+die\b",
    r"\b(self[\s-]?harm(ing)?|hurt(ing)?\s+myself|harm(ing)?\s+myself|cut(ting)?\s+myself)\b",
    r"\bno\s+(reason|point)\s+(to|in)\s+(live|living|go\s+on|going\s+on)\b",
    r"\b(don'?t|do\s+not)\s+want\s+to\s+(be\s+here|live|exist|wake\s+up)(\s+anymore)?\b",
    r"\b(better|easier)\s+(off\s+)?without\s+me\b",
    r"\b(end|ending)\s+it\s+all\b",
    r"\bcan'?t\s+(go\s+on|do\s+this\s+anymore|take\s+it\s+anymore)\b",
]

_ABUSE_PATTERNS = [
    r"\b(being|am\s+being|getting)\s+(abused|beaten|assaulted)\b",
    r"\b(he|she|they|someone|my\s+\w+)\s+(hit|hits|beat|beats|hurts?|threatens?)\s+me\b",
    r"\bdomestic\s+violence\b",
    r"\bsexual(ly)?\s+(assault(ed)?|abus(e|ed))\b",
    r"\b(raped|molested)\b",
]

SUICIDE_SELF_HARM_REGEX = re.compile("|".join(_SELF_HARM_PATTERNS), re.IGNORECASE)
ABUSE_VIOLENCE_REGEX = re.compile("|".join(_ABUSE_PATTERNS), re.IGNORECASE)

# Helpline numbers are shown to users verbatim — keep them in one place and
# re-verify before every release.
CRISIS_RESPONSES = {
    "suicide_self_harm": (
        "I'm really glad you told me, and your safety matters most right now. "
        "Please reach out to someone who can support you immediately: in India, call the "
        "iCall helpline at 9152987821, or AASRA at 9820466726 (24/7). "
        "If you are in immediate danger, please call 112 or go to the nearest hospital. "
        "You do not have to carry this alone."
    ),
    "abuse_violence": (
        "I hear you, and I want to make sure you are safe. "
        "If you are facing abuse or domestic danger, please connect with support: "
        "in India, call the National Commission for Women helpline at 7827170170, "
        "or emergency services at 112. "
        "Please reach out to a trusted person as soon as you can."
    ),
}

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
    "I'm right here with you, but I can't connect fully right now. "
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

    @property
    def is_crisis(self) -> bool:
        return self.source == SOURCE_CRISIS


def check_regex_crisis(text: str) -> tuple[str | None, str | None]:
    """Return (crisis_reply, mood) if the text matches a crisis category."""
    if SUICIDE_SELF_HARM_REGEX.search(text):
        return CRISIS_RESPONSES["suicide_self_harm"], "overwhelmed"
    if ABUSE_VIOLENCE_REGEX.search(text):
        return CRISIS_RESPONSES["abuse_violence"], "overwhelmed"
    return None, None


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
    latest_message = conversation_history[-1]["content"] if conversation_history else ""

    crisis_reply, crisis_mood = check_regex_crisis(latest_message)
    if crisis_reply:
        logger.info("Crisis safety screen triggered (LLM skipped).")
        return ChatResult(crisis_reply, crisis_mood, rolling_summary, SOURCE_CRISIS)

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY is missing; returning fallback reply.")
        return ChatResult(FALLBACK_NO_KEY, None, rolling_summary, SOURCE_FALLBACK)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if rolling_summary:
        messages.append({"role": "system", "content": f"Context summary of earlier chat: {rolling_summary}"})
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
        return ChatResult(reply, mood, summary, SOURCE_LLM)

    except Exception as exc:  # noqa: BLE001 - never surface LLM errors to the user
        logger.error("Groq generation failed: %s", exc)
        return ChatResult(FALLBACK_ERROR, None, rolling_summary, SOURCE_FALLBACK)
