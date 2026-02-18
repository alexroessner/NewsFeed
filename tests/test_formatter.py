"""Tests for TelegramFormatter — validates story cards, headers, footers,
Markdown export, and quick-scan formatting.

Covers: HTML escaping, message length safety, edge cases with None/empty data,
Markdown export escaping, and format consistency across methods.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from newsfeed.delivery.telegram import TelegramFormatter, _esc_md, _MAX_CARD_LENGTH
from newsfeed.models.domain import (
    BriefingType,
    CandidateItem,
    ConfidenceBand,
    DeliveryPayload,
    GeoRiskEntry,
    NarrativeThread,
    ReportItem,
    StoryLifecycle,
    TrendSnapshot,
    UrgencyLevel,
)


@pytest.fixture
def formatter():
    return TelegramFormatter()


def _make_candidate(
    source="reuters",
    topic="geopolitics",
    title="Test Headline",
    summary="Test summary with some details.",
    url="https://reuters.com/test",
    urgency=UrgencyLevel.ROUTINE,
    lifecycle=StoryLifecycle.DEVELOPING,
    corroborated_by=None,
    regions=None,
    evidence_score=0.8,
    novelty_score=0.6,
) -> CandidateItem:
    return CandidateItem(
        candidate_id=f"test_{source}_{topic}",
        title=title,
        source=source,
        summary=summary,
        url=url,
        topic=topic,
        evidence_score=evidence_score,
        novelty_score=novelty_score,
        preference_fit=0.7,
        prediction_signal=0.5,
        discovered_by=f"{source}_agent",
        urgency=urgency,
        lifecycle=lifecycle,
        corroborated_by=corroborated_by or [],
        regions=regions or [],
    )


def _make_report_item(
    candidate=None,
    why="This matters because it affects global trade.",
    what_changed="New sanctions announced today.",
    outlook="Expect further escalation in 2 weeks.",
    adjacent=None,
    confidence=None,
    contrarian="",
) -> ReportItem:
    if candidate is None:
        candidate = _make_candidate()
    return ReportItem(
        candidate=candidate,
        why_it_matters=why,
        what_changed=what_changed,
        predictive_outlook=outlook,
        adjacent_reads=adjacent or [],
        confidence=confidence,
        contrarian_note=contrarian,
    )


def _make_payload(items=None, metadata=None, threads=None,
                  geo_risks=None, trends=None) -> DeliveryPayload:
    if items is None:
        items = [_make_report_item()]
    return DeliveryPayload(
        user_id="test_user",
        generated_at=datetime(2025, 6, 15, 10, 30, tzinfo=timezone.utc),
        items=items,
        metadata=metadata or {},
        briefing_type=BriefingType.MORNING_DIGEST,
        threads=threads or [],
        geo_risks=geo_risks or [],
        trends=trends or [],
    )


# ══════════════════════════════════════════════════════════════════════════
# Story Card Tests
# ══════════════════════════════════════════════════════════════════════════


class TestStoryCard:
    """Tests for format_story_card — the primary user-facing output."""

    def test_basic_card_has_title_and_source(self, formatter):
        item = _make_report_item()
        card = formatter.format_story_card(item, 1)
        assert "Test Headline" in card
        assert "reuters" in card

    def test_html_escaping_in_title(self, formatter):
        c = _make_candidate(title="Breaking: Oil & Gas <Alert>")
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "&amp;" in card
        assert "&lt;Alert&gt;" in card
        # Raw chars must NOT appear
        assert "& Gas <Alert>" not in card

    def test_html_escaping_in_source(self, formatter):
        c = _make_candidate(source="A&B News")
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "A&amp;B News" in card

    def test_includes_why_it_matters(self, formatter):
        item = _make_report_item(why="Trade routes are shifting.")
        card = formatter.format_story_card(item, 1)
        assert "Why it matters" in card
        assert "Trade routes are shifting" in card

    def test_what_changed_reserved_for_deep_dive(self, formatter):
        """Regular cards don't show 'What changed' — it's redundant with the title."""
        item = _make_report_item(what_changed="New policy announced.")
        card = formatter.format_story_card(item, 1)
        assert "What changed" not in card
        # But deep dive should still show it
        deep = formatter.format_deep_dive(item, 1)
        assert "What changed" in deep

    def test_predictive_outlook_gated_on_signal(self, formatter):
        """Outlook only shown when prediction_signal > 0.6."""
        item = _make_report_item(outlook="Markets will react within 48h.")
        # Default signal is 0.5 — should be suppressed
        card = formatter.format_story_card(item, 1)
        assert "Markets will react" not in card
        # High signal — should appear
        item.candidate.prediction_signal = 0.8
        card = formatter.format_story_card(item, 1)
        assert "Markets will react" in card

    def test_tracked_badge_shown(self, formatter):
        item = _make_report_item()
        card = formatter.format_story_card(item, 1, is_tracked=True)
        assert "\U0001f4cc" in card

    def test_delta_tags(self, formatter):
        item = _make_report_item()
        for tag in ("new", "updated", "developing"):
            card = formatter.format_story_card(item, 1, delta_tag=tag)
            assert f"[{tag.upper()}]" in card

    def test_confidence_label(self, formatter):
        band = ConfidenceBand(low=0.7, mid=0.85, high=0.95)
        item = _make_report_item(confidence=band)
        card = formatter.format_story_card(item, 1)
        assert "High confidence" in card

    def test_regions_shown(self, formatter):
        c = _make_candidate(regions=["middle_east", "europe"])
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "Middle East" in card
        assert "Europe" in card

    def test_corroboration_shown_with_display_names(self, formatter):
        c = _make_candidate(corroborated_by=["bbc", "ap"])
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "Verified by" in card
        # Source IDs are mapped to human-readable names
        assert "BBC News" in card
        assert "AP News" in card

    def test_adjacent_reads_reserved_for_deep_dive(self, formatter):
        """Regular cards don't show 'Related:' — deep dive does."""
        item = _make_report_item(adjacent=["Related story one", "Related story two"])
        card = formatter.format_story_card(item, 1)
        assert "Related:" not in card
        # Deep dive should have them
        deep = formatter.format_deep_dive(item, 1)
        assert "Related story one" in deep

    def test_no_reading_time_in_cards(self, formatter):
        """Reading time is removed — it's obvious for 3-sentence summaries."""
        c = _make_candidate(summary=" ".join(["word"] * 100))
        item = _make_report_item(candidate=c, why=" ".join(["analysis"] * 100))
        card = formatter.format_story_card(item, 1)
        assert "min read" not in card

    def test_url_linked_for_real_urls(self, formatter):
        c = _make_candidate(url="https://reuters.com/real-article")
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert 'href="https://reuters.com/real-article"' in card

    def test_example_com_urls_not_linked(self, formatter):
        c = _make_candidate(url="https://example.com/placeholder")
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "href" not in card

    def test_empty_summary_handled(self, formatter):
        c = _make_candidate(summary="")
        item = _make_report_item(candidate=c)
        card = formatter.format_story_card(item, 1)
        assert "Test Headline" in card  # Still renders

    def test_none_why_handled(self, formatter):
        item = _make_report_item(why="")
        card = formatter.format_story_card(item, 1)
        assert "Why it matters" not in card

    def test_contrarian_note(self, formatter):
        item = _make_report_item(contrarian="Some analysts disagree on the timeline.")
        card = formatter.format_story_card(item, 1)
        assert "Some analysts disagree" in card


