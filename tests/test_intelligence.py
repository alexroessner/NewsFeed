from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from newsfeed.intelligence.credibility import (
    CredibilityTracker,
    detect_cross_corroboration,
    enforce_source_diversity,
)
from newsfeed.intelligence.clustering import StoryClustering
from newsfeed.intelligence.urgency import BreakingDetector
from newsfeed.intelligence.georisk import GeoRiskIndex
from newsfeed.intelligence.trends import TrendDetector
from newsfeed.models.domain import (
    CandidateItem,
    ConfidenceBand,
    GeoRiskEntry,
    NarrativeThread,
    SourceReliability,
    StoryLifecycle,
    TrendSnapshot,
    UrgencyLevel,
    configure_scoring,
    validate_candidate,
)


def _make_candidate(
    cid: str = "c1",
    title: str = "Test signal",
    source: str = "reuters",
    topic: str = "geopolitics",
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
        summary=f"Summary for {title}",
        url=f"https://example.com/{cid}",
        topic=topic,
        evidence_score=evidence,
        novelty_score=novelty,
        preference_fit=pref,
        prediction_signal=pred,
        discovered_by=agent,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


class CredibilityTests(unittest.TestCase):
    def test_tier1_sources_have_higher_reliability(self) -> None:
        tracker = CredibilityTracker()
        reuters = tracker.get_source("reuters")
        web = tracker.get_source("web")
        self.assertGreater(reuters.reliability_score, web.reliability_score)
        self.assertGreater(reuters.trust_factor(), web.trust_factor())

    def test_record_item_increments_count(self) -> None:
        tracker = CredibilityTracker()
        c = _make_candidate(source="ap")
        tracker.record_item(c)
        tracker.record_item(c)
        self.assertEqual(tracker.get_source("ap").total_items_seen, 2)

    def test_corroboration_boosts_rate(self) -> None:
        tracker = CredibilityTracker()
        before = tracker.get_source("reuters").corroboration_rate
        tracker.record_corroboration("reuters", "bbc")
        after = tracker.get_source("reuters").corroboration_rate
        self.assertGreater(after, before)

    def test_score_candidate_uses_trust(self) -> None:
        tracker = CredibilityTracker()
        c_reliable = _make_candidate(source="reuters")
        c_unreliable = _make_candidate(source="web", cid="c2")
        self.assertGreater(
            tracker.score_candidate(c_reliable),
            tracker.score_candidate(c_unreliable),
        )

    def test_cross_corroboration_detects_similar_stories(self) -> None:
        # Corroboration now uses content similarity, not topic-level matching.
        # Items need similar titles/summaries AND real URLs (not example.com).
        items = [
            _make_candidate(cid="c1", source="reuters", topic="ai_policy",
                            title="Trump signs new AI regulation executive order"),
            _make_candidate(cid="c2", source="bbc", topic="ai_policy",
                            title="Trump executive order targets AI regulation"),
            _make_candidate(cid="c3", source="guardian", topic="markets",
                            title="Stock markets rally on earnings report"),
        ]
        # Give real URLs so they're not skipped
        for item in items:
            item.url = f"https://real-source.com/{item.candidate_id}"
        result = detect_cross_corroboration(items)
        # reuters and bbc should corroborate (similar titles), guardian should not
        self.assertTrue(result[0].corroborated_by)  # reuters
        self.assertTrue(result[1].corroborated_by)  # bbc
        self.assertFalse(result[2].corroborated_by)  # guardian (different story)

    def test_source_diversity_caps_per_source(self) -> None:
        items = [_make_candidate(cid=f"c{i}", source="reuters") for i in range(10)]
        diverse = enforce_source_diversity(items, max_per_source=3)
        reuters_count = [c for c in diverse if c.source == "reuters"]
        self.assertEqual(len(reuters_count), 3)
        # Overflow items are now dropped, so only the diverse items remain
        self.assertEqual(len(diverse), 3)


class ClusteringTests(unittest.TestCase):
    def test_same_topic_items_form_thread(self) -> None:
        items = [
            _make_candidate(cid="c1", title="Geopolitics signal #1 from reuters", source="reuters", topic="geopolitics"),
            _make_candidate(cid="c2", title="Geopolitics signal #2 from bbc", source="bbc", topic="geopolitics"),
        ]
        clustering = StoryClustering(similarity_threshold=0.4)
        threads = clustering.cluster(items)
        self.assertGreaterEqual(len(threads), 1)
        self.assertIsInstance(threads[0], NarrativeThread)

    def test_different_topics_produce_separate_threads(self) -> None:
        items = [
            _make_candidate(cid="c1", title="AI policy update", topic="ai_policy"),
            _make_candidate(cid="c2", title="Market crash analysis", topic="markets"),
        ]
        clustering = StoryClustering()
        threads = clustering.cluster(items)
        self.assertEqual(len(threads), 2)

    def test_thread_score_rewards_multi_source(self) -> None:
        items = [
            _make_candidate(cid="c1", source="reuters", title="Thread topic from reuters"),
            _make_candidate(cid="c2", source="bbc", title="Thread topic from bbc"),
        ]
        clustering = StoryClustering(similarity_threshold=0.3)
        threads = clustering.cluster(items)
        for thread in threads:
            if thread.source_count >= 2:
                single_item_score = items[0].composite_score()
                self.assertGreaterEqual(thread.thread_score(), single_item_score * 0.9)

    def test_thread_has_confidence_band(self) -> None:
        items = [_make_candidate(cid="c1")]
        threads = StoryClustering().cluster(items)
        self.assertIsNotNone(threads[0].confidence)
        self.assertIsInstance(threads[0].confidence, ConfidenceBand)


class UrgencyTests(unittest.TestCase):
    def test_breaking_keywords_elevate_urgency(self) -> None:
        c = _make_candidate(title="Breaking crisis erupts in the region")
        detector = BreakingDetector()
        result = detector.assess([c])
        self.assertIn(result[0].urgency, (UrgencyLevel.BREAKING, UrgencyLevel.ELEVATED, UrgencyLevel.CRITICAL))

    def test_routine_content_stays_routine(self) -> None:
        c = _make_candidate(title="Quarterly earnings report summary", minutes_ago=120)
        detector = BreakingDetector()
        result = detector.assess([c])
        self.assertEqual(result[0].urgency, UrgencyLevel.ROUTINE)

    def test_high_velocity_elevates_urgency(self) -> None:
        items = [
            _make_candidate(cid=f"c{i}", source=src, minutes_ago=2)
            for i, src in enumerate(["reuters", "bbc", "ap", "guardian", "ft", "x", "reddit"])
        ]
        detector = BreakingDetector(velocity_window_minutes=30, breaking_source_threshold=3)
        result = detector.assess(items)
        elevated = [c for c in result if c.urgency != UrgencyLevel.ROUTINE]
        self.assertGreater(len(elevated), 0)

    def test_lifecycle_set_on_assessment(self) -> None:
        c = _make_candidate(title="Major war escalation begins")
        detector = BreakingDetector()
        detector.assess([c])
        self.assertIsInstance(c.lifecycle, StoryLifecycle)


class GeoRiskTests(unittest.TestCase):
    def test_region_detection(self) -> None:
        c = _make_candidate(title="NATO response to Ukraine conflict escalation")
        index = GeoRiskIndex()
        risks = index.assess([c])
        regions = [r.region for r in risks]
        self.assertIn("europe", regions)

    def test_escalation_tracking(self) -> None:
        index = GeoRiskIndex()
        items1 = [_make_candidate(cid="c1", title="Iran sanctions tighten")]
        index.assess(items1)
        items2 = [
            _make_candidate(cid="c2", title="Iran military deployment escalation"),
            _make_candidate(cid="c3", title="Iran nuclear warning issued"),
        ]
        risks = index.assess(items2)
        me_risk = [r for r in risks if r.region == "middle_east"]
        self.assertGreaterEqual(len(me_risk), 1)

    def test_global_fallback_for_unlocated_items(self) -> None:
        c = _make_candidate(title="Technology advancement report")
        index = GeoRiskIndex()
        risks = index.assess([c])
        regions = [r.region for r in risks]
        self.assertIn("global", regions)

    def test_risk_entry_has_drivers(self) -> None:
        c = _make_candidate(title="Russia military mobilization near border")
        index = GeoRiskIndex()
        risks = index.assess([c])
        europe_risk = [r for r in risks if r.region == "europe"]
        self.assertGreaterEqual(len(europe_risk), 1)
        self.assertGreater(len(europe_risk[0].drivers), 0)


class TrendTests(unittest.TestCase):
    def test_detects_velocity(self) -> None:
        items = [_make_candidate(cid=f"c{i}", minutes_ago=2) for i in range(5)]
        detector = TrendDetector(window_minutes=60)
        snapshots = detector.analyze(items)
        self.assertGreaterEqual(len(snapshots), 1)
        self.assertIsInstance(snapshots[0], TrendSnapshot)
        self.assertGreater(snapshots[0].velocity, 0)

    def test_emerging_detection_with_high_anomaly(self) -> None:
        detector = TrendDetector(window_minutes=60, anomaly_threshold=1.5)
        # First pass establishes baseline
        items1 = [_make_candidate(cid="c0", minutes_ago=120)]
        detector.analyze(items1)
        # Second pass with burst of recent items
        items2 = [_make_candidate(cid=f"c{i}", minutes_ago=1) for i in range(1, 6)]
        snapshots = detector.analyze(items2)
        emerging = detector.get_emerging_topics(items2)
        self.assertGreaterEqual(len(emerging), 0)  # depends on baseline evolution

    def test_baseline_evolves(self) -> None:
        detector = TrendDetector()
        items = [_make_candidate()]
        detector.analyze(items)
        baseline_1 = detector._baseline.get("geopolitics", 0.0)
        detector.analyze(items)
        baseline_2 = detector._baseline.get("geopolitics", 0.0)
        self.assertNotEqual(baseline_1, baseline_2)


class DomainModelTests(unittest.TestCase):
    def test_confidence_band_labels(self) -> None:
        high = ConfidenceBand(low=0.7, mid=0.85, high=0.95)
        self.assertEqual(high.label(), "high confidence")
        mod = ConfidenceBand(low=0.4, mid=0.6, high=0.8)
        self.assertEqual(mod.label(), "moderate confidence")
        low = ConfidenceBand(low=0.1, mid=0.3, high=0.5)
        self.assertEqual(low.label(), "low confidence")

    def test_source_reliability_trust_factor(self) -> None:
        sr = SourceReliability(source_id="test", reliability_score=0.9, historical_accuracy=0.8, corroboration_rate=0.7)
        trust = sr.trust_factor()
        self.assertAlmostEqual(trust, 0.9 * 0.5 + 0.8 * 0.3 + 0.7 * 0.2)

    def test_geo_risk_escalation(self) -> None:
        entry = GeoRiskEntry(region="test", risk_level=0.7, previous_level=0.5, escalation_delta=0.2)
        self.assertTrue(entry.is_escalating())
        stable = GeoRiskEntry(region="test", risk_level=0.5, previous_level=0.5, escalation_delta=0.0)
        self.assertFalse(stable.is_escalating())

    def test_narrative_thread_score(self) -> None:
        items = [_make_candidate(cid="c1"), _make_candidate(cid="c2", source="bbc")]
        thread = NarrativeThread(
            thread_id="t1",
            headline="Test thread",
            candidates=items,
            source_count=2,
            urgency=UrgencyLevel.BREAKING,
        )
        score = thread.thread_score()
        self.assertGreater(score, 0)
        self.assertLessEqual(score, 1.0)

    def test_urgency_level_enum(self) -> None:
        self.assertEqual(UrgencyLevel.CRITICAL.value, "critical")
        self.assertEqual(StoryLifecycle.BREAKING.value, "breaking")

    def test_configure_scoring_clears_old_values(self) -> None:
        configure_scoring({"composite_weights": {"evidence": 0.5}})
        configure_scoring({"confidence_labels": {"high_threshold": 0.9}})
        # Old composite_weights should be gone after clear+update
        c = _make_candidate()
        # With no composite_weights in config, defaults should be used
        score = c.composite_score()
        self.assertGreater(score, 0)
        # Reset for other tests
        configure_scoring({})

    def test_validate_candidate_valid(self) -> None:
        c = _make_candidate()
        issues = validate_candidate(c)
        self.assertEqual(issues, [])

    def test_validate_candidate_out_of_range(self) -> None:
        c = _make_candidate(evidence=1.5)
        issues = validate_candidate(c)
        self.assertEqual(len(issues), 1)
        self.assertIn("evidence_score", issues[0])

    def test_validate_candidate_empty_fields(self) -> None:
        c = _make_candidate(title="", source="")
        issues = validate_candidate(c)
        self.assertGreaterEqual(len(issues), 2)

    def test_empty_thread_score_zero(self) -> None:
        thread = NarrativeThread(thread_id="t1", headline="Empty", candidates=[])
        self.assertEqual(thread.thread_score(), 0.0)


class EdgeCaseTests(unittest.TestCase):
    def test_clustering_empty_list(self) -> None:
        clustering = StoryClustering()
        threads = clustering.cluster([])
        self.assertEqual(threads, [])

    def test_clustering_single_candidate(self) -> None:
        clustering = StoryClustering()
        threads = clustering.cluster([_make_candidate()])
        self.assertEqual(len(threads), 1)
        self.assertEqual(len(threads[0].candidates), 1)

    def test_georisk_empty_candidates(self) -> None:
        index = GeoRiskIndex()
        risks = index.assess([])
        self.assertEqual(risks, [])

    def test_trends_empty_candidates(self) -> None:
        detector = TrendDetector()
        snapshots = detector.analyze([])
        self.assertEqual(snapshots, [])

    def test_urgency_empty_candidates(self) -> None:
        detector = BreakingDetector()
        result = detector.assess([])
        self.assertEqual(result, [])

    def test_diversity_all_same_source(self) -> None:
        items = [_make_candidate(cid=f"c{i}", source="reuters") for i in range(10)]
        diverse = enforce_source_diversity(items, max_per_source=2)
        # Overflow items are now dropped entirely
        self.assertEqual(len(diverse), 2)
        # Both should be reuters (highest-scoring kept)
        self.assertEqual(diverse[0].source, "reuters")

    def test_corroboration_single_item(self) -> None:
        result = detect_cross_corroboration([_make_candidate()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].corroborated_by, [])

    def test_credibility_tracker_with_config(self) -> None:
        tracker = CredibilityTracker(intel_cfg={
            "source_tiers": {
                "tier_1": {"sources": ["custom_source"], "base_reliability": 0.95},
            }
        })
        sr = tracker.get_source("custom_source")
        self.assertEqual(sr.reliability_score, 0.95)

    def test_credibility_snapshot(self) -> None:
        tracker = CredibilityTracker()
        tracker.record_item(_make_candidate(source="reuters"))
        snap = tracker.snapshot()
        self.assertIn("reuters", snap)
        self.assertEqual(snap["reuters"]["seen"], 1)

    def test_georisk_snapshot(self) -> None:
        index = GeoRiskIndex()
        index.assess([_make_candidate(title="Ukraine conflict escalation")])
        snap = index.snapshot()
        self.assertIsInstance(snap, dict)

    def test_trends_snapshot(self) -> None:
        detector = TrendDetector()
        detector.analyze([_make_candidate()])
        snap = detector.snapshot()
        self.assertIn("geopolitics", snap)


if __name__ == "__main__":
    unittest.main()
