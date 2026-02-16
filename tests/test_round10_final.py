"""Tests for Round 10: Webhook diagnostics, health monitoring, SSRF re-validation,
entity extraction O(n²) cap, trend anomaly false positive fix, ArticleEnricher
coverage, and pipeline integration gaps.
"""
from __future__ import annotations

import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from newsfeed.intelligence.enrichment import (
    ArticleEnricher,
    extract_article_text,
    extractive_summary,
    fetch_article,
    _paragraph_score,
    _decode_entities,
)
from newsfeed.intelligence.entities import (
    extract_entities,
    build_entity_map,
    format_entity_dashboard,
)
from newsfeed.intelligence.trends import TrendDetector
from newsfeed.models.domain import (
    CandidateItem,
    ConfidenceBand,
    StoryLifecycle,
    TrendSnapshot,
    UrgencyLevel,
)
from newsfeed.orchestration.communication import DeliveryMetrics


# ── Helpers ──────────────────────────────────────────────────────────

def _make_candidate(
    cid: str = "c1",
    title: str = "Test signal",
    source: str = "reuters",
    topic: str = "geopolitics",
    summary: str = "",
    url: str = "",
    evidence: float = 0.8,
    novelty: float = 0.7,
    pref: float = 0.9,
    pred: float = 0.6,
    agent: str = "agent_1",
    minutes_ago: int = 5,
) -> CandidateItem:
    return CandidateItem(
        candidate_id=cid,
        title=title,
        source=source,
        summary=summary or f"Summary for {title}",
        url=url or f"https://example.com/{cid}",
        topic=topic,
        evidence_score=evidence,
        novelty_score=novelty,
        preference_fit=pref,
        prediction_signal=pred,
        discovered_by=agent,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


_SAMPLE_HTML = """
<html>
<head><title>Test Article</title></head>
<body>
<nav><a href="/">Home</a></nav>
<article>
<p>The European Central Bank announced a surprise rate cut on Thursday, citing weakening
economic indicators across the eurozone. ECB President Christine Lagarde said the decision
was aimed at stimulating growth.</p>
<p>The benchmark rate was lowered by 25 basis points to 3.75%, marking the first reduction
since 2019. Markets reacted positively, with the Euro Stoxx 50 rising 1.2%.</p>
<p>"We are seeing a moderation in inflationary pressures that allows us to act," Lagarde
told reporters at a press conference in Frankfurt.</p>
<p>Analysts at Goldman Sachs noted that the move was largely anticipated, though the timing
caught some traders off guard. Bond yields fell across the continent.</p>
<p>The decision comes amid growing concerns about a potential recession in Germany, the
bloc's largest economy, where manufacturing output has declined for three consecutive
quarters.</p>
</article>
<footer>Copyright 2025</footer>
</body>
</html>
"""

_BOILERPLATE_HTML = """
<html><body>
<p>Subscribe to our newsletter for the latest updates.</p>
<p>Cookie policy: we use cookies.</p>
<p>Follow us on social media for more news.</p>
</body></html>
"""


# ══════════════════════════════════════════════════════════════════════
# Article Text Extraction
# ══════════════════════════════════════════════════════════════════════


class TestExtractArticleText(unittest.TestCase):
    """Test HTML -> clean text extraction."""

    def test_extracts_article_paragraphs(self):
        text = extract_article_text(_SAMPLE_HTML)
        self.assertIn("European Central Bank", text)
        self.assertIn("Christine Lagarde", text)
        self.assertIn("25 basis points", text)

    def test_removes_nav_and_footer(self):
        text = extract_article_text(_SAMPLE_HTML)
        self.assertNotIn("Home", text)
        # Footer boilerplate filtered by min-length (< 40 chars)

    def test_removes_scripts_and_styles(self):
        html = '<script>alert("xss")</script><p>Safe paragraph text for extraction testing.</p>'
        text = extract_article_text(html)
        self.assertNotIn("alert", text)
        self.assertIn("Safe paragraph", text)

    def test_filters_boilerplate_paragraphs(self):
        """When real content and boilerplate coexist, boilerplate is stripped."""
        html_with_boilerplate = """<html><body>
        <p>The Federal Reserve announced a significant change in monetary policy that
        will affect global markets for years to come and impact interest rates.</p>
        <p>Click here to subscribe to our newsletter for the latest financial updates.</p>
        </body></html>"""
        text = extract_article_text(html_with_boilerplate)
        # Real content kept, boilerplate stripped
        self.assertIn("Federal Reserve", text)
        self.assertNotIn("subscribe", text.lower())

    def test_empty_html_returns_empty(self):
        self.assertEqual(extract_article_text(""), "")

    def test_fallback_to_raw_text(self):
        """HTML with no <p> tags falls back to raw text extraction."""
        html = "<div>" + "A" * 50 + "</div>"
        text = extract_article_text(html)
        self.assertIn("A" * 50, text)

    def test_decode_html_entities(self):
        result = _decode_entities("AT&amp;T &mdash; 100&nbsp;points")
        self.assertIn("AT&T", result)
        self.assertIn("100 points", result)


# ══════════════════════════════════════════════════════════════════════
# Extractive Summarization
# ══════════════════════════════════════════════════════════════════════


class TestExtractiveSummary(unittest.TestCase):
    """Test the extractive summarization fallback."""

    def test_returns_empty_for_empty_input(self):
        self.assertEqual(extractive_summary(""), "")

    def test_respects_target_length(self):
        article = extract_article_text(_SAMPLE_HTML)
        summary = extractive_summary(article, target_chars=200)
        # Allow slight overshoot for sentence-boundary trimming
        self.assertLessEqual(len(summary), 300)
        self.assertGreater(len(summary), 50)

    def test_preserves_key_content(self):
        article = extract_article_text(_SAMPLE_HTML)
        summary = extractive_summary(article, target_chars=500)
        # Should include early paragraphs (inverted pyramid)
        self.assertIn("European Central Bank", summary)

    def test_single_paragraph(self):
        text = "A single paragraph about the test subject matter."
        summary = extractive_summary(text, target_chars=100)
        self.assertEqual(summary, text)

    def test_paragraph_scoring(self):
        # First paragraph should score higher than last
        score_first = _paragraph_score("Important fact: 42% increase in Q3.", 0, 10)
        score_last = _paragraph_score("Important fact: 42% increase in Q3.", 9, 10)
        self.assertGreater(score_first, score_last)

    def test_quotes_boost_score(self):
        with_quote = _paragraph_score('"This is significant," said the expert about findings.', 2, 10)
        without_quote = _paragraph_score("This is significant said the expert about findings.", 2, 10)
        self.assertGreater(with_quote, without_quote)


# ══════════════════════════════════════════════════════════════════════
# Article Fetching
# ══════════════════════════════════════════════════════════════════════


class TestFetchArticle(unittest.TestCase):
    """Test URL fetching safety checks."""

    def test_skips_example_urls(self):
        result = fetch_article("https://example.com/story1")
        self.assertEqual(result, "")

    def test_skips_empty_url(self):
        result = fetch_article("")
        self.assertEqual(result, "")

    def test_blocks_file_scheme(self):
        result = fetch_article("file:///etc/passwd")
        self.assertEqual(result, "")

    def test_blocks_ftp_scheme(self):
        result = fetch_article("ftp://evil.com/data")
        self.assertEqual(result, "")

    def test_blocks_data_scheme(self):
        result = fetch_article("data:text/html,<h1>Hi</h1>")
        self.assertEqual(result, "")


# ══════════════════════════════════════════════════════════════════════
# ArticleEnricher (batch enrichment)
# ══════════════════════════════════════════════════════════════════════


class TestArticleEnricher(unittest.TestCase):
    """Test the ArticleEnricher batch enrichment pipeline."""

    def test_empty_candidates_returns_empty(self):
        enricher = ArticleEnricher()
        result = enricher.enrich([])
        self.assertEqual(result, [])

    def test_cache_hit_skips_fetch(self):
        enricher = ArticleEnricher()
        c = _make_candidate(url="https://news.com/story1", summary="Short")
        # Manually seed cache
        enricher._put_cached_summary("https://news.com/story1", "A much longer cached summary text here")
        result = enricher.enrich([c])
        self.assertEqual(result[0].summary, "A much longer cached summary text here")

    def test_cache_eviction_bounded(self):
        enricher = ArticleEnricher()
        enricher._SUMMARY_CACHE_MAX = 5
        for i in range(10):
            enricher._put_cached_summary(f"https://news.com/{i}", f"summary_{i}")
        # Cache should be capped at 5
        with enricher._cache_lock:
            self.assertLessEqual(len(enricher._summary_cache), 5)

    def test_cache_ttl_expiry(self):
        enricher = ArticleEnricher()
        enricher._CACHE_TTL_SECONDS = 0  # Immediate expiry
        enricher._put_cached_summary("https://news.com/old", "Old summary")
        time.sleep(0.01)
        result = enricher._get_cached_summary("https://news.com/old")
        self.assertIsNone(result)

    def test_summarize_uses_extractive_without_keys(self):
        enricher = ArticleEnricher()  # No API keys
        article_text = extract_article_text(_SAMPLE_HTML)
        summary = enricher._summarize(article_text, "Test Title", "reuters")
        self.assertGreater(len(summary), 50)
        self.assertIn("European Central Bank", summary)

    @patch("newsfeed.intelligence.enrichment.fetch_article")
    def test_enrich_replaces_short_summaries(self, mock_fetch):
        """Articles with longer extracted text should replace RSS teasers."""
        mock_fetch.return_value = _SAMPLE_HTML
        enricher = ArticleEnricher(max_workers=1)
        c = _make_candidate(
            url="https://reuters.com/ecb-rate-cut",
            summary="ECB cuts rates.",
        )
        result = enricher.enrich([c])
        # Summary should have been replaced with a longer extractive one
        self.assertGreater(len(result[0].summary), len("ECB cuts rates."))

    @patch("newsfeed.intelligence.enrichment.fetch_article")
    def test_enrich_preserves_summary_on_fetch_failure(self, mock_fetch):
        """If fetch returns empty, original summary should be preserved."""
        mock_fetch.return_value = ""
        enricher = ArticleEnricher(max_workers=1)
        c = _make_candidate(
            url="https://reuters.com/error-story",
            summary="Original summary text stays.",
        )
        result = enricher.enrich([c])
        self.assertEqual(result[0].summary, "Original summary text stays.")

    def test_enrich_skips_candidates_without_url(self):
        """Candidates without a URL should not trigger any fetch."""
        enricher = ArticleEnricher(max_workers=1)
        c = _make_candidate(url="", summary="No URL story.")
        result = enricher.enrich([c])
        # No URL → no fetch → summary unchanged
        self.assertEqual(result[0].summary, "No URL story.")


# ══════════════════════════════════════════════════════════════════════
# Entity Extraction Cap (O(n²) prevention)
# ══════════════════════════════════════════════════════════════════════


class TestEntityExtractionCap(unittest.TestCase):
    """Test that entity connection building is capped to prevent O(n²)."""

    def test_basic_entity_extraction(self):
        text = "President Biden met with Putin in Geneva."
        entities = extract_entities(text)
        self.assertIn("Biden", entities["people"])
        self.assertIn("Putin", entities["people"])

    def test_organization_detection(self):
        text = "NATO and the EU issued a joint statement."
        entities = extract_entities(text)
        self.assertIn("NATO", entities["organizations"])
        self.assertIn("EU", entities["organizations"])

    def test_country_detection(self):
        text = "Tensions between China and Taiwan escalated."
        entities = extract_entities(text)
        self.assertIn("China", entities["countries"])
        self.assertIn("Taiwan", entities["countries"])

    def test_empty_text_returns_empty_sets(self):
        entities = extract_entities("")
        self.assertEqual(len(entities["people"]), 0)
        self.assertEqual(len(entities["organizations"]), 0)
        self.assertEqual(len(entities["countries"]), 0)

    def test_entity_dashboard_caps_connections(self):
        """With many entities, connection building should be capped at 50."""
        # Create items with many unique entities to trigger the cap
        items = []
        for i in range(100):
            c = _make_candidate(
                cid=f"c{i}",
                title=f"Story {i} about Entity{i} Group and Company{i} Corp",
                summary=f"Entity{i} Group met with Company{i} Corp about important matters in Region{i} Land.",
            )
            items.append(MagicMock(candidate=c))
        dashboard = format_entity_dashboard(items)
        # The connections list should still be bounded
        self.assertLessEqual(len(dashboard["connections"]), 10)

    def test_entity_map_filters_singletons(self):
        """Entities appearing in only one story should be excluded."""
        c1 = _make_candidate(cid="c1", title="Biden speaks about NATO reform")
        c2 = _make_candidate(cid="c2", title="Biden visits NATO headquarters")
        c3 = _make_candidate(cid="c3", title="Putin comments on Ukraine")
        items = [MagicMock(candidate=c1), MagicMock(candidate=c2), MagicMock(candidate=c3)]
        entity_map = build_entity_map(items)
        # Biden and NATO appear in 2+ stories
        self.assertIn("Biden", entity_map)
        self.assertIn("NATO", entity_map)
        # Putin only in 1 story → excluded
        self.assertNotIn("Putin", entity_map)


# ══════════════════════════════════════════════════════════════════════
# Trend Anomaly False Positive Fix
# ══════════════════════════════════════════════════════════════════════


class TestTrendAnomalyFix(unittest.TestCase):
    """Test that decayed baselines don't produce 50x false positive anomaly scores."""

    def test_decayed_baseline_capped_at_reasonable_anomaly(self):
        """After many decay cycles, anomaly score should not exceed ~10x."""
        detector = TrendDetector(window_minutes=60, anomaly_threshold=2.0, baseline_decay=0.8)
        # Feed a topic many times to build baseline, then let it decay
        for _ in range(10):
            candidates = [_make_candidate(topic="stale_topic", minutes_ago=200)]
            detector.analyze(candidates)

        # Now baseline has decayed significantly. Inject a new story.
        recent = [_make_candidate(topic="stale_topic", minutes_ago=5)]
        snapshots = detector.analyze(recent)
        stale_snap = [s for s in snapshots if s.topic == "stale_topic"][0]
        # With floor of 0.1, max anomaly = velocity / 0.1 = 10
        # Previously with 0.01 floor, this could hit 50-100x
        self.assertLessEqual(stale_snap.anomaly_score, 15.0)

    def test_fresh_topic_anomaly_reasonable(self):
        """A topic seen for the first time should have reasonable anomaly score."""
        detector = TrendDetector(window_minutes=60, anomaly_threshold=2.0)
        candidates = [_make_candidate(topic="new_topic", minutes_ago=5)]
        snapshots = detector.analyze(candidates)
        snap = [s for s in snapshots if s.topic == "new_topic"][0]
        # velocity=1.0, baseline=0.3 → anomaly = 1.0/0.3 ≈ 3.3
        self.assertLess(snap.anomaly_score, 5.0)

    def test_zero_velocity_no_false_positive(self):
        """A topic with no recent activity should never flag as emerging."""
        detector = TrendDetector(window_minutes=60, anomaly_threshold=2.0)
        candidates = [_make_candidate(topic="old_news", minutes_ago=120)]
        snapshots = detector.analyze(candidates)
        snap = [s for s in snapshots if s.topic == "old_news"][0]
        self.assertFalse(snap.is_emerging)
        self.assertLessEqual(snap.anomaly_score, 1.0)

    def test_baseline_floor_prevents_division_spike(self):
        """Directly test that baseline floor is 0.1 not 0.01."""
        detector = TrendDetector(baseline_decay=0.8)
        # Force baseline to near-zero by repeated decay with no activity
        detector._baseline["dead_topic"] = 0.005  # Below old floor of 0.01
        candidates = [_make_candidate(topic="dead_topic", minutes_ago=5)]
        snapshots = detector.analyze(candidates)
        snap = [s for s in snapshots if s.topic == "dead_topic"][0]
        # velocity / max(0.005, 0.1) = velocity / 0.1 = 10 max
        self.assertLessEqual(snap.anomaly_score, 12.0)

    def test_topic_eviction_over_max(self):
        """Topics exceeding _MAX_TOPICS should be evicted."""
        detector = TrendDetector()
        detector._MAX_TOPICS = 10
        for i in range(20):
            c = _make_candidate(topic=f"topic_{i}", minutes_ago=5)
            detector.analyze([c])
        self.assertLessEqual(len(detector._baseline), 10)


# ══════════════════════════════════════════════════════════════════════
# Delivery Metrics (Health Monitoring)
# ══════════════════════════════════════════════════════════════════════


class TestDeliveryMetrics(unittest.TestCase):
    """Test the DeliveryMetrics health tracking class."""

    def test_initial_success_rate_is_one(self):
        metrics = DeliveryMetrics()
        self.assertEqual(metrics.success_rate("telegram"), 1.0)

    def test_success_rate_after_mixed_results(self):
        metrics = DeliveryMetrics()
        metrics.record_success("webhook")
        metrics.record_success("webhook")
        metrics.record_failure("webhook")
        rate = metrics.success_rate("webhook")
        self.assertAlmostEqual(rate, 2 / 3, places=2)

    def test_all_failures(self):
        metrics = DeliveryMetrics()
        metrics.record_failure("email")
        metrics.record_failure("email")
        self.assertEqual(metrics.success_rate("email"), 0.0)

    def test_summary_contains_all_channels(self):
        metrics = DeliveryMetrics()
        metrics.record_success("telegram")
        metrics.record_failure("webhook")
        summary = metrics.summary()
        self.assertIn("telegram", summary)
        self.assertIn("webhook", summary)
        self.assertIn("email", summary)
        self.assertEqual(summary["telegram"]["success"], 1)
        self.assertEqual(summary["webhook"]["failure"], 1)
        self.assertEqual(summary["email"]["total"], 0)

    def test_summary_rate_field(self):
        metrics = DeliveryMetrics()
        for _ in range(8):
            metrics.record_success("telegram")
        for _ in range(2):
            metrics.record_failure("telegram")
        summary = metrics.summary()
        self.assertAlmostEqual(summary["telegram"]["rate"], 0.8, places=2)


# ══════════════════════════════════════════════════════════════════════
# Webhook Diagnostics (send_webhook_with_detail)
# ══════════════════════════════════════════════════════════════════════


class TestWebhookDiagnostics(unittest.TestCase):
    """Test that send_webhook_with_detail returns specific error reasons."""

    def test_returns_success_tuple(self):
        from newsfeed.delivery.webhook import send_webhook_with_detail
        # Can't test real HTTP easily, but verify the function signature
        # exists and returns a tuple
        # Use a URL that will fail (localhost blocked)
        success, detail = send_webhook_with_detail(
            "https://192.0.2.1/webhook", {"test": True}, timeout=0.5
        )
        self.assertIsInstance(success, bool)
        self.assertIsInstance(detail, str)

    def test_send_webhook_calls_detail_version(self):
        """send_webhook should delegate to send_webhook_with_detail."""
        from newsfeed.delivery.webhook import send_webhook
        with patch("newsfeed.delivery.webhook.send_webhook_with_detail") as mock:
            mock.return_value = (True, "")
            result = send_webhook("https://hooks.example.com/x", {"test": True})
            self.assertTrue(result)
            mock.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
# SSRF Re-validation
# ══════════════════════════════════════════════════════════════════════


class TestSSRFRevalidation(unittest.TestCase):
    """Test URL validation for SSRF prevention."""

    def test_blocks_private_ip(self):
        from newsfeed.delivery.webhook import validate_webhook_url
        valid, err = validate_webhook_url("https://10.0.0.1/webhook")
        self.assertFalse(valid)
        self.assertIn("Private", err)

    def test_blocks_loopback(self):
        from newsfeed.delivery.webhook import validate_webhook_url
        valid, err = validate_webhook_url("https://127.0.0.1/webhook")
        self.assertFalse(valid)
        self.assertIn("Loopback", err)

    def test_blocks_http(self):
        from newsfeed.delivery.webhook import validate_webhook_url
        valid, err = validate_webhook_url("http://hooks.slack.com/xyz")
        self.assertFalse(valid)
        self.assertIn("HTTPS", err)

    def test_blocks_localhost(self):
        from newsfeed.delivery.webhook import validate_webhook_url
        valid, err = validate_webhook_url("https://localhost/admin")
        self.assertFalse(valid)

    def test_blocks_metadata_ip(self):
        from newsfeed.delivery.webhook import validate_webhook_url
        valid, err = validate_webhook_url("https://169.254.169.254/latest/meta-data/")
        self.assertFalse(valid)


# ══════════════════════════════════════════════════════════════════════
# Pipeline Integration Tests
# ══════════════════════════════════════════════════════════════════════


class TestPipelineIntegration(unittest.TestCase):
    """Integration tests for the full engine pipeline."""

    @classmethod
    def setUpClass(cls):
        from newsfeed.models.config import load_runtime_config
        from newsfeed.orchestration.engine import NewsFeedEngine
        root = Path(__file__).resolve().parents[1]
        cls._root = root
        cls._cfg = load_runtime_config(root / "config")

    def _make_engine(self):
        from newsfeed.orchestration.engine import NewsFeedEngine
        return NewsFeedEngine(
            self._cfg.agents, self._cfg.pipeline,
            self._cfg.personas, self._root / "personas",
        )

    def test_pipeline_with_empty_topics(self):
        """Pipeline should handle empty topic weights gracefully."""
        engine = self._make_engine()
        output = engine.handle_request(
            user_id="u-empty",
            prompt="briefing",
            weighted_topics={},
        )
        # Should produce output even with no topic weights
        self.assertIsInstance(output, str)
        self.assertGreater(len(output), 0)

    def test_pipeline_with_single_topic(self):
        """Pipeline should work with a single topic."""
        engine = self._make_engine()
        output = engine.handle_request(
            user_id="u-single",
            prompt="tech update",
            weighted_topics={"technology": 1.0},
        )
        self.assertIn("<b>", output)

    def test_pipeline_respects_max_items(self):
        """max_items should limit the number of stories in output."""
        engine = self._make_engine()
        payload = engine.handle_request_payload(
            user_id="u-limited",
            prompt="geopolitics briefing",
            weighted_topics={"geopolitics": 1.0},
            max_items=3,
        )
        self.assertLessEqual(len(payload.items), 3)

    def test_pipeline_show_more_returns_reserve(self):
        """show_more should return reserve candidates not in the initial set."""
        engine = self._make_engine()
        engine.handle_request(
            user_id="u-more",
            prompt="geopolitics",
            weighted_topics={"geopolitics": 0.9},
        )
        more = engine.show_more("u-more", "geopolitics", already_seen_ids=set(), limit=5)
        self.assertIsInstance(more, list)
        self.assertLessEqual(len(more), 5)

    def test_pipeline_feedback_updates_profile(self):
        """Feedback should update user profile topics and preferences."""
        engine = self._make_engine()
        updates = engine.apply_user_feedback("u-fb", "more technology less crypto")
        self.assertIn("topic:technology", updates)

    def test_pipeline_payload_has_metadata(self):
        """DeliveryPayload should include briefing_type and generated_at."""
        engine = self._make_engine()
        payload = engine.handle_request_payload(
            user_id="u-meta2",
            prompt="morning update",
            weighted_topics={"geopolitics": 0.8, "technology": 0.6},
        )
        self.assertIsNotNone(payload.briefing_type)
        self.assertIsNotNone(payload.generated_at)
        self.assertIsInstance(payload.items, list)

    def test_pipeline_different_users_independent(self):
        """Different users should have independent profiles."""
        engine = self._make_engine()
        engine.apply_user_feedback("u-alice", "tone: analyst")
        engine.apply_user_feedback("u-bob", "tone: brief")
        alice = engine.preferences.get_or_create("u-alice")
        bob = engine.preferences.get_or_create("u-bob")
        self.assertEqual(alice.tone, "analyst")
        self.assertEqual(bob.tone, "brief")


if __name__ == "__main__":
    unittest.main()
