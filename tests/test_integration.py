"""Integration tests — validate end-to-end behavior with simulated agents.

These tests exercise the full pipeline (research → intelligence → expert council →
editorial review → formatting) to catch issues that unit tests miss.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from newsfeed.models.config import load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


def _build_engine() -> NewsFeedEngine:
    """Build a fully configured engine using real config files."""
    root = Path(__file__).resolve().parents[1]
    config_dir = root / "config"
    personas_dir = root / "personas"
    cfg = load_runtime_config(config_dir)
    return NewsFeedEngine(
        config=cfg.agents, pipeline=cfg.pipeline,
        personas=cfg.personas, personas_dir=personas_dir,
    )


class TestFullPipeline(unittest.TestCase):
    """End-to-end pipeline tests with simulated agents."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = _build_engine()

    def test_briefing_returns_html(self) -> None:
        """Full pipeline produces valid HTML output."""
        output = self.engine.handle_request(
            user_id="integration-test",
            prompt="geopolitics and technology",
            weighted_topics={"geopolitics": 0.9, "technology": 0.7},
        )
        self.assertIsInstance(output, str)
        self.assertTrue(len(output) > 0, "Briefing should not be empty")

    def test_payload_has_metadata(self) -> None:
        """DeliveryPayload includes pipeline health metadata."""
        payload = self.engine.handle_request_payload(
            user_id="integration-meta",
            prompt="technology update",
            weighted_topics={"technology": 0.8},
        )
        self.assertIn("pipeline_trace", payload.metadata)
        self.assertIn("pipeline_health", payload.metadata)
        health = payload.metadata["pipeline_health"]
        self.assertIn("agents_total", health)
        self.assertIn("agents_contributing", health)

    def test_show_more_returns_candidates(self) -> None:
        """Show more returns cached reserve candidates."""
        # First run a briefing to populate cache
        self.engine.handle_request(
            user_id="integration-more",
            prompt="science",
            weighted_topics={"science": 0.9},
        )
        # Then ask for more
        more = self.engine.show_more(
            user_id="integration-more",
            topic="science",
            already_seen_ids=set(),
            limit=3,
        )
        self.assertIsInstance(more, list)

    def test_feedback_updates_preferences(self) -> None:
        """User feedback changes topic weights."""
        uid = f"integration-fb-{id(self)}"  # Unique per run
        profile_before = self.engine.preferences.get_or_create(uid)
        old_weight = profile_before.topic_weights.get("geopolitics", 0.0)
        self.engine.apply_user_feedback(uid, "more geopolitics")
        profile_after = self.engine.preferences.get_or_create(uid)
        new_weight = profile_after.topic_weights.get("geopolitics", 0.0)
        self.assertGreater(new_weight, old_weight)

    def test_preference_persistence_roundtrip(self) -> None:
        """Preferences survive save/restore cycle."""
        from newsfeed.memory.store import StatePersistence
        import tempfile
        uid = f"persist-test-{id(self)}"  # Unique per run to avoid stale state
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            self.engine.preferences.apply_weight_adjustment(uid, "crypto", 0.5)
            expected = self.engine.preferences.get_or_create(uid).topic_weights.get("crypto", 0.0)
            count = self.engine.preferences.persist(storage)
            self.assertGreater(count, 0)
            # Create fresh store and restore
            from newsfeed.memory.store import PreferenceStore
            fresh = PreferenceStore()
            restored = fresh.restore(storage)
            self.assertGreater(restored, 0)
            profile = fresh.get_or_create(uid)
            self.assertAlmostEqual(profile.topic_weights.get("crypto", 0.0), expected, places=2)

    def test_empty_research_produces_diagnostic(self) -> None:
        """When research returns nothing, payload metadata reflects it."""
        payload = self.engine.handle_request_payload(
            user_id="integration-empty",
            prompt="nonexistent_topic_xyz",
            weighted_topics={"nonexistent_topic_xyz": 0.9},
        )
        health = payload.metadata.get("pipeline_health", {})
        self.assertIn("total_candidates", health)

    def test_concurrent_users(self) -> None:
        """Multiple users can run briefings without data corruption."""
        import threading
        results = {}
        errors = []

        def run_briefing(uid: str) -> None:
            try:
                output = self.engine.handle_request(
                    user_id=uid,
                    prompt="markets",
                    weighted_topics={"markets": 0.8},
                )
                results[uid] = len(output)
            except Exception as e:
                errors.append((uid, str(e)))

        threads = [
            threading.Thread(target=run_briefing, args=(f"concurrent-{i}",))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 3)
        for uid, length in results.items():
            self.assertGreater(length, 0, f"{uid} got empty briefing")


