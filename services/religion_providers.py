"""
religion_providers.py — one ReligionProvider per supported religion.

Every provider exposes the same two methods so app.py and spiritual.html
never need to know which religion they're rendering:

    get_day(target: date) -> dict   # today's (or any day's) detail
    upcoming(from_date, limit) -> list[dict]   # [{date, name}, ...]

`get_day` always returns these common keys, used by the generic parts
of spiritual.html:

    religion        "hindu" | "islam" | "christianity" | "sikh" | "atheist"
    date            ISO date string
    weekday         "Thursday"
    calendar_label  short line under the date (Hijri date, liturgical
                    season, Vikram Samvat, Nanakshahi date, or "")
    headline        the big title for the day
    occasion        {"name": ..., "is_major": bool} or None
    guidance_title  heading for the guidance section
    guidance_steps  list[str] — what to actually do today
    extra           religion-specific payload the template's per-religion
                    block knows how to render (for Hindu this is simply
                    the existing spiritual_api.today() dict, unchanged)
    jaap_allowed    bool — only True for Hindu (controls whether the
                    voice/manual jaap counter section renders)

IMPORTANT — data-accuracy notes (tell the user, don't hide this):
- Hindu panchang: live via Prokerala where configured, else mock (unchanged
  from before).
- Islam: prayer timings + Hijri date are LIVE via the free Aladhan API.
- Christianity: season + fixed/movable feast dates are COMPUTED (Easter via
  the standard Gregorian algorithm), which is accurate. The daily
  reflection text is a small curated rotation, not an official lectionary
  reading.
- Sikh: gurpurabs are CURATED approximate dates for the current cycle —
  several Sikh observances follow the lunar Bikrami calendar and shift
  year to year, so these should be verified against a Gurdwara/SGPC
  calendar rather than treated as authoritative.
- Atheist: a curated list of fixed-date secular/civic observances. No
  devotional content.
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import requests

from services.spiritual_api import spiritual_api

logger = logging.getLogger(__name__)

ALLOWED_RELIGIONS = ("hindu", "islam", "christianity", "sikh", "atheist")
RELIGION_LABELS = {
    "hindu": "Hinduism",
    "islam": "Islam",
    "christianity": "Christianity",
    "sikh": "Sikhism",
    "atheist": "Atheist / no religion",
}


# ── Hindu — thin wrapper around the existing, unchanged spiritual_api ──────

class HinduProvider:
    religion = "hindu"

    # A small curated list of major pan-India festival dates. These are
    # approximate community-calendar dates (lunar festivals shift yearly)
    # — good enough for an "upcoming" preview, not authoritative.
    _CURATED_UPCOMING_2026 = [
        (date(2026, 10, 11), "Navratri begins"),
        (date(2026, 10, 20), "Dussehra / Vijayadashami"),
        (date(2026, 10, 27), "Karva Chauth"),
        (date(2026, 11, 8), "Diwali"),
        (date(2026, 11, 10), "Bhai Dooj"),
    ]

    def get_day(self, target: date) -> dict:
        daily = spiritual_api.today(target)
        occasion = None
        if daily.get("festival") and daily["festival"] != "No major festival in local data":
            occasion = {"name": daily["festival"], "is_major": bool(daily.get("fast", {}).get("observed"))}
        return {
            "religion": self.religion,
            "date": daily["date"],
            "weekday": daily["panchangam"].get("weekday") or target.strftime("%A"),
            "calendar_label": daily["panchangam"].get("lunar_month", ""),
            "headline": daily["deity"],
            "occasion": occasion,
            "guidance_title": "How to observe today" if occasion else "Today's worship",
            "guidance_steps": daily["fast"]["how"] if occasion else [daily["reflection"]],
            "extra": daily,
            "jaap_allowed": True,
        }

    def upcoming(self, from_date: date, limit: int = 5) -> list[dict]:
        items = [{"date": d.isoformat(), "name": n} for d, n in self._CURATED_UPCOMING_2026 if d >= from_date]
        return items[:limit]


# ── Islam — live via the free Aladhan API ──────────────────────────────────

_ISLAM_LAT = float(os.getenv("PANCHANG_LAT", "17.385"))
_ISLAM_LON = float(os.getenv("PANCHANG_LON", "78.4867"))

# Curated, approximate Gregorian dates for major Islamic occasions (Hijri
# dates are lunar and shift ~11 days earlier each Gregorian year — verify
# locally, especially since moon-sighting can shift these by a day).
_ISLAM_CURATED_2026 = [
    (date(2026, 2, 18), "Start of Ramadan (approx.)"),
    (date(2026, 3, 20), "Eid al-Fitr (approx.)"),
    (date(2026, 5, 27), "Eid al-Adha (approx.)"),
    (date(2026, 6, 16), "Islamic New Year (approx.)"),
    (date(2026, 6, 25), "Day of Ashura (approx.)"),
    (date(2026, 8, 25), "Mawlid al-Nabi (approx.)"),
]

_ISLAM_MOCK_TIMINGS = {
    "Fajr": "—", "Sunrise": "—", "Dhuhr": "—", "Asr": "—",
    "Maghrib": "—", "Isha": "—",
}


def _fetch_aladhan(target: date) -> dict | None:
    try:
        resp = requests.get(
            f"http://api.aladhan.com/v1/timings/{target.strftime('%d-%m-%Y')}",
            params={"latitude": _ISLAM_LAT, "longitude": _ISLAM_LON, "method": 2},
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        timings = data.get("timings", {})
        hijri = data.get("date", {}).get("hijri", {})
        hijri_label = f"{hijri.get('day', '—')} {hijri.get('month', {}).get('en', '')} {hijri.get('year', '')}".strip()
        return {"timings": timings, "hijri_label": hijri_label}
    except Exception as exc:
        logger.warning("Aladhan fetch failed: %s", exc)
        return None


class IslamProvider:
    religion = "islam"

    def get_day(self, target: date) -> dict:
        live = _fetch_aladhan(target)
        timings = live["timings"] if live else _ISLAM_MOCK_TIMINGS
        hijri_label = live["hijri_label"] if live else "Hijri date unavailable — check connection"

        occasion = None
        for d, name in _ISLAM_CURATED_2026:
            if d == target:
                occasion = {"name": name, "is_major": True}
                break
        if occasion is None and target.weekday() == 4:  # Friday
            occasion = {"name": "Jumu'ah (Friday prayer)", "is_major": False}

        if occasion and occasion["is_major"]:
            steps = [
                f"Today marks {occasion['name']} — check with your local masjid for the exact observed date, "
                "since moon-sighting can shift this by a day.",
                "Follow the specific guidance for this occasion (e.g. Eid prayer, fasting, or charity) as observed in your community.",
            ]
        elif occasion:  # Jumu'ah
            steps = [
                "Perform ghusl (ritual bath) before heading to the masjid if possible.",
                "Attend the Jumu'ah khutbah (sermon) and congregational prayer, replacing Dhuhr.",
                "Recite Surah Al-Kahf, as is traditionally recommended on Fridays.",
            ]
        else:
            steps = [
                "Perform wudu (ablution) before each of the five daily prayers.",
                "Face the Qibla and pray each Salah at its appointed time (see timings).",
                "Close the day with personal dua and reflection.",
            ]

        return {
            "religion": self.religion,
            "date": target.isoformat(),
            "weekday": target.strftime("%A"),
            "calendar_label": hijri_label,
            "headline": occasion["name"] if occasion else "Daily prayer schedule",
            "occasion": occasion,
            "guidance_title": "Guidance for today",
            "guidance_steps": steps,
            "extra": {"timings": timings, "source_note": "Live via Aladhan API." if live else "Aladhan API unreachable — showing placeholders."},
            "jaap_allowed": False,
        }

    def upcoming(self, from_date: date, limit: int = 5) -> list[dict]:
        items = [{"date": d.isoformat(), "name": n} for d, n in _ISLAM_CURATED_2026 if d >= from_date]
        return items[:limit]


# ── Christianity — computed liturgical calendar ────────────────────────────

def _easter_date(year: int) -> date:
    """Anonymous Gregorian algorithm (Meeus/Jones/Butcher) — accurate."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


