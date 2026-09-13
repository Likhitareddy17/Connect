"""
Unit tests for grounding.py practice recommendation logic.
"""
from datetime import datetime

import pytest
from services.grounding import recommend_practices, time_of_day, PRACTICES


class TestTimeOfDay:
    def test_morning(self):
        assert time_of_day(datetime(2024, 1, 1, 7, 0)) == "morning"

    def test_afternoon(self):
        assert time_of_day(datetime(2024, 1, 1, 14, 0)) == "afternoon"

    def test_evening(self):
        assert time_of_day(datetime(2024, 1, 1, 20, 0)) == "evening"

    def test_midnight_is_morning(self):
        assert time_of_day(datetime(2024, 1, 1, 0, 0)) == "morning"

    def test_noon_is_afternoon(self):
        assert time_of_day(datetime(2024, 1, 1, 12, 0)) == "afternoon"


class TestRecommendPractices:
    def test_returns_requested_limit(self):
        result = recommend_practices(mood="stressed", goal="grounding", limit=3)
        assert len(result) == 3

    def test_returns_fewer_if_not_enough(self):
        result = recommend_practices(mood="stressed", goal="grounding", limit=100)
        assert len(result) == len(PRACTICES)

    def test_result_contains_required_keys(self):
        practices = recommend_practices(mood="calm", goal="clarity", limit=2)
        for p in practices:
            assert "id" in p
            assert "title" in p
            assert "detail" in p

    def test_mood_match_scores_higher(self):
        """A practice matching the mood should rank above one that doesn't."""
        # 'breath' matches stressed, 'gratitude' does not
        practices = recommend_practices(mood="stressed", goal="grounding", limit=len(PRACTICES))
        ids = [p["id"] for p in practices]
        assert ids.index("breath") < ids.index("gratitude")

    def test_goal_match_scores_higher(self):
        """Gratitude goal should surface the gratitude practice."""
        practices = recommend_practices(mood="calm", goal="gratitude", limit=3)
        ids = [p["id"] for p in practices]
        assert "gratitude" in ids

    def test_unknown_mood_still_returns_results(self):
        result = recommend_practices(mood="unknown-mood", goal="grounding", limit=3)
        assert len(result) == 3

    def test_unknown_goal_still_returns_results(self):
        result = recommend_practices(mood="calm", goal="unknown-goal", limit=3)
        assert len(result) == 3

    def test_limit_zero_returns_empty(self):
        result = recommend_practices(mood="calm", goal="grounding", limit=0)
        assert result == []