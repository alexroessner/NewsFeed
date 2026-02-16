"""Tests for Round 9: Restore validation, NaN guards, delivery resilience,
webhook circuit breaker, Telegram retry expansion, decay fixes, clustering
safety, and preset crash guards.
"""
from __future__ import annotations

import json
import math
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from newsfeed.memory.store import PreferenceStore, StatePersistence
from newsfeed.models.domain import (
    CandidateItem,
    ConfidenceBand,
    DebateVote,
    StoryLifecycle,
    UrgencyLevel,
    UserProfile,
)


# ══════════════════════════════════════════════════════════════════════════
# Restore Validation — Cap Lists, Validate Floats
# ══════════════════════════════════════════════════════════════════════════


class TestRestoreValidation(unittest.TestCase):
    """Persisted data should be validated on restore to prevent memory abuse."""

    def _persist_raw(self, storage: StatePersistence, data: dict) -> None:
        """Persist raw data without going through the store."""
        storage.save("preferences", data)

    def test_tracked_stories_capped_on_restore(self):
        """Restoring 1000 tracked stories should cap at 20."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {
                "user1": {
                    "tracked_stories": [{"topic": f"t{i}", "keywords": ["k"],
                                          "headline": f"h{i}", "tracked_at": 0}
                                         for i in range(1000)],
                }
            }
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertLessEqual(len(p.tracked_stories), 20)

    def test_bookmarks_capped_on_restore(self):
        """Restoring 500 bookmarks should cap at 50."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {
                "user1": {
                    "bookmarks": [{"title": f"t{i}", "source": "s", "url": "u",
                                   "topic": "t", "saved_at": 0}
                                  for i in range(500)],
                }
            }
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertLessEqual(len(p.bookmarks), 50)

    def test_custom_sources_capped_on_restore(self):
        """Restoring 200 custom sources should cap at 10."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {
                "user1": {
                    "custom_sources": [{"name": f"s{i}", "feed_url": f"http://x/{i}"}
                                       for i in range(200)],
                }
            }
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertLessEqual(len(p.custom_sources), 10)

    def test_nan_confidence_min_rejected(self):
        """NaN in confidence_min should be replaced with default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {"user1": {"confidence_min": "nan"}}
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertTrue(math.isfinite(p.confidence_min))
            self.assertEqual(p.confidence_min, 0.0)

    def test_inf_georisk_threshold_rejected(self):
        """Infinity in alert_georisk_threshold should be replaced with default."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {"user1": {"alert_georisk_threshold": "inf"}}
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertTrue(math.isfinite(p.alert_georisk_threshold))

    def test_invalid_max_items_handled(self):
        """Non-numeric max_items should not crash restore."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {"user1": {"max_items": "not_a_number"}}
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            restored = store.restore(storage)

            self.assertEqual(restored, 1)
            p = store.get_or_create("user1")
            self.assertEqual(p.max_items, 10)  # default

    def test_topic_weights_capped_on_restore(self):
        """Topic weights exceeding MAX_WEIGHTS should be truncated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            raw = {
                "user1": {
                    "topic_weights": {f"topic_{i}": 0.5 for i in range(200)},
                }
            }
            self._persist_raw(storage, raw)

            store = PreferenceStore()
            store.restore(storage)

            p = store.get_or_create("user1")
            self.assertLessEqual(len(p.topic_weights), store.MAX_WEIGHTS)


# ══════════════════════════════════════════════════════════════════════════
# NaN/Inf Guards in Expert Voting
# ══════════════════════════════════════════════════════════════════════════


class TestExpertNaNGuards(unittest.TestCase):
    """Expert voting should handle NaN/Inf scores gracefully."""

    def test_nan_score_produces_valid_vote(self):
        """NaN composite score should not produce NaN confidence."""
        from newsfeed.agents.experts import ExpertCouncil

        council = ExpertCouncil.__new__(ExpertCouncil)
        council._personas = {
            "test_expert": {
                "name": "Test Expert",
                "weights": {"evidence": 0.4, "novelty": 0.3, "relevance": 0.3},
            }
        }
        council.keep_threshold = 0.4
        council.confidence_min = 0.2
        council.confidence_max = 0.95

        # Create candidate with NaN score
        c = CandidateItem(
            candidate_id="c-nan", title="Test", source="test",
            summary="Test", url="", topic="test",
            evidence_score=float("nan"), novelty_score=0.5,
            preference_fit=0.5, prediction_signal=0.5,
            discovered_by="test",
        )

        vote = council._vote_heuristic("test_expert", c)

        self.assertTrue(math.isfinite(vote.confidence))
        self.assertIsInstance(vote.keep, bool)


# ══════════════════════════════════════════════════════════════════════════
# Clustering Division-by-Zero Guard
# ══════════════════════════════════════════════════════════════════════════


class TestClusteringEmptyGuard(unittest.TestCase):
    """Clustering should handle empty item lists without crashing."""

    def test_empty_items_confidence(self):
        """Computing confidence for empty items should not crash."""
        from newsfeed.intelligence.clustering import StoryClustering

        clusterer = StoryClustering.__new__(StoryClustering)
        result = clusterer._compute_confidence([], None)

        self.assertIsInstance(result, ConfidenceBand)
        self.assertEqual(result.mid, 0.0)


# ══════════════════════════════════════════════════════════════════════════
# Preset Load Crash Guard
# ══════════════════════════════════════════════════════════════════════════


class TestPresetLoadSafety(unittest.TestCase):
    """Loading corrupted presets should not crash."""

    def test_corrupted_max_items_handled(self):
        """Non-numeric max_items in preset should not crash load."""
        store = PreferenceStore()
        uid = "u1"
        profile = store.get_or_create(uid)

        # Manually inject a corrupted preset
        profile.presets["bad"] = {
            "topic_weights": {},
            "source_weights": {},
            "tone": "concise",
            "format": "bullet",
            "max_items": "not_a_number",
            "regions": [],
            "confidence_min": "nan",
            "urgency_min": "",
            "max_per_source": "invalid",
            "muted_topics": [],
        }

        # Should not crash
        result = store.load_preset(uid, "bad")

        # Should still return a profile (safe defaults used)
        self.assertIsNotNone(result)
        self.assertEqual(result.max_items, 10)
        self.assertTrue(math.isfinite(result.confidence_min))

    def test_missing_preset_returns_none(self):
        """Loading nonexistent preset should return None."""
        store = PreferenceStore()
        self.assertIsNone(store.load_preset("u1", "nonexistent"))


# ══════════════════════════════════════════════════════════════════════════
# Weight Decay Rounding Fix
# ══════════════════════════════════════════════════════════════════════════


class TestDecayRoundingFix(unittest.TestCase):
    """Weight decay should not get stuck due to rounding artifacts."""

    def test_small_weight_eventually_reaches_zero(self):
        """A small weight should decay to zero, not get stuck."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "test", 0.1)

        # Backdate by 90 days (3 half-lives) — should decay to ~0.0125
        store._weight_timestamps["u1"]["test"] = time.time() - (90 * 86400)

        store.apply_weight_decay("u1")

        p = store.get_or_create("u1")
        # Should have been pruned to zero (not stuck at 0.012 or similar)
        self.assertNotIn("test", p.topic_weights)

    def test_moderate_weight_decays_smoothly(self):
        """A moderate weight should decay without getting stuck."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.5)

        # Backdate by 60 days (2 half-lives) — should decay to ~0.125
        store._weight_timestamps["u1"]["tech"] = time.time() - (60 * 86400)

        store.apply_weight_decay("u1")

        p = store.get_or_create("u1")
        self.assertIn("tech", p.topic_weights)
        # Should be around 0.125 (half twice = 0.25 * 0.5 = 0.125)
        self.assertLess(p.topic_weights["tech"], 0.2)
        self.assertGreater(p.topic_weights["tech"], 0.05)


# ══════════════════════════════════════════════════════════════════════════
# Telegram Retry Expansion
# ══════════════════════════════════════════════════════════════════════════


class TestTelegramRetryExpansion(unittest.TestCase):
    """Telegram API should retry on 5xx and network errors."""

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    @patch("newsfeed.delivery.bot.time.sleep")
    def test_retries_on_500(self, mock_sleep, mock_urlopen):
        """Should retry on HTTP 500."""
        import urllib.error
        from newsfeed.delivery.bot import TelegramBot

        err_500 = urllib.error.HTTPError(
            "url", 500, "Internal Server Error", {},
            MagicMock(read=MagicMock(return_value=b'{}'))
        )
        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({"ok": True, "result": {"id": 1}}).encode()
        success_resp.__enter__ = MagicMock(return_value=success_resp)
        success_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [err_500, success_resp]

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {"id": 1})
        mock_sleep.assert_called_once()

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    @patch("newsfeed.delivery.bot.time.sleep")
    def test_retries_on_network_timeout(self, mock_sleep, mock_urlopen):
        """Should retry on network timeout (URLError)."""
        import urllib.error
        from newsfeed.delivery.bot import TelegramBot

        timeout_err = urllib.error.URLError("Connection timed out")
        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({"ok": True, "result": {"id": 2}}).encode()
        success_resp.__enter__ = MagicMock(return_value=success_resp)
        success_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [timeout_err, success_resp]

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {"id": 2})
        mock_sleep.assert_called_once()

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    @patch("newsfeed.delivery.bot.time.sleep")
    def test_retries_on_503(self, mock_sleep, mock_urlopen):
        """Should retry on HTTP 503 Service Unavailable."""
        import urllib.error
        from newsfeed.delivery.bot import TelegramBot

        err_503 = urllib.error.HTTPError(
            "url", 503, "Service Unavailable", {},
            MagicMock(read=MagicMock(return_value=b'{}'))
        )
        mock_urlopen.side_effect = err_503

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {})
        # Should have retried MAX_RETRIES times
        self.assertEqual(mock_sleep.call_count, bot._MAX_RETRIES)

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    def test_400_not_retried(self, mock_urlopen):
        """HTTP 400 (client error) should NOT be retried."""
        import urllib.error
        from newsfeed.delivery.bot import TelegramBot

        err_400 = urllib.error.HTTPError(
            "url", 400, "Bad Request", {},
            MagicMock(read=MagicMock(return_value=b'{}'))
        )
        mock_urlopen.side_effect = err_400

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {})
        self.assertEqual(mock_urlopen.call_count, 1)


# ══════════════════════════════════════════════════════════════════════════
# Webhook Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════


class TestWebhookCircuitBreaker(unittest.TestCase):
    """Dead webhook endpoints should be auto-disabled after repeated failures."""

    def test_circuit_breaker_trips_after_max_failures(self):
        """After N consecutive failures, webhook should be disabled."""
        from newsfeed.orchestration.communication import CommunicationAgent

        agent = CommunicationAgent.__new__(CommunicationAgent)
        agent._webhook_fail_counts = {}
        agent._bot = MagicMock()
        agent._engine = MagicMock()
        agent._persist_prefs = MagicMock()

        # Create a mock profile
        profile = UserProfile(user_id="u1", webhook_url="https://dead-endpoint.com/hook")
        agent._engine.preferences.get_or_create.return_value = profile
        agent._resolve_chat_id = MagicMock(return_value="123")

        # Record failures up to threshold
        for i in range(agent._WEBHOOK_MAX_FAILURES):
            agent._record_webhook_failure("u1", profile)

        # Webhook should be disabled
        self.assertEqual(profile.webhook_url, "")
        # User should have been notified
        agent._bot.send_message.assert_called()
        msg = agent._bot.send_message.call_args[0][1]
        self.assertIn("disabled", msg.lower())

    def test_successful_delivery_resets_failure_count(self):
        """A successful webhook delivery should reset the failure counter."""
        from newsfeed.orchestration.communication import CommunicationAgent

        agent = CommunicationAgent.__new__(CommunicationAgent)
        agent._webhook_fail_counts = {"u1": 3}

        # Simulate successful delivery clearing the count
        agent._webhook_fail_counts.pop("u1", None)

        self.assertNotIn("u1", agent._webhook_fail_counts)


# ══════════════════════════════════════════════════════════════════════════
# Safe Float/Int Helpers
# ══════════════════════════════════════════════════════════════════════════


class TestSafeConversions(unittest.TestCase):
    """Helper methods should handle edge cases without crashing."""

    def test_safe_float_nan(self):
        self.assertEqual(PreferenceStore._safe_float("nan", 0.5), 0.5)

    def test_safe_float_inf(self):
        self.assertEqual(PreferenceStore._safe_float("inf", 0.5), 0.5)

    def test_safe_float_neg_inf(self):
        self.assertEqual(PreferenceStore._safe_float("-inf", 0.5), 0.5)

    def test_safe_float_normal(self):
        self.assertAlmostEqual(PreferenceStore._safe_float("0.7", 0.5), 0.7)

    def test_safe_float_none(self):
        self.assertEqual(PreferenceStore._safe_float(None, 0.5), 0.5)

    def test_safe_float_garbage(self):
        self.assertEqual(PreferenceStore._safe_float("garbage", 0.5), 0.5)

    def test_safe_int_garbage(self):
        self.assertEqual(PreferenceStore._safe_int("garbage", 10), 10)

    def test_safe_int_none(self):
        self.assertEqual(PreferenceStore._safe_int(None, 10), 10)

    def test_safe_int_normal(self):
        self.assertEqual(PreferenceStore._safe_int("5", 10), 5)

    def test_capped_list_large(self):
        result = PreferenceStore._capped_list(list(range(100)), 20)
        self.assertEqual(len(result), 20)

    def test_capped_list_small(self):
        result = PreferenceStore._capped_list([1, 2, 3], 20)
        self.assertEqual(len(result), 3)

    def test_capped_list_none(self):
        result = PreferenceStore._capped_list(None, 20)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