_CHRISTIAN_REFLECTIONS = [
    "Where can you offer patience today instead of judgment?",
    "Name one thing you're grateful for and let it shape how you treat someone today.",
    "Where is God inviting you to slow down?",
    "Who could use a small act of kindness from you today?",
    "What is one worry you can hand over instead of carrying alone?",
    "How can you make space for silence today?",
    "What would it look like to forgive one small thing today?",
]


class ChristianProvider:
    religion = "christianity"

    def _feast_calendar(self, year: int) -> dict[date, str]:
        easter = _easter_date(year)
        return {
            date(year, 1, 1): "Solemnity of Mary, Mother of God",
            date(year, 1, 6): "Epiphany",
            easter - timedelta(days=46): "Ash Wednesday",
            easter - timedelta(days=7): "Palm Sunday",
            easter - timedelta(days=2): "Good Friday",
            easter: "Easter Sunday",
            easter + timedelta(days=49): "Pentecost",
            date(year, 12, 25): "Christmas Day",
        }

    def _season(self, target: date) -> str:
        easter = _easter_date(target.year)
        ash_wed = easter - timedelta(days=46)
        pentecost = easter + timedelta(days=49)
        advent_start = date(target.year, 12, 25) - timedelta(weeks=4)
        if ash_wed <= target < easter:
            return "Lent"
        if easter <= target <= pentecost:
            return "Easter season"
        if advent_start <= target < date(target.year, 12, 25):
            return "Advent"
        if date(target.year, 12, 25) <= target or target <= date(target.year, 1, 6):
            return "Christmas season"
        return "Ordinary Time"

    def get_day(self, target: date) -> dict:
        feasts = self._feast_calendar(target.year)
        occasion = None
        if target in feasts:
            occasion = {"name": feasts[target], "is_major": True}
        elif target.weekday() == 6:
            occasion = {"name": "Sunday — the Lord's Day", "is_major": False}

        season = self._season(target)
        reflection = _CHRISTIAN_REFLECTIONS[target.toordinal() % len(_CHRISTIAN_REFLECTIONS)]

        if occasion and occasion["is_major"]:
            steps = [
                f"Today is {occasion['name']} — check your parish's specific liturgy and Mass times.",
                "Take a few quiet minutes for prayer or scripture reading fitting the occasion.",
            ]
        elif occasion:  # Sunday
            steps = [
                "Attend Sunday worship if you're able to.",
                "Spend a few minutes in prayer or scripture reading.",
                f"Reflection: {reflection}",
            ]
        else:
            steps = [
                "Take a few quiet minutes for prayer.",
                f"Reflection: {reflection}",
            ]

        return {
            "religion": self.religion,
            "date": target.isoformat(),
            "weekday": target.strftime("%A"),
            "calendar_label": season,
            "headline": occasion["name"] if occasion else season,
            "occasion": occasion,
            "guidance_title": "Today's guidance",
            "guidance_steps": steps,
            "extra": {"season": season, "reflection": reflection,
                      "source_note": "Season and feast dates are computed (Easter via the standard algorithm); "
                                      "the daily reflection is a curated prompt, not an official lectionary reading."},
            "jaap_allowed": False,
        }

    def upcoming(self, from_date: date, limit: int = 5) -> list[dict]:
        feasts = self._feast_calendar(from_date.year)
        feasts.update(self._feast_calendar(from_date.year + 1))
        items = sorted((d, n) for d, n in feasts.items() if d >= from_date)
        return [{"date": d.isoformat(), "name": n} for d, n in items[:limit]]


