"""Tests for smart narrative generation — validates that 'why', 'what_changed',
'outlook', and 'adjacent_reads' produce specific, metadata-driven text instead
of generic boilerplate.

These are intelligence quality tests: they verify that the TEXT OUTPUT reflects
the structured data that the pipeline produces, closing the gap between
"impressive prototype" and "daily-use tool".
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from newsfeed.intelligence.credibility import CredibilityTracker
from newsfeed.intelligence.narrative import (
    generate_adjacent_reads,
    generate_outlook,
    generate_what_changed,
    generate_why,
)
from newsfeed.models.domain import (
    CandidateItem,
    NarrativeThread,
    StoryLifecycle,
    UrgencyLevel,
    UserProfile,
)


@pytest.fixture
def credibility():
    """Build a CredibilityTracker with default config."""
    return CredibilityTracker()


def _make_candidate(
    source="reuters",
    topic="geopolitics",
    evidence=0.8,
    novelty=0.6,
    prediction=0.5,
    urgency=UrgencyLevel.ROUTINE,
    lifecycle=StoryLifecycle.DEVELOPING,
    corroborated_by=None,
    regions=None,
    title="Test headline for quality verification",
    summary="Test summary text for verification",
    url="https://reuters.com/test",
) -> CandidateItem:
    return CandidateItem(
        candidate_id=f"test_{source}_{topic}",
        title=title,
        source=source,
        summary=summary,
        url=url,
        topic=topic,
        evidence_score=evidence,
        novelty_score=novelty,
        preference_fit=0.7,
        prediction_signal=prediction,
        discovered_by=f"{source}_agent",
        urgency=urgency,
        lifecycle=lifecycle,
        corroborated_by=corroborated_by or [],
        regions=regions or [],
    )


# ══════════════════════════════════════════════════════════════════════════
# generate_why tests
# ══════════════════════════════════════════════════════════════════════════


class TestGenerateWhy:
    """Why text must reflect source quality, corroboration, urgency, and alignment."""

    def test_mentions_source_name(self, credibility):
        c = _make_candidate(source="reuters")
        result = generate_why(c, credibility)
        assert "Reuters" in result

    def test_mentions_topic(self, credibility):
        c = _make_candidate(topic="geopolitics")
        result = generate_why(c, credibility)
        assert "geopolitics" in result.lower()

    def test_mentions_corroboration_when_present(self, credibility):
        c = _make_candidate(corroborated_by=["bbc", "guardian"])
        result = generate_why(c, credibility)
        assert "corroborat" in result.lower() or "confirmed" in result.lower()

    def test_no_corroboration_text_when_absent(self, credibility):
        c = _make_candidate(corroborated_by=[])
        result = generate_why(c, credibility)
        assert "corroborat" not in result.lower()

    def test_mentions_regions_when_present(self, credibility):
        c = _make_candidate(regions=["middle_east", "europe"])
        result = generate_why(c, credibility)
        assert "Middle East" in result or "Europe" in result

    def test_breaking_urgency_reflected(self, credibility):
        c = _make_candidate(urgency=UrgencyLevel.BREAKING)
        result = generate_why(c, credibility)
        assert "breaking" in result.lower()

    def test_critical_urgency_reflected(self, credibility):
        c = _make_candidate(urgency=UrgencyLevel.CRITICAL)
        result = generate_why(c, credibility)
        assert "critical" in result.lower()

    def test_user_alignment_mentioned_for_high_weight(self, credibility):
        profile = UserProfile(user_id="u1", topic_weights={"geopolitics": 0.9})
        c = _make_candidate(topic="geopolitics")
        result = generate_why(c, credibility, profile)
        assert "high-priority" in result.lower() or "interest" in result.lower()

    def test_low_reliability_source_flagged(self, credibility):
        c = _make_candidate(source="reddit")
        result = generate_why(c, credibility)
        assert "lower-reliability" in result.lower() or "verify" in result.lower()

    def test_high_reliability_source_noted(self, credibility):
        c = _make_candidate(source="reuters", evidence=0.8)
        result = generate_why(c, credibility)
        assert "high-reliability" in result.lower() or "strong evidence" in result.lower()

    def test_never_produces_boilerplate(self, credibility):
        """The old boilerplate text must never appear."""
        c = _make_candidate()
        result = generate_why(c, credibility)
        assert "Aligned with your weighted interest" not in result
        assert "strong source quality" not in result

    def test_always_ends_with_period(self, credibility):
        c = _make_candidate()
        result = generate_why(c, credibility)
        assert result.endswith(".")


# ══════════════════════════════════════════════════════════════════════════
# generate_what_changed tests
# ══════════════════════════════════════════════════════════════════════════


class TestGenerateWhatChanged:
    """What_changed text must reflect lifecycle, corroboration, urgency, and novelty."""

    def test_breaking_lifecycle(self, credibility):
        c = _make_candidate(lifecycle=StoryLifecycle.BREAKING)
        result = generate_what_changed(c, credibility)
        assert "breaking" in result.lower()

    def test_developing_lifecycle(self, credibility):
        c = _make_candidate(lifecycle=StoryLifecycle.DEVELOPING)
        result = generate_what_changed(c, credibility)
        assert "developing" in result.lower()

    def test_waning_lifecycle(self, credibility):
        c = _make_candidate(lifecycle=StoryLifecycle.WANING)
        result = generate_what_changed(c, credibility)
        assert "declining" in result.lower() or "waning" in result.lower()

    def test_multi_source_corroboration(self, credibility):
        c = _make_candidate(corroborated_by=["bbc", "guardian", "ap"])
        result = generate_what_changed(c, credibility)
        assert "3" in result or "independent" in result.lower()

    def test_single_source_noted(self, credibility):
        c = _make_candidate(corroborated_by=[])
        result = generate_what_changed(c, credibility)
        assert "single-source" in result.lower() or "awaiting" in result.lower()

    def test_high_novelty_noted(self, credibility):
        c = _make_candidate(novelty=0.9)
        result = generate_what_changed(c, credibility)
        assert "novelty" in result.lower() or "first appearance" in result.lower()

    def test_never_produces_boilerplate(self, credibility):
        c = _make_candidate()
        result = generate_what_changed(c, credibility)
        assert "New cross-source confirmation" not in result
        assert "discussion momentum" not in result

    def test_always_ends_with_period(self, credibility):
        c = _make_candidate()
        result = generate_what_changed(c, credibility)
        assert result.endswith(".")


# ══════════════════════════════════════════════════════════════════════════
# generate_outlook tests
# ══════════════════════════════════════════════════════════════════════════


class TestGenerateOutlook:
    """Outlook text must reflect prediction signals, urgency, and evidence."""

    def test_strong_prediction_signal(self, credibility):
        c = _make_candidate(prediction=0.8)
        result = generate_outlook(c, credibility)
        assert "strong" in result.lower() or "significant" in result.lower()

    def test_weak_prediction_signal(self, credibility):
        c = _make_candidate(prediction=0.2)
        result = generate_outlook(c, credibility)
        assert "limited" in result.lower()

    def test_critical_urgency_escalation(self, credibility):
        c = _make_candidate(urgency=UrgencyLevel.CRITICAL)
        result = generate_outlook(c, credibility)
        assert "escalation" in result.lower() or "rapid" in result.lower()

    def test_breaking_urgency_timeframe(self, credibility):
        c = _make_candidate(urgency=UrgencyLevel.BREAKING)
        result = generate_outlook(c, credibility)
        assert "hours" in result.lower() or "follow-on" in result.lower()

    def test_market_topic_gets_market_mention(self, credibility):
        c = _make_candidate(topic="markets", prediction=0.6)
        result = generate_outlook(c, credibility)
        assert "market" in result.lower()

    def test_strong_evidence_noted(self, credibility):
        c = _make_candidate(evidence=0.9)
        result = generate_outlook(c, credibility)
        assert "strong evidence" in result.lower()

    def test_weak_evidence_noted(self, credibility):
        c = _make_candidate(evidence=0.3)
        result = generate_outlook(c, credibility)
        assert "limited evidence" in result.lower()

    def test_high_corroboration_conviction(self, credibility):
        c = _make_candidate(corroborated_by=["bbc", "guardian", "ap"])
        result = generate_outlook(c, credibility)
        assert "multi-source" in result.lower() or "conviction" in result.lower()

    def test_never_produces_boilerplate(self, credibility):
        c = _make_candidate()
        result = generate_outlook(c, credibility)
        assert "Market and narrative signals suggest" not in result
        assert "elevated watch priority" not in result or "urgency" in result.lower()

    def test_always_ends_with_period(self, credibility):
        c = _make_candidate()
        result = generate_outlook(c, credibility)
        assert result.endswith(".")


# ══════════════════════════════════════════════════════════════════════════
# generate_adjacent_reads tests
# ══════════════════════════════════════════════════════════════════════════


class TestGenerateAdjacentReads:
    """Adjacent reads must pull from thread siblings and reserve candidates, not placeholders."""

    def test_reads_from_thread_siblings(self):
        main = _make_candidate(source="reuters", title="Main story about NATO")
        sibling = _make_candidate(
            source="bbc",
            title="BBC: NATO summit analysis and key developments",
        )
        thread = NarrativeThread(
            thread_id="t1", headline="NATO",
            candidates=[main, sibling],
        )
        reads = generate_adjacent_reads(main, [thread])
        assert len(reads) >= 1
        assert "BBC" in reads[0] or "bbc" in reads[0]

    def test_no_self_reference(self):
        main = _make_candidate(source="reuters", title="Main story")
        thread = NarrativeThread(
            thread_id="t1", headline="Thread",
            candidates=[main],
        )
        reads = generate_adjacent_reads(main, [thread])
        assert len(reads) == 0

    def test_reads_from_reserve_when_no_thread(self):
        main = _make_candidate(source="reuters", topic="geopolitics")
        reserve = [
            _make_candidate(
                source="bbc",
                topic="geopolitics",
                title="Related geopolitics story from reserve",
            ),
        ]
        reads = generate_adjacent_reads(main, [], reserve)
        assert len(reads) >= 1
        assert "reserve" in reads[0].lower() or "geopolitics" in reads[0].lower()

    def test_never_produces_placeholder(self):
        main = _make_candidate()
        reserve = [
            _make_candidate(
                source="guardian",
                topic="geopolitics",
                title="Real story title from Guardian",
            ),
        ]
        reads = generate_adjacent_reads(main, [], reserve)
        for r in reads:
            assert "Context read" not in r, f"Placeholder text found: {r}"

    def test_respects_limit(self):
        main = _make_candidate(source="reuters")
        reserves = [
            _make_candidate(
                source=f"src_{i}",
                topic="geopolitics",
                title=f"Reserve story {i}",
            )
            for i in range(10)
        ]
        reads = generate_adjacent_reads(main, [], reserves, limit=3)
        assert len(reads) <= 3

    def test_deduplicates_by_candidate_id(self):
        main = _make_candidate(source="reuters")
        same = _make_candidate(source="reuters", title="Duplicate")
        thread = NarrativeThread(
            thread_id="t1", headline="Thread",
            candidates=[main, same],
        )
        reads = generate_adjacent_reads(main, [thread])
        # same source candidates don't get added
        assert len(reads) == 0

    def test_includes_source_attribution(self):
        main = _make_candidate(source="reuters")
        sibling = _make_candidate(source="bbc", title="BBC analysis of events")
        thread = NarrativeThread(
            thread_id="t1", headline="Thread",
            candidates=[main, sibling],
        )
        reads = generate_adjacent_reads(main, [thread])
        assert len(reads) >= 1
        # Source should be in brackets
        assert "[bbc]" in reads[0]


# ══════════════════════════════════════════════════════════════════════════
# Onboarding tests
# ══════════════════════════════════════════════════════════════════════════


class TestOnboarding:
    """Test the interactive onboarding flow."""

    def test_welcome_message_has_keyboard(self):
        from newsfeed.delivery.onboarding import build_welcome_message
        text, keyboard = build_welcome_message()
        assert "Step 1/3" in text
        assert "inline_keyboard" in keyboard

    def test_role_message_shows_selected_topics(self):
        from newsfeed.delivery.onboarding import build_role_message
        text, keyboard = build_role_message(["geopolitics", "ai_policy"])
        assert "Geopolitics" in text
        assert "AI" in text
        assert "Step 2/3" in text

    def test_detail_message_shows_role(self):
        from newsfeed.delivery.onboarding import build_detail_message
        text, keyboard = build_detail_message("investor")
        assert "Investor" in text
        assert "Step 3/3" in text

    def test_apply_profile_sets_weights(self):
        from newsfeed.delivery.onboarding import apply_onboarding_profile
        from newsfeed.memory.store import PreferenceStore

        prefs = PreferenceStore()
        weights = apply_onboarding_profile(
            prefs, "u1",
            selected_topics=["geopolitics", "markets"],
            role="investor",
            detail_level="standard",
        )
        profile = prefs.get_or_create("u1")
        assert profile.topic_weights["markets"] > 0.5
        assert profile.topic_weights["geopolitics"] > 0.5
        assert profile.tone == "concise"

    def test_apply_profile_boosts_selected(self):
        from newsfeed.delivery.onboarding import apply_onboarding_profile, ROLE_PRESETS
        from newsfeed.memory.store import PreferenceStore

        prefs = PreferenceStore()
        weights = apply_onboarding_profile(
            prefs, "u1",
            selected_topics=["climate"],
            role="general",
            detail_level="deep",
        )
        # Climate should be boosted above the general preset's baseline
        general_base = ROLE_PRESETS["general"].get("climate", 0.3)
        assert weights.get("climate", 0) >= general_base

    def test_headlines_detail_sets_15_items(self):
        from newsfeed.delivery.onboarding import apply_onboarding_profile
        from newsfeed.memory.store import PreferenceStore

        prefs = PreferenceStore()
        apply_onboarding_profile(prefs, "u1", ["geopolitics"], "general", "headlines")
        profile = prefs.get_or_create("u1")
        assert profile.max_items == 15

    def test_deep_detail_sets_analytical_tone(self):
        from newsfeed.delivery.onboarding import apply_onboarding_profile
        from newsfeed.memory.store import PreferenceStore

        prefs = PreferenceStore()
        apply_onboarding_profile(prefs, "u1", ["geopolitics"], "analyst", "deep")
        profile = prefs.get_or_create("u1")
        assert profile.tone == "analytical"

    def test_completion_message_shows_weights(self):
        from newsfeed.delivery.onboarding import build_completion_message
        msg = build_completion_message(
            ["geopolitics", "ai_policy"], "analyst", "standard",
            {"geopolitics": 0.9, "ai_policy": 0.8, "markets": 0.6},
        )
        assert "Setup complete" in msg
        assert "Geopolitics" in msg
        assert "/briefing" in msg


# ══════════════════════════════════════════════════════════════════════════
# Topic discovery tests
# ══════════════════════════════════════════════════════════════════════════


class TestTopicDiscovery:
    """Test that topic discovery surfaces emerging trends the user doesn't track."""

    def test_discovers_untracked_topics(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        result = fmt.format_topic_discovery(
            ["crypto", "climate", "geopolitics"],
            {"geopolitics": 0.8},
        )
        assert "crypto" in result.lower() or "climate" in result.lower()
        # geopolitics should NOT appear (user already tracks it)
        assert "Geopolitics" not in result

    def test_empty_when_all_tracked(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        result = fmt.format_topic_discovery(
            ["geopolitics"],
            {"geopolitics": 0.8},
        )
        assert result == ""

    def test_empty_when_no_emerging(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        result = fmt.format_topic_discovery(
            [],
            {"geopolitics": 0.8},
        )
        assert result == ""

    def test_limits_to_3_suggestions(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        result = fmt.format_topic_discovery(
            ["crypto", "climate", "defense", "space", "health"],
            {},
        )
        # Should show max 3 bullets
        bullet_count = result.count("\u2022")
        assert bullet_count <= 3


# ══════════════════════════════════════════════════════════════════════════
# Quick mode headlines-only tests
# ══════════════════════════════════════════════════════════════════════════


class TestQuickModeHeadlinesOnly:
    """Test that headlines_only mode produces truly compact output."""

    def test_headlines_only_no_context(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        from newsfeed.models.domain import ReportItem, ConfidenceBand
        fmt = TelegramFormatter()

        c = _make_candidate(source="reuters", title="NATO Summit Concludes")
        item = ReportItem(
            candidate=c,
            why_it_matters="Very important because of many reasons",
            what_changed="Things changed a lot",
            predictive_outlook="Outlook is uncertain",
            adjacent_reads=[],
            confidence=ConfidenceBand(low=0.6, mid=0.75, high=0.9),
        )

        result = fmt.format_quick_card(item, 1, headlines_only=True)
        # Should NOT include the why_it_matters text
        assert "Very important" not in result
        assert "many reasons" not in result
        # Should include headline and source
        assert "NATO Summit Concludes" in result
        assert "reuters" in result.lower()

    def test_headlines_only_is_single_line(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        from newsfeed.models.domain import ReportItem, ConfidenceBand
        fmt = TelegramFormatter()

        c = _make_candidate(source="bbc", title="Test headline")
        item = ReportItem(
            candidate=c,
            why_it_matters="Extended context text here",
            what_changed="Changed",
            predictive_outlook="Outlook",
            adjacent_reads=[],
        )

        result = fmt.format_quick_card(item, 1, headlines_only=True)
        # Should be a single line (no newlines)
        assert "\n" not in result

    def test_standard_quick_includes_context(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        from newsfeed.models.domain import ReportItem, ConfidenceBand
        fmt = TelegramFormatter()

        c = _make_candidate(source="reuters", title="Test headline")
        item = ReportItem(
            candidate=c,
            why_it_matters="Important context that should appear",
            what_changed="Changed",
            predictive_outlook="Outlook",
            adjacent_reads=[],
            confidence=ConfidenceBand(low=0.6, mid=0.75, high=0.9),
        )

        result = fmt.format_quick_card(item, 1, headlines_only=False)
        # Should include the why_it_matters snippet
        assert "Important context" in result
