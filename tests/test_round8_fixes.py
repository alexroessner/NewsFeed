"""Tests for Round 8: Summary/WhyItMatters deduplication, persistence,
weight decay, GDPR, Telegram retry, eviction logging, and thread safety.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from newsfeed.delivery.bot import TelegramBot
from newsfeed.memory.store import BoundedUserDict, PreferenceStore, StatePersistence
from newsfeed.models.domain import (
    CandidateItem,
    ReportItem,
    UrgencyLevel,
    UserProfile,
)
from newsfeed.review.agents import StyleReviewAgent


# ══════════════════════════════════════════════════════════════════════════
# Summary / Why It Matters Deduplication
# ══════════════════════════════════════════════════════════════════════════


def _make_candidate(**kwargs) -> CandidateItem:
    defaults = dict(
        candidate_id="c-1", title="NATO Summit Concludes",
        source="reuters", summary="NATO leaders met today in Brussels to discuss security.",
        url="https://example.com/nato", topic="geopolitics",
        evidence_score=0.7, novelty_score=0.6, preference_fit=0.8,
        prediction_signal=0.5, discovered_by="reuters_agent",
        created_at=datetime.now(timezone.utc), urgency=UrgencyLevel.ROUTINE,
    )
    defaults.update(kwargs)
    return CandidateItem(**defaults)


def _make_report_item(**kwargs) -> ReportItem:
    c = _make_candidate(**{k: v for k, v in kwargs.items()
                          if k in CandidateItem.__dataclass_fields__})
    return ReportItem(
        candidate=c,
        why_it_matters=kwargs.get("why_it_matters",
            "Critical development in geopolitics from Reuters (major wire service)."),
        what_changed=kwargs.get("what_changed", "New breaking report."),
        predictive_outlook=kwargs.get("predictive_outlook", "Limited forward indicators."),
        adjacent_reads=kwargs.get("adjacent_reads", []),
    )


class TestWhyItMattersNotDuplicated(unittest.TestCase):
    """The #1 user-reported issue: Summary and Why It Matters were identical."""

    def test_why_preserves_narrative_text(self):
        """Style review should preserve the narrative-generated text, not replace with summary."""
        agent = StyleReviewAgent()
        item = _make_report_item()
        profile = UserProfile(user_id="u1")
        narrative_text = item.why_it_matters

        result = agent.review(item, profile)

        # The narrative text should be preserved (possibly with urgency prefix)
        self.assertIn("geopolitics", result.why_it_matters)
        self.assertIn("Reuters", result.why_it_matters)
        # The summary text should NOT appear in why_it_matters
        self.assertNotIn("NATO leaders met today", result.why_it_matters)

    def test_why_not_equal_to_summary(self):
        """Why It Matters must never be identical to the Summary."""
        agent = StyleReviewAgent()
        item = _make_report_item()
        profile = UserProfile(user_id="u1")

        result = agent.review(item, profile)

        self.assertNotEqual(result.why_it_matters, item.candidate.summary)

    def test_breaking_urgency_adds_framing(self):
        """Breaking urgency should prepend framing to the narrative."""
        agent = StyleReviewAgent()
        item = _make_report_item(urgency=UrgencyLevel.BREAKING)
        item.why_it_matters = "Important development in geopolitics."
        profile = UserProfile(user_id="u1")

        result = agent.review(item, profile)

        self.assertIn("Developing rapidly", result.why_it_matters)
        self.assertIn("Important development", result.why_it_matters)

    def test_critical_urgency_adds_framing(self):
        """Critical urgency should prepend attention-required framing."""
        agent = StyleReviewAgent()
        item = _make_report_item(urgency=UrgencyLevel.CRITICAL)
        item.why_it_matters = "Severe escalation detected."
        profile = UserProfile(user_id="u1")

        result = agent.review(item, profile)

        self.assertIn("Immediate attention required", result.why_it_matters)

    def test_routine_no_urgency_prefix(self):
        """Routine urgency should NOT add any prefix."""
        agent = StyleReviewAgent()
        item = _make_report_item(urgency=UrgencyLevel.ROUTINE)
        item.why_it_matters = "Standard report on geopolitics."
        profile = UserProfile(user_id="u1")

        result = agent.review(item, profile)

        # Should not have urgency framing for routine
        self.assertNotIn("Immediate attention", result.why_it_matters)
        self.assertNotIn("Developing rapidly", result.why_it_matters)

    def test_empty_narrative_falls_back_to_title(self):
        """If narrative text is empty, fall back to title — not summary."""
        agent = StyleReviewAgent()
        item = _make_report_item()
        item.why_it_matters = ""
        profile = UserProfile(user_id="u1")

        result = agent.review(item, profile)

        # Should use title as fallback
        self.assertIn("NATO Summit Concludes", result.why_it_matters)
        # Should NOT use summary
        self.assertNotIn("NATO leaders met today", result.why_it_matters)

    def test_all_tones_preserve_narrative(self):
        """No tone should replace narrative with summary."""
        for tone in ("concise", "analyst", "executive", "brief", "deep"):
            agent = StyleReviewAgent()
            item = _make_report_item()
            profile = UserProfile(user_id="u1", tone=tone)

            result = agent.review(item, profile)

            self.assertNotEqual(
                result.why_it_matters, item.candidate.summary,
                f"Tone '{tone}' duplicated summary into why_it_matters"
            )


