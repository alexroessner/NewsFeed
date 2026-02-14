"""Run scheduled briefings for all users with active schedules.

Usage:
    python -m newsfeed.run_scheduled

Called by the GitHub Actions cron workflow. Checks all user profiles
for pending scheduled briefings and delivers them.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from newsfeed.models.config import ConfigError, load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger("newsfeed")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> None:
    setup_logging()
    log = logging.getLogger("newsfeed.run_scheduled")

    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    if not config_dir.is_dir():
        config_dir = Path("config")

    # Inject secrets from env vars (GH Actions)
    _inject_env_secrets(config_dir)

    try:
        cfg = load_runtime_config(config_dir)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    personas_dir = root / "config" / "personas"
    engine = NewsFeedEngine(
        config=cfg.agents,
        pipeline=cfg.pipeline,
        personas=cfg.personas,
        personas_dir=personas_dir,
    )

    if engine._comm_agent is None:
        log.error("No Telegram bot token — cannot send scheduled briefings")
        sys.exit(1)

    sent = engine._comm_agent.run_scheduled_briefings()
    log.info("Scheduled briefings sent: %d", sent)

    # If no schedules configured yet, send a default briefing to the owner
    # (useful for initial testing)
    owner_id = os.environ.get("TELEGRAM_OWNER_ID", "")
    if sent == 0 and owner_id:
        log.info("No schedules active — sending default briefing to owner %s", owner_id)
        profile = engine.preferences.get_or_create(owner_id)
        if not profile.topic_weights:
            profile.topic_weights = {"geopolitics": 0.8, "technology": 0.7, "markets": 0.5}
        engine._comm_agent._run_briefing(int(owner_id), owner_id, "geopolitics technology")
        log.info("Default briefing sent to %s", owner_id)


def _inject_env_secrets(config_dir: Path) -> None:
    """Write a temporary secrets.json from environment variables."""
    secrets_path = config_dir / "secrets.json"
    if secrets_path.exists():
        return

    env_map = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "gemini_api_key": "GEMINI_API_KEY",
        "guardian": "GUARDIAN_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "x_bearer_token": "X_BEARER_TOKEN",
    }

    secrets = {}
    for config_key, env_key in env_map.items():
        val = os.environ.get(env_key, "").strip()
        if val:
            secrets[config_key] = val

    if secrets:
        secrets_path.write_text(json.dumps(secrets, indent=2))


if __name__ == "__main__":
    main()
