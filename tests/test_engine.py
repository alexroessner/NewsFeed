from __future__ import annotations

import unittest
from pathlib import Path

from newsfeed.agents.simulated import ExpertCouncil
from newsfeed.models.config import load_runtime_config
from newsfeed.models.domain import ResearchTask
from newsfeed.orchestration.engine import NewsFeedEngine
from newsfeed.review.personas import PersonaReviewStack


class EngineTests(unittest.TestCase):
    def test_runtime_config_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        self.assertGreaterEqual(len(cfg.agents["research_agents"]), 10)
        self.assertGreaterEqual(len(cfg.pipeline["stages"]), 6)
        self.assertGreaterEqual(len(cfg.personas["default_personas"]), 1)

    def test_engine_generates_report_and_cache(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        output = engine.handle_request(
            user_id="u1",
            prompt="focus geopolitics",
            weighted_topics={"geopolitics": 0.9, "macro": 0.4},
        )

        # Briefing type is dynamic based on urgency detection
        self.assertTrue(
            "Morning Intelligence Digest" in output or "BREAKING ALERT" in output,
            "Expected a briefing header in output",
        )
        self.assertIn("Why it matters", output)
        # Persona context is now embedded by the style reviewer as [note1; note2]
        self.assertTrue(
            "Ensure output is precise" in output or "Review lenses" in output,
            "Expected persona context in output",
        )

        # Intelligence enrichment outputs
        self.assertIn("Confidence:", output)
        self.assertIn("NARRATIVE THREADS", output)

        more = engine.show_more("u1", "geopolitics", already_seen_ids=set(), limit=3)
        self.assertLessEqual(len(more), 3)

    def test_engine_report_contains_geo_risks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        output = engine.handle_request(
            user_id="u-geo",
            prompt="geopolitics briefing",
            weighted_topics={"geopolitics": 1.0},
        )

        self.assertIn("[", output)
        self.assertIn("Lifecycle:", output)

    def test_engine_metadata_includes_intelligence(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        output = engine.handle_request(
            user_id="u-meta",
            prompt="tech update",
            weighted_topics={"ai_policy": 0.8},
        )

        self.assertIn("Intelligence:", output)

    def test_expert_council_produces_votes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        # Use a simulated agent for reliable test candidates (real agents may 401)
        from newsfeed.agents.simulated import SimulatedResearchAgent
        agent = SimulatedResearchAgent(agent_id="test_sim", source="reuters", mandate="test")
        candidates = agent.run(
            ResearchTask(request_id="r1", user_id="u1", prompt="p", weighted_topics={"geopolitics": 1.0}),
            top_k=3,
        )
        council = ExpertCouncil()
        selected, reserve, debate = council.select(candidates, max_items=2)
        self.assertGreaterEqual(len(debate.votes), 3)
        self.assertLessEqual(len(selected), 2)
        self.assertIsInstance(reserve, list)

    def test_persona_stack_loads_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        stack = PersonaReviewStack(root / "personas", cfg.personas["default_personas"], cfg.personas["persona_notes"])
        context = stack.active_context()
        self.assertGreaterEqual(len(context), 1)
        self.assertIn("confidence bands", stack.refine_outlook("Base outlook."))

    def test_apply_user_feedback_updates_profile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        updates = engine.apply_user_feedback(
            "u-feedback",
            "more geopolitics less celebrity news tone analyst format sections",
        )

        self.assertEqual(updates.get("tone"), "analyst")
        self.assertEqual(updates.get("format"), "sections")
        self.assertIn("topic:geopolitics", updates)
        self.assertIn("topic:celebrity_news", updates)

    def test_feedback_region_command(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        updates = engine.apply_user_feedback("u-region", "region: europe")
        self.assertEqual(updates.get("region"), "europe")
        profile = engine.preferences.get_or_create("u-region")
        self.assertIn("europe", profile.regions_of_interest)

    def test_feedback_cadence_command(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        updates = engine.apply_user_feedback("u-cad", "cadence: morning")
        self.assertEqual(updates.get("cadence"), "morning")
        profile = engine.preferences.get_or_create("u-cad")
        self.assertEqual(profile.briefing_cadence, "morning")

    def test_feedback_max_items_command(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        updates = engine.apply_user_feedback("u-max", "max: 15")
        self.assertEqual(updates.get("max_items"), "15")
        profile = engine.preferences.get_or_create("u-max")
        self.assertEqual(profile.max_items, 15)

    def test_enabled_stages_from_config(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")
        expected = set(cfg.pipeline["intelligence"]["enabled_stages"])
        self.assertEqual(engine._enabled_stages, expected)


if __name__ == "__main__":
    unittest.main()