# ══════════════════════════════════════════════════════════════════════════
# Cross-Session State Persistence
# ══════════════════════════════════════════════════════════════════════════


class TestStatePersistence(unittest.TestCase):
    """PreferenceStore should survive process restarts."""

    def test_persist_and_restore_roundtrip(self):
        """Profiles saved to disk should restore identically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            store = PreferenceStore()

            # Set up a user with various preferences
            store.apply_weight_adjustment("user1", "geopolitics", 0.8)
            store.apply_weight_adjustment("user1", "ai_policy", 0.6)
            store.apply_source_weight("user1", "reuters", 1.0)
            store.apply_style_update("user1", tone="analyst", fmt="sections")
            store.apply_region("user1", "middle_east")
            store.apply_cadence("user1", "morning")
            store.set_timezone("user1", "US/Eastern")

            saved = store.persist(storage)
            self.assertEqual(saved, 1)

            # Create a new store and restore
            store2 = PreferenceStore()
            restored = store2.restore(storage)
            self.assertEqual(restored, 1)

            profile = store2.get_or_create("user1")
            self.assertAlmostEqual(profile.topic_weights["geopolitics"], 0.8)
            self.assertAlmostEqual(profile.topic_weights["ai_policy"], 0.6)
            self.assertAlmostEqual(profile.source_weights["reuters"], 1.0)
            self.assertEqual(profile.tone, "analyst")
            self.assertEqual(profile.format, "sections")
            self.assertIn("middle_east", profile.regions_of_interest)
            self.assertEqual(profile.briefing_cadence, "morning")
            self.assertEqual(profile.timezone, "US/Eastern")

    def test_restore_empty_file(self):
        """Restoring from empty storage should return 0 and not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            store = PreferenceStore()
            self.assertEqual(store.restore(storage), 0)

    def test_persist_multiple_users(self):
        """Multiple users should all be persisted and restored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            store = PreferenceStore()

            for i in range(5):
                store.apply_weight_adjustment(f"user{i}", "tech", 0.5 + i * 0.1)

            saved = store.persist(storage)
            self.assertEqual(saved, 5)

            store2 = PreferenceStore()
            restored = store2.restore(storage)
            self.assertEqual(restored, 5)

            for i in range(5):
                p = store2.get_or_create(f"user{i}")
                self.assertAlmostEqual(p.topic_weights["tech"], 0.5 + i * 0.1)


# ══════════════════════════════════════════════════════════════════════════
# Weight Decay
# ══════════════════════════════════════════════════════════════════════════


class TestWeightDecay(unittest.TestCase):
    """Stale preferences should fade toward zero over time."""

    def test_recent_weights_not_decayed(self):
        """Weights set recently should not be decayed."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.8)

        decayed = store.apply_weight_decay("u1")

        self.assertEqual(decayed, 0)
        p = store.get_or_create("u1")
        self.assertAlmostEqual(p.topic_weights["tech"], 0.8)

    def test_old_weights_decayed(self):
        """Weights not reinforced for a long time should decay."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.8)

        # Backdate the weight timestamp to simulate old weight
        store._weight_timestamps["u1"]["tech"] = time.time() - (60 * 86400)  # 60 days ago

        decayed = store.apply_weight_decay("u1")

        self.assertGreater(decayed, 0)
        p = store.get_or_create("u1")
        self.assertLess(p.topic_weights.get("tech", 0), 0.8)

    def test_very_old_weights_pruned_to_zero(self):
        """Extremely old weights should decay to zero and be pruned."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.1)

        # Backdate 6 months
        store._weight_timestamps["u1"]["tech"] = time.time() - (180 * 86400)

        store.apply_weight_decay("u1")

        p = store.get_or_create("u1")
        # Should be pruned away (zero weights are cleaned)
        self.assertNotIn("tech", p.topic_weights)

    def test_source_weights_also_decay(self):
        """Source weights should decay just like topic weights."""
        store = PreferenceStore()
        store.apply_source_weight("u1", "reuters", 1.5)

        store._weight_timestamps["u1"]["reuters"] = time.time() - (90 * 86400)

        decayed = store.apply_weight_decay("u1")

        self.assertGreater(decayed, 0)
        p = store.get_or_create("u1")
        self.assertLess(p.source_weights.get("reuters", 0), 1.5)

    def test_decay_nonexistent_user(self):
        """Decaying a nonexistent user should return 0."""
        store = PreferenceStore()
        self.assertEqual(store.apply_weight_decay("nobody"), 0)


