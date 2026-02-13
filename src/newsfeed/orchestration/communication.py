"""Communication agent — bridges Telegram bot and engine for end-to-end interaction.

The communication agent (Layer 0 in the vision) receives user requests via Telegram,
dispatches them through the engine, delivers results, and closes the feedback loop.
It is the single point of integration between the user-facing bot and the backend
intelligence pipeline.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from newsfeed.delivery.bot import TelegramBot, BriefingScheduler
    from newsfeed.orchestration.engine import NewsFeedEngine

log = logging.getLogger(__name__)

# Fallback topic weights — prefer engine's pipeline config default_topics
_FALLBACK_TOPICS = {
    "geopolitics": 0.8,
    "ai_policy": 0.7,
    "technology": 0.6,
    "markets": 0.5,
}


class CommunicationAgent:
    """Telegram-facing communication interface.

    Owns the full interaction loop:
    1. Receive user message via TelegramBot
    2. Parse command/intent
    3. Dispatch to engine (research cycle, feedback, cache lookup)
    4. Format and deliver response
    5. Process feedback and update preferences

    This is the glue between TelegramBot (low-level API) and
    NewsFeedEngine (intelligence pipeline).
    """

    agent_id = "communication_agent"

    def __init__(self, engine: NewsFeedEngine, bot: TelegramBot,
                 scheduler: BriefingScheduler | None = None,
                 default_topics: dict[str, float] | None = None) -> None:
        self._engine = engine
        self._bot = bot
        self._scheduler = scheduler
        self._default_topics = default_topics or _FALLBACK_TOPICS
        # Track items shown per user for "show more" dedup
        self._shown_ids: dict[str, set[str]] = {}
        # Track last briefing topic per user
        self._last_topic: dict[str, str] = {}

    def handle_update(self, update: dict) -> dict[str, Any] | None:
        """Process a single Telegram update end-to-end.

        Returns a result dict for testing/logging, or None if update was ignored.
        """
        parsed = self._bot.parse_command(update)
        if not parsed:
            return None

        cmd_type = parsed["type"]
        chat_id = parsed["chat_id"]
        user_id = parsed["user_id"]
        command = parsed["command"]
        args = parsed["args"]
        text = parsed["text"]

        if not chat_id or not user_id:
            return None

        log.info("Processing %s from user=%s: cmd=%s args=%r", cmd_type, user_id, command, args)

        try:
            if cmd_type == "command":
                return self._handle_command(chat_id, user_id, command, args)

            if cmd_type == "preference":
                return self._handle_preference(chat_id, user_id, command)

            if cmd_type == "feedback":
                return self._handle_feedback(chat_id, user_id, text)

            if cmd_type == "mute":
                return self._handle_mute(chat_id, user_id, args)

        except Exception:
            log.exception("Error handling update for user=%s", user_id)
            self._bot.send_message(chat_id, "Something went wrong. Please try again.")
            return {"action": "error", "user_id": user_id}

        return None

    def _handle_command(self, chat_id: int | str, user_id: str,
                        command: str, args: str) -> dict[str, Any]:
        """Route a slash command to the appropriate handler."""

        if command == "briefing":
            return self._run_briefing(chat_id, user_id, args)

        if command == "more":
            return self._show_more(chat_id, user_id, args)

        if command in ("feedback", ""):
            # "/feedback more geopolitics" or callback without command
            if args:
                return self._handle_feedback(chat_id, user_id, args)
            self._bot.send_message(
                chat_id,
                "Usage: /feedback <instruction>\n"
                "Example: more geopolitics, less crypto, tone: analyst"
            )
            return {"action": "feedback_help", "user_id": user_id}

        if command == "settings":
            return self._show_settings(chat_id, user_id)

        if command == "status":
            return self._show_status(chat_id, user_id)

        if command == "topics":
            return self._show_topics(chat_id, user_id)

        if command == "schedule":
            return self._set_schedule(chat_id, user_id, args)

        if command == "help":
            self._bot.send_message(chat_id, self._bot.format_help())
            return {"action": "help", "user_id": user_id}

        if command == "deep_dive":
            return self._run_briefing(chat_id, user_id, args, deep=True)

        # Unknown command
        self._bot.send_message(chat_id, f"Unknown command: /{command}. Try /help")
        return {"action": "unknown_command", "user_id": user_id, "command": command}

    def _run_briefing(self, chat_id: int | str, user_id: str,
                      topic_hint: str = "", deep: bool = False) -> dict[str, Any]:
        """Run a full research cycle and deliver the briefing."""
        profile = self._engine.preferences.get_or_create(user_id)

        # Build topic weights from profile, with optional topic hint boost
        topics = dict(profile.topic_weights) if profile.topic_weights else dict(self._default_topics)
        if topic_hint:
            topic_key = topic_hint.strip().lower().replace(" ", "_")
            topics[topic_key] = min(1.0, topics.get(topic_key, 0.5) + 0.3)

        prompt = topic_hint or "Generate intelligence briefing"
        max_items = profile.max_items
        if deep:
            max_items = min(max_items + 5, 20)  # Deep dives get more items

        # Run the intelligence pipeline
        formatted = self._engine.handle_request(
            user_id=user_id,
            prompt=prompt,
            weighted_topics=topics,
            max_items=max_items,
        )

        # Track shown items for "more" dedup
        self._shown_ids.setdefault(user_id, set())
        dominant_topic = max(topics, key=topics.get, default="general")
        self._last_topic[user_id] = dominant_topic

        # Deliver via bot
        self._bot.send_briefing(chat_id, formatted)

        log.info("Briefing delivered to user=%s chat=%s", user_id, chat_id)
        return {"action": "briefing", "user_id": user_id, "topic": dominant_topic}

    def _show_more(self, chat_id: int | str, user_id: str,
                   topic_hint: str = "") -> dict[str, Any]:
        """Show more items from cache, or run a new focused cycle."""
        topic = topic_hint.strip().lower().replace(" ", "_") if topic_hint else self._last_topic.get(user_id, "general")
        seen = self._shown_ids.get(user_id, set())

        more = self._engine.show_more(user_id, topic, seen, limit=5)

        if more:
            text = f"More on {topic}:\n\n" + "\n".join(f"- {item}" for item in more)
            self._bot.send_message(chat_id, text)
            return {"action": "show_more", "user_id": user_id, "count": len(more)}

        # Cache empty — run a fresh mini-cycle
        self._bot.send_message(chat_id, f"Searching for more on {topic}...")
        return self._run_briefing(chat_id, user_id, topic_hint=topic)

    def _handle_feedback(self, chat_id: int | str, user_id: str,
                         text: str) -> dict[str, Any]:
        """Apply user feedback to preferences and acknowledge."""
        results = self._engine.apply_user_feedback(user_id, text)

        if results:
            changes = ", ".join(f"{k}={v}" for k, v in results.items())
            self._bot.send_message(chat_id, f"Preferences updated: {changes}")
        else:
            self._bot.send_message(
                chat_id,
                "I didn't catch a preference change. Try:\n"
                "• more [topic] / less [topic]\n"
                "• tone: analyst\n"
                "• format: sections\n"
                "• max: 15"
            )

        return {"action": "feedback", "user_id": user_id, "changes": results}

    def _handle_preference(self, chat_id: int | str, user_id: str,
                           pref_command: str) -> dict[str, Any]:
        """Handle inline keyboard preference callbacks."""
        if pref_command == "more_similar":
            # Boost the dominant topic from last briefing
            topic = self._last_topic.get(user_id, "general")
            results = self._engine.apply_user_feedback(user_id, f"more {topic}")
            self._bot.send_message(chat_id, f"Got it — boosting {topic} in future briefings.")
            return {"action": "pref_more", "user_id": user_id, "topic": topic}

        if pref_command == "less_similar":
            topic = self._last_topic.get(user_id, "general")
            results = self._engine.apply_user_feedback(user_id, f"less {topic}")
            self._bot.send_message(chat_id, f"Understood — reducing {topic} weight.")
            return {"action": "pref_less", "user_id": user_id, "topic": topic}

        return {"action": "pref_unknown", "user_id": user_id, "command": pref_command}

    def _handle_mute(self, chat_id: int | str, user_id: str,
                     duration: str) -> dict[str, Any]:
        """Handle mute requests from breaking alert keyboard."""
        self._bot.send_message(chat_id, f"Breaking alerts muted for {duration} minutes.")
        return {"action": "mute", "user_id": user_id, "duration": duration}

    def _show_settings(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display user settings."""
        profile = self._engine.preferences.get_or_create(user_id)
        profile_dict = {
            "tone": profile.tone,
            "format": profile.format,
            "max_items": profile.max_items,
            "cadence": profile.briefing_cadence,
            "topic_weights": dict(profile.topic_weights),
            "regions": list(profile.regions_of_interest),
        }
        self._bot.send_message(chat_id, self._bot.format_settings(profile_dict))
        return {"action": "settings", "user_id": user_id}

    def _show_status(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display system status."""
        status = self._engine.engine_status()
        self._bot.send_message(chat_id, self._bot.format_status(status))
        return {"action": "status", "user_id": user_id}

    def _show_topics(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display available topics and user's current weights."""
        profile = self._engine.preferences.get_or_create(user_id)
        all_topics = sorted(set(
            list(profile.topic_weights.keys()) + list(self._default_topics.keys())
        ))

        lines = ["<b>Available Topics & Your Weights</b>", ""]
        for topic in all_topics:
            weight = profile.topic_weights.get(topic, 0.0)
            bar = "█" * max(1, int(abs(weight) * 10))
            sign = "+" if weight > 0 else "" if weight == 0 else ""
            lines.append(f"  {topic}: {sign}{weight:.1f} {bar}")

        lines.extend(["", "Adjust with: /feedback more [topic] or less [topic]"])
        self._bot.send_message(chat_id, "\n".join(lines))
        return {"action": "topics", "user_id": user_id}

    def _set_schedule(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Set briefing schedule."""
        if not self._scheduler:
            self._bot.send_message(chat_id, "Scheduling not available in current configuration.")
            return {"action": "schedule_unavailable", "user_id": user_id}

        parts = args.strip().split(maxsplit=1)
        schedule_type = parts[0].lower() if parts else "morning"
        time_str = parts[1] if len(parts) > 1 else ""

        valid_types = {"morning", "evening", "realtime", "off"}
        if schedule_type not in valid_types:
            self._bot.send_message(
                chat_id,
                f"Usage: /schedule [morning|evening|realtime|off] [HH:MM]\n"
                f"Example: /schedule morning 07:30"
            )
            return {"action": "schedule_help", "user_id": user_id}

        msg = self._scheduler.set_schedule(user_id, schedule_type, time_str)
        self._bot.send_message(chat_id, msg)
        return {"action": "schedule", "user_id": user_id, "type": schedule_type}

    def run_scheduled_briefings(self) -> int:
        """Check for and deliver any scheduled briefings that are due.

        Returns the number of briefings sent. Called by the main event loop.
        """
        if not self._scheduler:
            return 0

        due_users = self._scheduler.get_due_users()
        sent = 0
        for user_id in due_users:
            try:
                # Use user_id as chat_id for direct messages
                self._run_briefing(user_id, user_id)
                sent += 1
            except Exception:
                log.exception("Failed to deliver scheduled briefing to user=%s", user_id)

        return sent