class TestAnalyticsDB(unittest.TestCase):
    """Analytics database integration tests."""

    def test_auto_purge(self) -> None:
        """Auto-purge removes old data without error."""
        engine = _build_engine()
        # Record some test data
        engine.analytics.record_user_seen("purge-test", "12345")
        # Purge with 0 retention (delete everything)
        result = engine.analytics.auto_purge(retention_days=0)
        self.assertIsInstance(result, dict)

    def test_connection_health_check(self) -> None:
        """DB connection survives health check."""
        engine = _build_engine()
        engine.analytics.record_user_seen("health-test", "12345")
        # Force a query to exercise connection
        user = engine.analytics.get_user_summary("health-test")
        # Should not raise — user was just recorded


class TestThreadSafety(unittest.TestCase):
    """Thread safety tests for shared data structures."""

    def test_bounded_user_dict_concurrent_writes(self) -> None:
        """BoundedUserDict handles concurrent writes without corruption."""
        from newsfeed.memory.store import BoundedUserDict
        import threading

        d: BoundedUserDict[int] = BoundedUserDict(maxlen=50)
        errors = []

        def writer(start: int) -> None:
            try:
                for i in range(100):
                    d[f"key-{start}-{i}"] = i
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertLessEqual(len(d), 50)

    def test_preference_store_concurrent_access(self) -> None:
        """PreferenceStore handles concurrent reads/writes safely."""
        from newsfeed.memory.store import PreferenceStore
        import threading

        store = PreferenceStore()
        errors = []

        def updater(uid: str) -> None:
            try:
                for _ in range(20):
                    store.apply_weight_adjustment(uid, "geopolitics", 0.01)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=updater, args=(f"user-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")


class TestBoundedUserDictSemantics(unittest.TestCase):
    """Verify LRU semantics and RLock reentrancy in BoundedUserDict."""

    def test_setdefault_does_not_deadlock(self) -> None:
        """setdefault calls __setitem__ internally — RLock must allow reentrancy."""
        from newsfeed.memory.store import BoundedUserDict
        d: BoundedUserDict[str] = BoundedUserDict(maxlen=10)
        # This would deadlock with threading.Lock instead of RLock
        result = d.setdefault("a", "default")
        self.assertEqual(result, "default")
        # Second call returns existing value without insert
        result = d.setdefault("a", "other")
        self.assertEqual(result, "default")

    def test_lru_eviction_order(self) -> None:
        """Oldest (least recently used) key is evicted first."""
        from newsfeed.memory.store import BoundedUserDict
        d: BoundedUserDict[int] = BoundedUserDict(maxlen=3)
        d["a"] = 1
        d["b"] = 2
        d["c"] = 3
        # "a" is oldest; adding "d" should evict "a"
        d["d"] = 4
        self.assertNotIn("a", d)
        self.assertIn("b", d)
        self.assertIn("c", d)
        self.assertIn("d", d)

    def test_lru_refresh_on_update(self) -> None:
        """Updating an existing key refreshes it (moves to end)."""
        from newsfeed.memory.store import BoundedUserDict
        d: BoundedUserDict[int] = BoundedUserDict(maxlen=3)
        d["a"] = 1
        d["b"] = 2
        d["c"] = 3
        # Refresh "a" — now "b" is oldest
        d["a"] = 10
        d["d"] = 4
        self.assertNotIn("b", d)  # "b" was oldest after "a" was refreshed
        self.assertIn("a", d)


