from datetime import datetime


# The single source of truth for valid moods — imported by app.py (to
# validate user input) and journal_analysis.py (to tell Groq which moods
# it's allowed to pick).
ALLOWED_MOODS = ["calm", "stressed", "overwhelmed", "grateful", "low-energy"]


PRACTICES = [
    {
        "id": "walking",
        "icon": "step",
        "title": "Short walk",
        "detail": "Walk without scrolling for seven minutes.",
        "moods": ["stressed", "overwhelmed", "low-energy"],
        "times": ["morning", "afternoon"],
        "goals": ["grounding", "energy"],
    },
    {
        "id": "hydration",
        "icon": "water",
        "title": "Hydrate and pause",
        "detail": "Drink water, then sit still for ten breaths.",
        "moods": ["low-energy", "stressed"],
        "times": ["morning", "afternoon", "evening"],
        "goals": ["grounding", "energy"],
    },
    {
        "id": "silence",
        "icon": "quiet",
        "title": "Two minutes of silence",
        "detail": "Put the phone down and let the room be enough.",
        "moods": ["calm", "overwhelmed", "stressed"],
        "times": ["morning", "evening"],
        "goals": ["grounding", "clarity"],
    },
    {
        "id": "stretching",
        "icon": "soft",
        "title": "Soft stretch",
        "detail": "Relax your jaw, neck, shoulders, and hands.",
        "moods": ["stressed", "low-energy"],
        "times": ["afternoon", "evening"],
        "goals": ["energy", "grounding"],
    },
    {
        "id": "music",
        "icon": "note",
        "title": "Calming music",
        "detail": "Play one instrumental track and do nothing else.",
        "moods": ["overwhelmed", "low-energy"],
        "times": ["afternoon", "evening"],
        "goals": ["grounding"],
    },
    {
        "id": "sunlight",
        "icon": "sun",
        "title": "Find sunlight",
        "detail": "Stand near a window or step outside briefly.",
        "moods": ["low-energy"],
        "times": ["morning", "afternoon"],
        "goals": ["energy"],
    },
    {
        "id": "gratitude",
        "icon": "spark",
        "title": "One gratitude line",
        "detail": "Write one thing that quietly supported you.",
        "moods": ["grateful", "calm"],
        "times": ["evening"],
        "goals": ["gratitude"],
    },
    {
        "id": "breath",
        "icon": "circle",
        "title": "Breath reset",
        "detail": "Inhale for 4, exhale for 6, four rounds.",
        "moods": ["stressed", "overwhelmed"],
        "times": ["morning", "afternoon", "evening"],
        "goals": ["clarity", "grounding"],
    },
]


def time_of_day(now=None):
    hour = (now or datetime.now()).hour
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    return "evening"


def recommend_practices(mood="calm", goal="grounding", limit=3):
    period = time_of_day()
    scored = []
    for practice in PRACTICES:
        score = 0
        if mood in practice["moods"]:
            score += 3
        if goal in practice["goals"]:
            score += 2
        if period in practice["times"]:
            score += 1
        scored.append((score, practice))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [practice for _, practice in scored[:limit]]