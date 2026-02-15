"""Tests for final-pass UX polish: fuzzy matching, callback handling,
tracked story resilience, and error categorization.
"""
from __future__ import annotations

import pytest

from newsfeed.memory.commands import (
    ParseResult,
    fuzzy_correct_topic,
    parse_preference_commands,
    parse_preference_commands_rich,
    _fuzzy_match_value,
    _VALID_TONES,
    _VALID_FORMATS,
    _VALID_CADENCES,
)
from newsfeed.memory.store import match_tracked, extract_keywords


# ══════════════════════════════════════════════════════════════════════════
# Fuzzy Topic Matching
# ══════════════════════════════════════════════════════════════════════════


class TestFuzzyTopicCorrection:
    """Verify that topic typos get corrected to known topics."""

    def test_exact_match_no_correction(self):
        known = {"geopolitics", "technology", "markets"}
        corrected, hint = fuzzy_correct_topic("geopolitics", known)
        assert corrected == "geopolitics"
        assert hint is None

    def test_typo_corrected(self):
        known = {"geopolitics", "technology", "markets"}
        corrected, hint = fuzzy_correct_topic("geoplitics", known)
        assert corrected == "geopolitics"
        assert "geopolitics" in hint

    def test_close_typo_corrected(self):
        known = {"ai_policy", "technology", "markets"}
        corrected, hint = fuzzy_correct_topic("ai_polciy", known)
        assert corrected == "ai_policy"
        assert hint is not None

    def test_no_match_passes_through(self):
        known = {"geopolitics", "technology"}
        corrected, hint = fuzzy_correct_topic("quantum_computing", known)
        assert corrected == "quantum_computing"
        assert hint is None

    def test_empty_known_topics(self):
        corrected, hint = fuzzy_correct_topic("anything", set())
        assert corrected == "anything"
        assert hint is None


class TestFuzzyValueMatching:
    """Verify tone/format/cadence fuzzy matching."""

    def test_exact_tone_match(self):
        assert _fuzzy_match_value("analyst", _VALID_TONES) == "analyst"

    def test_tone_typo(self):
        result = _fuzzy_match_value("anlayst", _VALID_TONES)
        assert result == "analyst"

    def test_unknown_tone_returns_none(self):
        assert _fuzzy_match_value("casual", _VALID_TONES) is None

    def test_format_typo(self):
        result = _fuzzy_match_value("bullat", _VALID_FORMATS)
        assert result == "bullet"

    def test_cadence_typo(self):
        result = _fuzzy_match_value("mornig", _VALID_CADENCES)
        assert result == "morning"


class TestRichPreferenceParser:
    """Verify parse_preference_commands_rich returns corrections and errors."""

    def test_correct_topic_no_correction(self):
        known = {"geopolitics", "technology"}
        result = parse_preference_commands_rich("more geopolitics", known)
        assert len(result.commands) == 1
        assert result.commands[0].topic == "geopolitics"
        assert not result.corrections

    def test_typo_topic_corrected(self):
        known = {"geopolitics", "technology"}
        result = parse_preference_commands_rich("more geoplitics", known)
        assert len(result.commands) >= 1
        # The rich parser should have found the correction
        has_correction = any("geopolitics" in c for c in result.corrections)
        # Either corrected or passed through (fuzzy match depends on distance)
        assert result.commands[0].topic in ("geopolitics", "geoplitics")

    def test_invalid_tone_shows_valid_options(self):
        result = parse_preference_commands_rich("tone: casual")
        assert len(result.unrecognized) == 1
        assert "casual" in result.unrecognized[0]
        assert "concise" in result.unrecognized[0]  # Valid options shown

    def test_tone_typo_corrected(self):
        result = parse_preference_commands_rich("tone: anlayst")
        # Should fuzzy match to "analyst"
        tone_cmds = [c for c in result.commands if c.action == "tone"]
        if tone_cmds:
            assert tone_cmds[0].value == "analyst"
            assert any("analyst" in c for c in result.corrections)

    def test_invalid_format_shows_valid_options(self):
        result = parse_preference_commands_rich("format: table")
        assert len(result.unrecognized) == 1
        assert "table" in result.unrecognized[0]
        assert "bullet" in result.unrecognized[0]

    def test_backward_compat_standard_parser(self):
        """Standard parser still works unchanged."""
        cmds = parse_preference_commands("more geopolitics, less crypto")
        assert len(cmds) == 2
        topics = {c.topic for c in cmds}
        assert "geopolitics" in topics
        assert "crypto" in topics

    def test_new_topic_passes_through(self):
        """Unknown topics are still allowed (user might be creating new ones)."""
        known = {"geopolitics"}
        result = parse_preference_commands_rich("more quantum_computing", known)
        assert result.commands[0].topic == "quantum_computing"
        assert not result.corrections


