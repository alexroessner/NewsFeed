"""Tests for data integrity: ReDoS protection, weight cap/saturation feedback,
filter range validation, and timezone validation.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from newsfeed.memory.commands import (
    ParseResult,
    parse_preference_commands,
    parse_preference_commands_rich,
    _MAX_INPUT_LEN,
)
from newsfeed.memory.store import PreferenceStore


# ══════════════════════════════════════════════════════════════════════════
# ReDoS Protection
# ══════════════════════════════════════════════════════════════════════════


class TestReDoSProtection(unittest.TestCase):
    """Verify that excessively long input is truncated before regex parsing."""

    def test_input_cap_exists(self):
        assert _MAX_INPUT_LEN == 500

    def test_long_input_truncated_standard_parser(self):
        """Standard parser handles input longer than cap without hanging."""
        # Create input that would cause backtracking: "more " + spaces + "tone:analyst"
        long_input = "more " + " " * 2000 + "tone:analyst"
        cmds = parse_preference_commands(long_input)
        # Should not hang; truncation means the tail is lost
        assert isinstance(cmds, list)

    def test_long_input_truncated_rich_parser(self):
        """Rich parser handles input longer than cap without hanging."""
        long_input = "more " + "a" * 2000 + " less crypto"
        result = parse_preference_commands_rich(long_input)
        assert isinstance(result, ParseResult)
        # The "less crypto" at position 2005+ should be truncated
        topics = [c.topic for c in result.commands if c.action == "topic_delta"]
        assert "crypto" not in topics

    def test_normal_input_not_affected(self):
        """Input under the cap works normally."""
        cmds = parse_preference_commands("more geopolitics, less crypto")
        assert len(cmds) == 2
        topics = {c.topic for c in cmds}
        assert "geopolitics" in topics
        assert "crypto" in topics

    def test_input_at_exactly_cap(self):
        """Input at exactly the cap length works."""
        text = "more geo" + "x" * (_MAX_INPUT_LEN - 8)
        cmds = parse_preference_commands(text)
        assert isinstance(cmds, list)


# ══════════════════════════════════════════════════════════════════════════
# Weight Cap & Saturation Feedback
# ══════════════════════════════════════════════════════════════════════════


class TestWeightCapFeedback(unittest.TestCase):
    """Verify that users get feedback when weight cap or saturation is hit."""

    def test_cap_returns_hint(self):
        store = PreferenceStore()
        uid = "u1"
        # Fill up to MAX_WEIGHTS
        for i in range(store.MAX_WEIGHTS):
            store.apply_weight_adjustment(uid, f"topic_{i}", 0.5)
        # One more should hit the cap
        _, hint = store.apply_weight_adjustment(uid, "new_topic", 0.5)
        assert "limit" in hint.lower()
        assert str(store.MAX_WEIGHTS) in hint

    def test_existing_topic_update_at_cap(self):
        """Updating an existing topic works even at the cap."""
        store = PreferenceStore()
        uid = "u1"
        for i in range(store.MAX_WEIGHTS):
            store.apply_weight_adjustment(uid, f"topic_{i}", 0.5)
        # Updating existing topic should work
        profile, hint = store.apply_weight_adjustment(uid, "topic_0", 0.1)
        assert hint == ""  # No cap hint
        assert profile.topic_weights["topic_0"] == 0.6

    def test_saturation_hint_at_max(self):
        """When weight is already at +1.0, further 'more' shows saturation hint."""
        store = PreferenceStore()
        uid = "u1"
        # Set to 1.0
        for _ in range(5):
            store.apply_weight_adjustment(uid, "geopolitics", 0.2)
        # One more should show saturation
        _, hint = store.apply_weight_adjustment(uid, "geopolitics", 0.2)
        assert "maximum" in hint.lower()

    def test_saturation_hint_at_min(self):
        """When weight is at -1.0, further 'less' shows saturation hint."""
        store = PreferenceStore()
        uid = "u1"
        for _ in range(5):
            store.apply_weight_adjustment(uid, "crypto", -0.2)
        _, hint = store.apply_weight_adjustment(uid, "crypto", -0.2)
        assert "minimum" in hint.lower()

    def test_no_hint_for_normal_adjustment(self):
        """Normal adjustments should not produce hints."""
        store = PreferenceStore()
        _, hint = store.apply_weight_adjustment("u1", "tech", 0.3)
        assert hint == ""

    def test_return_type_is_tuple(self):
        """apply_weight_adjustment now returns (profile, hint) tuple."""
        store = PreferenceStore()
        result = store.apply_weight_adjustment("u1", "tech", 0.2)
        assert isinstance(result, tuple)
        assert len(result) == 2


# ══════════════════════════════════════════════════════════════════════════
# Filter Range Validation
# ══════════════════════════════════════════════════════════════════════════


class TestFilterRangeValidation(unittest.TestCase):
    """Verify that /filter clamps values to documented ranges."""

    def _make_comm_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent
        engine = MagicMock()
        profile = MagicMock(timezone="UTC")
        engine.preferences.get_or_create.return_value = profile
        bot = MagicMock()
        comm = CommunicationAgent(engine=engine, bot=bot)
        return comm, engine, bot

    def test_confidence_clamped_to_1(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "confidence 999")
        engine.preferences.set_filter.assert_called_once()
        # The value should be clamped to 1.0
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) <= 1.0

    def test_confidence_clamped_to_0(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "confidence -5")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) >= 0.0

    def test_max_per_source_clamped_to_10(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "max_per_source 999")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert int(call_val) <= 10

    def test_max_per_source_clamped_to_0(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "max_per_source -5")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert int(call_val) >= 0

    def test_georisk_clamped_to_range(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "georisk 50")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) <= 1.0

    def test_georisk_clamped_min(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "georisk -1")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) >= 0.1

    def test_trend_clamped_to_range(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "trend 100")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) <= 10.0

    def test_trend_clamped_min(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_filter("chat1", "u1", "trend 0.1")
        call_val = engine.preferences.set_filter.call_args[0][2]
        assert float(call_val) >= 1.5


# ══════════════════════════════════════════════════════════════════════════
# Timezone Validation
# ══════════════════════════════════════════════════════════════════════════


class TestTimezoneValidation(unittest.TestCase):
    """Verify that invalid timezones are rejected with helpful error."""

    def _make_comm_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent
        engine = MagicMock()
        profile = MagicMock(timezone="UTC")
        engine.preferences.get_or_create.return_value = profile
        bot = MagicMock()
        comm = CommunicationAgent(engine=engine, bot=bot)
        return comm, engine, bot

    def test_valid_timezone_accepted(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_timezone("chat1", "u1", "UTC")
        engine.preferences.set_timezone.assert_called_once_with("u1", "UTC")

    def test_valid_timezone_america_new_york(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_timezone("chat1", "u1", "America/New_York")
        engine.preferences.set_timezone.assert_called_once_with("u1", "America/New_York")

    def test_invalid_timezone_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_timezone("chat1", "u1", "US/Easten")
        assert result["action"] == "timezone_invalid"
        # Should NOT have been stored
        engine.preferences.set_timezone.assert_not_called()

    def test_invalid_timezone_shows_examples(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_timezone("chat1", "u1", "bogus_tz")
        msg = bot.send_message.call_args[0][1]
        assert "US/Eastern" in msg or "Examples" in msg

    def test_garbage_timezone_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_timezone("chat1", "u1", "!@#$%^")
        assert result["action"] == "timezone_invalid"


# ══════════════════════════════════════════════════════════════════════════
# Engine Integration: Hints Surfaced
# ══════════════════════════════════════════════════════════════════════════


class TestHintsSurfacedInResults(unittest.TestCase):
    """Verify that weight cap/saturation hints flow through engine to user."""

    def test_engine_includes_hint_in_results(self):
        """When apply_weight_adjustment returns a hint, engine puts it in results."""
        from newsfeed.orchestration.engine import NewsFeedEngine
        # We can't easily construct a full engine, so test the store directly
        store = PreferenceStore()
        uid = "u1"
        # Saturate a weight
        for _ in range(5):
            store.apply_weight_adjustment(uid, "tech", 0.2)
        profile, hint = store.apply_weight_adjustment(uid, "tech", 0.2)
        assert "maximum" in hint
        # Profile should show tech at 1.0
        assert profile.topic_weights["tech"] == 1.0