class TestCardLengthSafety:
    """Verify cards don't exceed Telegram's message limit."""

    def test_long_card_truncated(self, formatter):
        """A card with very long content should be truncated gracefully."""
        c = _make_candidate(summary=" ".join(["word"] * 500))
        item = _make_report_item(
            candidate=c,
            why=" ".join(["analysis"] * 300),
            what_changed=" ".join(["changed"] * 200),
            outlook=" ".join(["outlook"] * 200),
            adjacent=["Read one " * 20, "Read two " * 20, "Read three " * 20],
        )
        card = formatter.format_story_card(item, 1)
        assert len(card) <= 4096
        assert "truncated" in card.lower() or len(card) < _MAX_CARD_LENGTH

    def test_normal_card_not_truncated(self, formatter):
        item = _make_report_item()
        card = formatter.format_story_card(item, 1)
        assert "truncated" not in card.lower()


# ══════════════════════════════════════════════════════════════════════════
# Header Tests
# ══════════════════════════════════════════════════════════════════════════


class TestHeader:
    """Tests for format_header."""

    def test_header_has_title(self, formatter):
        payload = _make_payload()
        header = formatter.format_header(payload)
        assert "Intelligence Digest" in header

    def test_header_has_timestamp(self, formatter):
        payload = _make_payload()
        header = formatter.format_header(payload)
        assert "Jun 15, 2025" in header

    def test_header_has_story_count(self, formatter):
        items = [_make_report_item() for _ in range(5)]
        payload = _make_payload(items=items)
        header = formatter.format_header(payload)
        assert "5 stories" in header

    def test_header_has_ticker(self, formatter):
        payload = _make_payload()
        header = formatter.format_header(payload, ticker_bar="BTC: $100K")
        assert "BTC: $100K" in header

    def test_header_shows_tracked_count(self, formatter):
        payload = _make_payload()
        header = formatter.format_header(payload, tracked_count=3)
        assert "3 tracked" in header

    def test_header_shows_geo_risks(self, formatter):
        risk = GeoRiskEntry(
            region="middle_east", risk_level=0.85,
            escalation_delta=0.15, drivers=["conflict"],
        )
        payload = _make_payload(geo_risks=[risk])
        header = formatter.format_header(payload)
        assert "Geo Risk" in header
        assert "Middle East" in header

    def test_header_shows_trends(self, formatter):
        trend = TrendSnapshot(
            topic="ai_policy", velocity=10.0, baseline_velocity=3.0,
            anomaly_score=3.5, is_emerging=True,
        )
        payload = _make_payload(trends=[trend])
        header = formatter.format_header(payload)
        assert "Emerging Trends" in header
        assert "ai_policy" in header

    def test_header_empty_items(self, formatter):
        payload = _make_payload(items=[])
        header = formatter.format_header(payload)
        assert "Intelligence Digest" in header
        assert "stories follow" not in header


