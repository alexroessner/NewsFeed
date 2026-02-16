from __future__ import annotations

import atexit
import logging
import signal
import sys
import time
from pathlib import Path

from newsfeed.models.config import ConfigError, load_runtime_config
from newsfeed.orchestration.engine import NewsFeedEngine

log = logging.getLogger("newsfeed")

# Graceful shutdown flag
_shutdown = False

# Hot reload flag
_reload = False


def _handle_signal(signum: int, frame: object) -> None:
    global _shutdown
    log.info("Received signal %d — shutting down gracefully", signum)
    _shutdown = True


def _handle_reload(signum: int, frame: object) -> None:
    global _reload
    log.info("Received SIGHUP — will reload configuration")
    _reload = True


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

    # ── Startup status dashboard ──────────────────────────────────
    api_keys = cfg.pipeline.get("api_keys", {})
    real_agents = []
    simulated_agents = []
    from newsfeed.agents.registry import create_agent
    for a in research_agents:
        agent = create_agent(a, api_keys)
        if type(agent).__name__ == "SimulatedResearchAgent":
            simulated_agents.append(a.get("id", "?"))
        else:
            real_agents.append(a.get("id", "?"))
    db_backend = engine.analytics.backend if hasattr(engine.analytics, 'backend') else "unknown"
    missing_keys = []
    for key_name in ("guardian", "newsapi", "x_bearer_token", "reddit_client_id", "anthropic_api_key"):
        if not api_keys.get(key_name):
            missing_keys.append(key_name.upper())
    log.info("=" * 60)
    log.info("STARTUP STATUS")
    log.info("  Agents: %d real, %d simulated", len(real_agents), len(simulated_agents))
    if simulated_agents:
        log.info("  Simulated (missing keys): %s", ", ".join(simulated_agents[:5]))
    if missing_keys:
        log.info("  Missing API keys: %s", ", ".join(missing_keys))
    log.info("  Database: %s", db_backend)
    log.info("  Telegram: %s", "connected" if engine.is_telegram_connected() else "not configured")
    log.info("  LLM-backed experts: %s", "yes" if engine.is_llm_backed() else "no (heuristic only)")
    log.info("  Intelligence stages: %d/%d enabled", len(enabled_stages), 7)
    log.info("=" * 60)

    # If Telegram bot is configured, start the polling loop
    if engine.is_telegram_connected():
        log.info("Telegram bot configured — starting polling loop")
        _run_bot_loop(engine, config_dir)
    else:
        # No Telegram token — run a demo cycle
        log.info("No Telegram token — running demo cycle")
        report = engine.handle_request(
            user_id="demo-user",
            prompt="Give me high-signal geopolitics and AI policy updates",
            weighted_topics={"geopolitics": 0.9, "ai_policy": 0.8},
        )
        print("\n--- Demo report preview ---")
        print(report.splitlines()[0])
        print(f"\nFull report: {len(report)} chars, {len(report.splitlines())} lines")
        print("\nTo enable Telegram delivery, set api_keys.telegram_bot_token in config/pipelines.json")


def _run_bot_loop(engine: NewsFeedEngine, config_dir: Path) -> None:
    """Main Telegram polling loop with scheduled briefing support."""
    bot = engine.get_bot()
    comm = engine.get_comm_agent()
    scheduler_check_interval = 60  # Check for scheduled briefings every 60s
    last_scheduler_check = 0.0

    # Register bot commands with Telegram
    bot.set_commands()
    me = bot.get_me()
    if me:
        log.info("Bot online: @%s (%s)", me.get("username", "?"), me.get("first_name", "?"))
    else:
        log.warning("Could not verify bot token — check api_keys.telegram_bot_token")
        return

    # Graceful shutdown on SIGINT/SIGTERM
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, _handle_reload)

    log.info("Polling for updates... (Ctrl+C to stop)")
    while not _shutdown:
        try:
            # Long-poll for new messages (blocks up to 30s)
            updates = bot.get_updates(timeout=30)

            for update in updates:
                try:
                    result = comm.handle_update(update)
                    if result:
                        log.info("Handled: %s", result.get("action", "unknown"))
                except Exception:
                    log.exception("Failed to handle update: %s", update.get("update_id", "?"))

            # Periodically check for scheduled briefings
            now = time.time()
            if now - last_scheduler_check > scheduler_check_interval:
                sent = comm.run_scheduled_briefings()
                if sent:
                    log.info("Delivered %d scheduled briefings", sent)
                last_scheduler_check = now

            # Hot reload config on SIGHUP
            global _reload
            if _reload:
                _reload = False
                log.info("Reloading configuration...")
                try:
                    new_cfg = load_runtime_config(config_dir)
                    # Update scoring config
                    from newsfeed.models.domain import configure_scoring
                    configure_scoring(new_cfg.pipeline.get("scoring", {}))
                    log.info("Configuration reloaded successfully (config v%s)", new_cfg.pipeline.get("version", "?"))
                except Exception:
                    log.exception("Config reload failed — continuing with previous config")

        except Exception:
            log.exception("Polling loop error — retrying in 5s")
            time.sleep(5)

    # Graceful shutdown: flush pending state
    log.info("Flushing state before shutdown...")
    try:
        engine.persist_preferences()
    except Exception:
        log.exception("Failed to persist preferences during shutdown")
    try:
        if hasattr(engine, 'analytics') and engine.analytics:
            if hasattr(engine.analytics, '_local'):
                conn = getattr(engine.analytics._local, 'conn', None)
                if conn:
                    conn.close()
                    log.info("Analytics DB connection closed")
    except Exception:
        log.exception("Failed to close analytics DB during shutdown")
    log.info("Bot stopped.")


if __name__ == "__main__":
    main()
