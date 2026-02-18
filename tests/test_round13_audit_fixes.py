"""Tests for Round 13 — Final audit fixes.

Covers:
- Optimistic concurrency (version counter) on PreferenceStore
- README source count accuracy
- Agent failure surfacing in briefing footer
"""
from __future__ import annotations

import re
import threading
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
        self.assertIn("779+", text)

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
                      agents_contributing=20):
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


if __name__ == "__main__":
    unittest.main()