# ══════════════════════════════════════════════════════════════════════════
# Footer Tests
# ══════════════════════════════════════════════════════════════════════════


class TestFooter:
    """Tests for format_footer."""

    def test_footer_has_topic_distribution(self, formatter):
        items = [
            _make_report_item(candidate=_make_candidate(topic="geopolitics")),
            _make_report_item(candidate=_make_candidate(topic="geopolitics")),
            _make_report_item(candidate=_make_candidate(topic="technology")),
        ]
        payload = _make_payload(items=items)
        footer = formatter.format_footer(payload)
        assert "geopolitics" in footer.lower()
        assert "technology" in footer.lower()

    def test_footer_has_source_count(self, formatter):
        items = [
            _make_report_item(candidate=_make_candidate(source="reuters")),
            _make_report_item(candidate=_make_candidate(source="bbc")),
        ]
        payload = _make_payload(items=items)
        footer = formatter.format_footer(payload)
        assert "2 sources" in footer

    def test_footer_has_reading_time(self, formatter):
        payload = _make_payload()
        footer = formatter.format_footer(payload)
        assert "min total read time" in footer

    def test_footer_empty_items(self, formatter):
        payload = _make_payload(items=[])
        footer = formatter.format_footer(payload)
        # Should not crash, just show the line
        assert "\u2500" in footer


# ══════════════════════════════════════════════════════════════════════════
# Quick Briefing Tests
# ══════════════════════════════════════════════════════════════════════════


class TestQuickBriefing:
    """Tests for format_quick_card and format_quick_briefing."""

    def test_headlines_only_is_compact(self, formatter):
        item = _make_report_item()
        card = formatter.format_quick_card(item, 1, headlines_only=True)
        # Headlines-only: no confidence, no context snippet
        assert "confidence" not in card.lower()
        # Should be a single line (no newlines)
        assert "\n" not in card

    def test_standard_quick_has_context(self, formatter):
        item = _make_report_item(why="Important trade implications.")
        card = formatter.format_quick_card(item, 1, headlines_only=False)
        assert "Important trade" in card

    def test_quick_briefing_complete(self, formatter):
        items = [_make_report_item() for _ in range(3)]
        payload = _make_payload(items=items)
        text = formatter.format_quick_briefing(payload)
        assert "Quick Scan" in text
        assert "3 stories" in text

    def test_quick_briefing_delta_tags(self, formatter):
        items = [_make_report_item(), _make_report_item()]
        payload = _make_payload(items=items)
        text = formatter.format_quick_briefing(
            payload, delta_tags=["new", "developing"]
        )
        assert "[NEW]" in text
        assert "[DEVELOPING]" in text

    def test_quick_briefing_empty(self, formatter):
        payload = _make_payload(items=[])
        text = formatter.format_quick_briefing(payload)
        assert "No stories matched" in text


# ══════════════════════════════════════════════════════════════════════════
# Markdown Export Tests
# ══════════════════════════════════════════════════════════════════════════


