from __future__ import annotations

from pathlib import Path

from newsfeed.models.config import load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    config_dir = root / "config"
    cfg = load_runtime_config(config_dir)

    stages = cfg.pipeline.get("stages", [])
    research_agents = cfg.agents.get("research_agents", [])

    print("NewsFeed v1 bootstrap")
    print(f"Loaded stages: {len(stages)}")
    print(f"Loaded research agents: {len(research_agents)}")

    engine = NewsFeedEngine(config=cfg.agents, pipeline=cfg.pipeline)
    report = engine.handle_request(
        user_id="demo-user",
        prompt="Give me high-signal geopolitics and AI policy updates",
        weighted_topics={"geopolitics": 0.9, "ai_policy": 0.8},
    )
    print("\n--- Demo report preview ---")
    print(report.splitlines()[0])


if __name__ == "__main__":
    main()
