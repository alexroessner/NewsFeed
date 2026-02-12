from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from newsfeed.delivery.formatter import JsonFormatter
from newsfeed.delivery.telegram import TelegramFormatter
from newsfeed.models.domain import (
    BriefingType,
    CandidateItem,
    ConfidenceBand,
    DeliveryPayload,
    GeoRiskEntry,
    NarrativeThread,
    ReportItem,
    TrendSnapshot,
    UrgencyLevel,
)


def _make_payload(
    briefing_type: BriefingType = BriefingType.MORNING_DIGEST,
    include_threads: bool = False,
    include_geo: bool = False,
    include_trends: bool = False,
) -> DeliveryPayload:
    candidate = CandidateItem(
        candidate_id="c1", title="Test Signal", source="reuters",
        summary="Summary", url="https://example.com/c1", topic="geopolitics",
        evidence_score=0.8, novelty_score=0.7, preference_fit=0.9,
        prediction_signal=0.6, discovered_by="agent",
    )
    confidence = ConfidenceBand(low=0.6, mid=0.75, high=0.9, key_assumptions=["Source trusted"])
    item = ReportItem(
        candidate=candidate, why_it_matters="Important", what_changed="New data",
        predictive_outlook="Watch closely", adjacent_reads=["Read 1"],
        confidence=confidence, contrarian_note="",
    )

    threads = []
    if include_threads:
        threads = [NarrativeThread(
            thread_id="t1", headline="Thread headline",
            candidates=[candidate], source_count=2,
            urgency=UrgencyLevel.ELEVATED,
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )]

    geo_risks = []
    if include_geo:
        geo_risks = [GeoRiskEntry(
            region="europe", risk_level=0.65, previous_level=0.5,
            escalation_delta=0.15, drivers=["Escalation signal: NATO"],
        )]

    trends = []
    if include_trends:
        trends = [TrendSnapshot(
            topic="ai_policy", velocity=0.8, baseline_velocity=0.3,
            anomaly_score=2.67, is_emerging=True,
        )]

    return DeliveryPayload(
        user_id="u1", generated_at=datetime.now(timezone.utc),
        items=[item], metadata={"selected_count": 1},
        briefing_type=briefing_type, threads=threads,
        geo_risks=geo_risks, trends=trends,
    )


class TelegramFormatterTests(unittest.TestCase):
    def test_basic_format(self) -> None:
        payload = _make_payload()
        output = TelegramFormatter().format(payload)
        self.assertIn("Morning Intelligence Digest", output)
        self.assertIn("Test Signal", output)
        self.assertIn("Why it matters", output)

    def test_breaking_alert_header(self) -> None:
        payload = _make_payload(briefing_type=BriefingType.BREAKING_ALERT)
        output = TelegramFormatter().format(payload)
        self.assertIn("BREAKING ALERT", output)

    def test_geo_risk_section(self) -> None:
        payload = _make_payload(include_geo=True)
        output = TelegramFormatter().format(payload)
        self.assertIn("GEO RISK ALERTS", output)
        self.assertIn("europe", output)

    def test_trends_section(self) -> None:
        payload = _make_payload(include_trends=True)
        output = TelegramFormatter().format(payload)
        self.assertIn("EMERGING TRENDS", output)
        self.assertIn("ai_policy", output)

    def test_threads_section(self) -> None:
        payload = _make_payload(include_threads=True)
        output = TelegramFormatter().format(payload)
        self.assertIn("NARRATIVE THREADS", output)
        self.assertIn("Thread headline", output)


class JsonFormatterTests(unittest.TestCase):
    def test_valid_json_output(self) -> None:
        payload = _make_payload(include_threads=True, include_geo=True, include_trends=True)
        output = JsonFormatter().format(payload)
        data = json.loads(output)
        self.assertEqual(data["user_id"], "u1")
        self.assertEqual(data["briefing_type"], "morning_digest")

    def test_items_structure(self) -> None:
        payload = _make_payload()
        data = json.loads(JsonFormatter().format(payload))
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["title"], "Test Signal")
        self.assertEqual(item["source"], "reuters")
        self.assertIn("confidence", item)
        self.assertEqual(item["confidence"]["label"], "moderate confidence")

    def test_threads_in_json(self) -> None:
        payload = _make_payload(include_threads=True)
        data = json.loads(JsonFormatter().format(payload))
        self.assertEqual(len(data["threads"]), 1)
        self.assertEqual(data["threads"][0]["headline"], "Thread headline")

    def test_geo_risks_in_json(self) -> None:
        payload = _make_payload(include_geo=True)
        data = json.loads(JsonFormatter().format(payload))
        self.assertEqual(len(data["geo_risks"]), 1)
        self.assertEqual(data["geo_risks"][0]["region"], "europe")
        self.assertTrue(data["geo_risks"][0]["is_escalating"])

    def test_trends_in_json(self) -> None:
        payload = _make_payload(include_trends=True)
        data = json.loads(JsonFormatter().format(payload))
        self.assertEqual(len(data["trends"]), 1)
        self.assertTrue(data["trends"][0]["is_emerging"])


if __name__ == "__main__":
    unittest.main()