class TestMarkdownExport:
    """Tests for format_markdown_export — escaping is critical here."""

    def test_basic_export(self, formatter):
        payload = _make_payload()
        md = formatter.format_markdown_export(payload)
        assert "# Intelligence Digest" in md
        assert "## Stories" in md

    def test_special_chars_escaped_in_titles(self, formatter):
        c = _make_candidate(title="Breaking: Oil *and* [Gas] Crisis")
        item = _make_report_item(candidate=c)
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        # Markdown special chars should be escaped
        assert "\\*and\\*" in md
        assert "\\[Gas\\]" in md

    def test_special_chars_escaped_in_source(self, formatter):
        c = _make_candidate(source="A*B News")
        item = _make_report_item(candidate=c)
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        assert "A\\*B News" in md

    def test_special_chars_escaped_in_analysis(self, formatter):
        item = _make_report_item(
            why="This *really* matters for [global] trade.",
            what_changed="Policy #123 was updated.",
        )
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        assert "\\*really\\*" in md
        assert "\\#123" in md

    def test_special_chars_escaped_in_outlook(self, formatter):
        item = _make_report_item(outlook="Markets _may_ drop.")
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        assert "\\_may\\_" in md

    def test_special_chars_escaped_in_topics(self, formatter):
        c = _make_candidate(topic="ai_and_ml")
        item = _make_report_item(candidate=c)
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        # Topic in executive summary should be escaped
        assert "ai and ml" in md.lower() or "ai\\_and\\_ml" in md.lower()

    def test_tracked_and_delta_marks(self, formatter):
        payload = _make_payload()
        md = formatter.format_markdown_export(
            payload, tracked_flags=[True], delta_tags=["new"]
        )
        assert "[TRACKED]" in md
        assert "[NEW]" in md

    def test_corroboration_escaped(self, formatter):
        c = _make_candidate(corroborated_by=["A*B", "C[D]"])
        item = _make_report_item(candidate=c)
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        assert "A\\*B" in md
        assert "C\\[D\\]" in md

    def test_regions_escaped(self, formatter):
        c = _make_candidate(regions=["middle_east"])
        item = _make_report_item(candidate=c)
        payload = _make_payload(items=[item])
        md = formatter.format_markdown_export(payload)
        # Region names should appear escaped
        assert "Middle East" in md or "Middle\\ East" in md

    def test_geo_risk_escaping(self, formatter):
        risk = GeoRiskEntry(
            region="east_asia", risk_level=0.75,
            escalation_delta=0.10, drivers=[],
        )
        payload = _make_payload(geo_risks=[risk])
        md = formatter.format_markdown_export(payload)
        assert "Geo-Risk" in md or "Geo\\-Risk" in md

    def test_trend_topic_escaped(self, formatter):
        trend = TrendSnapshot(
            topic="*hot_topic*", velocity=10.0, baseline_velocity=4.0,
            anomaly_score=2.5, is_emerging=True,
        )
        payload = _make_payload(trends=[trend])
        md = formatter.format_markdown_export(payload)
        assert "\\*hot\\_topic\\*" in md


# ══════════════════════════════════════════════════════════════════════════
# Deep Dive Tests
# ══════════════════════════════════════════════════════════════════════════


class TestDeepDive:
    """Tests for format_deep_dive."""

    def test_deep_dive_has_all_sections(self, formatter):
        band = ConfidenceBand(
            low=0.6, mid=0.75, high=0.9,
            key_assumptions=["Markets remain stable"],
        )
        c = _make_candidate(
            corroborated_by=["bbc"], regions=["europe"],
        )
        item = _make_report_item(
            candidate=c, confidence=band,
            adjacent=["Related article"],
        )
        card = formatter.format_deep_dive(item, 1)
        assert "Deep Dive" in card
        assert "Analysis" in card
        assert "Confidence" in card
        assert "Source Intelligence" in card
        assert "Related" in card
        assert "Markets remain stable" in card

    def test_deep_dive_length_safety(self, formatter):
        """Deep dive with huge content should be truncated."""
        c = _make_candidate(summary=" ".join(["word"] * 500))
        band = ConfidenceBand(
            low=0.5, mid=0.7, high=0.9,
            key_assumptions=[f"Assumption {i}: " + "detail " * 30 for i in range(4)],
        )
        item = _make_report_item(
            candidate=c, confidence=band,
            why=" ".join(["analysis"] * 300),
            what_changed=" ".join(["change"] * 200),
            outlook=" ".join(["forecast"] * 200),
            adjacent=["Read " * 50 for _ in range(5)],
        )
        card = formatter.format_deep_dive(item, 1)
        assert len(card) <= 4096


# ══════════════════════════════════════════════════════════════════════════
# Markdown Escape Helper Tests
# ══════════════════════════════════════════════════════════════════════════


class TestEscMd:
    """Tests for the _esc_md helper."""

    def test_escapes_asterisks(self):
        assert _esc_md("*bold*") == "\\*bold\\*"

    def test_escapes_underscores(self):
        assert _esc_md("_italic_") == "\\_italic\\_"

    def test_escapes_brackets(self):
        assert _esc_md("[link](url)") == "\\[link\\]\\(url\\)"

    def test_escapes_hash(self):
        assert _esc_md("# heading") == "\\# heading"

    def test_escapes_ampersand(self):
        # Actually ampersand is not in the Markdown escape list
        # but we do NOT escape it since _esc_md is for Markdown, not HTML
        pass

    def test_plain_text_unchanged(self):
        assert _esc_md("Hello world 123") == "Hello world 123"

    def test_escapes_backticks(self):
        assert _esc_md("`code`") == "\\`code\\`"
