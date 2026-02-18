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

    def get_last_items(self, user_id: str) -> list[dict]:
        """Get last briefing items for a user, loading from D1 via engine if needed."""
        items = self.last_items.get(user_id)
        if items is not None:
            return items
        loaded = self.engine.last_briefing_items(user_id)
        if loaded:
            self.last_items[user_id] = loaded
        return loaded

    def persist_prefs(self, chat_id: int | str | None = None) -> bool:
        """Persist preferences immediately. Returns True on success.

        If chat_id is provided and persistence fails, notifies the user.
        """
        try:
            self.engine.persist_preferences()
            return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Failed to persist preferences")
            if chat_id is not None:
                try:
                    self.bot.send_message(
                        chat_id,
                        "\u26a0\ufe0f Your change was applied but could not be saved to disk. "
                        "It may be lost if the system restarts. Please try again."
                    )
                except Exception:
                    pass
            return False