# ── Sikh — curated Nanakshahi-adjacent dates ───────────────────────────────

_SIKH_CURATED_2026 = [
    (date(2026, 1, 5), "Guru Gobind Singh Ji's Parkash Purab (approx.)"),
    (date(2026, 4, 14), "Vaisakhi"),
    (date(2026, 11, 8), "Bandi Chhor Divas (approx., coincides with Diwali)"),
    (date(2026, 11, 24), "Guru Nanak Dev Ji's Gurpurab (approx.)"),
]


class SikhProvider:
    religion = "sikh"

    def get_day(self, target: date) -> dict:
        occasion = None
        for d, name in _SIKH_CURATED_2026:
            if d == target:
                occasion = {"name": name, "is_major": True}
                break

        if occasion:
            steps = [
                f"Today is {occasion['name']} — check with your local Gurdwara for the exact program and timing.",
                "Attend Gurdwara for kirtan and langar if possible.",
            ]
        else:
            steps = [
                "Begin the day with Waheguru simran (remembrance through chanting).",
                "Read or listen to a passage from Gurbani.",
                "Recite Ardas before an important task or at day's end.",
            ]

        return {
            "religion": self.religion,
            "date": target.isoformat(),
            "weekday": target.strftime("%A"),
            "calendar_label": "",
            "headline": occasion["name"] if occasion else "Daily Simran & Ardas",
            "occasion": occasion,
            "guidance_title": "Today's guidance",
            "guidance_steps": steps,
            "extra": {
                "source_note": "Gurpurab dates here are curated approximations — several follow the lunar "
                                "Bikrami calendar and shift yearly, so verify against your Gurdwara's calendar."
            },
            "jaap_allowed": False,
        }

    def upcoming(self, from_date: date, limit: int = 5) -> list[dict]:
        items = [{"date": d.isoformat(), "name": n} for d, n in _SIKH_CURATED_2026 if d >= from_date]
        return items[:limit]


