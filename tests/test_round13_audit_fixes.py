"""Tests for Round 13 — Final audit fixes.

Covers:
- Optimistic concurrency (version counter) on PreferenceStore
- README source count accuracy
- Agent failure surfacing in briefing footer
- Cloudflare Worker payload validation
- Engine concurrency backpressure (semaphore)
- Intelligence stage failure tracking in metadata + footer
- Pipeline-level timeout enforcement
- README accuracy (test count, free source list)
"""
from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

# ── 1. Optimistic Concurrency Version Counter ─────────────────────


class TestPreferenceStoreVersioning(unittest.TestCase):
    """Verify every mutation bumps the version counter on UserProfile."""

    def _store(self):
        from newsfeed.memory.store import PreferenceStore
        return PreferenceStore()

    def test_new_profile_starts_at_version_zero(self):
        store = self._store()
        profile = store.get_or_create("u1")
        self.assertEqual(profile.version, 0)

    def test_apply_weight_adjustment_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p, _ = store.apply_weight_adjustment("u1", "tech", 0.3)
        self.assertEqual(p.version, 1)
        p, _ = store.apply_weight_adjustment("u1", "tech", 0.1)
        self.assertEqual(p.version, 2)

    def test_apply_style_update_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.apply_style_update("u1", tone="analyst")
        self.assertEqual(p.version, 1)

    def test_apply_region_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.apply_region("u1", "europe")
        self.assertEqual(p.version, 1)

    def test_apply_cadence_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.apply_cadence("u1", "daily")
        self.assertEqual(p.version, 1)

    def test_apply_max_items_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.apply_max_items("u1", 20)
        self.assertEqual(p.version, 1)

    def test_apply_source_weight_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p, _ = store.apply_source_weight("u1", "bbc", 0.5)
        self.assertEqual(p.version, 1)

    def test_remove_region_bumps_version(self):
        store = self._store()
        store.apply_region("u1", "asia")
        p = store.remove_region("u1", "asia")
        self.assertEqual(p.version, 2)

    def test_set_watchlist_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.set_watchlist("u1", crypto=["BTC"])
        self.assertEqual(p.version, 1)

    def test_set_timezone_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.set_timezone("u1", "US/Eastern")
        self.assertEqual(p.version, 1)

    def test_mute_unmute_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.mute_topic("u1", "sports")
        self.assertEqual(p.version, 1)
        p = store.unmute_topic("u1", "sports")
        self.assertEqual(p.version, 2)

    def test_track_untrack_story_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.track_story("u1", "tech", "Apple launches new product line")
        self.assertEqual(p.version, 1)
        p = store.untrack_story("u1", 1)
        self.assertEqual(p.version, 2)

    def test_bookmark_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.save_bookmark("u1", "Story", "bbc", "https://example.com", "tech")
        self.assertEqual(p.version, 1)
        p = store.remove_bookmark("u1", 1)
        self.assertEqual(p.version, 2)

    def test_preset_lifecycle_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p, err = store.save_preset("u1", "my-preset")
        self.assertEqual(err, "")
        self.assertEqual(p.version, 1)
        p = store.load_preset("u1", "my-preset")
        self.assertIsNotNone(p)
        self.assertEqual(p.version, 2)
        deleted = store.delete_preset("u1", "my-preset")
        self.assertTrue(deleted)
        p = store.get_or_create("u1")
        self.assertEqual(p.version, 3)

    def test_set_filter_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.set_filter("u1", "confidence", "0.5")
        self.assertEqual(p.version, 1)

    def test_alert_keyword_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p, _ = store.add_alert_keyword("u1", "bitcoin")
        self.assertEqual(p.version, 1)
        p, removed = store.remove_alert_keyword("u1", "bitcoin")
        self.assertTrue(removed)
        self.assertEqual(p.version, 2)

    def test_set_email_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p = store.set_email("u1", "test@example.com")
        self.assertEqual(p.version, 1)

    def test_custom_source_bumps_version(self):
        store = self._store()
        store.get_or_create("u1")
        p, err = store.add_custom_source("u1", "MyFeed", "https://example.com/rss")
        self.assertEqual(err, "")
        self.assertEqual(p.version, 1)
        p, removed = store.remove_custom_source("u1", "MyFeed")
        self.assertTrue(removed)
        self.assertEqual(p.version, 2)

    def test_reset_bumps_version(self):
        store = self._store()
        store.apply_weight_adjustment("u1", "tech", 0.5)  # version 1
        p = store.reset("u1")
        self.assertEqual(p.version, 2)

    def test_update_if_current_success(self):
        store = self._store()
        p = store.get_or_create("u1")
        v0 = p.version
        result = store.update_if_current("u1", v0)
        self.assertIsNotNone(result)
        self.assertEqual(result.user_id, "u1")

    def test_update_if_current_conflict(self):
        store = self._store()
        store.get_or_create("u1")
        v0 = 0
        store.apply_weight_adjustment("u1", "tech", 0.5)  # version -> 1
        result = store.update_if_current("u1", v0)
        self.assertIsNone(result, "Should return None when version has advanced")

    def test_update_if_current_nonexistent_user(self):
        store = self._store()
        result = store.update_if_current("nonexistent", 0)
        self.assertIsNone(result)

    def test_version_persists_through_snapshot_restore(self):
        from newsfeed.memory.store import StatePersistence
        import tempfile
        store = self._store()
        store.apply_weight_adjustment("u1", "tech", 0.5)  # version -> 1
        store.apply_style_update("u1", tone="analyst")      # version -> 2
        p = store.get_or_create("u1")
        self.assertEqual(p.version, 2)

        with tempfile.TemporaryDirectory() as tmpdir:
            persistence = StatePersistence(Path(tmpdir))
            store.persist(persistence)

            store2 = self._store()
            store2.restore(persistence)
            p2 = store2.get_or_create("u1")
            self.assertEqual(p2.version, 2)

    def test_concurrent_version_increments(self):
        """Multiple threads mutating the same profile produce monotonically increasing versions."""
        store = self._store()
        store.get_or_create("u1")

        def mutate():
            for i in range(10):
                store.apply_weight_adjustment("u1", f"topic_{i}", 0.1)

        threads = [threading.Thread(target=mutate) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        p = store.get_or_create("u1")
        # 4 threads x 10 mutations = 40 version bumps
        self.assertEqual(p.version, 40)


# ── 2. README Source Count Accuracy ────────────────────────────────


class TestReadmeSourceCount(unittest.TestCase):
    """Verify README matches config/agents.json."""

    def test_readme_mentions_23_agents(self):
        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text()
        self.assertIn("23 research agents", text)
        self.assertIn("17 sources", text)

    def test_readme_architecture_matches(self):
        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text()
        self.assertIn("23 Research", text)
        self.assertIn("23 agents across 17 source types", text)

    def test_readme_test_count_current(self):
        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text()
        self.assertIn("821+", text)

    def test_readme_free_sources_complete(self):
        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text()
        for src in ["BBC", "Al Jazeera", "NPR", "CNBC", "France 24",
                     "TechCrunch", "Nature", "HackerNews", "arXiv",
                     "GDELT", "Google News"]:
            self.assertIn(src, text, f"Free source {src!r} missing from README")

    def test_config_agent_count_matches_readme(self):
        import json
        config_path = Path(__file__).resolve().parent.parent / "config" / "agents.json"
        config = json.loads(config_path.read_text())
        agents = config["research_agents"]
        self.assertEqual(len(agents), 23,
                         f"Expected 23 research agents, got {len(agents)}")
        unique_sources = set(a["source"] for a in agents)
        self.assertEqual(len(unique_sources), 17,
                         f"Expected 17 unique source types, got {len(unique_sources)}: {unique_sources}")


# ── 3. Agent Failure Surfacing in Briefing Footer ─────────────────


class TestAgentFailureSurfacing(unittest.TestCase):
    """Verify the formatter surfaces agent failures in the footer."""

    def _make_payload(self, failed_agents=None, agents_total=23,
                      agents_contributing=20, failed_stages=None):
        from newsfeed.models.domain import (
            CandidateItem, DeliveryPayload, ReportItem, ConfidenceBand,
        )
        candidate = CandidateItem(
            candidate_id="c1", title="Test Story", source="bbc",
            summary="Summary", url="https://example.com", topic="tech",
            evidence_score=0.8, novelty_score=0.7, preference_fit=0.6,
            prediction_signal=0.5, discovered_by="bbc_agent",
        )
        item = ReportItem(
            candidate=candidate,
            why_it_matters="Matters because.",
            what_changed="Changed.",
            predictive_outlook="Outlook.",
            adjacent_reads=[],
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )
        health = {
            "agents_total": agents_total,
            "agents_contributing": agents_contributing,
            "agents_silent": agents_total - agents_contributing,
            "agents_failed": failed_agents or [],
            "stages_enabled": ["credibility"],
            "stages_failed": failed_stages or [],
            "total_candidates": 10,
        }
        return DeliveryPayload(
            user_id="u1",
            generated_at=datetime.now(timezone.utc),
            items=[item],
            metadata={"pipeline_health": health},
        )

    def test_footer_shows_degradation_when_agents_fail(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        payload = self._make_payload(
            failed_agents=["x_agent_1", "reddit_agent_2", "gdelt_agent"],
            agents_total=23,
            agents_contributing=20,
        )
        footer = fmt.format_footer(payload)
        self.assertIn("20/23 sources reporting", footer)
        self.assertIn("3 unavailable", footer)

    def test_footer_no_degradation_when_all_agents_ok(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        payload = self._make_payload(
            failed_agents=[],
            agents_total=23,
            agents_contributing=23,
        )
        footer = fmt.format_footer(payload)
        self.assertNotIn("unavailable", footer)
        self.assertNotIn("\u26a0\ufe0f", footer)

    def test_footer_degradation_single_agent(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        payload = self._make_payload(
            failed_agents=["x_agent_1"],
            agents_total=23,
            agents_contributing=22,
        )
        footer = fmt.format_footer(payload)
        self.assertIn("22/23 sources reporting", footer)
        self.assertIn("1 unavailable", footer)

    def test_engine_tracks_failed_agents_in_metadata(self):
        """Verify the engine populates agents_failed in pipeline_health."""
        import json
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"

        from newsfeed.orchestration.engine import NewsFeedEngine
        engine = NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)

        payload = engine.handle_request_payload(
            user_id="test_user",
            prompt="Daily briefing",
            weighted_topics={"technology": 0.8},
        )
        health = payload.metadata.get("pipeline_health", {})
        # agents_failed should be a list (possibly empty for simulated agents)
        self.assertIsInstance(health.get("agents_failed"), list)
        # stages_failed should also be a list
        self.assertIsInstance(health.get("stages_failed"), list)


# ── 4. Cloudflare Worker Payload Validation ───────────────────────


class TestWorkerPayloadValidation(unittest.TestCase):
    """Verify the Cloudflare Worker validates Telegram update structure."""

    def test_worker_js_validates_update_id(self):
        """Worker.js must reject payloads without update_id."""
        worker_path = Path(__file__).resolve().parent.parent / "cloudflare-worker" / "worker.js"
        code = worker_path.read_text()
        self.assertIn("update.update_id", code)
        self.assertIn("422", code, "Should return 422 for invalid payloads")

    def test_worker_js_checks_message_or_callback(self):
        """Worker must require at least one recognized Telegram field."""
        worker_path = Path(__file__).resolve().parent.parent / "cloudflare-worker" / "worker.js"
        code = worker_path.read_text()
        self.assertIn("update.message", code)
        self.assertIn("update.callback_query", code)

    def test_worker_js_rejects_missing_webhook_secret(self):
        """Worker must reject requests when WEBHOOK_SECRET is not configured."""
        worker_path = Path(__file__).resolve().parent.parent / "cloudflare-worker" / "worker.js"
        code = worker_path.read_text()
        # Must check !env.WEBHOOK_SECRET (not just env.WEBHOOK_SECRET &&)
        self.assertIn("!env.WEBHOOK_SECRET", code)


# ── 5. Engine Concurrency Backpressure ────────────────────────────


class TestEngineConcurrency(unittest.TestCase):
    """Verify the engine limits concurrent pipeline runs."""

    def _make_engine(self, max_concurrent=2):
        import json
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        pipeline_cfg.setdefault("limits", {})["max_concurrent_requests"] = max_concurrent
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"
        from newsfeed.orchestration.engine import NewsFeedEngine
        return NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)

    def test_engine_has_semaphore(self):
        engine = self._make_engine(max_concurrent=3)
        self.assertIsNotNone(engine._request_semaphore)
        # Semaphore should allow up to 3 concurrent acquires
        acquired = [engine._request_semaphore.acquire(timeout=0) for _ in range(3)]
        self.assertTrue(all(acquired))
        # 4th should fail (non-blocking)
        self.assertFalse(engine._request_semaphore.acquire(timeout=0))
        # Release all
        for _ in range(3):
            engine._request_semaphore.release()

    def test_concurrent_requests_complete(self):
        """Multiple concurrent briefings should all complete under backpressure."""
        engine = self._make_engine(max_concurrent=2)
        results = []
        errors = []

        def run_briefing(user_id):
            try:
                payload = engine.handle_request_payload(
                    user_id=user_id,
                    prompt="Test briefing",
                    weighted_topics={"technology": 0.8},
                )
                results.append(payload)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=run_briefing, args=(f"user_{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(len(errors), 0, f"Errors: {errors}")
        self.assertEqual(len(results), 3)

    def test_default_max_concurrent_from_class(self):
        """Engine should use class default when config doesn't specify."""
        import json
        from newsfeed.orchestration.engine import NewsFeedEngine
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        # Remove any explicit max_concurrent_requests
        pipeline_cfg.get("limits", {}).pop("max_concurrent_requests", None)
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"
        engine = NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)
        self.assertEqual(engine.MAX_CONCURRENT_REQUESTS, 4)


# ── 6. Intelligence Stage Failure Tracking ────────────────────────


class TestStageFailureSurfacing(unittest.TestCase):
    """Verify the formatter surfaces intelligence stage failures."""

    def _make_payload(self, failed_stages):
        from newsfeed.models.domain import (
            CandidateItem, DeliveryPayload, ReportItem, ConfidenceBand,
        )
        candidate = CandidateItem(
            candidate_id="c1", title="Test", source="bbc",
            summary="S", url="https://x.com", topic="tech",
            evidence_score=0.8, novelty_score=0.7, preference_fit=0.6,
            prediction_signal=0.5, discovered_by="bbc_agent",
        )
        item = ReportItem(
            candidate=candidate, why_it_matters="M",
            what_changed="C", predictive_outlook="O",
            adjacent_reads=[],
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )
        health = {
            "agents_total": 23, "agents_contributing": 23,
            "agents_silent": 0, "agents_failed": [],
            "stages_enabled": ["credibility", "urgency", "enrichment"],
            "stages_failed": failed_stages,
            "total_candidates": 10,
        }
        return DeliveryPayload(
            user_id="u1",
            generated_at=datetime.now(timezone.utc),
            items=[item],
            metadata={"pipeline_health": health},
        )

    def test_footer_shows_degraded_stages(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        payload = self._make_payload(["enrichment", "clustering"])
        footer = fmt.format_footer(payload)
        self.assertIn("Degraded stages", footer)
        self.assertIn("enrichment", footer)
        self.assertIn("clustering", footer)

    def test_footer_no_stage_warning_when_all_ok(self):
        from newsfeed.delivery.telegram import TelegramFormatter
        fmt = TelegramFormatter()
        payload = self._make_payload([])
        footer = fmt.format_footer(payload)
        self.assertNotIn("Degraded stages", footer)

    def test_footer_shows_both_agent_and_stage_failures(self):
        """When both agents and stages fail, both warnings should appear."""
        from newsfeed.delivery.telegram import TelegramFormatter
        from newsfeed.models.domain import (
            CandidateItem, DeliveryPayload, ReportItem, ConfidenceBand,
        )
        candidate = CandidateItem(
            candidate_id="c1", title="Test", source="bbc",
            summary="S", url="https://x.com", topic="tech",
            evidence_score=0.8, novelty_score=0.7, preference_fit=0.6,
            prediction_signal=0.5, discovered_by="bbc_agent",
        )
        item = ReportItem(
            candidate=candidate, why_it_matters="M",
            what_changed="C", predictive_outlook="O",
            adjacent_reads=[],
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )
        health = {
            "agents_total": 23, "agents_contributing": 21,
            "agents_silent": 2, "agents_failed": ["x_agent_1", "gdelt_agent"],
            "stages_enabled": ["credibility", "urgency"],
            "stages_failed": ["enrichment"],
            "total_candidates": 10,
        }
        payload = DeliveryPayload(
            user_id="u1",
            generated_at=datetime.now(timezone.utc),
            items=[item],
            metadata={"pipeline_health": health},
        )
        fmt = TelegramFormatter()
        footer = fmt.format_footer(payload)
        self.assertIn("21/23 sources reporting", footer)
        self.assertIn("2 unavailable", footer)
        self.assertIn("Degraded stages: enrichment", footer)

    def test_engine_tracks_stages_failed_in_metadata(self):
        """Verify the engine populates stages_failed in pipeline_health."""
        import json
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"

        from newsfeed.orchestration.engine import NewsFeedEngine
        engine = NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)

        payload = engine.handle_request_payload(
            user_id="test_user",
            prompt="Daily briefing",
            weighted_topics={"technology": 0.8},
        )
        health = payload.metadata.get("pipeline_health", {})
        self.assertIsInstance(health.get("stages_failed"), list)
        # With simulated agents, no stages should fail
        self.assertEqual(len(health["stages_failed"]), 0)


# ── 7. Pipeline-Level Timeout Enforcement ─────────────────────────


class TestPipelineTimeout(unittest.TestCase):
    """Verify the engine enforces a hard deadline on pipeline runs."""

    def _make_engine(self, timeout_s=120):
        import json
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        pipeline_cfg.setdefault("limits", {})["pipeline_timeout_seconds"] = timeout_s
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"
        from newsfeed.orchestration.engine import NewsFeedEngine
        return NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)

    def test_engine_has_pipeline_timeout_config(self):
        engine = self._make_engine(timeout_s=60)
        self.assertEqual(engine._pipeline_timeout_s, 60)

    def test_engine_uses_default_timeout_when_not_configured(self):
        import json
        config_path = Path(__file__).resolve().parent.parent / "config"
        agents_cfg = json.loads((config_path / "agents.json").read_text())
        pipeline_cfg = json.loads((config_path / "pipelines.json").read_text())
        pipeline_cfg.get("limits", {}).pop("pipeline_timeout_seconds", None)
        personas_cfg = {"default_personas": ["engineer"]}
        personas_dir = Path(__file__).resolve().parent.parent / "personas"
        from newsfeed.orchestration.engine import NewsFeedEngine
        engine = NewsFeedEngine(agents_cfg, pipeline_cfg, personas_cfg, personas_dir)
        self.assertEqual(engine._pipeline_timeout_s, NewsFeedEngine.DEFAULT_PIPELINE_TIMEOUT_S)

    def test_run_with_deadline_raises_on_timeout(self):
        """Simulate a slow pipeline and verify TimeoutError is raised."""
        engine = self._make_engine(timeout_s=1)
        # Monkey-patch _handle_request_inner to sleep longer than the deadline
        original = engine._handle_request_inner

        def slow_inner(*args, **kwargs):
            time.sleep(5)
            return original(*args, **kwargs)

        engine._handle_request_inner = slow_inner
        with self.assertRaises(TimeoutError) as ctx:
            engine._run_with_deadline("u1", "test", {"tech": 0.5}, None)
        self.assertIn("timed out", str(ctx.exception))

    def test_normal_pipeline_completes_within_deadline(self):
        """A normal pipeline (simulated agents) should finish well within timeout."""
        engine = self._make_engine(timeout_s=60)
        payload = engine.handle_request_payload(
            user_id="timeout_test_user",
            prompt="Quick briefing",
            weighted_topics={"technology": 0.8},
        )
        self.assertIsNotNone(payload)
        self.assertTrue(len(payload.items) > 0)

    def test_timeout_error_message_includes_duration(self):
        """The TimeoutError message should include the configured timeout value."""
        engine = self._make_engine(timeout_s=1)
        engine._handle_request_inner = lambda *a, **kw: time.sleep(10)
        with self.assertRaises(TimeoutError) as ctx:
            engine._run_with_deadline("u1", "test", {}, None)
        self.assertIn("1s", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