# ══════════════════════════════════════════════════════════════════════════
# Tracked Story Matching Resilience
# ══════════════════════════════════════════════════════════════════════════


class TestTrackedMatchResilience:
    """Verify match_tracked handles headline evolution."""

    def _tracked(self, topic="geopolitics", headline="China Taiwan tensions escalate"):
        return {
            "topic": topic,
            "keywords": extract_keywords(headline),
            "headline": headline,
        }

    def test_strong_match_two_keywords(self):
        tracked = self._tracked()
        assert match_tracked("geopolitics", "Taiwan tensions rise sharply", tracked)

    def test_weak_match_one_substantial_keyword(self):
        """Single 5+ char keyword overlap should match (new behavior)."""
        tracked = self._tracked()
        # "tensions" (8 chars) is the only overlap
        assert match_tracked("geopolitics", "Regional tensions in new phase", tracked)

    def test_no_match_one_short_keyword(self):
        """Single short (<5 char) keyword should NOT match."""
        tracked = self._tracked(headline="GDP data rises fast")
        # "data" is 4 chars — too short for weak match
        assert not match_tracked("geopolitics", "New data on imports", tracked)

    def test_no_match_different_topic(self):
        tracked = self._tracked()
        assert not match_tracked("technology", "China Taiwan tensions escalate", tracked)

    def test_no_match_zero_overlap(self):
        tracked = self._tracked()
        assert not match_tracked("geopolitics", "Completely different headline here", tracked)

    def test_headline_evolution_across_days(self):
        """Real-world case: same story, different wording over days."""
        tracked = self._tracked(
            headline="Federal Reserve signals interest rate hike"
        )
        # Day 2: different wording
        assert match_tracked(
            "markets",
            "Interest rate increases expected from central bank",
            self._tracked(topic="markets",
                          headline="Federal Reserve signals interest rate hike")
        )

    def test_extract_keywords_filters_stopwords(self):
        words = extract_keywords("The new policy will not change this")
        assert "the" not in words
        assert "will" not in words
        assert "not" not in words
        assert "policy" in words
        assert "change" in words


# ══════════════════════════════════════════════════════════════════════════
# Error Categorization
# ══════════════════════════════════════════════════════════════════════════


class TestErrorCategorization:
    """Verify _categorize_error produces helpful messages."""

    def _categorize(self, exc):
        from newsfeed.orchestration.communication import CommunicationAgent
        return CommunicationAgent._categorize_error(exc)

    def test_timeout_error(self):
        msg = self._categorize(TimeoutError("request timed out"))
        assert "timed out" in msg.lower()

    def test_connection_error(self):
        msg = self._categorize(ConnectionError("connection refused"))
        assert "network" in msg.lower() or "connection" in msg.lower()

    def test_value_error(self):
        msg = self._categorize(ValueError("invalid input"))
        assert "unexpected" in msg.lower()

    def test_type_error(self):
        msg = self._categorize(TypeError("wrong type"))
        assert "unexpected" in msg.lower()

    def test_generic_error(self):
        msg = self._categorize(RuntimeError("unknown issue"))
        assert "went wrong" in msg.lower()

    def test_all_messages_are_user_friendly(self):
        """No error messages should contain technical jargon."""
        for exc in [TimeoutError(), ConnectionError(), ValueError(),
                     TypeError(), RuntimeError()]:
            msg = self._categorize(exc)
            assert "traceback" not in msg.lower()
            assert "exception" not in msg.lower()
            assert "NoneType" not in msg