# ── Atheist / no religion — plain civic calendar, no devotional content ───

_SECULAR_CURATED = [
    (1, 1, "New Year's Day"),
    (3, 8, "International Women's Day"),
    (4, 22, "Earth Day"),
    (5, 1, "Labour Day"),
    (6, 5, "World Environment Day"),
    (10, 2, "Gandhi Jayanti"),
    (10, 24, "United Nations Day"),
    (11, 14, "Children's Day"),
]


class AtheistProvider:
    religion = "atheist"

    def get_day(self, target: date) -> dict:
        occasion = None
        for month, day, name in _SECULAR_CURATED:
            if target.month == month and target.day == day:
                occasion = {"name": name, "is_major": False}
                break

        steps = (
            [f"Today is {occasion['name']}."]
            if occasion
            else ["Take a mindful pause and set an intention for the day — no ritual required, just a moment of focus."]
        )

        return {
            "religion": self.religion,
            "date": target.isoformat(),
            "weekday": target.strftime("%A"),
            "calendar_label": "",
            "headline": occasion["name"] if occasion else target.strftime("%A, %B %d"),
            "occasion": occasion,
            "guidance_title": "Today",
            "guidance_steps": steps,
            "extra": {},
            "jaap_allowed": False,
        }

    def upcoming(self, from_date: date, limit: int = 5) -> list[dict]:
        items = []
        for month, day, name in _SECULAR_CURATED:
            d = date(from_date.year, month, day)
            if d < from_date:
                d = date(from_date.year + 1, month, day)
            items.append((d, name))
        items.sort(key=lambda pair: pair[0])
        return [{"date": d.isoformat(), "name": n} for d, n in items[:limit]]


# ── Registry ────────────────────────────────────────────────────────────

_PROVIDERS = {
    "hindu": HinduProvider(),
    "islam": IslamProvider(),
    "christianity": ChristianProvider(),
    "sikh": SikhProvider(),
    "atheist": AtheistProvider(),
}


def get_provider(religion: str):
    return _PROVIDERS.get(religion, _PROVIDERS["atheist"])