"""Tests for Round 11: Missing _resolve_chat_id, alert dedup, webhook alert
SSRF re-validation, admin health command, delivery metrics instrumentation.
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from newsfeed.orchestration.communication import CommunicationAgent, DeliveryMetrics
from newsfeed.models.domain import (
    CandidateItem,
    ConfidenceBand,
    StoryLifecycle,
    UrgencyLevel,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_candidate(
    cid: str = "c1",
    title: str = "Test signal",
    source: str = "reuters",
    topic: str = "geopolitics",
    minutes_ago: int = 5,
) -> CandidateItem:
    return CandidateItem(
        candidate_id=cid,
        title=title,
        source=source,
        summary=f"Summary for {title}",
        url=f"https://example.com/{cid}",
        topic=topic,
        evidence_score=0.8,
        novelty_score=0.7,
        preference_fit=0.9,
        prediction_signal=0.6,
        discovered_by="agent_1",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def _make_agent():
    """Build a CommunicationAgent with mocked dependencies."""
    engine = MagicMock()
    engine.preferences.get_or_create.return_value = MagicMock(
        webhook_url="", email="", topic_weights={}, source_weights={},
        regions_of_interest=[], muted_topics=[], tracked_stories=[],
        alert_keywords=[], watchlist_crypto=[], watchlist_stocks=[],
        confidence_min=0, urgency_min="", max_per_source=0,
        max_items=10, tone="concise", format="sections",
        briefing_cadence="morning", timezone="UTC",
        alert_georisk_threshold=0.5, alert_trend_threshold=3.0,
        presets={},
    )
    engine.preferences.snapshot.return_value = {}
    engine.analytics.get_user_summary.return_value = {"chat_id": "123"}
    engine.georisk.snapshot.return_value = {}
    engine.trends.snapshot.return_value = {}

    bot = MagicMock()
    scheduler = MagicMock()
    scheduler.get_due_users.return_value = []

    agent = CommunicationAgent(engine, bot, scheduler)
    return agent, engine, bot


# ══════════════════════════════════════════════════════════════════════
# _resolve_chat_id — previously missing, caused AttributeError
# ══════════════════════════════════════════════════════════════════════


class TestResolveChatId(unittest.TestCase):
    """Test that _resolve_chat_id correctly resolves user_id to chat_id."""

    def test_resolves_from_analytics(self):
        agent, engine, _ = _make_agent()
        engine.analytics.get_user_summary.return_value = {"chat_id": "42"}
        result = agent._resolve_chat_id("user1")
        self.assertEqual(result, "42")

    def test_falls_back_to_user_id(self):
        agent, engine, _ = _make_agent()
        engine.analytics.get_user_summary.return_value = None
        result = agent._resolve_chat_id("user1")
        self.assertEqual(result, "user1")

    def test_handles_analytics_exception(self):
        agent, engine, _ = _make_agent()
        engine.analytics.get_user_summary.side_effect = Exception("db error")
        result = agent._resolve_chat_id("user1")
        self.assertEqual(result, "user1")

    def test_handles_empty_chat_id(self):
        agent, engine, _ = _make_agent()
        engine.analytics.get_user_summary.return_value = {"chat_id": ""}
        result = agent._resolve_chat_id("user1")
        # Empty string is falsy, should fall back to user_id
        self.assertEqual(result, "user1")


# ══════════════════════════════════════════════════════════════════════
# Alert Dedup — same alert shouldn't fire every cycle
# ══════════════════════════════════════════════════════════════════════


class TestAlertDedup(unittest.TestCase):
    """Test that intelligence alerts are deduplicated within cooldown window."""

    def test_georisk_alert_not_sent_twice(self):
        """Same geo-risk alert shouldn't fire on consecutive check_intelligence_alerts calls."""
        agent, engine, bot = _make_agent()

        # Set up a geo-risk that exceeds the user's threshold
        engine.georisk.snapshot.return_value = {"middle_east": 0.8}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["Middle East"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.formatter.format_intelligence_alert.return_value = "Alert!"

        # First call should send
        sent1 = agent.check_intelligence_alerts()
        self.assertEqual(sent1, 1)

        # Second call within cooldown should be deduped
        sent2 = agent.check_intelligence_alerts()
        self.assertEqual(sent2, 0)

    def test_trend_alert_not_sent_twice(self):
        """Same trend alert shouldn't fire on consecutive calls."""
        agent, engine, bot = _make_agent()

        engine.georisk.snapshot.return_value = {}
        engine.trends.snapshot.return_value = {"ai_policy": 5.0}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": [],
                "topic_weights": {"ai_policy": 0.8},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.formatter.format_intelligence_alert.return_value = "Trend Alert!"

        sent1 = agent.check_intelligence_alerts()
        self.assertEqual(sent1, 1)

        sent2 = agent.check_intelligence_alerts()
        self.assertEqual(sent2, 0)

    def test_different_alerts_not_deduped(self):
        """Different regions/topics should not be deduped against each other."""
        agent, engine, bot = _make_agent()

        engine.georisk.snapshot.return_value = {"middle_east": 0.8}
        engine.trends.snapshot.return_value = {}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["Middle East"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.formatter.format_intelligence_alert.return_value = "Alert!"

        sent1 = agent.check_intelligence_alerts()
        self.assertEqual(sent1, 1)

        # Now change to a different region
        engine.georisk.snapshot.return_value = {"east_asia": 0.9}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["East Asia"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }

        sent2 = agent.check_intelligence_alerts()
        self.assertEqual(sent2, 1)

    def test_alert_sent_after_cooldown_expires(self):
        """Alert should be re-sent after cooldown window passes."""
        agent, engine, bot = _make_agent()
        agent._ALERT_COOLDOWN = 0.01  # 10ms for test

        engine.georisk.snapshot.return_value = {"europe": 0.7}
        engine.trends.snapshot.return_value = {}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["Europe"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.formatter.format_intelligence_alert.return_value = "Alert!"

        sent1 = agent.check_intelligence_alerts()
        self.assertEqual(sent1, 1)

        time.sleep(0.02)

        sent2 = agent.check_intelligence_alerts()
        self.assertEqual(sent2, 1)

    def test_stale_dedup_entries_evicted(self):
        """Expired dedup entries should be cleaned up to prevent memory growth."""
        agent, engine, bot = _make_agent()
        agent._ALERT_COOLDOWN = 0.01

        # Seed some old entries
        agent._sent_alerts = {
            "user1:georisk:old_region": time.monotonic() - 100,
            "user2:trend:old_topic": time.monotonic() - 100,
        }

        engine.georisk.snapshot.return_value = {}
        engine.trends.snapshot.return_value = {}
        engine.preferences.snapshot.return_value = {}

        agent.check_intelligence_alerts()

        # Old entries should have been evicted
        self.assertEqual(len(agent._sent_alerts), 0)


# ══════════════════════════════════════════════════════════════════════
# Webhook Alert SSRF Re-validation
# ══════════════════════════════════════════════════════════════════════


class TestWebhookAlertSSRF(unittest.TestCase):
    """Test that _auto_webhook_alert re-validates URL like _auto_webhook_briefing does."""

    @patch("newsfeed.orchestration.communication.CommunicationAgent._auto_webhook_alert")
    def test_webhook_alert_method_exists(self, mock_alert):
        """Verify _auto_webhook_alert is callable."""
        agent, _, _ = _make_agent()
        agent._auto_webhook_alert("user1", "georisk", {"region": "test"})

    def test_webhook_alert_skips_invalid_url(self):
        """Webhook alert should not send to private IP URLs."""
        agent, engine, _ = _make_agent()
        profile = MagicMock(webhook_url="https://10.0.0.1/hook")
        engine.preferences.get_or_create.return_value = profile

        # Should not raise, just log and skip
        agent._auto_webhook_alert("user1", "georisk", {"region": "test"})

        # Verify delivery_metrics tracked the skip (no success recorded)
        # The validate_webhook_url should block it
        self.assertEqual(agent._delivery_metrics._success.get("webhook", 0), 0)


# ══════════════════════════════════════════════════════════════════════
# Admin Health Command
# ══════════════════════════════════════════════════════════════════════


class TestAdminHealthCommand(unittest.TestCase):
    """Test the /admin health command."""

    def test_admin_health_returns_metrics(self):
        agent, engine, bot = _make_agent()

        # Simulate some delivery history
        agent._delivery_metrics.record_success("telegram")
        agent._delivery_metrics.record_success("telegram")
        agent._delivery_metrics.record_failure("webhook")

        # Mock admin check
        with patch.object(agent, '_is_admin', return_value=True):
            result = agent._handle_admin(123, "admin_user", "health")

        self.assertEqual(result["action"], "admin_health")
        # Verify bot.send_message was called with health data
        call_args = bot.send_message.call_args
        msg = call_args[0][1]
        self.assertIn("Delivery Health", msg)
        self.assertIn("telegram", msg)

    def test_admin_help_lists_health(self):
        agent, engine, bot = _make_agent()

        with patch.object(agent, '_is_admin', return_value=True):
            result = agent._handle_admin(123, "admin_user", "help")

        call_args = bot.send_message.call_args
        msg = call_args[0][1]
        self.assertIn("health", msg)


# ══════════════════════════════════════════════════════════════════════
# Delivery Metrics Instrumentation
# ══════════════════════════════════════════════════════════════════════


class TestDeliveryMetricsInstrumented(unittest.TestCase):
    """Test that delivery events are properly tracked in metrics."""

    def test_scheduled_briefing_success_tracked(self):
        agent, engine, bot = _make_agent()
        scheduler = agent._scheduler
        scheduler.get_due_users.return_value = ["user1"]

        # Mock _run_briefing to succeed
        with patch.object(agent, '_run_briefing'):
            with patch.object(agent, '_auto_email_digest'):
                agent.run_scheduled_briefings()

        self.assertEqual(agent._delivery_metrics._success["telegram"], 1)

    def test_scheduled_briefing_failure_tracked(self):
        agent, engine, bot = _make_agent()
        scheduler = agent._scheduler
        scheduler.get_due_users.return_value = ["user1"]

        # Mock _run_briefing to fail
        with patch.object(agent, '_run_briefing', side_effect=Exception("test")):
            agent.run_scheduled_briefings()

        self.assertEqual(agent._delivery_metrics._failure["telegram"], 1)

    def test_alert_success_tracked_in_metrics(self):
        agent, engine, bot = _make_agent()

        engine.georisk.snapshot.return_value = {"test_region": 0.9}
        engine.trends.snapshot.return_value = {}
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["Test Region"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.formatter.format_intelligence_alert.return_value = "Alert!"

        agent.check_intelligence_alerts()

        self.assertEqual(agent._delivery_metrics._success["telegram"], 1)


# ══════════════════════════════════════════════════════════════════════
# Entity Dashboard Cap Integration
# ══════════════════════════════════════════════════════════════════════


class TestEntityDashboardCap(unittest.TestCase):
    """Verify entity connection cap works in the entity dashboard."""

    def test_entity_connection_cap_does_not_crash(self):
        """format_entity_dashboard should handle many entities without O(n²) blowup."""
        from newsfeed.intelligence.entities import format_entity_dashboard

        # Create 200 items with diverse entities
        items = []
        for i in range(200):
            c = _make_candidate(
                cid=f"c{i}",
                title=f"Story {i} about NATO and EU policy changes",
            )
            items.append(MagicMock(candidate=c))

        # Should complete quickly without memory issues
        result = format_entity_dashboard(items)
        self.assertIn("connections", result)
        self.assertLessEqual(len(result["connections"]), 10)


# ══════════════════════════════════════════════════════════════════════
# Trend Baseline Floor
# ══════════════════════════════════════════════════════════════════════


class TestTrendBaselineFloor(unittest.TestCase):
    """Verify the baseline floor prevents extreme anomaly scores."""

    def test_heavily_decayed_baseline_capped(self):
        from newsfeed.intelligence.trends import TrendDetector

        detector = TrendDetector(baseline_decay=0.8)
        # Set baseline extremely low (simulating heavy decay)
        detector._baseline["stale"] = 0.001
        candidates = [_make_candidate(topic="stale", minutes_ago=5)]
        snapshots = detector.analyze(candidates)
        snap = [s for s in snapshots if s.topic == "stale"][0]
        # With floor of 0.1, max score = 1.0 / 0.1 = 10
        self.assertLessEqual(snap.anomaly_score, 11.0)


if __name__ == "__main__":
    unittest.main()
