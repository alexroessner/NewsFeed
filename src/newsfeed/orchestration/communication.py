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
from newsfeed.memory.store import match_tracked

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

        result: dict[str, Any] | None = None
        try:
            if cmd_type == "command":
                result = self._handle_command(chat_id, user_id, command, args)

            elif cmd_type == "preference":
                result = self._handle_preference(chat_id, user_id, command)

            elif cmd_type == "feedback":
                result = self._handle_feedback(chat_id, user_id, text)

            elif cmd_type == "mute":
                result = self._handle_mute(chat_id, user_id, args)

            elif cmd_type == "rate":
                result = self._handle_rate(chat_id, user_id, command)

        except Exception:
            log.exception("Error handling update for user=%s", user_id)
            self._bot.send_message(chat_id, "Something went wrong. Please try again.")
            result = {"action": "error", "user_id": user_id}

        # Analytics: record EVERY interaction
        self._engine.analytics.record_interaction(
            user_id=user_id, chat_id=chat_id,
            interaction_type=cmd_type, command=command,
            args=args, raw_text=text,
            result_action=result.get("action") if result else None,
            result_data=result,
        )

        return result

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
            # If args is a story number, deep dive on that specific story
            try:
                story_num = int(args.strip())
                return self._deep_dive_story(chat_id, user_id, story_num)
            except (ValueError, TypeError):
                pass
            # Otherwise run a fuller briefing
            return self._run_briefing(chat_id, user_id, args, deep=True)

        if command == "compare":
            return self._compare_story(chat_id, user_id, args)

        if command == "recall":
            return self._recall(chat_id, user_id, args)

        if command == "insights":
            return self._show_insights(chat_id, user_id)

        if command == "weekly":
            return self._show_weekly(chat_id, user_id)

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

        if command == "track":
            return self._track_story(chat_id, user_id, args)

        if command == "tracked":
            return self._show_tracked(chat_id, user_id)

        if command == "untrack":
            return self._untrack_story(chat_id, user_id, args)

        if command == "reset":
            self._engine.preferences.reset(user_id)
            self._engine.apply_user_feedback(user_id, "reset preferences")
            self._shown_ids.pop(user_id, None)
            self._last_items.pop(user_id, None)
            self._last_topic.pop(user_id, None)
            self._bot.send_message(chat_id, "All preferences reset to defaults.")
            return {"action": "reset", "user_id": user_id}

        if command == "admin":
            return self._handle_admin(chat_id, user_id, args)

        # Unknown command
        self._bot.send_message(chat_id, f"Unknown command: /{command}. Try /help")
        return {"action": "unknown_command", "user_id": user_id, "command": command}

    def _onboard(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Welcome a new user and create their default profile."""
        self._engine.preferences.get_or_create(user_id)
        self._engine.analytics.record_user_seen(user_id, chat_id)
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
        """Run a full research cycle and deliver as multi-message flow.

        Flow: header (ticker + overview) -> individual story cards -> action buttons
        """
        profile = self._engine.preferences.get_or_create(user_id)

        # Build topic weights from profile, with optional topic hint boost
        topics = dict(profile.topic_weights) if profile.topic_weights else dict(self._default_topics)
        if topic_hint:
            topic_key = topic_hint.strip().lower().replace(" ", "_")
            topics[topic_key] = min(1.0, topics.get(topic_key, 0.5) + 0.3)

        prompt = topic_hint or "Generate intelligence briefing"
        max_items = profile.max_items
        if deep:
            max_items = min(max_items + 5, 20)

        # Run the intelligence pipeline — get structured payload
        payload = self._engine.handle_request_payload(
            user_id=user_id,
            prompt=prompt,
            weighted_topics=topics,
            max_items=max_items,
        )

        # Build market ticker bar
        ticker_bar = ""
        try:
            crypto_ids = profile.watchlist_crypto or None
            stock_tickers = profile.watchlist_stocks or None
            market_data = self._market.fetch_all(crypto_ids, stock_tickers)
            all_quotes = market_data.get("crypto", []) + market_data.get("stocks", [])
            if all_quotes:
                ticker_bar = MarketTicker.format_ticker_bar(all_quotes)
        except Exception:
            log.debug("Market ticker fetch skipped", exc_info=True)

        # Track shown items for "more" dedup
        self._shown_ids.setdefault(user_id, set())
        dominant_topic = max(topics, key=topics.get, default="general")
        self._last_topic[user_id] = dominant_topic
        self._last_items[user_id] = self._engine.last_briefing_items(user_id)

        formatter = self._engine.formatter

        # Messages 2..N: Individual story cards with per-story feedback
        tracked = profile.tracked_stories
        tracked_count = 0
        tracked_flags: list[bool] = []
        for item in payload.items:
            is_tracked = any(
                match_tracked(item.candidate.topic, item.candidate.title, t)
                for t in tracked
            )
            tracked_flags.append(is_tracked)
            if is_tracked:
                tracked_count += 1

        # Message 1: Header (ticker + exec summary + geo risks + trends + threads)
        header = formatter.format_header(payload, ticker_bar, tracked_count=tracked_count)
        self._bot.send_message(chat_id, header)

        for idx, item in enumerate(payload.items, start=1):
            is_tracked = tracked_flags[idx - 1]
            card = formatter.format_story_card(item, idx, is_tracked=is_tracked)
            self._bot.send_story_card(chat_id, card, story_index=idx, is_tracked=is_tracked)

        if not payload.items:
            self._bot.send_message(
                chat_id,
                "No stories matched your current filters. Try /feedback to adjust."
            )

        # Closing message: weightings + action buttons
        if payload.items:
            closing = formatter.format_closing(
                payload,
                topic_weights=dict(profile.topic_weights) if profile.topic_weights else dict(topics),
                source_weights=dict(profile.source_weights) if profile.source_weights else None,
            )
            self._bot.send_closing(chat_id, closing)

        log.info(
            "Multi-message briefing: user=%s chat=%s (%d cards)",
            user_id, chat_id, len(payload.items),
        )
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
                self._engine.analytics.record_preference_change(
                    user_id, "topic_boost", topic, None, "+0.2",
                    source="more_similar_button",
                )
                adjustments.append(topic)
            topic_list = ", ".join(adjustments)
            self._bot.send_message(chat_id, f"Got it — boosting {topic_list} in future briefings.")
            return {"action": "pref_more", "user_id": user_id, "topics": adjustments}

        if pref_command == "less_similar":
            # Reduce ALL topics from the last briefing
            adjustments = []
            for topic in briefing_topics:
                self._engine.apply_user_feedback(user_id, f"less {topic}")
                self._engine.analytics.record_preference_change(
                    user_id, "topic_reduce", topic, None, "-0.2",
                    source="less_similar_button",
                )
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

        # Analytics: record per-item rating
        self._engine.analytics.record_rating(
            user_id, item_num, direction,
            topic=topic, source=item.get("source"),
            title=item.get("title"),
        )

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

        # Ensure scheduler has the user's timezone for local-time scheduling
        profile = self._engine.preferences.get_or_create(user_id)
        self._scheduler.set_user_timezone(user_id, profile.timezone)

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

        # Also check for proactive tracked story updates
        try:
            sent += self.check_tracked_updates()
        except Exception:
            log.exception("Tracked story update check failed")

        # Check for intelligence alerts (geo-risk, trend spikes)
        try:
            sent += self.check_intelligence_alerts()
        except Exception:
            log.exception("Intelligence alert check failed")

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
        # Sync timezone to scheduler so scheduled briefings fire at user-local time
        if self._scheduler:
            self._scheduler.set_user_timezone(user_id, tz)
        self._bot.send_message(chat_id, f"Timezone set to <code>{tz}</code>. Scheduled briefings now use your local time.")
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

    def _track_story(self, chat_id: int | str, user_id: str,
                     args: str) -> dict[str, Any]:
        """Track a story from the last briefing for cross-session continuity."""
        try:
            story_num = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(chat_id, "Usage: tap the \U0001f4cc Track button on a story card.")
            return {"action": "track_help", "user_id": user_id}

        items = self._last_items.get(user_id, [])
        if story_num < 1 or story_num > len(items):
            self._bot.send_message(chat_id, "That story is no longer available. Run /briefing first.")
            return {"action": "track_expired", "user_id": user_id}

        item = items[story_num - 1]
        topic = item["topic"]
        headline = item["title"]

        self._engine.preferences.track_story(user_id, topic, headline)
        self._persist_prefs()
        self._bot.send_message(
            chat_id,
            f"\U0001f4cc Now tracking: <b>{headline}</b>\n"
            f"You'll see \U0001f4cc badges when new developments appear in future briefings.\n"
            f"View tracked stories: /tracked"
        )
        return {"action": "track", "user_id": user_id, "story": story_num}

    def _show_tracked(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Show all stories the user is currently tracking."""
        import html as html_mod
        profile = self._engine.preferences.get_or_create(user_id)
        tracked = profile.tracked_stories

        if not tracked:
            self._bot.send_message(
                chat_id,
                "You're not tracking any stories yet.\n"
                "Tap \U0001f4cc Track on a story card to follow it across briefings."
            )
            return {"action": "tracked_empty", "user_id": user_id}

        lines = [f"<b>\U0001f4cc Tracked Stories ({len(tracked)})</b>", ""]
        for i, t in enumerate(tracked, 1):
            topic = t["topic"].replace("_", " ").title()
            headline = html_mod.escape(t["headline"][:80])
            lines.append(f"  {i}. <b>{headline}</b>")
            lines.append(f"     <i>{topic}</i>")
        lines.append("")
        lines.append("<i>Untrack: /untrack [number]</i>")
        self._bot.send_message(chat_id, "\n".join(lines))
        return {"action": "tracked", "user_id": user_id, "count": len(tracked)}

    def _untrack_story(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Stop tracking a story by its position in /tracked list."""
        try:
            index = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(chat_id, "Usage: /untrack [number] — see /tracked for the list.")
            return {"action": "untrack_help", "user_id": user_id}

        profile = self._engine.preferences.get_or_create(user_id)
        if index < 1 or index > len(profile.tracked_stories):
            self._bot.send_message(chat_id, f"No tracked story #{index}. See /tracked for the list.")
            return {"action": "untrack_invalid", "user_id": user_id}

        removed = profile.tracked_stories[index - 1]
        self._engine.preferences.untrack_story(user_id, index)
        self._persist_prefs()
        self._bot.send_message(
            chat_id,
            f"Stopped tracking: <b>{removed['headline'][:80]}</b>"
        )
        return {"action": "untrack", "user_id": user_id, "index": index}

    def _compare_story(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Show how different sources cover the same story."""
        try:
            story_num = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(chat_id, "Usage: /compare [story number]")
            return {"action": "compare_help", "user_id": user_id}

        item, others = self._engine.get_story_thread(user_id, story_num)
        if not item:
            self._bot.send_message(
                chat_id,
                f"Story #{story_num} not found. Run /briefing first."
            )
            return {"action": "compare_not_found", "user_id": user_id}

        formatter = self._engine.formatter
        card = formatter.format_comparison(item, others, story_num)
        self._bot.send_message(chat_id, card)
        return {"action": "compare", "user_id": user_id, "story": story_num,
                "source_count": 1 + len(others)}

    def _recall(self, chat_id: int | str, user_id: str,
                args: str) -> dict[str, Any]:
        """Search past briefing history for a keyword."""
        keyword = args.strip()
        if not keyword:
            self._bot.send_message(
                chat_id,
                "Usage: /recall [keyword]\n"
                "Example: /recall AI regulation"
            )
            return {"action": "recall_help", "user_id": user_id}

        items = self._engine.analytics.search_briefing_items(user_id, keyword)
        formatter = self._engine.formatter
        card = formatter.format_recall(keyword, items)
        self._bot.send_message(chat_id, card)
        return {"action": "recall", "user_id": user_id, "keyword": keyword,
                "results": len(items)}

    def check_tracked_updates(self) -> int:
        """Check for proactive tracked story updates across all users.

        Scans cached candidates against each user's tracked stories.
        Sends notifications for matches the user hasn't seen yet.
        Returns the number of notifications sent.
        """
        sent = 0
        for user_id, profile_data in self._engine.preferences.snapshot().items():
            tracked = profile_data.get("tracked_stories", [])
            if not tracked:
                continue

            # Get all fresh candidates from cache
            fresh = self._engine.cache.get_all_fresh(user_id)
            if not fresh:
                continue

            # Check for matches not already seen
            seen = self._shown_ids.get(user_id, set())
            formatter = self._engine.formatter

            for candidate in fresh:
                if candidate.candidate_id in seen:
                    continue
                for t in tracked:
                    if match_tracked(candidate.topic, candidate.title, t):
                        # Send proactive notification
                        msg = formatter.format_tracked_update(candidate, t["headline"])
                        self._bot.send_message(user_id, msg)
                        self._shown_ids.setdefault(user_id, set()).add(candidate.candidate_id)
                        sent += 1
                        break  # Only notify once per candidate

        return sent

    def _show_insights(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Show preference learning insights and apply auto-adjustments."""
        insights = self._engine.analytics.get_rating_insights(user_id)

        # Auto-tune: generate suggestions and apply small adjustments
        suggestions: list[str] = []
        applied: list[str] = []

        for t in insights.get("topics", []):
            ups = t["ups"] or 0
            downs = t["downs"] or 0
            total = t["total"] or 0
            if total < 3:
                continue  # Need minimum data
            topic = t["topic"]
            pct = ups / total * 100
            name = topic.replace("_", " ").title()

            if pct >= 80 and total >= 5:
                # Strong positive signal — boost if not already high
                profile = self._engine.preferences.get_or_create(user_id)
                current = profile.topic_weights.get(topic, 0.5)
                if current < 0.9:
                    self._engine.preferences.apply_weight_adjustment(user_id, topic, 0.1)
                    applied.append(f"Boosted {name} (+0.1) based on consistent positive ratings")
                else:
                    suggestions.append(f"{name} is already at max weight — you clearly love this topic")
            elif pct <= 20 and total >= 5:
                # Strong negative signal — reduce
                profile = self._engine.preferences.get_or_create(user_id)
                current = profile.topic_weights.get(topic, 0.5)
                if current > -0.5:
                    self._engine.preferences.apply_weight_adjustment(user_id, topic, -0.1)
                    applied.append(f"Reduced {name} (-0.1) based on consistent negative ratings")
                else:
                    suggestions.append(f"Consider muting {name} entirely: /mute {topic}")

        for s in insights.get("sources", []):
            ups = s["ups"] or 0
            downs = s["downs"] or 0
            total = s["total"] or 0
            if total < 3:
                continue
            source = s["source"]
            pct = ups / total * 100

            if pct >= 80 and total >= 5:
                profile = self._engine.preferences.get_or_create(user_id)
                current = profile.source_weights.get(source, 0.0)
                if current < 1.5:
                    self._engine.preferences.apply_source_weight(user_id, source, 0.2)
                    applied.append(f"Boosted {source} source weight based on high approval")
            elif pct <= 20 and total >= 5:
                profile = self._engine.preferences.get_or_create(user_id)
                current = profile.source_weights.get(source, 0.0)
                if current > -1.5:
                    self._engine.preferences.apply_source_weight(user_id, source, -0.2)
                    applied.append(f"Reduced {source} source weight based on low approval")

        if applied:
            self._persist_prefs()

        insights["suggestions"] = suggestions
        insights["applied"] = applied

        formatter = self._engine.formatter
        card = formatter.format_insights(insights)
        self._bot.send_message(chat_id, card)
        return {"action": "insights", "user_id": user_id,
                "applied": len(applied), "suggestions": len(suggestions)}

    def _show_weekly(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Show weekly intelligence digest."""
        summary = self._engine.analytics.get_weekly_summary(user_id)
        formatter = self._engine.formatter
        card = formatter.format_weekly(summary)
        self._bot.send_message(chat_id, card)
        return {"action": "weekly", "user_id": user_id,
                "briefings": summary.get("briefing_count", 0),
                "stories": summary.get("story_count", 0)}

    def check_intelligence_alerts(self) -> int:
        """Check for geo-risk escalations and trend spikes, notify relevant users.

        Runs against the last available intelligence data (from the most
        recent briefing pipeline run). Sends alerts to users whose regions
        or topics match the escalation.
        Returns the number of alerts sent.
        """
        sent = 0
        formatter = self._engine.formatter

        # Check geo-risks from last pipeline run
        last_georisks = self._engine.georisk.snapshot()
        for region, risk_level in last_georisks.items():
            # Detect escalation > 15%
            if not isinstance(risk_level, (int, float)):
                continue
            if risk_level < 0.5:
                continue

            alert_data = {
                "region": region,
                "risk_level": risk_level,
                "escalation_delta": 0.15,  # Threshold we're alerting on
                "drivers": [],
            }

            # Find users interested in this region
            for user_id, profile_data in self._engine.preferences.snapshot().items():
                regions = profile_data.get("regions", [])
                region_norm = {r.lower().replace(" ", "_") for r in regions}
                if region.lower().replace(" ", "_") in region_norm:
                    msg = formatter.format_intelligence_alert("georisk", alert_data)
                    self._bot.send_message(user_id, msg)
                    sent += 1

        # Check trend spikes
        last_trends = self._engine.trends.snapshot()
        for topic, velocity in last_trends.items():
            if not isinstance(velocity, (int, float)):
                continue
            if velocity < 3.0:  # Only alert on 3x+ baseline
                continue

            alert_data = {
                "topic": topic,
                "anomaly_score": velocity,
            }

            # Find users with positive weight for this topic
            for user_id, profile_data in self._engine.preferences.snapshot().items():
                weights = profile_data.get("topic_weights", {})
                if weights.get(topic, 0) > 0.3:
                    msg = formatter.format_intelligence_alert("trend", alert_data)
                    self._bot.send_message(user_id, msg)
                    sent += 1

        return sent

    def _deep_dive_story(self, chat_id: int | str, user_id: str,
                         story_num: int) -> dict[str, Any]:
        """Deep dive into a specific story from the last briefing.

        Shows full analysis: confidence band, key assumptions,
        evidence/novelty breakdown, discovery agent, lifecycle stage.
        """
        item = self._engine.get_report_item(user_id, story_num)
        if not item:
            self._bot.send_message(
                chat_id,
                f"Story #{story_num} not found. Run /briefing first, then tap Dive deeper."
            )
            return {"action": "deep_dive_not_found", "user_id": user_id}

        formatter = self._engine.formatter
        card = formatter.format_deep_dive(item, story_num)
        self._bot.send_message(chat_id, card)

        self._engine.analytics.record_interaction(
            user_id=user_id, chat_id=chat_id,
            interaction_type="deep_dive", command="deep_dive",
            args=str(story_num), raw_text="",
            result_action="deep_dive_story",
            result_data={"story": story_num, "title": item.candidate.title},
        )

        return {"action": "deep_dive_story", "user_id": user_id, "story": story_num}

    # ──────────────────────────────────────────────────────────────
    # ADMIN COMMANDS — owner-only analytics dashboard via Telegram
    # ──────────────────────────────────────────────────────────────

    def _is_admin(self, user_id: str) -> bool:
        """Check if user is the admin/owner."""
        import os
        owner_id = os.environ.get("TELEGRAM_OWNER_ID", "")
        if not owner_id:
            # If no owner configured, allow the first registered user
            users = self._engine.analytics.get_all_users()
            if users:
                owner_id = users[-1]["user_id"]  # oldest user
        return str(user_id) == str(owner_id)

    def _handle_admin(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Handle /admin commands for analytics dashboard."""
        import html as html_mod
        from datetime import datetime, timezone

        if not self._is_admin(user_id):
            self._bot.send_message(chat_id, "Admin access required.")
            return {"action": "admin_denied", "user_id": user_id}

        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else "help"
        subargs = parts[1].strip() if len(parts) > 1 else ""
        db = self._engine.analytics

        if subcmd == "help":
            self._bot.send_message(chat_id, (
                "<b>Admin Commands</b>\n\n"
                "/admin stats \u2014 System-wide statistics\n"
                "/admin users \u2014 All registered users\n"
                "/admin user [id] \u2014 Full user profile + activity\n"
                "/admin interactions [id] \u2014 User interaction log\n"
                "/admin ratings [id] \u2014 User rating history\n"
                "/admin feedback [id] \u2014 User feedback history\n"
                "/admin prefs [id] \u2014 User preference change log\n"
                "/admin requests \u2014 Recent pipeline requests\n"
                "/admin request [id] \u2014 Full request detail\n"
                "/admin topics \u2014 Top topics (30 days)\n"
                "/admin sources \u2014 Top sources (30 days)\n"
                "/admin briefings [user_id] \u2014 User briefing history"
            ))
            return {"action": "admin_help", "user_id": user_id}

        if subcmd == "stats":
            stats = db.get_system_stats()
            lines = ["<b>System Analytics</b>", ""]
            for key, val in stats.items():
                lines.append(f"  {key}: <code>{val}</code>")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_stats", "user_id": user_id}

        if subcmd == "users":
            users = db.get_all_users()
            if not users:
                self._bot.send_message(chat_id, "No users registered yet.")
                return {"action": "admin_users", "user_id": user_id}
            lines = [f"<b>All Users ({len(users)})</b>", ""]
            for u in users:
                last = datetime.fromtimestamp(u["last_active_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                lines.append(
                    f"  <code>{u['user_id']}</code> | "
                    f"req:{u['total_requests']} brief:{u['total_briefings']} "
                    f"fb:{u['total_feedback']} rate:{u['total_ratings']} | "
                    f"last: {last}"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_users", "user_id": user_id}

        if subcmd == "user":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /admin user [user_id]")
                return {"action": "admin_user_help", "user_id": user_id}
            target = subargs.strip()
            summary = db.get_user_summary(target)
            if not summary:
                self._bot.send_message(chat_id, f"User {target} not found.")
                return {"action": "admin_user_notfound", "user_id": user_id}
            first = datetime.fromtimestamp(summary["first_seen_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            last = datetime.fromtimestamp(summary["last_active_at"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            # Get current profile
            profile = self._engine.preferences.get_or_create(target)
            lines = [
                f"<b>User: {html_mod.escape(target)}</b>",
                f"  Chat ID: <code>{summary['chat_id']}</code>",
                f"  First seen: {first}",
                f"  Last active: {last}",
                f"  Requests: {summary['total_requests']}",
                f"  Briefings: {summary['total_briefings']}",
                f"  Feedback: {summary['total_feedback']}",
                f"  Ratings: {summary['total_ratings']}",
                "",
                "<b>Current Profile:</b>",
                f"  Tone: {profile.tone}",
                f"  Format: {profile.format}",
                f"  Max items: {profile.max_items}",
                f"  Cadence: {profile.briefing_cadence}",
                f"  Timezone: {profile.timezone}",
                f"  Topics: {dict(profile.topic_weights)}",
                f"  Sources: {dict(profile.source_weights)}",
                f"  Regions: {list(profile.regions_of_interest)}",
                f"  Muted: {list(profile.muted_topics)}",
                f"  Crypto: {list(profile.watchlist_crypto)}",
                f"  Stocks: {list(profile.watchlist_stocks)}",
            ]
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_user", "user_id": user_id, "target": target}

        if subcmd == "interactions":
            target = subargs.strip() if subargs else user_id
            interactions = db.get_user_interactions(target, limit=30)
            if not interactions:
                self._bot.send_message(chat_id, f"No interactions for {target}.")
                return {"action": "admin_interactions", "user_id": user_id}
            lines = [f"<b>Interactions: {html_mod.escape(target)}</b> (last 30)", ""]
            for i in interactions:
                ts = datetime.fromtimestamp(i["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                cmd = i["command"] or "-"
                text = (i["raw_text"] or "")[:60]
                lines.append(f"  {ts} [{i['interaction_type']}] /{cmd} {html_mod.escape(text)}")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_interactions", "user_id": user_id}

        if subcmd == "ratings":
            target = subargs.strip() if subargs else user_id
            ratings = db.get_user_ratings(target, limit=30)
            if not ratings:
                self._bot.send_message(chat_id, f"No ratings for {target}.")
                return {"action": "admin_ratings", "user_id": user_id}
            lines = [f"<b>Ratings: {html_mod.escape(target)}</b> (last 30)", ""]
            for r in ratings:
                ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                icon = "\U0001f44d" if r["direction"] == "up" else "\U0001f44e"
                title = html_mod.escape((r["title"] or r["topic"] or "?")[:50])
                lines.append(f"  {ts} {icon} #{r['item_index']} {title} [{r['source']}]")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_ratings", "user_id": user_id}

        if subcmd == "feedback":
            target = subargs.strip() if subargs else user_id
            feedback = db.get_user_feedback_history(target, limit=20)
            if not feedback:
                self._bot.send_message(chat_id, f"No feedback for {target}.")
                return {"action": "admin_feedback", "user_id": user_id}
            lines = [f"<b>Feedback: {html_mod.escape(target)}</b> (last 20)", ""]
            for f in feedback:
                ts = datetime.fromtimestamp(f["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                text = html_mod.escape((f["feedback_text"] or "")[:80])
                changes = f["changes_applied"] or "{}"
                lines.append(f"  {ts} \"{text}\"")
                lines.append(f"    Changes: <code>{html_mod.escape(changes[:100])}</code>")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_feedback", "user_id": user_id}

        if subcmd == "prefs":
            target = subargs.strip() if subargs else user_id
            prefs = db.get_user_preference_history(target, limit=30)
            if not prefs:
                self._bot.send_message(chat_id, f"No preference changes for {target}.")
                return {"action": "admin_prefs", "user_id": user_id}
            lines = [f"<b>Preference History: {html_mod.escape(target)}</b> (last 30)", ""]
            for p in prefs:
                ts = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                lines.append(
                    f"  {ts} [{p['change_type']}] {p['field']}: "
                    f"{p['old_value']} -> {p['new_value']} ({p['source']})"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_prefs", "user_id": user_id}

        if subcmd == "requests":
            reqs = db.get_recent_requests(limit=15)
            if not reqs:
                self._bot.send_message(chat_id, "No requests recorded.")
                return {"action": "admin_requests", "user_id": user_id}
            lines = ["<b>Recent Requests</b> (last 15)", ""]
            for r in reqs:
                ts = datetime.fromtimestamp(r["started_at"], tz=timezone.utc).strftime("%m-%d %H:%M")
                elapsed = f"{r['total_elapsed_s']:.1f}s" if r["total_elapsed_s"] else "?"
                lines.append(
                    f"  {ts} <code>{r['request_id'][:20]}</code> "
                    f"u:{r['user_id'][:8]} cand:{r['candidate_count']} "
                    f"sel:{r['selected_count']} {elapsed} [{r['status']}]"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_requests", "user_id": user_id}

        if subcmd == "request":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /admin request [request_id]")
                return {"action": "admin_request_help", "user_id": user_id}
            detail = db.get_request_detail(subargs.strip())
            req = detail["request"]
            if not req:
                self._bot.send_message(chat_id, f"Request {subargs} not found.")
                return {"action": "admin_request_notfound", "user_id": user_id}
            lines = [
                f"<b>Request: {html_mod.escape(req['request_id'][:30])}</b>",
                f"  User: {req['user_id']}",
                f"  Prompt: {html_mod.escape((req['prompt'] or '')[:80])}",
                f"  Candidates: {req['candidate_count']}",
                f"  Selected: {req['selected_count']}",
                f"  Type: {req['briefing_type']}",
                f"  Elapsed: {req['total_elapsed_s']}s",
                f"  Status: {req['status']}",
                "",
                f"<b>Votes:</b> {len(detail['votes'])} total",
                f"<b>Items delivered:</b> {len(detail['items'])}",
            ]
            # Show top selected candidates
            selected = [c for c in detail["candidates"] if c["was_selected"]]
            if selected:
                lines.append("")
                lines.append("<b>Selected Stories:</b>")
                for c in selected[:10]:
                    lines.append(
                        f"  [{c['source']}] {html_mod.escape((c['title'] or '')[:60])} "
                        f"(score:{c['composite_score']:.3f})"
                    )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_request", "user_id": user_id}

        if subcmd == "topics":
            topics = db.get_top_topics()
            if not topics:
                self._bot.send_message(chat_id, "No topic data yet.")
                return {"action": "admin_topics", "user_id": user_id}
            lines = ["<b>Top Topics (30 days)</b>", ""]
            for t in topics:
                lines.append(
                    f"  {t['topic']}: {t['count']} candidates, "
                    f"{t['times_selected']} selected, "
                    f"avg score: {t['avg_score']:.3f}"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_topics", "user_id": user_id}

        if subcmd == "sources":
            sources = db.get_top_sources()
            if not sources:
                self._bot.send_message(chat_id, "No source data yet.")
                return {"action": "admin_sources", "user_id": user_id}
            lines = ["<b>Top Sources (30 days)</b>", ""]
            for s in sources:
                sel_rate = (s["times_selected"] / s["total_candidates"] * 100) if s["total_candidates"] else 0
                lines.append(
                    f"  {s['source']}: {s['total_candidates']} cand, "
                    f"{s['times_selected']} sel ({sel_rate:.0f}%), "
                    f"avg: {s['avg_score']:.3f}"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_sources", "user_id": user_id}

        if subcmd == "briefings":
            target = subargs.strip() if subargs else user_id
            briefings = db.get_user_briefings(target, limit=15)
            if not briefings:
                self._bot.send_message(chat_id, f"No briefings for {target}.")
                return {"action": "admin_briefings", "user_id": user_id}
            lines = [f"<b>Briefings: {html_mod.escape(target)}</b> (last 15)", ""]
            for b in briefings:
                ts = datetime.fromtimestamp(b["delivered_at"], tz=timezone.utc).strftime("%m-%d %H:%M")
                lines.append(
                    f"  {ts} [{b['briefing_type']}] "
                    f"{b['item_count']} items | "
                    f"<code>{b['request_id'][:16]}</code>"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_briefings", "user_id": user_id}

        self._bot.send_message(chat_id, f"Unknown admin command: {subcmd}. Try /admin help")
        return {"action": "admin_unknown", "user_id": user_id}
