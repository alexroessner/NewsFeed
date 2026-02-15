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
        # Per-user rate limiting for resource-intensive commands
        self._rate_limits: dict[str, float] = {}  # user_id -> last_expensive_command_ts
        self._RATE_LIMIT_SECONDS = 15  # Min seconds between briefing/sitrep/quick

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

    def _check_rate_limit(self, chat_id: int | str, user_id: str) -> bool:
        """Check if user is rate-limited for expensive commands.

        Returns True if the request should be BLOCKED (rate limited).
        Uses monotonic clock so system time adjustments can't bypass limits.
        """
        import time
        now = time.monotonic()
        last = self._rate_limits.get(user_id, 0)
        if now - last < self._RATE_LIMIT_SECONDS:
            remaining = int(self._RATE_LIMIT_SECONDS - (now - last))
            self._bot.send_message(
                chat_id,
                f"Please wait {remaining}s before requesting another briefing."
            )
            return True
        self._rate_limits[user_id] = now
        return False

    def _handle_command(self, chat_id: int | str, user_id: str,
                        command: str, args: str) -> dict[str, Any]:
        """Route a slash command to the appropriate handler."""

        if command == "start":
            return self._onboard(chat_id, user_id)

        # Rate-limit expensive commands that trigger external API calls
        if command in ("briefing", "deep_dive", "quick", "sitrep"):
            if self._check_rate_limit(chat_id, user_id):
                return {"action": "rate_limited", "user_id": user_id}

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

        if command == "quick":
            return self._run_quick_briefing(chat_id, user_id, args)

        if command == "export":
            return self._export_briefing(chat_id, user_id)

        if command == "sitrep":
            return self._run_sitrep(chat_id, user_id, args)

        if command == "diff":
            return self._briefing_diff(chat_id, user_id)

        if command == "entities":
            return self._show_entities(chat_id, user_id)

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

        if command == "save":
            return self._save_story(chat_id, user_id, args)

        if command == "saved":
            return self._show_saved(chat_id, user_id)

        if command == "unsave":
            return self._unsave_story(chat_id, user_id, args)

        if command == "timeline":
            return self._show_timeline(chat_id, user_id, args)

        if command == "email":
            return self._set_email(chat_id, user_id, args)

        if command == "digest":
            return self._send_email_digest(chat_id, user_id)

        if command == "stats":
            return self._show_stats(chat_id, user_id)

        if command == "sources":
            return self._show_sources(chat_id, user_id)

        if command == "webhook":
            return self._set_webhook(chat_id, user_id, args)

        if command == "filter":
            return self._set_filter(chat_id, user_id, args)

        if command == "preset":
            return self._handle_preset(chat_id, user_id, args)

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
        import html as html_mod
        self._bot.send_message(chat_id, f"Unknown command: /{html_mod.escape(command)}. Try /help")
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
            "\u2022 /briefing \u2014 Full intelligence briefing\n"
            "\u2022 /quick \u2014 Fast headlines-only scan\n"
            "\u2022 /schedule morning 08:00 \u2014 Daily delivery\n\n"
            "<b>Just ask me anything:</b>\n"
            "\u2022 \"What's happening with AI?\"\n"
            "\u2022 \"Show me geopolitics news\"\n"
            "\u2022 \"What's trending?\"\n\n"
            "<b>Customize:</b>\n"
            "\u2022 Type \"more AI, less crypto\" to adjust\n"
            "\u2022 /filter confidence 0.7 \u2014 Quality filter\n"
            "\u2022 /watchlist crypto BTC ETH \u2014 Market tickers\n"
            "\u2022 /help \u2014 Full command list"
        )
        return {"action": "onboard", "user_id": user_id}

    def _run_sitrep(self, chat_id: int | str, user_id: str,
                    args: str = "") -> dict[str, Any]:
        """Generate a structured Situation Report (SITREP).

        Runs the same pipeline as /briefing but renders output as a single
        cohesive intelligence document organized by priority sections.
        """
        from newsfeed.intelligence.entities import build_entity_map

        profile = self._engine.preferences.get_or_create(user_id)
        topics = dict(profile.topic_weights) if profile.topic_weights else dict(self._default_topics)

        prompt = args.strip() or "Generate situation report"
        payload = self._engine.handle_request_payload(
            user_id=user_id,
            prompt=prompt,
            weighted_topics=topics,
            max_items=profile.max_items,
        )

        # Apply user filters
        has_filters = profile.confidence_min > 0 or profile.urgency_min or profile.max_per_source > 0
        if has_filters and payload.items:
            payload.items = self._apply_user_filters(payload.items, profile)

        # Build market ticker
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

        # Build entity cross-reference map
        entity_map = build_entity_map(payload.items) if payload.items else {}

        # Store items for follow-up commands
        self._last_items[user_id] = self._engine.last_briefing_items(user_id)

        # Render SITREP
        formatter = self._engine.formatter
        sitrep = formatter.format_sitrep(payload, entity_map, ticker_bar)
        self._bot.send_message(chat_id, sitrep)

        # Push to webhook if configured
        self._auto_webhook_briefing(user_id, payload.items)

        log.info("SITREP delivered: user=%s (%d items)", user_id, len(payload.items))
        return {"action": "sitrep", "user_id": user_id, "items": len(payload.items)}

    def _briefing_diff(self, chat_id: int | str,
                       user_id: str) -> dict[str, Any]:
        """Compare current briefing against the previous one holistically."""
        from newsfeed.memory.store import extract_keywords

        analytics = self._engine.analytics

        # Get the two most recent briefings
        briefings = analytics.get_user_briefings(user_id, limit=2)
        if len(briefings) < 2:
            self._bot.send_message(
                chat_id,
                "Need at least two briefings to compare. "
                "Run /briefing again to generate a second one."
            )
            return {"action": "diff_insufficient", "user_id": user_id}

        current_id = briefings[0].get("request_id", "")
        previous_id = briefings[1].get("request_id", "")

        # Get items from both briefings via public API
        current_items = analytics.get_briefing_items_by_request(current_id) if current_id else []
        previous_items = analytics.get_briefing_items_by_request(previous_id) if previous_id else []

        if not current_items and not previous_items:
            self._bot.send_message(chat_id, "No briefing item data available for comparison.")
            return {"action": "diff_no_data", "user_id": user_id}

        # Build keyword indices for matching
        prev_kw_index: dict[str, dict] = {}
        for item in previous_items:
            kws = extract_keywords(item.get("title", ""))
            key = f"{item.get('topic', '')}:{':'.join(sorted(kws))}"
            prev_kw_index[key] = item

        curr_kw_index: dict[str, dict] = {}
        for item in current_items:
            kws = extract_keywords(item.get("title", ""))
            key = f"{item.get('topic', '')}:{':'.join(sorted(kws))}"
            curr_kw_index[key] = item

        # Classify changes
        new_stories = []
        continuing = []
        escalated = []
        deescalated = []
        resolved = []

        urgency_rank = {"critical": 4, "breaking": 3, "elevated": 2, "routine": 1}

        for key, item in curr_kw_index.items():
            if key in prev_kw_index:
                prev_item = prev_kw_index[key]
                curr_urg = urgency_rank.get(item.get("urgency", "routine"), 1)
                prev_urg = urgency_rank.get(prev_item.get("urgency", "routine"), 1)
                if curr_urg > prev_urg:
                    escalated.append({
                        **item,
                        "reason": f"{prev_item.get('urgency', 'routine')} \u2192 {item.get('urgency', 'routine')}",
                    })
                elif curr_urg < prev_urg:
                    deescalated.append(item)
                else:
                    continuing.append(item)
            else:
                new_stories.append(item)

        for key, item in prev_kw_index.items():
            if key not in curr_kw_index:
                resolved.append(item)

        # Topic shifts
        prev_topics: dict[str, int] = {}
        for item in previous_items:
            t = item.get("topic", "unknown")
            prev_topics[t] = prev_topics.get(t, 0) + 1

        curr_topics: dict[str, int] = {}
        for item in current_items:
            t = item.get("topic", "unknown")
            curr_topics[t] = curr_topics.get(t, 0) + 1

        all_topics = set(prev_topics) | set(curr_topics)
        topic_shifts = {
            t: curr_topics.get(t, 0) - prev_topics.get(t, 0)
            for t in all_topics
            if curr_topics.get(t, 0) != prev_topics.get(t, 0)
        }

        diff_data = {
            "new_stories": new_stories,
            "resolved_stories": resolved,
            "escalated": escalated,
            "deescalated": deescalated,
            "continuing": continuing,
            "topic_shifts": topic_shifts,
        }

        formatter = self._engine.formatter
        msg = formatter.format_briefing_diff(diff_data)
        self._bot.send_message(chat_id, msg)
        return {"action": "diff", "user_id": user_id}

    def _show_entities(self, chat_id: int | str,
                       user_id: str) -> dict[str, Any]:
        """Show entity intelligence map across last briefing stories."""
        from newsfeed.intelligence.entities import format_entity_dashboard

        items = self._engine._last_report_items.get(user_id, [])
        if not items:
            self._bot.send_message(
                chat_id,
                "No briefing data available. Run /briefing or /sitrep first."
            )
            return {"action": "entities_empty", "user_id": user_id}

        entity_data = format_entity_dashboard(items)
        formatter = self._engine.formatter
        msg = formatter.format_entity_dashboard(entity_data, items)
        self._bot.send_message(chat_id, msg)
        return {"action": "entities", "user_id": user_id}

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

        # Apply user-configured advanced filters
        has_filters = profile.confidence_min > 0 or profile.urgency_min or profile.max_per_source > 0
        if has_filters and payload.items:
            pre_count = len(payload.items)
            payload.items = self._apply_user_filters(payload.items, profile)
            filtered_out = pre_count - len(payload.items)
            if filtered_out:
                log.info("User filters removed %d/%d items for user=%s", filtered_out, pre_count, user_id)

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

        # Compute delta tags (NEW / UPDATED / DEVELOPING)
        delta_tags = self._compute_delta_tags(user_id, payload)

        # Build thread-to-story mapping for grouping
        thread_map = self._build_thread_map(payload)

        # Message 1: Header (ticker + exec summary + geo risks + trends + threads)
        header = formatter.format_header(payload, ticker_bar, tracked_count=tracked_count)
        self._bot.send_message(chat_id, header)

        # Send story cards grouped by narrative thread
        shown_threads: set[str] = set()
        for idx, item in enumerate(payload.items, start=1):
            # Insert thread separator before first story of each new thread
            thread_info = thread_map.get(idx)
            if thread_info and thread_info["thread_id"] not in shown_threads:
                shown_threads.add(thread_info["thread_id"])
                if thread_info["story_count"] > 1:
                    sep = formatter.format_thread_separator(thread_info)
                    self._bot.send_message(chat_id, sep)

            is_tracked = tracked_flags[idx - 1]
            delta_tag = delta_tags[idx - 1] if idx - 1 < len(delta_tags) else ""
            card = formatter.format_story_card(item, idx, is_tracked=is_tracked,
                                                delta_tag=delta_tag)
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

        # Push to webhook if configured
        self._auto_webhook_briefing(user_id, payload.items)

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
        import html as html_mod
        self._bot.send_message(chat_id, f"Searching for more on {html_mod.escape(topic)}...")
        return self._run_briefing(chat_id, user_id, topic_hint=topic)

    def _detect_intent(self, text: str) -> tuple[str, str]:
        """Detect conversational intent from natural language text.

        Returns (intent, argument) where intent is one of:
        - "briefing_query": user wants news on a topic ("what's happening with AI?")
        - "trending": user asks about trends ("what's trending?")
        - "status_query": user asks about system status
        - "help_query": user asks for help
        - "search_query": user wants to find past stories
        - "preference": standard preference adjustment (more/less/tone)
        - "unknown": no recognizable intent
        """
        import re as re_mod
        lower = text.lower().strip()

        # Trending / what's hot
        if re_mod.search(r"\b(what'?s\s+trending|trending|what'?s\s+hot|hot\s+topics?)\b", lower):
            return ("trending", "")

        # Briefing query — "what's happening with X", "show me X news", "tell me about X"
        m = re_mod.search(
            r"\b(?:what'?s\s+happening\s+(?:with|in)\s+|"
            r"show\s+(?:me\s+)?(?:the\s+)?(?:latest\s+)?|"
            r"tell\s+me\s+about\s+|"
            r"(?:any\s+)?news\s+(?:on|about)\s+|"
            r"brief\s+me\s+on\s+|"
            r"update\s+(?:me\s+)?on\s+)"
            r"(.+?)(?:[?.!]|$)", lower
        )
        if m:
            topic = m.group(1).strip().rstrip("?.! ")
            return ("briefing_query", topic)

        # Search/recall — "find stories about X", "search for X"
        m = re_mod.search(
            r"\b(?:find|search|look\s+up|recall)\s+(?:stories?\s+)?(?:about|for|on)\s+(.+?)(?:[?.!]|$)",
            lower,
        )
        if m:
            return ("search_query", m.group(1).strip().rstrip("?.! "))

        # Help query
        if re_mod.search(r"\b(how\s+do\s+i|help\s+me|what\s+can\s+you\s+do|commands?)\b", lower):
            return ("help_query", "")

        # Status query
        if re_mod.search(r"\b(status|are\s+you\s+(?:online|working|running))\b", lower):
            return ("status_query", "")

        return ("unknown", "")

    def _handle_feedback(self, chat_id: int | str, user_id: str,
                         text: str) -> dict[str, Any]:
        """Apply user feedback to preferences, or detect conversational intent."""
        results = self._engine.apply_user_feedback(
            user_id, text, is_admin=self._is_admin(user_id)
        )

        if results:
            import html as html_mod
            changes = ", ".join(f"{html_mod.escape(str(k))}={html_mod.escape(str(v))}" for k, v in results.items())
            self._bot.send_message(chat_id, f"Preferences updated: {changes}")
            return {"action": "feedback", "user_id": user_id, "changes": results}

        # No preference match — try conversational intent detection
        intent, arg = self._detect_intent(text)

        if intent == "briefing_query":
            import html as html_mod
            self._bot.send_message(chat_id, f"Pulling intelligence on {html_mod.escape(arg)}...")
            return self._run_briefing(chat_id, user_id, topic_hint=arg)

        if intent == "trending":
            return self._show_weekly(chat_id, user_id)

        if intent == "search_query":
            return self._recall(chat_id, user_id, arg)

        if intent == "help_query":
            self._bot.send_message(chat_id, self._bot.format_help())
            return {"action": "help", "user_id": user_id}

        if intent == "status_query":
            return self._show_status(chat_id, user_id)

        # Truly unrecognized
        self._bot.send_message(
            chat_id,
            "I can help with that! Try:\n"
            "\u2022 <b>Ask me:</b> \"What's happening with AI?\"\n"
            "\u2022 <b>Search:</b> \"Find stories about regulation\"\n"
            "\u2022 <b>Adjust:</b> more [topic] / less [topic]\n"
            "\u2022 <b>Commands:</b> /briefing, /quick, /help"
        )

        return {"action": "feedback_unrecognized", "user_id": user_id}

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
            import html as html_mod
            topic_list = ", ".join(adjustments)
            self._bot.send_message(chat_id, f"Got it — boosting {html_mod.escape(topic_list)} in future briefings.")
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
            import html as html_mod
            topic_list = ", ".join(adjustments)
            self._bot.send_message(chat_id, f"Understood — reducing {html_mod.escape(topic_list)} weight.")
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
            import html as html_mod
            self._bot.send_message(chat_id, f"\U0001f44d Boosted {html_mod.escape(topic)} (story #{item_num})")
        elif direction == "down":
            self._engine.apply_user_feedback(user_id, f"less {topic}")
            if item.get("source"):
                self._engine.preferences.apply_source_weight(user_id, item["source"], -0.3)
            import html as html_mod
            self._bot.send_message(chat_id, f"\U0001f44e Reduced {html_mod.escape(topic)} (story #{item_num})")
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
            "email": profile.email,
            "webhook_url": profile.webhook_url,
            "confidence_min": profile.confidence_min,
            "urgency_min": profile.urgency_min,
            "max_per_source": profile.max_per_source,
            "alert_georisk_threshold": profile.alert_georisk_threshold,
            "alert_trend_threshold": profile.alert_trend_threshold,
            "presets": {k: {} for k in profile.presets},  # names only for display
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
            import html as html_mod
            custom_tag = " (custom)" if is_custom else " (default)"
            lines.append(f"  {html_mod.escape(topic)}: {sign}{weight:.1f} {bar}{custom_tag}")

        # Show source weights if any
        if profile.source_weights:
            lines.append("")
            lines.append("<b>Source Preferences:</b>")
            for src, sw in sorted(profile.source_weights.items(), key=lambda x: -x[1]):
                import html as html_mod
                label = "boosted" if sw > 0 else "demoted"
                lines.append(f"  {html_mod.escape(src)}: {label} ({sw:+.1f})")

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
                # Auto-send email digest if user has email configured
                self._auto_email_digest(user_id)
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
            import html as html_mod
            self._bot.send_message(chat_id, f"Crypto watchlist: {html_mod.escape(', '.join(t.upper() for t in cleaned))}")
            return {"action": "watchlist_crypto", "user_id": user_id}

        if category == "stocks" and tickers:
            cleaned = [t.upper() for t in tickers]
            self._engine.preferences.set_watchlist(user_id, stocks=cleaned)
            self._persist_prefs()
            import html as html_mod
            self._bot.send_message(chat_id, f"Stock watchlist: {html_mod.escape(', '.join(cleaned))}")
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
        import html as html_mod
        self._bot.send_message(chat_id, f"Timezone set to <code>{html_mod.escape(tz)}</code>. Scheduled briefings now use your local time.")
        return {"action": "timezone", "user_id": user_id, "tz": tz}

    def _mute_topic(self, chat_id: int | str, user_id: str,
                    args: str) -> dict[str, Any]:
        """Mute a topic from future briefings."""
        topic = args.strip().lower().replace(" ", "_")
        if not topic:
            profile = self._engine.preferences.get_or_create(user_id)
            import html as html_mod
            muted = ", ".join(profile.muted_topics) or "none"
            self._bot.send_message(
                chat_id,
                f"Muted topics: <code>{html_mod.escape(muted)}</code>\n"
                f"Mute with: /mute crypto"
            )
            return {"action": "mute_show", "user_id": user_id}

        self._engine.preferences.mute_topic(user_id, topic)
        self._persist_prefs()
        import html as html_mod
        self._bot.send_message(chat_id, f"Topic <code>{html_mod.escape(topic)}</code> muted. /unmute {html_mod.escape(topic)} to reverse.")
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
        import html as html_mod
        self._bot.send_message(chat_id, f"Topic <code>{html_mod.escape(topic)}</code> unmuted.")
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
        track_count = len(self._engine.preferences.get_or_create(user_id).tracked_stories)
        import html as html_mod
        self._bot.send_message(
            chat_id,
            f"\U0001f4cc Now tracking: <b>{html_mod.escape(headline)}</b>\n"
            f"You'll see \U0001f4cc badges when new developments appear in future briefings.\n"
            f"View tracked stories: /tracked\n"
            f"<i>Tip: Use /timeline {track_count} to see this story's evolution over time</i>"
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
            topic = html_mod.escape(t["topic"].replace("_", " ").title())
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
        import html as html_mod
        self._bot.send_message(
            chat_id,
            f"Stopped tracking: <b>{html_mod.escape(removed['headline'][:80])}</b>"
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
            if not isinstance(risk_level, (int, float)):
                continue

            alert_data = {
                "region": region,
                "risk_level": risk_level,
                "escalation_delta": 0.15,
                "drivers": [],
            }

            # Find users interested in this region, respecting per-user thresholds
            for user_id, profile_data in self._engine.preferences.snapshot().items():
                user_threshold = profile_data.get("alert_georisk_threshold", 0.5)
                if risk_level < user_threshold:
                    continue
                regions = profile_data.get("regions", [])
                region_norm = {r.lower().replace(" ", "_") for r in regions}
                if region.lower().replace(" ", "_") in region_norm:
                    msg = formatter.format_intelligence_alert("georisk", alert_data)
                    self._bot.send_message(user_id, msg)
                    self._auto_webhook_alert(user_id, "georisk", alert_data)
                    sent += 1

        # Check trend spikes
        last_trends = self._engine.trends.snapshot()
        for topic, velocity in last_trends.items():
            if not isinstance(velocity, (int, float)):
                continue

            alert_data = {
                "topic": topic,
                "anomaly_score": velocity,
            }

            # Find users with positive weight for this topic, respecting per-user thresholds
            for user_id, profile_data in self._engine.preferences.snapshot().items():
                user_threshold = profile_data.get("alert_trend_threshold", 3.0)
                if velocity < user_threshold:
                    continue
                weights = profile_data.get("topic_weights", {})
                if weights.get(topic, 0) > 0.3:
                    msg = formatter.format_intelligence_alert("trend", alert_data)
                    self._bot.send_message(user_id, msg)
                    self._auto_webhook_alert(user_id, "trend", alert_data)
                    sent += 1

        return sent

    def _save_story(self, chat_id: int | str, user_id: str,
                    args: str) -> dict[str, Any]:
        """Bookmark a story from the last briefing."""
        try:
            story_num = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(chat_id, "Usage: tap the \U0001f516 Save button on a story card.")
            return {"action": "save_help", "user_id": user_id}

        items = self._last_items.get(user_id, [])
        if story_num < 1 or story_num > len(items):
            self._bot.send_message(chat_id, "That story is no longer available. Run /briefing first.")
            return {"action": "save_expired", "user_id": user_id}

        item = items[story_num - 1]
        # Get URL from ReportItem if available
        report_item = self._engine.get_report_item(user_id, story_num)
        url = report_item.candidate.url if report_item else ""

        self._engine.preferences.save_bookmark(
            user_id,
            title=item["title"],
            source=item.get("source", ""),
            url=url,
            topic=item["topic"],
        )
        self._persist_prefs()

        import html as html_mod
        bookmark_count = len(self._engine.preferences.get_or_create(user_id).bookmarks)
        self._bot.send_message(
            chat_id,
            f"\U0001f516 Saved: <b>{html_mod.escape(item['title'][:80])}</b>\n"
            f"View bookmarks: /saved ({bookmark_count} total)\n"
            f"<i>Tip: /export to get all stories as Markdown for your notes app</i>"
        )
        return {"action": "save", "user_id": user_id, "story": story_num}

    def _show_saved(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Show all bookmarked stories."""
        profile = self._engine.preferences.get_or_create(user_id)
        formatter = self._engine.formatter
        card = formatter.format_bookmarks(profile.bookmarks)
        self._bot.send_message(chat_id, card)
        return {"action": "saved", "user_id": user_id, "count": len(profile.bookmarks)}

    def _unsave_story(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Remove a bookmark by index."""
        try:
            index = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(chat_id, "Usage: /unsave [number] \u2014 see /saved for the list.")
            return {"action": "unsave_help", "user_id": user_id}

        profile = self._engine.preferences.get_or_create(user_id)
        if index < 1 or index > len(profile.bookmarks):
            self._bot.send_message(chat_id, f"No bookmark #{index}. See /saved for the list.")
            return {"action": "unsave_invalid", "user_id": user_id}

        removed = profile.bookmarks[index - 1]
        self._engine.preferences.remove_bookmark(user_id, index)
        self._persist_prefs()
        import html as html_mod
        self._bot.send_message(
            chat_id,
            f"Removed bookmark: <b>{html_mod.escape(removed['title'][:80])}</b>"
        )
        return {"action": "unsave", "user_id": user_id, "index": index}

    def _show_timeline(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Show timeline of a tracked story's evolution from analytics."""
        try:
            index = int(args.strip())
        except (ValueError, TypeError):
            self._bot.send_message(
                chat_id,
                "Usage: /timeline [number] \u2014 where number is from /tracked list."
            )
            return {"action": "timeline_help", "user_id": user_id}

        profile = self._engine.preferences.get_or_create(user_id)
        if index < 1 or index > len(profile.tracked_stories):
            self._bot.send_message(chat_id, f"No tracked story #{index}. See /tracked for the list.")
            return {"action": "timeline_invalid", "user_id": user_id}

        tracked = profile.tracked_stories[index - 1]
        items = self._engine.analytics.get_story_timeline(
            user_id, tracked["topic"], tracked["keywords"],
        )

        formatter = self._engine.formatter
        card = formatter.format_timeline(tracked["headline"], items)
        self._bot.send_message(chat_id, card)
        return {"action": "timeline", "user_id": user_id, "index": index,
                "results": len(items)}

    def _set_email(self, chat_id: int | str, user_id: str,
                   args: str) -> dict[str, Any]:
        """Set or show user email for digest delivery."""
        import re as re_mod
        email = args.strip()

        if not email:
            profile = self._engine.preferences.get_or_create(user_id)
            import html as html_mod
            current = profile.email or "not set"
            self._bot.send_message(
                chat_id,
                f"Email: <code>{html_mod.escape(current)}</code>\n"
                f"Set with: /email user@example.com"
            )
            return {"action": "email_show", "user_id": user_id}

        # Basic email validation
        if not re_mod.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            self._bot.send_message(chat_id, "That doesn't look like a valid email address.")
            return {"action": "email_invalid", "user_id": user_id}

        self._engine.preferences.set_email(user_id, email)
        self._persist_prefs()
        import html as html_mod
        self._bot.send_message(
            chat_id,
            f"Email set to <code>{html_mod.escape(email)}</code>\n"
            f"Send a digest anytime: /digest"
        )
        return {"action": "email", "user_id": user_id, "email": email}

    def _set_webhook(self, chat_id: int | str, user_id: str,
                     args: str) -> dict[str, Any]:
        """Set, show, test, or clear outbound webhook URL."""
        from newsfeed.delivery.webhook import validate_webhook_url, send_webhook

        url = args.strip()
        profile = self._engine.preferences.get_or_create(user_id)

        if not url:
            import html as html_mod
            current = profile.webhook_url or "not set"
            self._bot.send_message(
                chat_id,
                "<b>\U0001f517 Webhook Delivery</b>\n\n"
                f"URL: <code>{html_mod.escape(current)}</code>\n\n"
                "Briefings and alerts are pushed as structured JSON.\n"
                "Compatible with Slack, Discord, and custom endpoints.\n\n"
                "<b>Commands:</b>\n"
                "/webhook https://hooks.slack.com/... \u2014 Set URL\n"
                "/webhook test \u2014 Send test payload\n"
                "/webhook off \u2014 Disable webhook"
            )
            return {"action": "webhook_show", "user_id": user_id}

        if url.lower() == "off":
            profile.webhook_url = ""
            self._persist_prefs()
            self._bot.send_message(chat_id, "Webhook delivery disabled.")
            return {"action": "webhook_off", "user_id": user_id}

        if url.lower() == "test":
            if not profile.webhook_url:
                self._bot.send_message(chat_id, "No webhook URL set. Use /webhook [url] first.")
                return {"action": "webhook_test_nourl", "user_id": user_id}
            test_payload = {
                "type": "test",
                "message": "NewsFeed webhook connected successfully",
                "user_id": user_id,
            }
            success = send_webhook(profile.webhook_url, test_payload)
            if success:
                self._bot.send_message(chat_id, "\u2705 Test payload delivered successfully.")
            else:
                self._bot.send_message(chat_id, "\u274c Delivery failed. Check your webhook URL.")
            return {"action": "webhook_test", "user_id": user_id, "success": success}

        # Validate and set URL
        valid, error = validate_webhook_url(url)
        if not valid:
            import html as html_mod
            self._bot.send_message(chat_id, f"Invalid webhook URL: {html_mod.escape(error)}")
            return {"action": "webhook_invalid", "user_id": user_id}

        profile.webhook_url = url
        self._persist_prefs()
        import html as html_mod
        self._bot.send_message(
            chat_id,
            f"\U0001f517 Webhook set.\n"
            f"URL: <code>{html_mod.escape(url[:60])}...</code>\n\n"
            "Briefings and alerts will be pushed automatically.\n"
            "Test it: /webhook test"
        )
        return {"action": "webhook", "user_id": user_id}

    def _send_email_digest(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Send an HTML email digest of the last briefing."""
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.email:
            self._bot.send_message(
                chat_id,
                "No email configured. Set one with: /email user@example.com"
            )
            return {"action": "digest_no_email", "user_id": user_id}

        # Get last report items to build a payload
        report_items = self._engine._last_report_items.get(user_id, [])
        if not report_items:
            self._bot.send_message(
                chat_id,
                "No recent briefing to send. Run /briefing first."
            )
            return {"action": "digest_no_briefing", "user_id": user_id}

        from datetime import datetime, timezone
        from newsfeed.delivery.email_digest import EmailDigest
        from newsfeed.models.domain import BriefingType, DeliveryPayload

        # Reconstruct a lightweight payload from stored report items
        payload = DeliveryPayload(
            user_id=user_id,
            generated_at=datetime.now(timezone.utc),
            items=report_items,
            briefing_type=BriefingType.MORNING_DIGEST,
        )

        # Compute tracked flags
        tracked = profile.tracked_stories
        tracked_flags = []
        for item in report_items:
            is_tracked = any(
                match_tracked(item.candidate.topic, item.candidate.title, t)
                for t in tracked
            )
            tracked_flags.append(is_tracked)

        # Build weekly summary for inclusion
        weekly = self._engine.analytics.get_weekly_summary(user_id)

        smtp_cfg = self._engine.pipeline.get("smtp", {})
        digest = EmailDigest(smtp_cfg)
        html_body = digest.render(payload, tracked_flags, weekly)

        if not digest.is_configured:
            # Save HTML locally and notify
            self._bot.send_message(
                chat_id,
                "SMTP not configured \u2014 email digest rendered but cannot send.\n"
                "Configure SMTP_HOST, SMTP_FROM, SMTP_USER, SMTP_PASSWORD environment variables."
            )
            return {"action": "digest_no_smtp", "user_id": user_id}

        subject = f"Intelligence Digest \u2014 {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
        success = digest.send(profile.email, subject, html_body)

        if success:
            import html as html_mod
            self._bot.send_message(
                chat_id,
                f"\u2709\ufe0f Digest sent to <code>{html_mod.escape(profile.email)}</code>"
            )
        else:
            self._bot.send_message(
                chat_id,
                "Failed to send email. Check SMTP configuration."
            )

        return {"action": "digest", "user_id": user_id, "sent": success}

    def _auto_email_digest(self, user_id: str) -> None:
        """Silently send an email digest after a scheduled briefing.

        Only sends if the user has email configured and SMTP is available.
        Does not notify via Telegram on failure — this is a background task.
        """
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.email:
            return

        report_items = self._engine._last_report_items.get(user_id, [])
        if not report_items:
            return

        try:
            from datetime import datetime, timezone
            from newsfeed.delivery.email_digest import EmailDigest
            from newsfeed.models.domain import BriefingType, DeliveryPayload

            payload = DeliveryPayload(
                user_id=user_id,
                generated_at=datetime.now(timezone.utc),
                items=report_items,
                briefing_type=BriefingType.MORNING_DIGEST,
            )

            tracked = profile.tracked_stories
            tracked_flags = [
                any(match_tracked(item.candidate.topic, item.candidate.title, t)
                    for t in tracked)
                for item in report_items
            ]

            weekly = self._engine.analytics.get_weekly_summary(user_id)
            smtp_cfg = self._engine.pipeline.get("smtp", {})
            digest = EmailDigest(smtp_cfg)

            if not digest.is_configured:
                return

            html_body = digest.render(payload, tracked_flags, weekly)
            subject = f"Intelligence Digest \u2014 {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
            digest.send(profile.email, subject, html_body)
            log.info("Auto-sent email digest to user=%s", user_id)
        except Exception:
            log.debug("Auto email digest failed for user=%s", user_id, exc_info=True)

    def _build_thread_map(self, payload) -> dict[int, dict]:
        """Map 1-based story indices to their narrative thread info.

        Returns dict: {story_index -> {"thread_id", "headline", "source_count",
                                        "story_count", "urgency", "lifecycle"}}
        """
        if not payload.threads:
            return {}

        # Build candidate_id -> thread lookup
        candidate_to_thread: dict[str, dict] = {}
        for thread in payload.threads:
            thread_info = {
                "thread_id": thread.thread_id,
                "headline": thread.headline,
                "source_count": thread.source_count,
                "story_count": len(thread.candidates),
                "urgency": thread.urgency.value if hasattr(thread.urgency, "value") else str(thread.urgency),
                "lifecycle": thread.lifecycle.value if hasattr(thread.lifecycle, "value") else str(thread.lifecycle),
            }
            for candidate in thread.candidates:
                candidate_to_thread[candidate.candidate_id] = thread_info

        # Map story indices to thread info
        result: dict[int, dict] = {}
        for idx, item in enumerate(payload.items, start=1):
            cid = item.candidate.candidate_id
            if cid in candidate_to_thread:
                result[idx] = candidate_to_thread[cid]

        return result

    def _auto_webhook_briefing(self, user_id: str, items: list) -> None:
        """Push briefing to user's webhook if configured."""
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.webhook_url or not items:
            return
        try:
            from newsfeed.delivery.webhook import (
                _detect_platform, format_briefing_payload, send_webhook,
            )
            platform = _detect_platform(profile.webhook_url)
            payload = format_briefing_payload(user_id, items, platform)
            send_webhook(profile.webhook_url, payload)
            log.info("Webhook briefing pushed for user=%s", user_id)
        except Exception:
            log.debug("Webhook briefing failed for user=%s", user_id, exc_info=True)

    def _auto_webhook_alert(self, user_id: str, alert_type: str,
                            alert_data: dict) -> None:
        """Push an intelligence alert to user's webhook if configured."""
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.webhook_url:
            return
        try:
            from newsfeed.delivery.webhook import (
                _detect_platform, format_alert_payload, send_webhook,
            )
            platform = _detect_platform(profile.webhook_url)
            payload = format_alert_payload(alert_type, alert_data, platform)
            send_webhook(profile.webhook_url, payload)
        except Exception:
            log.debug("Webhook alert failed for user=%s", user_id, exc_info=True)

    def _compute_delta_tags(self, user_id: str,
                            payload) -> list[str]:
        """Compute delta tags (NEW/UPDATED/DEVELOPING) relative to last briefing.

        Compares current payload items against the user's previous briefing
        stored in analytics. Tags:
        - "new": story not seen in any recent briefing
        - "updated": same topic + similar title (keyword overlap)
        - "developing": tracked story with new developments
        - "": no change info (first briefing or no analytics data)
        """
        from newsfeed.memory.store import extract_keywords

        # Get previous briefing items from analytics (last 24h)
        prev_items = self._engine.analytics.search_briefing_items(
            user_id, "", limit=50
        )
        if not prev_items:
            return [""] * len(payload.items)

        # Build keyword index from previous items
        prev_by_topic: dict[str, list[set[str]]] = {}
        for pi in prev_items:
            topic = pi.get("topic", "")
            title = pi.get("title", "")
            kw = set(extract_keywords(title))
            prev_by_topic.setdefault(topic, []).append(kw)

        tags: list[str] = []
        for item in payload.items:
            c = item.candidate
            current_kw = set(extract_keywords(c.title))
            topic_prev = prev_by_topic.get(c.topic, [])

            if not topic_prev:
                tags.append("new")
                continue

            # Check for keyword overlap with previous items
            best_overlap = 0
            for prev_kw in topic_prev:
                overlap = len(current_kw & prev_kw)
                best_overlap = max(best_overlap, overlap)

            if best_overlap >= 3:
                tags.append("developing")
            elif best_overlap >= 2:
                tags.append("updated")
            else:
                tags.append("new")

        return tags

    def _run_quick_briefing(self, chat_id: int | str, user_id: str,
                            topic_hint: str = "") -> dict[str, Any]:
        """Run a quick headlines-only briefing — compact scan format."""
        profile = self._engine.preferences.get_or_create(user_id)

        topics = dict(profile.topic_weights) if profile.topic_weights else dict(self._default_topics)
        if topic_hint:
            topic_key = topic_hint.strip().lower().replace("_", " ")
            topics[topic_key] = min(1.0, topics.get(topic_key, 0.5) + 0.3)

        prompt = topic_hint or "Generate intelligence briefing"

        payload = self._engine.handle_request_payload(
            user_id=user_id,
            prompt=prompt,
            weighted_topics=topics,
            max_items=profile.max_items,
        )

        # Apply user-configured advanced filters
        has_filters = profile.confidence_min > 0 or profile.urgency_min or profile.max_per_source > 0
        if has_filters and payload.items:
            payload.items = self._apply_user_filters(payload.items, profile)

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

        # Track items for downstream features
        dominant_topic = max(topics, key=topics.get, default="general")
        self._last_topic[user_id] = dominant_topic
        self._last_items[user_id] = self._engine.last_briefing_items(user_id)

        # Compute tracked flags
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

        # Compute delta tags
        delta_tags = self._compute_delta_tags(user_id, payload)

        formatter = self._engine.formatter
        card = formatter.format_quick_briefing(
            payload, ticker_bar, tracked_flags, delta_tags, tracked_count,
        )
        self._bot.send_quick_briefing(chat_id, card, item_count=len(payload.items))

        log.info(
            "Quick briefing: user=%s chat=%s (%d items)",
            user_id, chat_id, len(payload.items),
        )
        return {"action": "quick_briefing", "user_id": user_id,
                "topic": dominant_topic, "items": len(payload.items)}

    def _export_briefing(self, chat_id: int | str,
                         user_id: str) -> dict[str, Any]:
        """Export the last briefing as Markdown."""
        report_items = self._engine._last_report_items.get(user_id, [])
        if not report_items:
            self._bot.send_message(
                chat_id,
                "No recent briefing to export. Run /briefing first."
            )
            return {"action": "export_empty", "user_id": user_id}

        from datetime import datetime, timezone
        from newsfeed.models.domain import BriefingType, DeliveryPayload

        payload = DeliveryPayload(
            user_id=user_id,
            generated_at=datetime.now(timezone.utc),
            items=report_items,
            briefing_type=BriefingType.MORNING_DIGEST,
        )

        # Tracked flags
        profile = self._engine.preferences.get_or_create(user_id)
        tracked = profile.tracked_stories
        tracked_flags = [
            any(match_tracked(item.candidate.topic, item.candidate.title, t)
                for t in tracked)
            for item in report_items
        ]

        # Delta tags
        delta_tags = self._compute_delta_tags(user_id, payload)

        formatter = self._engine.formatter
        markdown = formatter.format_markdown_export(payload, tracked_flags, delta_tags)

        # Send as a code block (Telegram renders monospace in <pre>)
        # Split into chunks if needed (Telegram 4096 char limit)
        header = "<b>\U0001f4dd Markdown Export</b>\n\n"
        header += "<i>Copy the text below into Obsidian, Notion, or any Markdown editor:</i>\n\n"
        self._bot.send_message(chat_id, header)

        # Send markdown in pre-formatted blocks
        chunk_size = 3900  # Leave room for <pre> tags
        for i in range(0, len(markdown), chunk_size):
            chunk = markdown[i:i + chunk_size]
            import html as html_mod
            self._bot.send_message(
                chat_id,
                f"<pre>{html_mod.escape(chunk)}</pre>"
            )

        return {"action": "export", "user_id": user_id,
                "length": len(markdown)}

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

    def _show_stats(self, chat_id: int | str,
                    user_id: str) -> dict[str, Any]:
        """Show personal analytics dashboard with engagement metrics."""
        import html as html_mod
        from collections import Counter

        analytics = self._engine.analytics
        lines: list[str] = []
        lines.append("<b>\U0001f4ca Personal Analytics Dashboard</b>")
        lines.append("")

        # Briefing history
        briefings = analytics.get_user_briefings(user_id, limit=100)
        total_briefings = len(briefings)
        if briefings:
            first_date = briefings[-1].get("delivered_at", "")[:10]
            last_date = briefings[0].get("delivered_at", "")[:10]
            total_stories = sum(b.get("item_count", 0) for b in briefings)
            lines.append("<b>\U0001f4e8 Briefing History</b>")
            lines.append(f"  Total briefings: <b>{total_briefings}</b>")
            lines.append(f"  Stories delivered: <b>{total_stories}</b>")
            lines.append(f"  Active since: {first_date}")
            if total_briefings > 1:
                lines.append(f"  Last briefing: {last_date}")
            lines.append("")

        # Rating engagement
        ratings = analytics.get_user_ratings(user_id, limit=200)
        if ratings:
            ups = sum(1 for r in ratings if r.get("direction") == "up")
            downs = sum(1 for r in ratings if r.get("direction") == "down")
            total_ratings = len(ratings)
            approval_rate = ups / total_ratings if total_ratings else 0

            # Most rated topics
            topic_counts = Counter(r.get("topic", "unknown") for r in ratings)
            top_topics = topic_counts.most_common(3)

            # Most rated sources
            source_counts = Counter(r.get("source", "unknown") for r in ratings)
            top_sources = source_counts.most_common(3)

            lines.append("<b>\u2b50 Rating Activity</b>")
            lines.append(f"  Total ratings: <b>{total_ratings}</b>")
            lines.append(f"  Approval rate: <b>{approval_rate:.0%}</b> ({ups}\u2191 / {downs}\u2193)")
            if top_topics:
                topic_str = ", ".join(
                    f"{t.replace('_', ' ')} ({c})" for t, c in top_topics
                )
                lines.append(f"  Top rated topics: {topic_str}")
            if top_sources:
                src_str = ", ".join(f"{s} ({c})" for s, c in top_sources)
                lines.append(f"  Top rated sources: {src_str}")
            lines.append("")

        # Command usage patterns
        interactions = analytics.get_user_interactions(user_id, limit=200)
        if interactions:
            cmd_counts = Counter(
                i.get("command", i.get("interaction_type", "unknown"))
                for i in interactions
            )
            total_interactions = len(interactions)
            top_cmds = cmd_counts.most_common(5)

            lines.append("<b>\u2699\ufe0f Interaction Patterns</b>")
            lines.append(f"  Total interactions: <b>{total_interactions}</b>")
            if top_cmds:
                for cmd, count in top_cmds:
                    pct = count / total_interactions * 100
                    bar_len = round(pct / 10)
                    bar = "\u2588" * bar_len + "\u2591" * (10 - bar_len)
                    lines.append(f"  {bar} /{html_mod.escape(cmd)} ({count})")
            lines.append("")

        # Preference evolution
        pref_changes = analytics.get_user_preference_history(user_id, limit=50)
        if pref_changes:
            change_types = Counter(p.get("change_type", "unknown") for p in pref_changes)
            lines.append("<b>\U0001f504 Preference Evolution</b>")
            lines.append(f"  Total adjustments: <b>{len(pref_changes)}</b>")
            for ct, count in change_types.most_common(5):
                lines.append(f"  \u2022 {html_mod.escape(ct)}: {count}")
            # Show last 3 changes
            lines.append("  <i>Recent changes:</i>")
            for p in pref_changes[:3]:
                field = p.get("field", "?")
                source = p.get("source", "manual")
                lines.append(f"    \u2022 {html_mod.escape(field)} ({source})")
            lines.append("")

        # Feedback effectiveness
        feedback = analytics.get_user_feedback_history(user_id, limit=20)
        if feedback:
            lines.append("<b>\U0001f4ac Feedback Effectiveness</b>")
            lines.append(f"  Total feedback given: <b>{len(feedback)}</b>")
            for f in feedback[:3]:
                text = (f.get("feedback_text", "") or "")[:40]
                changes = f.get("changes_applied", "")
                if text:
                    lines.append(f'  \u2022 "{html_mod.escape(text)}"')
                    if changes:
                        lines.append(f"    \u2192 {html_mod.escape(str(changes)[:60])}")
            lines.append("")

        if not briefings and not ratings and not interactions:
            lines.append("<i>No analytics data yet. Use /briefing to get started.</i>")

        self._bot.send_message(chat_id, "\n".join(lines).strip())
        return {"action": "stats", "user_id": user_id}

    def _handle_preset(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Save, load, list, or delete briefing presets."""
        import html as html_mod
        profile = self._engine.preferences.get_or_create(user_id)

        parts = args.strip().split(maxsplit=1)
        action = parts[0].lower() if parts else ""
        name = parts[1].strip() if len(parts) > 1 else ""

        if not action or action == "list":
            # Show saved presets
            presets = profile.presets
            if not presets:
                self._bot.send_message(
                    chat_id,
                    "<b>\U0001f4be Briefing Presets</b>\n\n"
                    "<i>No presets saved yet.</i>\n\n"
                    "<b>Save current settings:</b>\n"
                    "/preset save Work\n"
                    "/preset save Weekend\n\n"
                    "<b>Then switch:</b>\n"
                    "/preset load Work"
                )
                return {"action": "preset_list", "user_id": user_id}

            lines = ["<b>\U0001f4be Briefing Presets</b>", ""]
            for pname, pdata in presets.items():
                topics = pdata.get("topic_weights", {})
                top3 = sorted(topics, key=topics.get, reverse=True)[:3]
                topic_str = ", ".join(t.replace("_", " ") for t in top3) if top3 else "default"
                tone = pdata.get("tone", "concise")
                items = pdata.get("max_items", 10)
                conf = pdata.get("confidence_min", 0)
                extras = []
                if conf:
                    extras.append(f"conf\u2265{conf:.0%}")
                urg = pdata.get("urgency_min", "")
                if urg:
                    extras.append(f"urg\u2265{urg}")
                extra_str = f" \u00b7 {', '.join(extras)}" if extras else ""
                lines.append(
                    f"\u2022 <b>{html_mod.escape(pname)}</b>: {topic_str} \u00b7 "
                    f"{tone} \u00b7 {items} items{extra_str}"
                )
            lines.append("")
            lines.append(
                "<b>Commands:</b>\n"
                "/preset load [name] \u2014 Switch to preset\n"
                "/preset save [name] \u2014 Save current settings\n"
                "/preset delete [name] \u2014 Remove preset"
            )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "preset_list", "user_id": user_id}

        if action == "save":
            if not name:
                self._bot.send_message(chat_id, "Usage: /preset save [name]")
                return {"action": "preset_save_help", "user_id": user_id}
            self._engine.preferences.save_preset(user_id, name)
            self._persist_prefs()
            self._bot.send_message(
                chat_id,
                f"\U0001f4be Preset <b>{html_mod.escape(name)}</b> saved.\n"
                f"Switch to it anytime: /preset load {html_mod.escape(name)}"
            )
            return {"action": "preset_save", "user_id": user_id, "name": name}

        if action == "load":
            if not name:
                self._bot.send_message(chat_id, "Usage: /preset load [name]")
                return {"action": "preset_load_help", "user_id": user_id}
            result = self._engine.preferences.load_preset(user_id, name)
            if result is None:
                available = ", ".join(profile.presets.keys()) if profile.presets else "none"
                self._bot.send_message(
                    chat_id,
                    f"Preset <b>{html_mod.escape(name)}</b> not found.\n"
                    f"Available: {html_mod.escape(available)}"
                )
                return {"action": "preset_not_found", "user_id": user_id}
            self._persist_prefs()
            self._bot.send_message(
                chat_id,
                f"\u2705 Switched to preset <b>{html_mod.escape(name)}</b>.\n"
                f"Run /briefing or /quick to see the difference."
            )
            return {"action": "preset_load", "user_id": user_id, "name": name}

        if action == "delete":
            if not name:
                self._bot.send_message(chat_id, "Usage: /preset delete [name]")
                return {"action": "preset_delete_help", "user_id": user_id}
            deleted = self._engine.preferences.delete_preset(user_id, name)
            self._persist_prefs()
            if deleted:
                self._bot.send_message(chat_id, f"Preset <b>{html_mod.escape(name)}</b> deleted.")
            else:
                self._bot.send_message(chat_id, f"Preset <b>{html_mod.escape(name)}</b> not found.")
            return {"action": "preset_delete", "user_id": user_id}

        self._bot.send_message(
            chat_id,
            "Usage: /preset [save|load|delete|list] [name]\n"
            "Example: /preset save Work"
        )
        return {"action": "preset_help", "user_id": user_id}

    def _show_sources(self, chat_id: int | str,
                     user_id: str) -> dict[str, Any]:
        """Show source credibility dashboard with reliability, bias, and trust."""
        credibility = self._engine.credibility
        profile = self._engine.preferences.get_or_create(user_id)

        # Build source data list using public API
        tier_map = {}
        for tier_name, source_ids in credibility.get_all_sources_by_tier().items():
            for sid in source_ids:
                tier_map[sid] = tier_name
        all_source_ids = set(tier_map.keys())

        sources_data: list[dict] = []
        for sid in sorted(all_source_ids):
            sr = credibility.get_source(sid)
            sources_data.append({
                "source_id": sid,
                "tier": tier_map.get(sid, "unknown"),
                "reliability": sr.reliability_score,
                "bias": sr.bias_rating,
                "trust_factor": sr.trust_factor(),
                "corroboration_rate": sr.corroboration_rate,
                "items_seen": sr.total_items_seen,
            })

        formatter = self._engine.formatter
        msg = formatter.format_sources(sources_data, profile.source_weights)
        self._bot.send_message(chat_id, msg)
        return {"action": "sources", "user_id": user_id}

    def _set_filter(self, chat_id: int | str, user_id: str,
                    args: str) -> dict[str, Any]:
        """Set or show advanced briefing filters."""
        import html as html_mod
        profile = self._engine.preferences.get_or_create(user_id)

        if not args.strip():
            # Show current filters
            conf = f"{profile.confidence_min:.0%}" if profile.confidence_min > 0 else "off"
            urg = profile.urgency_min or "off"
            mps = str(profile.max_per_source) if profile.max_per_source > 0 else "off"
            geo_t = f"{profile.alert_georisk_threshold:.0%}"
            trend_t = f"{profile.alert_trend_threshold:.1f}x"
            self._bot.send_message(
                chat_id,
                "<b>\U0001f527 Briefing Filters &amp; Alert Sensitivity</b>\n\n"
                "<b>Story Filters:</b>\n"
                f"\u2022 Confidence threshold: <code>{conf}</code>\n"
                f"\u2022 Minimum urgency: <code>{urg}</code>\n"
                f"\u2022 Max stories per source: <code>{mps}</code>\n\n"
                "<b>Alert Thresholds:</b>\n"
                f"\u2022 Geo-risk alert at: <code>{geo_t}</code>\n"
                f"\u2022 Trend spike alert at: <code>{trend_t}</code>\n\n"
                "<b>Set with:</b>\n"
                "/filter confidence 0.7 \u2014 Only high-confidence stories\n"
                "/filter urgency elevated \u2014 Skip routine stories\n"
                "/filter max_per_source 2 \u2014 Limit source repetition\n"
                "/filter georisk 0.3 \u2014 More sensitive geo-risk alerts\n"
                "/filter trend 2.0 \u2014 More sensitive trend alerts\n"
                "/filter off \u2014 Remove all filters"
            )
            return {"action": "filter_show", "user_id": user_id}

        parts = args.strip().split(maxsplit=1)
        field = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""

        if field == "off":
            # Clear all filters, reset alert thresholds to defaults
            profile.confidence_min = 0.0
            profile.urgency_min = ""
            profile.max_per_source = 0
            profile.alert_georisk_threshold = 0.5
            profile.alert_trend_threshold = 3.0
            self._persist_prefs()
            self._bot.send_message(chat_id, "All briefing filters and alert thresholds reset to defaults.")
            return {"action": "filter_off", "user_id": user_id}

        if field == "confidence":
            try:
                val = float(value)
                self._engine.preferences.set_filter(user_id, "confidence", str(val))
                self._persist_prefs()
                label = f"{val:.0%}" if val > 0 else "off"
                self._bot.send_message(
                    chat_id,
                    f"Confidence filter set to <code>{label}</code>. "
                    f"Stories below this threshold will be hidden."
                )
            except ValueError:
                self._bot.send_message(chat_id, "Usage: /filter confidence 0.7 (value 0.0-1.0)")
            return {"action": "filter_confidence", "user_id": user_id}

        if field == "urgency":
            valid = {"routine", "elevated", "breaking", "critical", "off"}
            if value.lower() not in valid:
                self._bot.send_message(
                    chat_id,
                    "Usage: /filter urgency [routine|elevated|breaking|critical|off]"
                )
                return {"action": "filter_urgency_help", "user_id": user_id}
            urg_val = "" if value.lower() == "off" else value.lower()
            self._engine.preferences.set_filter(user_id, "urgency", urg_val)
            self._persist_prefs()
            label = value.lower() if urg_val else "off"
            self._bot.send_message(
                chat_id,
                f"Urgency filter set to <code>{label}</code>."
            )
            return {"action": "filter_urgency", "user_id": user_id}

        if field in ("max_per_source", "source_limit"):
            try:
                val = int(value) if value.lower() != "off" else 0
                self._engine.preferences.set_filter(user_id, "max_per_source", str(val))
                self._persist_prefs()
                label = str(val) if val > 0 else "off"
                self._bot.send_message(
                    chat_id,
                    f"Source limit set to <code>{label}</code> stories per source."
                )
            except ValueError:
                self._bot.send_message(chat_id, "Usage: /filter max_per_source 2 (1-10 or off)")
            return {"action": "filter_source_limit", "user_id": user_id}

        if field == "georisk":
            try:
                val = float(value)
                self._engine.preferences.set_filter(user_id, "georisk", str(val))
                self._persist_prefs()
                self._bot.send_message(
                    chat_id,
                    f"Geo-risk alert threshold set to <code>{val:.0%}</code>.\n"
                    f"{'Lower = more sensitive.' if val < 0.5 else 'Higher = fewer alerts.'}"
                )
            except ValueError:
                self._bot.send_message(chat_id, "Usage: /filter georisk 0.3 (0.1-1.0)")
            return {"action": "filter_georisk", "user_id": user_id}

        if field == "trend":
            try:
                val = float(value)
                self._engine.preferences.set_filter(user_id, "trend", str(val))
                self._persist_prefs()
                self._bot.send_message(
                    chat_id,
                    f"Trend alert threshold set to <code>{val:.1f}x</code> baseline.\n"
                    f"{'Lower = more alerts.' if val < 3.0 else 'Higher = only extreme spikes.'}"
                )
            except ValueError:
                self._bot.send_message(chat_id, "Usage: /filter trend 2.0 (1.5-10.0)")
            return {"action": "filter_trend", "user_id": user_id}

        self._bot.send_message(
            chat_id,
            "Unknown filter. Available:\n"
            "/filter confidence 0.7\n"
            "/filter urgency elevated\n"
            "/filter max_per_source 2\n"
            "/filter off"
        )
        return {"action": "filter_unknown", "user_id": user_id}

    def _apply_user_filters(self, items: list, profile) -> list:
        """Apply user's advanced filters to briefing items.

        Filters by confidence threshold, urgency minimum, and max-per-source.
        """
        from newsfeed.models.domain import UrgencyLevel

        urgency_rank = {
            "routine": 0, "elevated": 1, "breaking": 2, "critical": 3,
        }

        filtered = list(items)

        # Confidence filter
        if profile.confidence_min > 0:
            filtered = [
                item for item in filtered
                if not item.confidence or item.confidence.mid >= profile.confidence_min
            ]

        # Urgency filter
        if profile.urgency_min:
            min_rank = urgency_rank.get(profile.urgency_min, 0)
            filtered = [
                item for item in filtered
                if urgency_rank.get(item.candidate.urgency.value, 0) >= min_rank
            ]

        # Max per source
        if profile.max_per_source > 0:
            source_count: dict[str, int] = {}
            source_filtered = []
            for item in filtered:
                src = item.candidate.source
                count = source_count.get(src, 0)
                if count < profile.max_per_source:
                    source_filtered.append(item)
                    source_count[src] = count + 1
            filtered = source_filtered

        return filtered

    # ──────────────────────────────────────────────────────────────
    # ADMIN COMMANDS — owner-only analytics dashboard via Telegram
    # ──────────────────────────────────────────────────────────────

    def _is_admin(self, user_id: str) -> bool:
        """Check if user is the admin/owner.

        SECURITY: Requires explicit TELEGRAM_OWNER_ID configuration.
        If not set, admin access is denied entirely — no fallback to
        'first user' which would allow arbitrary privilege escalation.
        """
        import os
        owner_id = os.environ.get("TELEGRAM_OWNER_ID", "")
        if not owner_id:
            return False
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
                self._bot.send_message(chat_id, f"User {html_mod.escape(target)} not found.")
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
                self._bot.send_message(chat_id, f"No interactions for {html_mod.escape(target)}.")
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
                self._bot.send_message(chat_id, f"No ratings for {html_mod.escape(target)}.")
                return {"action": "admin_ratings", "user_id": user_id}
            lines = [f"<b>Ratings: {html_mod.escape(target)}</b> (last 30)", ""]
            for r in ratings:
                ts = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                icon = "\U0001f44d" if r["direction"] == "up" else "\U0001f44e"
                title = html_mod.escape((r["title"] or r["topic"] or "?")[:50])
                lines.append(f"  {ts} {icon} #{r['item_index']} {title} [{html_mod.escape(r['source'] or '')}]")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_ratings", "user_id": user_id}

        if subcmd == "feedback":
            target = subargs.strip() if subargs else user_id
            feedback = db.get_user_feedback_history(target, limit=20)
            if not feedback:
                self._bot.send_message(chat_id, f"No feedback for {html_mod.escape(target)}.")
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
                self._bot.send_message(chat_id, f"No preference changes for {html_mod.escape(target)}.")
                return {"action": "admin_prefs", "user_id": user_id}
            lines = [f"<b>Preference History: {html_mod.escape(target)}</b> (last 30)", ""]
            for p in prefs:
                ts = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                lines.append(
                    f"  {ts} [{html_mod.escape(p['change_type'] or '')}] {html_mod.escape(p['field'] or '')}: "
                    f"{html_mod.escape(str(p['old_value'] or ''))} -> {html_mod.escape(str(p['new_value'] or ''))} ({html_mod.escape(p['source'] or '')})"
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
                self._bot.send_message(chat_id, f"Request {html_mod.escape(subargs)} not found.")
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
                        f"  [{html_mod.escape(c['source'] or '')}] {html_mod.escape((c['title'] or '')[:60])} "
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
                    f"  {html_mod.escape(t['topic'] or '')}: {t['count']} candidates, "
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
                    f"  {html_mod.escape(s['source'] or '')}: {s['total_candidates']} cand, "
                    f"{s['times_selected']} sel ({sel_rate:.0f}%), "
                    f"avg: {s['avg_score']:.3f}"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_sources", "user_id": user_id}

        if subcmd == "briefings":
            target = subargs.strip() if subargs else user_id
            briefings = db.get_user_briefings(target, limit=15)
            if not briefings:
                self._bot.send_message(chat_id, f"No briefings for {html_mod.escape(target)}.")
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

        self._bot.send_message(chat_id, f"Unknown admin command: {html_mod.escape(subcmd)}. Try /admin help")
        return {"action": "admin_unknown", "user_id": user_id}
