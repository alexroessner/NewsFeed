"""Command handler modules — splits communication agent into focused groups.

Each module handles a logical group of commands:
- briefing: /briefing, /quick, /sitrep, /deep_dive, /more, /export
- analysis: /diff, /entities, /compare, /recall, /insights, /weekly, /timeline
- management: /track, /untrack, /tracked, /save, /saved, /unsave, /stats,
              /alert, /source, /sources, /filter, /preset

The CommunicationAgent in communication.py remains the router and owns
shared state (rate limits, shown IDs, etc). Handlers receive a HandlerContext
with references to all shared resources.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from newsfeed.delivery.bot import TelegramBot, BriefingScheduler
    from newsfeed.orchestration.engine import NewsFeedEngine
    from newsfeed.memory.store import BoundedUserDict


@dataclass
class HandlerContext:
    """Shared context passed to all command handlers.

    Holds references to engine, bot, and shared state without
    each handler needing to know about CommunicationAgent internals.
    """
    engine: NewsFeedEngine
    bot: TelegramBot
    scheduler: BriefingScheduler | None
    default_topics: dict[str, float]
    shown_ids: BoundedUserDict
    last_topic: BoundedUserDict
    last_items: BoundedUserDict

    def persist_prefs(self) -> None:
        """Persist preferences immediately."""
        try:
            self.engine.persist_preferences()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Failed to persist preferences")
