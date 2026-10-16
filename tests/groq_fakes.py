"""A fake Groq endpoint that tells the safety classifier's call apart from the chat call."""
import json
from unittest.mock import MagicMock


def _response(content: str):
    res = MagicMock(status_code=200)
    res.raise_for_status = MagicMock()
    res.json.return_value = {"choices": [{"message": {"content": content}}]}
    return res


class FakeGroq:
    """Use as ``patch("requests.post", side_effect=FakeGroq(...))``; inspect ``.classifier_calls`` / ``.chat_calls``."""

    def __init__(self, verdict=None, classifier_raises=None, classifier_raw=None,
                 reply="I hear you.", mood="calm"):
        self.verdict = verdict
        self.classifier_raises = classifier_raises
        self.classifier_raw = classifier_raw
        self.reply, self.mood = reply, mood
        self.classifier_calls, self.chat_calls = [], []

    def __call__(self, url, **kwargs):
        payload = kwargs["json"]
        first_system = payload["messages"][0]["content"]
        if "safety classifier" in first_system:
            self.classifier_calls.append(payload)
            if self.classifier_raises:
                raise self.classifier_raises
            raw = self.classifier_raw if self.classifier_raw is not None else json.dumps(self.verdict)
            return _response(raw)
        if payload.get("max_tokens") == 150:  # rolling-summary call
            return _response("summary")
        self.chat_calls.append(payload)
        return _response(json.dumps({"reply": self.reply, "mood": self.mood}))
