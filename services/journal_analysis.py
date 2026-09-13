"""
Chat companion for CONNECT's emotional module with Regex Safety Router,
Dynamic Rolling Summarization, and Groq Mood Classification.
"""
from __future__ import annotations

import json
import logging
import os
import re

import requests

from services.grounding import ALLOWED_MOODS

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-20b"

# Multi-Category Regex Patterns for Crisis Routing
SUICIDE_SELF_HARM_REGEX = re.compile(
    r"\b(kill|end)\s+(my\s*self|myself|my\s+life)\b|\bsuicid(e|al)\b|\b(want|wish)\s+to\s+die\b|\b(self-?harm|hurt\s+myself)\b|\bno\s+reason\s+to\s+live\b",
    re.IGNORECASE,
)

ABUSE_VIOLENCE_REGEX = re.compile(
    r"\b(being\s+abused|physically\s+beaten|domestic\s+violence|someone\s+is\s+hurting\s+me|hit\s+me|hit\s+myself|sexual\s+assault)\b",
    re.IGNORECASE,
)

CRISIS_RESPONSES = {
    "suicide_self_harm": (
        "I'm deeply glad you reached out, and your safety is the most important thing right now. "
        "Please reach out to someone who can support you immediately: in India, call the "
        "iCall helpline at 9152987821, or AASRA at 9820466726 (24/7). "
        "If you are in immediate danger, please call 112 or visit the nearest hospital. "
        "You do not have to carry this alone."
    ),
    "abuse_violence": (
        "I hear you, and I want to make sure you are in a safe place. "
        "If you are facing abuse or domestic danger, please connect with support services: "
        "in India, call the National Commission for Women Helpline at 7827170170 or emergency 112. "
        "Please reach out to local emergency services or a trusted person as soon as possible."
    ),
}

SYSTEM_PROMPT = (
    "You are CONNECT, an empathetic, logical, and emotionally grounded wellness companion. "
    "Listen and validate the user's feelings sincerely like a supportive peer or therapeutic guide, "
    "but NEVER claim to be a licensed therapist or medical professional. "
    "NEVER encourage self-harm, suicide, or abuse under any circumstances.\n\n"
    "Respond ONLY with a single valid JSON object in this exact schema:\n"
    '{"reply": "<your 2-3 sentence grounded reply>", "mood": "<one_of_allowed_moods>"}\n\n'
    "Allowed moods: " + ", ".join(ALLOWED_MOODS) + ".\n"
    "Categorize the mood strictly based on the user's latest statement, handling negation properly "
    "(e.g., 'not feeling calm' is NOT calm; 'devastated' is overwhelmed)."
)

SUMMARY_PROMPT = (
    "Summarize the core emotional state and main points of this conversation so far in 2-3 concise sentences. "
    "Focus on underlying emotional themes for ongoing context."
)

FALLBACK_MOOD = "calm"


def check_regex_crisis(text: str) -> tuple[str | None, str | None]:
    """Inspects incoming user text against distinct safety category patterns."""
    if SUICIDE_SELF_HARM_REGEX.search(text):
        return CRISIS_RESPONSES["suicide_self_harm"], "overwhelmed"
    if ABUSE_VIOLENCE_REGEX.search(text):
        return CRISIS_RESPONSES["abuse_violence"], "overwhelmed"
    return None, None


def update_rolling_summary(history: list[dict[str, str]], existing_summary: str, api_key: str) -> str:
    """Generates an updated summary of the dialogue context using Groq."""
    messages = [
        {"role": "system", "content": SUMMARY_PROMPT},
        {"role": "user", "content": f"Previous summary: {existing_summary}\nRecent messages: {json.dumps(history[-4:])}"},
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
    except Exception as exc:
        logger.warning("Failed to update rolling summary: %s", exc)
    return existing_summary


def generate_chat_response(
    conversation_history: list[dict[str, str]],
    rolling_summary: str = "",
) -> tuple[str, str, str]:
    """
    Processes chat message through Regex inspection, runs dynamic Groq analysis,
    and returns (reply, detected_mood, updated_rolling_summary).
    """
    latest_message = conversation_history[-1]["content"] if conversation_history else ""

    # Step 1: Regex Safety Inspection
    crisis_reply, crisis_mood = check_regex_crisis(latest_message)
    if crisis_reply:
        logger.info("Regex Crisis Safety Filter triggered.")
        return crisis_reply, crisis_mood, rolling_summary

    # Step 2: Groq API Setup
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.warning("GROQ_API_KEY is missing.")
        return (
            "I'm right here with you, but I am unable to connect fully right now. Let's take a quiet breath together.",
            FALLBACK_MOOD,
            rolling_summary,
        )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if rolling_summary:
        messages.append({"role": "system", "content": f"Context Summary of Chat History: {rolling_summary}"})

    for msg in conversation_history[-6:]:
        messages.append({
            "role": "assistant" if msg.get("role") == "assistant" else "user",
            "content": str(msg.get("content", ""))[:500],
        })

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
        raw_content = response.json()["choices"][0]["message"]["content"].strip()

        if not raw_content.startswith("{"):
            start, end = raw_content.find("{"), raw_content.rfind("}")
            if start != -1 and end != -1:
                raw_content = raw_content[start:end + 1]

        parsed = json.loads(raw_content)
        reply = str(parsed.get("reply", "")).strip()
        mood = str(parsed.get("mood", "")).strip().lower()

        if mood not in ALLOWED_MOODS:
            mood = FALLBACK_MOOD

        # Step 3: Rolling Summarization trigger every 3 turns
        updated_summary = rolling_summary
        if len(conversation_history) % 3 == 0:
            updated_summary = update_rolling_summary(conversation_history, rolling_summary, api_key)

        return reply, mood, updated_summary

    except Exception as exc:
        logger.error("Groq generation failed: %s", exc)
        return "I hear how much is going on right now. I'm right here with you—let's step back for a moment.", FALLBACK_MOOD, rolling_summary