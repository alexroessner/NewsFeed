from __future__ import annotations

import unittest
from pathlib import Path

from newsfeed.agents.simulated import ExpertCouncil
from newsfeed.models.config import load_runtime_config
from newsfeed.models.domain import ResearchTask
from newsfeed.orchestration.engine import NewsFeedEngine
from newsfeed.review.personas import PersonaReviewStack
from newsfeed.models.config import load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


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
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline)

        output = engine.handle_request(
            user_id="u1",
            prompt="focus geopolitics",
            weighted_topics={"geopolitics": 0.9, "macro": 0.4},
        )

        self.assertIn("NewsFeed Brief", output)
        self.assertIn("Why it matters", output)
        self.assertIn("Review lenses", output)

        more = engine.show_more("u1", "geopolitics", already_seen_ids=set(), limit=3)
        self.assertLessEqual(len(more), 3)

    def test_expert_council_produces_votes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

        candidates = engine._research_agents()[0].run(
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


if __name__ == "__main__":
    unittest.main()
