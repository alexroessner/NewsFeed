from __future__ import annotations

import logging
import sys
from pathlib import Path

from newsfeed.models.config import ConfigError, load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine

log = logging.getLogger("newsfeed")


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger("newsfeed")
    root.setLevel(level)
    root.addHandler(handler)


def main() -> None:
    setup_logging()

    root = Path(__file__).resolve().parents[3]
    config_dir = root / "config"
    personas_dir = root / "personas"

    try:
        cfg = load_runtime_config(config_dir)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    stages = cfg.pipeline.get("stages", [])
    research_agents = cfg.agents.get("research_agents", [])
    enabled_stages = cfg.pipeline.get("intelligence", {}).get("enabled_stages", [])
    config_version = cfg.pipeline.get("version", "unknown")

    log.info("NewsFeed bootstrap (config v%s)", config_version)
    log.info("Loaded stages: %d", len(stages))
    log.info("Loaded research agents: %d", len(research_agents))
    log.info("Intelligence stages: %s", ", ".join(enabled_stages) or "all defaults")

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
