"""Process a single Telegram update from a GitHub Actions dispatch.

Usage:
    python -m newsfeed.run_command '{"update_id": ..., "message": {...}}'

Designed to be called from the GitHub Actions workflow that receives
Telegram webhook updates via the Cloudflare Worker bridge.
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
    log = logging.getLogger("newsfeed.run_command")

    # Accept update as CLI arg or env var (GH Actions passes via env)
    raw = None
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        raw = os.environ.get("TELEGRAM_UPDATE")

    if not raw:
        log.error("No update provided. Pass as argument or TELEGRAM_UPDATE env var.")
        sys.exit(1)

    try:
        update = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Invalid JSON: %s", e)
        sys.exit(1)

    # Locate config — works both locally and in GH Actions checkout
    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    if not config_dir.is_dir():
        # Fallback: maybe we're running from repo root
        config_dir = Path("config")

    # Override secrets from environment variables (GH Actions secrets)
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
        log.error("No Telegram bot token configured — cannot process update")
        sys.exit(1)

    result = engine._comm_agent.handle_update(update)
    if result:
        log.info("Handled: %s", result)
    else:
        log.info("Update ignored (no actionable content)")


def _inject_env_secrets(config_dir: Path) -> None:
    """Write a temporary secrets.json from environment variables.

    In GitHub Actions, secrets are passed as env vars. This bridges them
    into the config system that expects secrets.json.
    """
    secrets_path = config_dir / "secrets.json"
    if secrets_path.exists():
        return  # Local secrets file takes priority

    env_map = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "gemini_api_key": "GEMINI_API_KEY",
        "guardian": "GUARDIAN_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "x_bearer_token": "X_BEARER_TOKEN",
    }

    secrets = {}
    for config_key, env_key in env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            secrets[config_key] = val

    if secrets:
        secrets_path.write_text(json.dumps(secrets, indent=2))


if __name__ == "__main__":
    main()