class TestCommandRateLimiter(unittest.TestCase):
    """Verify per-command rate limiting logic."""

    def setUp(self) -> None:
        from unittest.mock import MagicMock
        from newsfeed.orchestration.communication import CommunicationAgent
        from newsfeed.models.domain import UserProfile

        self.mock_engine = MagicMock()
        self.mock_engine.preferences.get_or_create.return_value = UserProfile(
            user_id="u1", topic_weights={"geopolitics": 0.8}, max_items=10,
        )
        mock_payload = MagicMock()
        mock_payload.items = []
        self.mock_engine.handle_request_payload.return_value = mock_payload
        self.mock_engine.handle_request.return_value = "Briefing text"
        self.mock_engine.apply_user_feedback.return_value = {"topic:geopolitics": "1.0"}
        self.mock_engine.last_briefing_items.return_value = []

        self.mock_bot = MagicMock()
        self.mock_bot.parse_command.return_value = None

        self.agent = CommunicationAgent(engine=self.mock_engine, bot=self.mock_bot)

    def test_unknown_command_not_rate_limited(self) -> None:
        """Commands not in _COMMAND_RATE_LIMITS pass through."""
        self.assertFalse(self.agent._check_command_rate_limit("u1", "help"))
        self.assertFalse(self.agent._check_command_rate_limit("u1", "settings"))

    def test_first_request_allowed(self) -> None:
        """First request for a rate-limited command succeeds."""
        self.assertFalse(self.agent._check_command_rate_limit("u1", "feedback"))

    def test_exceeding_limit_blocks(self) -> None:
        """Exceeding the rate limit blocks the request."""
        # "recall" limit is (5, 60) — 5 per 60 seconds
        for _ in range(5):
            self.assertFalse(self.agent._check_command_rate_limit("u1", "recall"))
        # 6th request should be blocked
        self.assertTrue(self.agent._check_command_rate_limit("u1", "recall"))

    def test_different_users_separate_limits(self) -> None:
        """Each user has their own rate limit window."""
        for _ in range(5):
            self.agent._check_command_rate_limit("user_a", "recall")
        # user_a is blocked
        self.assertTrue(self.agent._check_command_rate_limit("user_a", "recall"))
        # user_b is NOT blocked
        self.assertFalse(self.agent._check_command_rate_limit("user_b", "recall"))

    def test_rate_limit_wiring_in_handle_command(self) -> None:
        """Rate-limited command returns command_rate_limited action."""
        # Exhaust the recall limit
        for _ in range(5):
            self.agent._check_command_rate_limit("u1", "recall")
        # Now route a recall command through _handle_command
        result = self.agent._handle_command(123, "u1", "recall", "test")
        self.assertEqual(result["action"], "command_rate_limited")
        self.mock_bot.send_message.assert_called()


class TestPreferenceConfirmation(unittest.TestCase):
    """Verify feedback confirmation message is sent."""

    def setUp(self) -> None:
        from unittest.mock import MagicMock
        from newsfeed.orchestration.communication import CommunicationAgent
        from newsfeed.models.domain import UserProfile

        self.mock_engine = MagicMock()
        self.mock_engine.preferences.get_or_create.return_value = UserProfile(
            user_id="u1", topic_weights={"geopolitics": 0.8}, max_items=10,
        )
        self.mock_engine.apply_user_feedback.return_value = {
            "topic:geopolitics": "1.0",
            "hint:geopolitics": "Already at maximum",
        }
        self.mock_engine._preference_deltas = {"more": 0.2, "less": -0.2}
        self.mock_engine.last_briefing_items.return_value = []

        self.mock_bot = MagicMock()
        self.mock_bot.parse_command.return_value = {
            "type": "feedback", "chat_id": 123, "user_id": "u1",
            "command": "", "args": "", "text": "more geopolitics",
        }

        self.agent = CommunicationAgent(engine=self.mock_engine, bot=self.mock_bot)

    def test_confirmation_sent_after_feedback(self) -> None:
        """Feedback triggers a confirmation message."""
        self.agent.handle_update({})
        # Check that send_message was called with confirmation
        calls = self.mock_bot.send_message.call_args_list
        confirmations = [c for c in calls if "Confirmed" in str(c)]
        self.assertGreater(len(confirmations), 0, "No confirmation message sent")

    def test_hints_excluded_from_confirmation(self) -> None:
        """Hint messages (hint:*) are not shown in confirmation."""
        self.agent.handle_update({})
        calls = self.mock_bot.send_message.call_args_list
        for call in calls:
            msg = str(call)
            if "Confirmed" in msg:
                self.assertNotIn("hint:", msg)
                self.assertNotIn("Already at maximum", msg)


if __name__ == "__main__":
    unittest.main()