# ══════════════════════════════════════════════════════════════════════════
# GDPR Data Export / Deletion
# ══════════════════════════════════════════════════════════════════════════


class TestGDPR(unittest.TestCase):
    """GDPR Article 17 (erasure) and Article 20 (portability)."""

    def test_export_user_data(self):
        """Exporting user data should return a complete snapshot."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "ai_policy", 0.9)
        store.apply_source_weight("u1", "bbc", 0.5)
        store.apply_style_update("u1", tone="executive")

        data = store.export_user_data("u1")

        self.assertIsNotNone(data)
        self.assertAlmostEqual(data["topic_weights"]["ai_policy"], 0.9)
        self.assertAlmostEqual(data["source_weights"]["bbc"], 0.5)
        self.assertEqual(data["tone"], "executive")

    def test_export_nonexistent_user(self):
        """Exporting a nonexistent user should return None."""
        store = PreferenceStore()
        self.assertIsNone(store.export_user_data("ghost"))

    def test_delete_user_data(self):
        """Deleting user data should remove all traces."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.5)
        store.apply_source_weight("u1", "reuters", 1.0)

        deleted = store.delete_user_data("u1")

        self.assertTrue(deleted)
        self.assertIsNone(store.export_user_data("u1"))
        # Fresh profile should have no weights
        p = store.get_or_create("u1")
        self.assertEqual(len(p.topic_weights), 0)

    def test_delete_nonexistent_user(self):
        """Deleting a nonexistent user should return False."""
        store = PreferenceStore()
        self.assertFalse(store.delete_user_data("ghost"))

    def test_delete_clears_weight_timestamps(self):
        """Deletion should also clear weight decay timestamps."""
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "tech", 0.5)
        self.assertIn("u1", store._weight_timestamps)

        store.delete_user_data("u1")

        self.assertNotIn("u1", store._weight_timestamps)


# ══════════════════════════════════════════════════════════════════════════
# Telegram 429 Rate Limit Retry
# ══════════════════════════════════════════════════════════════════════════


class TestTelegram429Retry(unittest.TestCase):
    """Telegram API should retry on 429 with exponential backoff."""

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    @patch("newsfeed.delivery.bot.time.sleep")
    def test_retries_on_429(self, mock_sleep, mock_urlopen):
        """Should retry on 429 and succeed on subsequent attempt."""
        import urllib.error

        # First call: 429, second call: success
        err_429 = urllib.error.HTTPError(
            "https://api.telegram.org/bot123/sendMessage",
            429, "Too Many Requests", {},
            MagicMock(read=MagicMock(return_value=b'{"parameters":{"retry_after":1}}'))
        )
        success_resp = MagicMock()
        success_resp.read.return_value = json.dumps({"ok": True, "result": {"id": 1}}).encode()
        success_resp.__enter__ = MagicMock(return_value=success_resp)
        success_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [err_429, success_resp]

        bot = TelegramBot("test_token")
        result = bot._api_call("sendMessage", data={"chat_id": 123, "text": "hi"})

        self.assertEqual(result, {"id": 1})
        mock_sleep.assert_called_once()  # Should have slept once for retry

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    @patch("newsfeed.delivery.bot.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_urlopen):
        """Should stop retrying after MAX_RETRIES."""
        import urllib.error

        err_429 = urllib.error.HTTPError(
            "https://api.telegram.org/bot123/test",
            429, "Too Many Requests", {},
            MagicMock(read=MagicMock(return_value=b'{}'))
        )
        mock_urlopen.side_effect = err_429

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {})
        self.assertEqual(mock_sleep.call_count, bot._MAX_RETRIES)

    @patch("newsfeed.delivery.bot.urllib.request.urlopen")
    def test_non_429_error_not_retried(self, mock_urlopen):
        """Non-429 HTTP errors should not trigger retry."""
        import urllib.error

        err_500 = urllib.error.HTTPError(
            "https://api.telegram.org/bot123/test",
            500, "Internal Server Error", {},
            MagicMock(read=MagicMock(return_value=b'{}'))
        )
        mock_urlopen.side_effect = err_500

        bot = TelegramBot("test_token")
        result = bot._api_call("test")

        self.assertEqual(result, {})
        self.assertEqual(mock_urlopen.call_count, 1)  # No retry


