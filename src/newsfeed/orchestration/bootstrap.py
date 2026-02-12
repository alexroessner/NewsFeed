from __future__ import annotations

from pathlib import Path

from newsfeed.models.config import load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    config_dir = root / "config"
    personas_dir = root / "personas"
    cfg = load_runtime_config(config_dir)

    stages = cfg.pipeline.get("stages", [])
    research_agents = cfg.agents.get("research_agents", [])
    enabled_stages = cfg.pipeline.get("intelligence", {}).get("enabled_stages", [])
    config_version = cfg.pipeline.get("version", "unknown")

    print(f"NewsFeed bootstrap (config v{config_version})")
    print(f"Loaded stages: {len(stages)}")
    print(f"Loaded research agents: {len(research_agents)}")
    print(f"Intelligence stages: {', '.join(enabled_stages) or 'all defaults'}")

    engine = NewsFeedEngine(config=cfg.agents, pipeline=cfg.pipeline, personas=cfg.personas, personas_dir=personas_dir)
    report = engine.handle_request(
        user_id="demo-user",
        prompt="Give me high-signal geopolitics and AI policy updates",
        weighted_topics={"geopolitics": 0.9, "ai_policy": 0.8},
    )
    print("\n--- Demo report preview ---")
    print(report.splitlines()[0])


if __name__ == "__main__":
    main()
