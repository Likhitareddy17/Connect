"""
Unit tests for spiritual_api.py — tests mock/static paths only (no network).
"""
from datetime import date
from unittest.mock import patch

import pytest
from services.spiritual_api import SpiritualApi, WEEKDAY_DEVOTION, DATE_OVERRIDES


@pytest.fixture
def api():
    return SpiritualApi()


class TestSpiritualApiToday:
    def test_returns_dict_with_required_keys(self, api):
        result = api.today()
        for key in ("deity", "focus", "tone", "mantra", "reflection", "panchangam", "date"):
            assert key in result, f"Missing key: {key}"

    def test_date_matches_target(self, api):
        target = date(2025, 3, 15)
        result = api.today(target_date=target)
        assert result["date"] == "2025-03-15"

    def test_weekday_mapping_covers_all_days(self, api):
        for weekday in range(7):
            # Find a date with that weekday
            base = date(2025, 1, 6)  # Monday
            delta = (weekday - base.weekday()) % 7
            from datetime import timedelta
            target = base + timedelta(days=delta)
            result = api.today(target_date=target)
            assert result["deity"] == WEEKDAY_DEVOTION[weekday]["deity"]

    def test_date_override_is_applied(self, api):
        override_date = next(iter(DATE_OVERRIDES))
        result = api.today(target_date=override_date)
        assert result["deity"] == DATE_OVERRIDES[override_date]["deity"]

    def test_mock_fallback_when_no_api_key(self, api):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("PROKERALA_CLIENT_ID", None)
            os.environ.pop("PROKERALA_CLIENT_SECRET", None)
            result = api.today()
            assert result["source"] == "mock"

    def test_panchang_contains_source(self, api):
        result = api.panchang()
        assert "source" in result
        assert "panchangam" in result

    def test_reflection_is_nonempty_string(self, api):
        result = api.today()
        assert isinstance(result["reflection"], str)
        assert len(result["reflection"]) > 5


class TestSpiritualApiProkeralaFallback:
    def test_falls_back_on_network_error(self, api):
        """If Prokerala raises, today() should still return mock data."""
        with patch("services.spiritual_api._prokerala_panchang", side_effect=Exception("network error")):
            result = api.today()
            assert result["deity"] in [d["deity"] for d in WEEKDAY_DEVOTION.values()]
