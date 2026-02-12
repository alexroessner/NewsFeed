from __future__ import annotations

import unittest
from pathlib import Path

from newsfeed.models.config import load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


class EngineTests(unittest.TestCase):
    def test_runtime_config_loads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        self.assertGreaterEqual(len(cfg.agents["research_agents"]), 10)
        self.assertGreaterEqual(len(cfg.pipeline["stages"]), 6)

    def test_engine_generates_report_and_cache(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        engine = NewsFeedEngine(cfg.agents, cfg.pipeline)

        output = engine.handle_request(
            user_id="u1",
            prompt="focus geopolitics",
            weighted_topics={"geopolitics": 0.9, "macro": 0.4},
        )

        self.assertIn("NewsFeed Brief", output)
        self.assertIn("Why it matters", output)

        more = engine.show_more("u1", "geopolitics", already_seen_ids=set(), limit=3)
        self.assertLessEqual(len(more), 3)


if __name__ == "__main__":
    unittest.main()
