"""Tests for platform hardening: zero-weight cleanup, source weight feedback,
email header injection, preset name sanitization, and LLM garbage fallback.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from newsfeed.memory.store import PreferenceStore
from newsfeed.delivery.email_digest import EmailDigest


# ══════════════════════════════════════════════════════════════════════════
# Zero-Weight Topic Cleanup
# ══════════════════════════════════════════════════════════════════════════


class TestZeroWeightCleanup(unittest.TestCase):
    """Verify that zero-weight topics are pruned when hitting MAX_WEIGHTS."""

    def test_zero_weights_pruned_when_at_cap(self):
        """Adding a new topic at cap succeeds if zero-weights can be pruned."""
        store = PreferenceStore()
        uid = "u1"
        # Fill up to MAX_WEIGHTS with alternating zero and non-zero
        for i in range(store.MAX_WEIGHTS):
            store.apply_weight_adjustment(uid, f"topic_{i}", 0.5)
        # Set half of them to zero (by adjusting -0.5)
        for i in range(0, store.MAX_WEIGHTS, 2):
            store.apply_weight_adjustment(uid, f"topic_{i}", -0.5)
        # Now at cap, but half are zero-weight
        profile = store.get_or_create(uid)
        assert len(profile.topic_weights) == store.MAX_WEIGHTS
        # Adding a new topic should succeed after pruning zeros
        profile, hint = store.apply_weight_adjustment(uid, "new_topic", 0.3)
        assert "new_topic" in profile.topic_weights
        assert hint == ""  # No cap hint

    def test_cap_still_rejects_when_all_nonzero(self):
        """If all weights are non-zero, cap rejection still works."""
        store = PreferenceStore()
        uid = "u1"
        for i in range(store.MAX_WEIGHTS):
            store.apply_weight_adjustment(uid, f"topic_{i}", 0.5)
        _, hint = store.apply_weight_adjustment(uid, "one_too_many", 0.3)
        assert "limit" in hint.lower()

    def test_prune_zero_weights_removes_zeros(self):
        """Direct test of _prune_zero_weights static method."""
        weights = {"a": 0.5, "b": 0.0, "c": -0.3, "d": 0.0}
        pruned = PreferenceStore._prune_zero_weights(weights)
        assert pruned == 2
        assert "b" not in weights
        assert "d" not in weights
        assert len(weights) == 2

    def test_prune_no_zeros(self):
        """Prune does nothing if there are no zeros."""
        weights = {"a": 0.5, "b": -0.3}
        pruned = PreferenceStore._prune_zero_weights(weights)
        assert pruned == 0
        assert len(weights) == 2


# ══════════════════════════════════════════════════════════════════════════
# Source Weight Cap Feedback
# ══════════════════════════════════════════════════════════════════════════


class TestSourceWeightFeedback(unittest.TestCase):
    """Verify that source weight operations return hints."""

    def test_source_cap_returns_hint(self):
        store = PreferenceStore()
        uid = "u1"
        for i in range(store.MAX_WEIGHTS):
            store.apply_source_weight(uid, f"source_{i}", 0.5)
        _, hint = store.apply_source_weight(uid, "new_source", 0.5)
        assert "limit" in hint.lower()

    def test_source_saturation_hint_at_max(self):
        """When source weight is at max, further boost shows saturation."""
        store = PreferenceStore()
        uid = "u1"
        # Source weights cap at 2.0
        for _ in range(10):
            store.apply_source_weight(uid, "reuters", 0.5)
        _, hint = store.apply_source_weight(uid, "reuters", 0.5)
        assert "maximum" in hint.lower()

    def test_source_saturation_hint_at_min(self):
        """When source weight is at min, further demote shows saturation."""
        store = PreferenceStore()
        uid = "u1"
        for _ in range(10):
            store.apply_source_weight(uid, "tabloid", -0.5)
        _, hint = store.apply_source_weight(uid, "tabloid", -0.5)
        assert "minimum" in hint.lower()

    def test_normal_source_weight_no_hint(self):
        store = PreferenceStore()
        _, hint = store.apply_source_weight("u1", "bbc", 0.3)
        assert hint == ""

    def test_source_weight_returns_tuple(self):
        store = PreferenceStore()
        result = store.apply_source_weight("u1", "bbc", 0.3)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_source_zero_weight_pruned_at_cap(self):
        """Zero-weight sources are pruned when at cap."""
        store = PreferenceStore()
        uid = "u1"
        for i in range(store.MAX_WEIGHTS):
            store.apply_source_weight(uid, f"source_{i}", 0.5)
        # Zero out half
        for i in range(0, store.MAX_WEIGHTS, 2):
            store.apply_source_weight(uid, f"source_{i}", -0.5)
        # New source should work after pruning
        _, hint = store.apply_source_weight(uid, "new_source", 0.5)
        assert hint == ""
        profile = store.get_or_create(uid)
        assert "new_source" in profile.source_weights


# ══════════════════════════════════════════════════════════════════════════
# Email Header Injection Protection
# ══════════════════════════════════════════════════════════════════════════


class TestEmailHeaderInjection(unittest.TestCase):
    """Verify that CRLF injection is blocked in email addresses."""

    def _make_comm_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent
        engine = MagicMock()
        profile = MagicMock(timezone="UTC", email="")
        engine.preferences.get_or_create.return_value = profile
        bot = MagicMock()
        comm = CommunicationAgent(engine=engine, bot=bot)
        return comm, engine, bot

    def test_crlf_in_email_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_email("chat1", "u1", "attacker@evil.com\r\nBcc: victim@example.com")
        assert result["action"] == "email_invalid"
        engine.preferences.set_email.assert_not_called()

    def test_newline_in_email_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_email("chat1", "u1", "attacker@evil.com\nBcc: victim@example.com")
        assert result["action"] == "email_invalid"

    def test_null_byte_in_email_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_email("chat1", "u1", "user@example.com\x00evil")
        assert result["action"] == "email_invalid"

    def test_overlength_email_rejected(self):
        comm, engine, bot = self._make_comm_agent()
        result = comm._set_email("chat1", "u1", "a" * 250 + "@b.com")
        assert result["action"] == "email_invalid"

    def test_valid_email_accepted(self):
        comm, engine, bot = self._make_comm_agent()
        comm._set_email("chat1", "u1", "user@example.com")
        engine.preferences.set_email.assert_called_once()

    def test_email_digest_sanitize_header(self):
        """EmailDigest._sanitize_header strips CRLF."""
        assert EmailDigest._sanitize_header("test\r\nBcc: evil") == "testBcc: evil"
        assert EmailDigest._sanitize_header("clean") == "clean"
        assert EmailDigest._sanitize_header("null\x00byte") == "nullbyte"


# ══════════════════════════════════════════════════════════════════════════
# Preset Name Sanitization
# ══════════════════════════════════════════════════════════════════════════


class TestPresetNameSanitization(unittest.TestCase):
    """Verify that preset names reject dangerous characters."""

    def test_valid_name_accepted(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "Work Mode")
        assert err == ""

    def test_alphanumeric_with_hyphens(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "work-mode-2024")
        assert err == ""

    def test_underscore_allowed(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "deep_analysis")
        assert err == ""

    def test_ansi_escape_rejected(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "Work\x1b[31mHACKED\x1b[0m")
        assert "letters" in err.lower() or "only" in err.lower()

    def test_unicode_override_rejected(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "Test\u202eReversed")
        assert err != ""

    def test_html_injection_rejected(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "<script>alert(1)</script>")
        assert err != ""

    def test_empty_name_rejected(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "")
        assert "empty" in err.lower()

    def test_whitespace_only_rejected(self):
        store = PreferenceStore()
        _, err = store.save_preset("u1", "   ")
        assert "empty" in err.lower()

    def test_returns_tuple(self):
        store = PreferenceStore()
        result = store.save_preset("u1", "test")
        assert isinstance(result, tuple)
        assert len(result) == 2


# ══════════════════════════════════════════════════════════════════════════
# LLM Garbage Response Fallback
# ══════════════════════════════════════════════════════════════════════════


class TestLLMGarbageFallback(unittest.TestCase):
    """Verify that garbage LLM responses fall back to heuristic."""

    def test_parse_llm_json_returns_empty_for_garbage(self):
        """_parse_llm_json returns {} for unparseable content."""
        from newsfeed.agents.experts import ExpertCouncil
        council = ExpertCouncil.__new__(ExpertCouncil)
        result = council._parse_llm_json("This is not JSON at all!")
        assert result == {}

    def test_parse_llm_json_returns_empty_for_html(self):
        """_parse_llm_json returns {} for HTML error pages."""
        from newsfeed.agents.experts import ExpertCouncil
        council = ExpertCouncil.__new__(ExpertCouncil)
        result = council._parse_llm_json("<html><body>500 Internal Server Error</body></html>")
        assert result == {}

    def test_parse_llm_json_valid_json_works(self):
        """_parse_llm_json correctly parses valid JSON."""
        from newsfeed.agents.experts import ExpertCouncil
        council = ExpertCouncil.__new__(ExpertCouncil)
        result = council._parse_llm_json('{"keep": true, "confidence": 0.8}')
        assert result["keep"] is True
        assert result["confidence"] == 0.8


# ══════════════════════════════════════════════════════════════════════════
# Preset Save via Communication Agent
# ══════════════════════════════════════════════════════════════════════════


class TestPresetSaveIntegration(unittest.TestCase):
    """Verify that preset save errors surface to user."""

    def _make_comm_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent
        engine = MagicMock()
        profile = MagicMock(timezone="UTC", presets={})
        engine.preferences.get_or_create.return_value = profile
        bot = MagicMock()
        comm = CommunicationAgent(engine=engine, bot=bot)
        return comm, engine, bot

    def test_invalid_preset_name_shows_error(self):
        comm, engine, bot = self._make_comm_agent()
        engine.preferences.save_preset.return_value = (MagicMock(), "bad chars")
        result = comm._handle_preset("chat1", "u1", "save <script>bad</script>")
        assert result["action"] == "preset_save_error"
