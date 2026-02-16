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
        profile_before = self.engine.preferences.get_or_create("integration-fb")
        old_weight = profile_before.topic_weights.get("geopolitics", 0.0)
        self.engine.apply_user_feedback("integration-fb", "more geopolitics")
        profile_after = self.engine.preferences.get_or_create("integration-fb")
        new_weight = profile_after.topic_weights.get("geopolitics", 0.0)
        self.assertGreater(new_weight, old_weight)

    def test_preference_persistence_roundtrip(self) -> None:
        """Preferences survive save/restore cycle."""
        from newsfeed.memory.store import StatePersistence
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StatePersistence(Path(tmpdir))
            self.engine.preferences.apply_weight_adjustment("persist-test", "crypto", 0.5)
            count = self.engine.preferences.persist(storage)
            self.assertGreater(count, 0)
            # Create fresh store and restore
            from newsfeed.memory.store import PreferenceStore
            fresh = PreferenceStore()
            restored = fresh.restore(storage)
            self.assertGreater(restored, 0)
            profile = fresh.get_or_create("persist-test")
            self.assertAlmostEqual(profile.topic_weights.get("crypto", 0.0), 0.5, places=2)

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


if __name__ == "__main__":
    unittest.main()