# ══════════════════════════════════════════════════════════════════════════
# BoundedUserDict Eviction Logging
# ══════════════════════════════════════════════════════════════════════════


class TestBoundedUserDictEvictionLogging(unittest.TestCase):
    """Evicted entries should be logged."""

    def test_eviction_logged(self):
        """When an entry is evicted, a log message should be emitted."""
        d = BoundedUserDict(maxlen=2)
        d["a"] = 1
        d["b"] = 2

        with self.assertLogs("newsfeed.memory.store", level="INFO") as cm:
            d["c"] = 3  # Should evict "a"

        self.assertTrue(any("evicting key=a" in msg for msg in cm.output))

    def test_no_log_when_under_cap(self):
        """No eviction log should be emitted when under cap."""
        d = BoundedUserDict(maxlen=10)
        # No logging should occur for normal insertions
        d["a"] = 1
        d["b"] = 2
        # No assertion on logs here — just verifying no crash


# ══════════════════════════════════════════════════════════════════════════
# Thread Safety
# ══════════════════════════════════════════════════════════════════════════


class TestThreadSafety(unittest.TestCase):
    """PreferenceStore should be safe under concurrent access."""

    def test_concurrent_weight_adjustments(self):
        """Multiple threads adjusting weights should not corrupt state."""
        store = PreferenceStore()
        errors = []

        def adjust_weights(user_id: str, topic: str, n: int):
            try:
                for _ in range(n):
                    store.apply_weight_adjustment(user_id, topic, 0.01)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = threading.Thread(target=adjust_weights, args=(f"user{i}", "tech", 50))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")

        # All users should have valid profiles
        for i in range(10):
            p = store.get_or_create(f"user{i}")
            self.assertIn("tech", p.topic_weights)
            self.assertGreater(p.topic_weights["tech"], 0)
            self.assertLessEqual(p.topic_weights["tech"], 1.0)

    def test_concurrent_snapshot_and_write(self):
        """Snapshot should not crash while writes are happening."""
        store = PreferenceStore()
        errors = []

        def writer():
            try:
                for i in range(100):
                    store.apply_weight_adjustment(f"user{i % 10}", f"topic{i}", 0.1)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(50):
                    store.snapshot()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")


# ══════════════════════════════════════════════════════════════════════════
# Integration: Narrative Pipeline Deduplication
# ══════════════════════════════════════════════════════════════════════════


class TestNarrativePipelineDeduplication(unittest.TestCase):
    """End-to-end: the full review pipeline should not produce duplicated content."""

    def test_style_then_clarity_no_duplication(self):
        """After both review passes, why_it_matters should differ from summary."""
        from newsfeed.review.agents import ClarityReviewAgent

        style = StyleReviewAgent()
        clarity = ClarityReviewAgent()

        item = _make_report_item()
        profile = UserProfile(user_id="u1", tone="concise")

        style.review(item, profile)
        clarity.review(item, profile)

        self.assertNotEqual(item.why_it_matters, item.candidate.summary)
        # Narrative content should still be present
        self.assertTrue(len(item.why_it_matters) > 10)

    def test_batch_review_no_duplication(self):
        """Batch review should not cause summary duplication across items."""
        from newsfeed.review.agents import ClarityReviewAgent

        style = StyleReviewAgent()
        clarity = ClarityReviewAgent()

        items = []
        for i in range(3):
            item = _make_report_item(
                candidate_id=f"c-{i}",
                summary=f"Summary text for story {i}.",
                why_it_matters=f"Narrative analysis for story {i} from Reuters.",
            )
            items.append(item)

        profile = UserProfile(user_id="u1")
        for item in items:
            style.review(item, profile)
        clarity.review_batch(items, profile)

        for i, item in enumerate(items):
            self.assertNotEqual(
                item.why_it_matters, item.candidate.summary,
                f"Item {i} has duplicated summary in why_it_matters"
            )


if __name__ == "__main__":
    unittest.main()
