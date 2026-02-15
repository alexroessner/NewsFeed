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


def _send_error_to_chat(update: dict, token: str, error_msg: str) -> None:
    """Best-effort: notify user that processing failed."""
    import traceback
    import urllib.request
    chat_id = None
    try:
        msg = update.get("message") or update.get("callback_query", {}).get("message")
        if msg:
            chat_id = msg.get("chat", {}).get("id")
    except Exception:
        pass
    if not chat_id or not token:
        return
    try:
        safe_msg = error_msg[:300].replace("<", "&lt;").replace(">", "&gt;")
        payload = json.dumps({
            "chat_id": chat_id,
            "text": f"Something went wrong processing your message.\n\n<code>{safe_msg}</code>",
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Best effort


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

    log.info("TELEGRAM_UPDATE length=%d, first 200 chars: %s", len(raw), raw[:200])

    try:
        update = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error("Invalid JSON: %s", e)
        sys.exit(1)

    log.info("Parsed update keys: %s", list(update.keys()) if isinstance(update, dict) else type(update))

    # Locate config — works both locally and in GH Actions checkout
    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    if not config_dir.is_dir():
        # Fallback: maybe we're running from repo root
        config_dir = Path("config")

    # Override secrets from environment variables (GH Actions secrets)
    _inject_env_secrets(config_dir)

    # Log which secrets were injected (names only, not values)
    secrets_path = config_dir / "secrets.json"
    if secrets_path.exists():
        try:
            sdata = json.loads(secrets_path.read_text())
            log.info("Secrets available: %s", [k for k, v in sdata.items() if v])
        except Exception:
            pass

    try:
        cfg = load_runtime_config(config_dir)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    log.info("Config loaded. api_keys present: %s",
             [k for k in cfg.pipeline.get("api_keys", {}) if cfg.pipeline["api_keys"][k]])

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    try:
        personas_dir = root / "config" / "personas"
        engine = NewsFeedEngine(
            config=cfg.agents,
            pipeline=cfg.pipeline,
            personas=cfg.personas,
            personas_dir=personas_dir,
        )
    except Exception as e:
        log.exception("Engine initialization failed")
        _send_error_to_chat(update, token, "Service temporarily unavailable. Please try again later.")
        sys.exit(1)

    if engine._comm_agent is None:
        log.error("No Telegram bot token configured — cannot process update")
        has_token = bool(cfg.pipeline.get("api_keys", {}).get("telegram_bot_token"))
        log.error("telegram_bot_token present in config: %s", has_token)
        _send_error_to_chat(update, token, "Bot token not found in config")
        sys.exit(1)

    try:
        result = engine._comm_agent.handle_update(update)
    except Exception as e:
        log.exception("handle_update failed")
        _send_error_to_chat(update, token, "Something went wrong. Please try again.")
        sys.exit(1)

    if result:
        log.info("Handled: %s", result)
    else:
        log.info("Update ignored (no actionable content)")


def _inject_env_secrets(config_dir: Path) -> None:
    """Write a temporary secrets.json from environment variables.

    In GitHub Actions, secrets are passed as env vars. This bridges them
    into the config system that expects secrets.json.
    """
    log = logging.getLogger("newsfeed.run_command")
    secrets_path = config_dir / "secrets.json"
    if secrets_path.exists():
        log.info("Local secrets.json exists, skipping env injection")
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
        val = os.environ.get(env_key, "").strip()
        if val:
            secrets[config_key] = val
            log.info("Env secret found: %s -> %s (%d chars)", env_key, config_key, len(val))
        else:
            log.warning("Env secret MISSING: %s", env_key)

    if secrets:
        secrets_path.write_text(json.dumps(secrets, indent=2))
        log.info("Wrote secrets.json with %d keys", len(secrets))
    else:
        log.error("No secrets found in environment — bot will not work")


if __name__ == "__main__":
    main()
