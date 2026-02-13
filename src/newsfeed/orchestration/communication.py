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

from newsfeed.delivery.market import MarketTicker

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
        self._market = MarketTicker()
        # Track items shown per user for "show more" dedup
        self._shown_ids: dict[str, set[str]] = {}
        # Track last briefing topic per user
        self._last_topic: dict[str, str] = {}
        # Track last briefing items per user for per-item feedback
        self._last_items: dict[str, list[dict]] = {}  # user_id -> [{topic, source}, ...]

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

            if cmd_type == "rate":
                return self._handle_rate(chat_id, user_id, command)

        except Exception:
            log.exception("Error handling update for user=%s", user_id)
            self._bot.send_message(chat_id, "Something went wrong. Please try again.")
            return {"action": "error", "user_id": user_id}

        return None

    def _handle_command(self, chat_id: int | str, user_id: str,
                        command: str, args: str) -> dict[str, Any]:
        """Route a slash command to the appropriate handler."""

        if command == "start":
            return self._onboard(chat_id, user_id)

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

        if command == "watchlist":
            return self._set_watchlist(chat_id, user_id, args)

        if command == "timezone":
            return self._set_timezone(chat_id, user_id, args)

        if command == "mute":
            return self._mute_topic(chat_id, user_id, args)

        if command == "unmute":
            return self._unmute_topic(chat_id, user_id, args)

        if command == "rate_prompt":
            return self._send_rate_prompt(chat_id, user_id)

        if command == "reset":
            self._engine.preferences.reset(user_id)
            self._engine.apply_user_feedback(user_id, "reset preferences")
            self._shown_ids.pop(user_id, None)
            self._last_items.pop(user_id, None)
            self._last_topic.pop(user_id, None)
            self._bot.send_message(chat_id, "All preferences reset to defaults.")
            return {"action": "reset", "user_id": user_id}

        # Unknown command
        self._bot.send_message(chat_id, f"Unknown command: /{command}. Try /help")
        return {"action": "unknown_command", "user_id": user_id, "command": command}

    def _onboard(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Welcome a new user and create their default profile."""
        self._engine.preferences.get_or_create(user_id)
        self._bot.send_message(
            chat_id,
            "<b>\U0001f4e1 Welcome to NewsFeed Intelligence</b>\n\n"
            "Personalized news briefings from 23+ sources "
            "across geopolitics, AI, technology, markets, and more.\n\n"
            "<b>Quick start:</b>\n"
            "\u2022 /briefing \u2014 Get your first briefing\n"
            "\u2022 /topics \u2014 See topics and weights\n"
            "\u2022 /watchlist crypto BTC ETH \u2014 Set market tickers\n"
            "\u2022 /feedback more AI, less crypto \u2014 Customize\n"
            "\u2022 /schedule morning 08:00 \u2014 Set daily delivery\n"
            "\u2022 /help \u2014 Full command list\n\n"
            "Send any text to give feedback (e.g. \"more geopolitics\")."
        )
        return {"action": "onboard", "user_id": user_id}

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

        # Prepend market ticker bar
        try:
            crypto_ids = profile.watchlist_crypto or None
            stock_tickers = profile.watchlist_stocks or None
            market_data = self._market.fetch_all(crypto_ids, stock_tickers)
            all_quotes = market_data.get("crypto", []) + market_data.get("stocks", [])
            if all_quotes:
                ticker_bar = MarketTicker.format_ticker_bar(all_quotes)
                if ticker_bar:
                    formatted = ticker_bar + "\n\n" + formatted
        except Exception:
            log.debug("Market ticker fetch skipped", exc_info=True)

        # Track shown items for "more" dedup
        self._shown_ids.setdefault(user_id, set())
        dominant_topic = max(topics, key=topics.get, default="general")
        self._last_topic[user_id] = dominant_topic

        # Store per-item info for per-item feedback buttons
        self._last_items[user_id] = self._engine.last_briefing_items(user_id)

        # Deliver via bot with clean action buttons
        self._bot.send_briefing(chat_id, formatted)

        log.info("Briefing delivered to user=%s chat=%s", user_id, chat_id)
        return {"action": "briefing", "user_id": user_id, "topic": dominant_topic}

    def _show_more(self, chat_id: int | str, user_id: str,
                   topic_hint: str = "") -> dict[str, Any]:
        """Show more items from cache with HTML formatting, or run a fresh cycle."""
        import html as html_mod
        topic = topic_hint.strip().lower().replace(" ", "_") if topic_hint else self._last_topic.get(user_id, "general")
        seen = self._shown_ids.get(user_id, set())

        more = self._engine.show_more(user_id, topic, seen, limit=5)

        if more:
            lines = [f"<b>More on {html_mod.escape(topic)}:</b>", ""]
            for c in more:
                title_esc = html_mod.escape(c.title)
                if c.url and not c.url.startswith("https://example.com"):
                    lines.append(f'\u2022 <a href="{html_mod.escape(c.url)}">{title_esc}</a> [{html_mod.escape(c.source)}]')
                else:
                    lines.append(f"\u2022 {title_esc} [{html_mod.escape(c.source)}]")
                if c.summary:
                    lines.append(f"  <i>{html_mod.escape(c.summary[:120])}</i>")
                # Track as seen
                self._shown_ids.setdefault(user_id, set()).add(c.candidate_id)
            self._bot.send_message(chat_id, "\n".join(lines))
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
        """Handle inline keyboard preference callbacks.

        Uses the actual topics from the last briefing's items rather
        than just the dominant request topic.
        """
        briefing_topics = self._engine.last_briefing_topics(user_id)
        # Fallback to tracked dominant topic if engine has nothing
        if not briefing_topics:
            fallback = self._last_topic.get(user_id, "general")
            briefing_topics = [fallback]

        if pref_command == "more_similar":
            # Boost ALL topics that appeared in the last briefing
            adjustments = []
            for topic in briefing_topics:
                self._engine.apply_user_feedback(user_id, f"more {topic}")
                adjustments.append(topic)
            topic_list = ", ".join(adjustments)
            self._bot.send_message(chat_id, f"Got it — boosting {topic_list} in future briefings.")
            return {"action": "pref_more", "user_id": user_id, "topics": adjustments}

        if pref_command == "less_similar":
            # Reduce ALL topics from the last briefing
            adjustments = []
            for topic in briefing_topics:
                self._engine.apply_user_feedback(user_id, f"less {topic}")
                adjustments.append(topic)
            topic_list = ", ".join(adjustments)
            self._bot.send_message(chat_id, f"Understood — reducing {topic_list} weight.")
            return {"action": "pref_less", "user_id": user_id, "topics": adjustments}

        return {"action": "pref_unknown", "user_id": user_id, "command": pref_command}

    def _handle_mute(self, chat_id: int | str, user_id: str,
                     duration: str) -> dict[str, Any]:
        """Handle mute requests from breaking alert keyboard."""
        minutes = 60  # default
        try:
            minutes = int(duration)
        except (ValueError, TypeError):
            pass

        if self._scheduler:
            msg = self._scheduler.mute(user_id, minutes)
            self._bot.send_message(chat_id, msg)
        else:
            self._bot.send_message(chat_id, f"Alerts muted for {minutes} minutes.")

        return {"action": "mute", "user_id": user_id, "duration": str(minutes)}

    def _handle_rate(self, chat_id: int | str, user_id: str,
                     rate_data: str) -> dict[str, Any]:
        """Handle per-item thumbs up/down from inline keyboard.

        rate_data format: "rate:N:up" or "rate:N:down"
        """
        parts = rate_data.split(":")
        if len(parts) != 3:
            return {"action": "rate_error", "user_id": user_id}

        try:
            item_num = int(parts[1])
        except ValueError:
            return {"action": "rate_error", "user_id": user_id}

        direction = parts[2].lower()
        items = self._last_items.get(user_id, [])

        if item_num < 1 or item_num > len(items):
            self._bot.send_message(chat_id, "That item is no longer available.")
            return {"action": "rate_expired", "user_id": user_id}

        item = items[item_num - 1]
        topic = item["topic"]

        if direction == "up":
            self._engine.apply_user_feedback(user_id, f"more {topic}")
            if item.get("source"):
                self._engine.preferences.apply_source_weight(user_id, item["source"], 0.3)
            self._bot.send_message(chat_id, f"\U0001f44d Boosted {topic} (story #{item_num})")
        elif direction == "down":
            self._engine.apply_user_feedback(user_id, f"less {topic}")
            if item.get("source"):
                self._engine.preferences.apply_source_weight(user_id, item["source"], -0.3)
            self._bot.send_message(chat_id, f"\U0001f44e Reduced {topic} (story #{item_num})")
        else:
            return {"action": "rate_error", "user_id": user_id}

        return {"action": "rate", "user_id": user_id, "item": item_num, "direction": direction}

    def _show_settings(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display user settings including schedule info."""
        profile = self._engine.preferences.get_or_create(user_id)
        schedule_info = ""
        if self._scheduler:
            sched = self._scheduler._schedules.get(user_id)
            if sched:
                schedule_info = f"{sched['type']}" + (f" at {sched['time']} UTC" if sched.get("time") else "")
            else:
                schedule_info = "not set"
        profile_dict = {
            "tone": profile.tone,
            "format": profile.format,
            "max_items": profile.max_items,
            "cadence": profile.briefing_cadence,
            "schedule": schedule_info,
            "topic_weights": dict(profile.topic_weights),
            "source_weights": dict(profile.source_weights),
            "regions": list(profile.regions_of_interest),
            "timezone": profile.timezone,
            "watchlist_crypto": list(profile.watchlist_crypto),
            "watchlist_stocks": list(profile.watchlist_stocks),
            "muted_topics": list(profile.muted_topics),
        }
        self._bot.send_message(chat_id, self._bot.format_settings(profile_dict))
        return {"action": "settings", "user_id": user_id}

    def _show_status(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display system status."""
        status = self._engine.engine_status()
        self._bot.send_message(chat_id, self._bot.format_status(status))
        return {"action": "status", "user_id": user_id}

    def _show_topics(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Display available topics with effective weights (defaults + user overrides)."""
        profile = self._engine.preferences.get_or_create(user_id)

        # Build effective weights: start from defaults, overlay user's customizations
        effective = dict(self._default_topics)
        for topic, weight in profile.topic_weights.items():
            effective[topic] = weight

        all_topics = sorted(effective.keys(), key=lambda t: effective.get(t, 0), reverse=True)

        lines = ["<b>Available Topics &amp; Effective Weights</b>", ""]
        for topic in all_topics:
            weight = effective[topic]
            is_custom = topic in profile.topic_weights
            bar = "\u2588" * max(1, int(abs(weight) * 10))
            sign = "+" if weight > 0 else "" if weight == 0 else ""
            custom_tag = " (custom)" if is_custom else " (default)"
            lines.append(f"  {topic}: {sign}{weight:.1f} {bar}{custom_tag}")

        # Show source weights if any
        if profile.source_weights:
            lines.append("")
            lines.append("<b>Source Preferences:</b>")
            for src, sw in sorted(profile.source_weights.items(), key=lambda x: -x[1]):
                label = "boosted" if sw > 0 else "demoted"
                lines.append(f"  {src}: {label} ({sw:+.1f})")

        lines.extend([
            "",
            "Adjust with: /feedback more [topic] or less [topic]",
            "Sources: /feedback prefer [source] or demote [source]",
            "Reset: /feedback reset preferences",
        ])
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

    def _persist_prefs(self) -> None:
        """Persist preferences immediately."""
        if self._engine._persistence:
            try:
                self._engine._persistence.save("preferences", self._engine.preferences.snapshot())
            except Exception:
                log.exception("Failed to persist preferences")

    def _set_watchlist(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Set crypto or stock watchlist."""
        import html as html_mod
        if not args.strip():
            profile = self._engine.preferences.get_or_create(user_id)
            crypto = ", ".join(c.upper() for c in profile.watchlist_crypto) or "default (BTC, ETH, SOL)"
            stocks = ", ".join(profile.watchlist_stocks) or "default (SPY, QQQ)"
            self._bot.send_message(
                chat_id,
                f"<b>Your Market Watchlist</b>\n\n"
                f"Crypto: {html_mod.escape(crypto)}\n"
                f"Stocks: {html_mod.escape(stocks)}\n\n"
                f"<b>Set with:</b>\n"
                f"/watchlist crypto bitcoin ethereum solana\n"
                f"/watchlist stocks AAPL MSFT SPY"
            )
            return {"action": "watchlist_show", "user_id": user_id}

        parts = args.strip().split(maxsplit=1)
        category = parts[0].lower()
        tickers = parts[1].split() if len(parts) > 1 else []

        if category == "crypto" and tickers:
            cleaned = [t.lower() for t in tickers]
            self._engine.preferences.set_watchlist(user_id, crypto=cleaned)
            self._persist_prefs()
            self._bot.send_message(chat_id, f"Crypto watchlist: {', '.join(t.upper() for t in cleaned)}")
            return {"action": "watchlist_crypto", "user_id": user_id}

        if category == "stocks" and tickers:
            cleaned = [t.upper() for t in tickers]
            self._engine.preferences.set_watchlist(user_id, stocks=cleaned)
            self._persist_prefs()
            self._bot.send_message(chat_id, f"Stock watchlist: {', '.join(cleaned)}")
            return {"action": "watchlist_stocks", "user_id": user_id}

        self._bot.send_message(
            chat_id,
            "Usage:\n/watchlist crypto bitcoin ethereum solana\n/watchlist stocks AAPL MSFT SPY"
        )
        return {"action": "watchlist_help", "user_id": user_id}

    def _set_timezone(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Set user timezone."""
        tz = args.strip()
        if not tz:
            profile = self._engine.preferences.get_or_create(user_id)
            self._bot.send_message(
                chat_id,
                f"Current timezone: <code>{profile.timezone}</code>\n"
                f"Set with: /timezone US/Eastern"
            )
            return {"action": "timezone_show", "user_id": user_id}

        self._engine.preferences.set_timezone(user_id, tz)
        self._persist_prefs()
        self._bot.send_message(chat_id, f"Timezone set to <code>{tz}</code>")
        return {"action": "timezone", "user_id": user_id, "tz": tz}

    def _mute_topic(self, chat_id: int | str, user_id: str,
                    args: str) -> dict[str, Any]:
        """Mute a topic from future briefings."""
        topic = args.strip().lower().replace(" ", "_")
        if not topic:
            profile = self._engine.preferences.get_or_create(user_id)
            muted = ", ".join(profile.muted_topics) or "none"
            self._bot.send_message(
                chat_id,
                f"Muted topics: <code>{muted}</code>\n"
                f"Mute with: /mute crypto"
            )
            return {"action": "mute_show", "user_id": user_id}

        self._engine.preferences.mute_topic(user_id, topic)
        self._persist_prefs()
        self._bot.send_message(chat_id, f"Topic <code>{topic}</code> muted. /unmute {topic} to reverse.")
        return {"action": "mute_topic", "user_id": user_id, "topic": topic}

    def _unmute_topic(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Unmute a previously muted topic."""
        topic = args.strip().lower().replace(" ", "_")
        if not topic:
            self._bot.send_message(chat_id, "Usage: /unmute [topic]")
            return {"action": "unmute_help", "user_id": user_id}

        self._engine.preferences.unmute_topic(user_id, topic)
        self._persist_prefs()
        self._bot.send_message(chat_id, f"Topic <code>{topic}</code> unmuted.")
        return {"action": "unmute_topic", "user_id": user_id, "topic": topic}

    def _send_rate_prompt(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Send a compact per-item rating prompt on demand."""
        import html as html_mod
        items = self._last_items.get(user_id, [])
        if not items:
            self._bot.send_message(chat_id, "No recent briefing to rate. Run /briefing first.")
            return {"action": "rate_no_items", "user_id": user_id}

        lines = ["\u2b50 <b>Rate Your Stories</b>", ""]
        for i, item in enumerate(items, 1):
            title = html_mod.escape(item.get("title", item.get("topic", f"Story {i}")))
            lines.append(f"  {i}. {title}")
        lines.append("")
        lines.append("Tap \U0001f44d to boost or \U0001f44e to reduce similar stories:")

        # Compact layout: two stories per row of buttons
        rows: list[list[dict]] = []
        for i in range(0, len(items), 2):
            row: list[dict] = []
            for j in range(i, min(i + 2, len(items))):
                num = j + 1
                row.append({"text": f"\U0001f44d{num}", "callback_data": f"rate:{num}:up"})
                row.append({"text": f"\U0001f44e{num}", "callback_data": f"rate:{num}:down"})
            rows.append(row)

        keyboard = {"inline_keyboard": rows}
        self._bot.send_message(chat_id, "\n".join(lines), reply_markup=keyboard)
        return {"action": "rate_prompt", "user_id": user_id}
