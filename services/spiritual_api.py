"""
SpiritualApi — daily panchang and deity data.
Primary: Prokerala Panchang API (requires PROKERALA_CLIENT_ID + PROKERALA_CLIENT_SECRET).
Fallback: weekday-keyed mock data (identical to original, always safe to use).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone, timedelta
from functools import lru_cache

import requests

logger = logging.getLogger(__name__)

# ── Static devotion map ────────────────────────────────────────────────────
# Python weekday(): 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday

WEEKDAY_DEVOTION: dict[int, dict] = {
    0: {
        "deity": "Shiva", "focus": "stillness and release", "tone": "quiet",
        "mantra": "Om Namah Shivaya", "mantra_sanskrit": "ॐ नमः शिवाय",
        "meaning": "I bow to Shiva, the presence of inner stillness and transformation.",
        "audio_text": "Om Namah Shivaya",
        "reflection": "Let one unnecessary burden loosen before the day moves forward.",
    },
    1: {
        "deity": "Hanuman", "focus": "courage and protection", "tone": "steady",
        "mantra": "Om Hanumate Namah", "mantra_sanskrit": "ॐ हनुमते नमः",
        "meaning": "I bow to Hanuman, the force of devotion, strength, and service.",
        "audio_text": "Om Hanumate Namah",
        "reflection": "Choose courage without harshness. Strength can be gentle.",
    },
    2: {
        "deity": "Ganesha", "focus": "clear beginnings", "tone": "grounded",
        "mantra": "Om Gam Ganapataye Namah", "mantra_sanskrit": "ॐ गं गणपतये नमः",
        "meaning": "I bow to Ganesha, remover of obstacles and guardian of beginnings.",
        "audio_text": "Om Gam Ganapataye Namah",
        "reflection": "Begin with one clean step. Do not ask the whole path to appear.",
    },
    3: {
        "deity": "Vishnu", "focus": "balance and protection", "tone": "clear",
        "mantra": "Om Namo Bhagavate Vasudevaya", "mantra_sanskrit": "ॐ नमो भगवते वासुदेवाय",
        "meaning": "I offer reverence to Vishnu, the sustaining presence of dharma.",
        "audio_text": "Om Namo Bhagavate Vasudevaya",
        "reflection": "Let devotion show up as steadiness in one ordinary action.",
    },
    4: {
        "deity": "Lakshmi", "focus": "gratitude and grace", "tone": "warm",
        "mantra": "Om Shreem Mahalakshmyai Namah", "mantra_sanskrit": "ॐ श्रीं महालक्ष्म्यै नमः",
        "meaning": "I bow to Mahalakshmi, presence of grace, beauty, and abundance.",
        "audio_text": "Om Shreem Mahalakshmyai Namah",
        "reflection": "Receive what is already supporting you. Gratitude is also worship.",
    },
    5: {
        "deity": "Shani", "focus": "discipline and patience", "tone": "deep",
        "mantra": "Om Sham Shanicharaya Namah", "mantra_sanskrit": "ॐ शं शनैश्चराय नमः",
        "meaning": "I bow to Shani, teacher of patience, karma, and sincere effort.",
        "audio_text": "Om Sham Shanicharaya Namah",
        "reflection": "Move honestly. What is slow can still be sacred.",
    },
    6: {
        "deity": "Surya", "focus": "light and renewal", "tone": "bright",
        "mantra": "Om Suryaya Namah", "mantra_sanskrit": "ॐ सूर्याय नमः",
        "meaning": "I bow to Surya, source of light, vitality, and clear seeing.",
        "audio_text": "Om Suryaya Namah",
        "reflection": "Let light touch the places where you have been rushing in the dark.",
    },
}

DATE_OVERRIDES: dict[date, dict] = {
    date(2026, 5, 22): {
        "deity": "Murugan", "also_known_as": "Kartikeya, Skanda, Subramanya",
        "focus": "courage, purity, and disciplined devotion",
        "tone": "bright and protective",
        "mantra": "Om Saravanabhavaya Namah", "mantra_sanskrit": "ॐ सरवनभवाय नमः",
        "meaning": "I bow to the radiant one born in Saravana, Lord Murugan.",
        "audio_text": "Om Saravanabhavaya Namah",
        "tithi": "Shukla Paksha Sashti", "nakshatra": "Ashlesha, varies by location",
        "festival": "Muruga Sashti / Skanda Sashti observance",
        "fast": {
            "observed": True, "name": "Sashti Vrat for Lord Murugan",
            "summary": "A devotional fast observed on the sixth lunar day.",
            "how": [
                "Begin after a simple morning bath and sankalpa.",
                "Offer flowers, light, and a quiet Murugan mantra jaap.",
                "Keep food simple: fruit, milk, sattvic vrat food, or a partial fast.",
            ],
            "break": [
                "Break the fast after evening worship.",
                "Begin gently with water, fruit, or light sattvic food.",
            ],
        },
        "festivals": [
            {"name": "Muruga Sashti Fasting", "detail": "A Sashti vrat day associated with Lord Murugan worship."},
        ],
        "reflection": "Today is not random. Let your courage become clean.",
        "depth": "Sashti worship is a devotional way to refine willpower.",
        "panchangam": {
            "location": "Ujjain, Madhya Pradesh, India", "weekday": "Friday",
            "vikram_samvat": "2083", "lunar_month": "Jyeshta Sukla Paksha",
            "sunrise": "5:47 AM", "sunset": "7:00 PM",
            "moonrise": "11:11 AM", "moonset": "12:34 AM, May 23",
            "tithi": "Sukla Paksha Shashthi until 6:24 AM, then Saptami",
            "nakshatra": "Ashlesha until 2:08 AM, May 23",
            "yoga": "Vridhi until 8:18 AM, then Dhruva",
            "karana": "Taitila until 6:25 AM, Garija until 5:39 PM, then Vanija",
            "rahu_kalam": "10:44 AM - 12:23 PM",
            "abhijit_muhurat": "11:57 AM - 12:49 PM",
            "source_note": "Override data for Muruga Sashti.",
        },
    },
}

_MOCK_PANCHANG = {
    "location": "India (default)",
    "weekday": "",
    "vikram_samvat": "Pending Panchang API",
    "lunar_month": "Pending Panchang API",
    "sunrise": "Pending location",
    "sunset": "Pending location",
    "moonrise": "Pending location",
    "moonset": "Pending location",
    "tithi": "Pending Panchang API",
    "nakshatra": "Pending Panchang API",
    "yoga": "Pending Panchang API",
    "karana": "Pending Panchang API",
    "rahu_kalam": "Pending location",
    "abhijit_muhurat": "Pending location",
    "source_note": "Mock fallback — connect Prokerala API for live data.",
}

# Variable tracking token expiration to prevent unnecessary API overloads
_cached_token: str | None = None
_token_expires_at: datetime | None = None

# ── Prokerala helpers ───────────────────────────────────────────────────────

def _prokerala_token() -> str | None:
    """Fetch OAuth2 token from Prokerala and cache it natively until it expires."""
    global _cached_token, _token_expires_at
    
    # If we have a token and it is still valid for at least another 2 minutes, return it
    if _cached_token and _token_expires_at and datetime.now(timezone.utc) < _token_expires_at:
        return _cached_token

    client_id = os.getenv("PROKERALA_CLIENT_ID")
    client_secret = os.getenv("PROKERALA_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    try:
        resp = requests.post(
            "https://api.prokerala.com/token",
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=6,
        )
        resp.raise_for_status()
        json_data = resp.json()
        
        _cached_token = json_data.get("access_token")
        expires_in = json_data.get("expires_in", 3600)
        # Give ourselves a 120-second safety margin
        _token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 120)
        
        return _cached_token
    except Exception as exc:
        logger.warning("Prokerala token fetch failed: %s", exc)
        return None

def _prokerala_panchang(target: date, lat: float = 17.385, lon: float = 78.4867) -> dict | None:
    token = _prokerala_token()
    if not token:
        return None

    lat = float(os.getenv("PANCHANG_LAT", lat))
    lon = float(os.getenv("PANCHANG_LON", lon))
    ayanamsa = os.getenv("PANCHANG_AYANAMSA", "1")

    # Prokerala's free tier only accepts Jan 1st of the current year as the
    # query date. This means the panchang shown is always for Jan 1st, not
    # today — a real limitation of the free plan, not a bug. Upgrading the
    # Prokerala plan removes this restriction.
    dt_str = f"{target.year}-01-01T06:00:00+05:30"

    try:
        resp = requests.get(
            "https://api.prokerala.com/v2/astrology/panchang",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "ayanamsa": ayanamsa,
                "coordinates": f"{lat},{lon}",
                "datetime": dt_str,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return _normalise_prokerala(data, target)
    except requests.exceptions.HTTPError as exc:
        # Print the actual response body to see Prokerala's error message
        logger.warning("Prokerala panchang fetch failed: %s | body: %s", exc, exc.response.text)
        return None
    except Exception as exc:
        logger.warning("Prokerala panchang fetch failed: %s", exc)
        return None    

def _first(lst, field: str, default: str = "—") -> str:
    if lst and isinstance(lst, list):
        return lst[0].get(field, default)
    return default


def _format_time_window(window) -> str:
    """Helper to safely format sub-objects like Rahu Kalam into clean UI strings."""
    if isinstance(window, dict):
        return f"{window.get('start', '—')} - {window.get('end', '—')}"
    return str(window) if window else "—"


def _normalise_prokerala(data: dict, target: date) -> dict:
    nakshatra = _first(data.get("nakshatra"), "name")
    tithi = _first(data.get("tithi"), "name")
    yoga = _first(data.get("yoga"), "name")
    karana = _first(data.get("karana"), "name")
    sunrise = data.get("sunrise", "—")
    sunset = data.get("sunset", "—")
    moonrise = data.get("moonrise", "—")
    moonset = data.get("moonset", "—")
    
    return {
        "location": f"{os.getenv('PANCHANG_LAT', '17.39')}°N, {os.getenv('PANCHANG_LON', '78.49')}°E",
        "weekday": target.strftime("%A"),
        "vikram_samvat": str(data.get("vikram_samvat", "—")),
        "lunar_month": data.get("lunar_month", {}).get("name", "—") if isinstance(data.get("lunar_month"), dict) else "—",
        "sunrise": sunrise,
        "sunset": sunset,
        "moonrise": moonrise,
        "moonset": moonset,
        "tithi": tithi,
        "nakshatra": nakshatra,
        "yoga": yoga,
        "karana": karana,
        "rahu_kalam": _format_time_window(data.get("rahu_kalam")),
        "abhijit_muhurat": _format_time_window(data.get("abhijit_muhurta")),
        "source_note": "Live data via Prokerala API.",
    }


# ── Main class ─────────────────────────────────────────────────────────────

class SpiritualApi:
    def today(self, target_date: date | None = None) -> dict:
        target = target_date or date.today()
        base = WEEKDAY_DEVOTION[target.weekday()].copy()
        override = DATE_OVERRIDES.get(target, {})

        # Try live panchang first; fall back to mock
        try:
            live_panchang = _prokerala_panchang(target)
        except Exception as exc:
            logger.warning("Panchang lookup crashed, falling back to mock: %s", exc)
            live_panchang = None
            
        mock = _MOCK_PANCHANG.copy()
        mock["weekday"] = target.strftime("%A")
        panchang = live_panchang or override.get("panchangam", mock)

        daily: dict = {
            "source": "prokerala" if live_panchang else "mock",
            "date": target.isoformat(),
            "panchangam": panchang,
            "tithi": override.get("tithi", panchang.get("tithi", "—")),
            "nakshatra": override.get("nakshatra", panchang.get("nakshatra", "—")),
            "festival": override.get("festival", "No major festival in local data"),
            "fast": override.get("fast", {
                "observed": False, "name": "No specific vrat today",
                "summary": "Keep the day simple with your chosen mantra and a small act of gratitude.",
                "how": ["Offer a short prayer and chant with attention."],
                "break": ["No fast-breaking guidance needed today."],
            }),
            "festivals": override.get("festivals", []),
            "also_known_as": override.get("also_known_as", ""),
            "depth": override.get("depth", "Connect Prokerala and Panchang APIs to enrich this section."),
            **base,
        }
        daily.update(override)
        return daily

    def panchang(self) -> dict:
        daily = self.today()
        return {
            "source": daily["source"],
            "tithi": daily["tithi"],
            "festival": daily["festival"],
            "nakshatra": daily["nakshatra"],
            "panchangam": daily["panchangam"],
            "api_ready": True,
        }

    def festivals(self) -> list[dict]:
        return [{"source": "mock", "name": self.today()["festival"]}]

    def tithi(self) -> dict:
        return {"source": "mock", "name": self.today()["tithi"]}


spiritual_api = SpiritualApi()