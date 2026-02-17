"""Communication agent — bridges Telegram bot and engine for end-to-end interaction.

The communication agent (Layer 0 in the vision) receives user requests via Telegram,
dispatches them through the engine, delivers results, and closes the feedback loop.
It is the single point of integration between the user-facing bot and the backend
intelligence pipeline.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from newsfeed.delivery.bot import TelegramBot, BriefingScheduler
    from newsfeed.orchestration.engine import NewsFeedEngine

from newsfeed.delivery.market import MarketTicker
from newsfeed.orchestration.handlers import HandlerContext
from newsfeed.orchestration.handlers.analysis import (
    handle_compare as _h_compare,
    handle_deep_dive_story as _h_deep_dive_story,
    handle_recall as _h_recall,
    handle_timeline as _h_timeline,
)
from newsfeed.orchestration.handlers.management import (
    handle_save as _h_save,
    handle_saved as _h_saved,
    handle_track as _h_track,
    handle_tracked as _h_tracked,
    handle_unsave as _h_unsave,
    handle_untrack as _h_untrack,
)
from newsfeed.memory.commands import parse_preference_commands_rich
from newsfeed.delivery.onboarding import (
    OnboardingState,
    apply_onboarding_profile,
    build_completion_message,
    build_detail_message,
    build_role_message,
    build_welcome_message,
)
from newsfeed.memory.store import BoundedUserDict, match_tracked

log = logging.getLogger(__name__)

# Fallback topic weights — prefer engine's pipeline config default_topics
_FALLBACK_TOPICS = {
    "geopolitics": 0.8,
    "ai_policy": 0.7,
    "technology": 0.6,
    "markets": 0.5,
}


class DeliveryMetrics:
    """Track delivery success/failure rates across channels.

    Provides a lightweight in-memory view of recent delivery health.
    No persistence needed — resets on restart, which is fine for a
    health dashboard that shows recent trends.
    """

    _CHANNELS = ("telegram", "webhook", "email")

    def __init__(self) -> None:
        self._success: dict[str, int] = defaultdict(int)
        self._failure: dict[str, int] = defaultdict(int)
        self._last_success: dict[str, float] = {}
        self._last_failure: dict[str, float] = {}

    def record_success(self, channel: str) -> None:
        self._success[channel] += 1
        self._last_success[channel] = time.monotonic()

    def record_failure(self, channel: str) -> None:
        self._failure[channel] += 1
        self._last_failure[channel] = time.monotonic()

    def success_rate(self, channel: str) -> float:
        """Return success rate as 0.0–1.0. Returns 1.0 if no deliveries."""
        total = self._success[channel] + self._failure[channel]
        if total == 0:
            return 1.0
        return self._success[channel] / total

    def summary(self) -> dict[str, Any]:
        """Return health summary for all channels."""
        result: dict[str, Any] = {}
        for ch in self._CHANNELS:
            total = self._success[ch] + self._failure[ch]
            result[ch] = {
                "success": self._success[ch],
                "failure": self._failure[ch],
                "total": total,
                "rate": self.success_rate(ch),
            }
        return result


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
        self._delivery_metrics = DeliveryMetrics()
        # Track items shown per user for "show more" dedup
        # All per-user dicts use BoundedUserDict to cap at 500 users
        # with LRU eviction — prevents unbounded memory growth.
        self._shown_ids: BoundedUserDict[set[str]] = BoundedUserDict(maxlen=500)
        # Track last briefing topic per user
        self._last_topic: BoundedUserDict[str] = BoundedUserDict(maxlen=500)
        # Track last briefing items per user for per-item feedback
        self._last_items: BoundedUserDict[list[dict]] = BoundedUserDict(maxlen=500)
        # Per-user rate limiting for resource-intensive commands
        self._rate_limits: BoundedUserDict[float] = BoundedUserDict(maxlen=500)
        self._RATE_LIMIT_SECONDS = 15  # Min seconds between briefing/sitrep/quick
        self._cmd_rate_windows: dict[str, list[float]] = {}  # Per-command sliding window
        # Alert dedup: track which alerts have been sent to avoid repeat notifications.
        # Key: "user_id:alert_type:region_or_topic", Value: monotonic timestamp.
        self._sent_alerts: dict[str, float] = {}
        _ALERT_COOLDOWN_SECONDS = 3600  # 1 hour between identical alerts
        self._ALERT_COOLDOWN = _ALERT_COOLDOWN_SECONDS
        # Onboarding state for interactive setup flow
        self._onboarding: BoundedUserDict[OnboardingState] = BoundedUserDict(maxlen=500)
        # Pending reset confirmations (user_id -> monotonic timestamp)
        self._pending_resets: BoundedUserDict[float] = BoundedUserDict(maxlen=200)
        # Handler context for delegated command modules
        self._ctx = HandlerContext(
            engine=engine, bot=bot, scheduler=scheduler,
            default_topics=self._default_topics,
            shown_ids=self._shown_ids,
            last_topic=self._last_topic,
            last_items=self._last_items,
        )

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

        # Access control gate — check authorization before processing
        ac = self._engine.access_control
        if cmd_type == "command" and command == "start":
            pass  # /start always allowed — triggers registration flow
        elif not ac.is_allowed(user_id):
            self._bot.send_message(
                chat_id,
                "You don't have access to this bot.\n"
                "Use /start to request access from the administrator.",
            )
            return {"action": "access_denied", "user_id": user_id}

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

            elif cmd_type == "onboard":
                result = self._handle_onboard_callback(chat_id, user_id, command)

        except Exception as exc:
            log.exception("Error handling update for user=%s", user_id)
            # Categorize the error so the user gets actionable feedback
            error_msg = self._categorize_error(exc)
            self._bot.send_message(chat_id, error_msg)
            result = {"action": "error", "user_id": user_id,
                      "error_type": type(exc).__name__}

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

    # Per-command rate limits for non-briefing commands (requests per minute)
    _COMMAND_RATE_LIMITS: dict[str, tuple[int, int]] = {
        "feedback": (10, 60),
        "track": (20, 60),
        "untrack": (20, 60),
        "save": (20, 60),
        "unsave": (20, 60),
        "recall": (5, 60),
        "more": (10, 60),
    }

    def _check_command_rate_limit(self, user_id: str, command: str) -> bool:
        """Check per-command rate limit. Returns True if BLOCKED."""
        limits = self._COMMAND_RATE_LIMITS.get(command)
        if not limits:
            return False
        max_requests, window_secs = limits
        now = time.monotonic()
        key = f"{user_id}:{command}"
        timestamps = self._cmd_rate_windows.get(key, [])
        # Expire old timestamps
        timestamps = [t for t in timestamps if now - t < window_secs]
        if not timestamps:
            # Clean up empty key to prevent unbounded growth
            self._cmd_rate_windows.pop(key, None)
        if len(timestamps) >= max_requests:
            self._cmd_rate_windows[key] = timestamps
            return True
        timestamps.append(now)
        self._cmd_rate_windows[key] = timestamps
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

        # Per-command rate limits for lighter commands
        if self._check_command_rate_limit(user_id, command):
            limits = self._COMMAND_RATE_LIMITS.get(command)
            window = limits[1] if limits else 60
            self._bot.send_message(
                chat_id,
                f"Too many /{command} requests — please wait up to {window}s.",
            )
            return {"action": "command_rate_limited", "user_id": user_id, "command": command}

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
                return _h_deep_dive_story(self._ctx, chat_id, user_id, story_num)
            except (ValueError, TypeError):
                pass
            # No story number — prompt user to pick one
            items = self._last_items.get(user_id, [])
            if items:
                import html as html_mod
                lines = [
                    "<b>\U0001f50d Which story to dive into?</b>",
                    "",
                ]
                for i, item in enumerate(items[:10], 1):
                    title = html_mod.escape(item.get("title", "")[:60])
                    lines.append(f"  {i}. {title}")
                lines.append("")
                lines.append("<i>Tap \U0001f50d Dive deeper on a card, or type /deep_dive [number]</i>")
                self._bot.send_message(chat_id, "\n".join(lines))
            else:
                self._bot.send_message(
                    chat_id,
                    "No stories to dive into. Run /briefing first, "
                    "then tap \U0001f50d Dive deeper on any story card."
                )
            return {"action": "deep_dive_prompt", "user_id": user_id}

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
            return _h_compare(self._ctx, chat_id, user_id, args)

        if command == "recall":
            return _h_recall(self._ctx, chat_id, user_id, args)

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
            return _h_track(self._ctx, chat_id, user_id, args)

        if command == "tracked":
            return _h_tracked(self._ctx, chat_id, user_id)

        if command == "untrack":
            return _h_untrack(self._ctx, chat_id, user_id, args)

        if command == "save":
            return _h_save(self._ctx, chat_id, user_id, args)

        if command == "saved":
            return _h_saved(self._ctx, chat_id, user_id)

        if command == "unsave":
            return _h_unsave(self._ctx, chat_id, user_id, args)

        if command == "timeline":
            return _h_timeline(self._ctx, chat_id, user_id, args)

        if command == "email":
            return self._set_email(chat_id, user_id, args)

        if command == "digest":
            return self._send_email_digest(chat_id, user_id)

        if command == "stats":
            return self._show_stats(chat_id, user_id)

        if command == "alert":
            return self._manage_alert(chat_id, user_id, args)

        if command == "source":
            return self._manage_source(chat_id, user_id, args)

        if command == "sources":
            return self._show_sources(chat_id, user_id)

        if command == "webhook":
            return self._set_webhook(chat_id, user_id, args)

        if command == "filter":
            return self._set_filter(chat_id, user_id, args)

        if command == "preset":
            return self._handle_preset(chat_id, user_id, args)

        if command == "reset":
            import time as _time
            # Two-step confirmation to prevent accidental data loss.
            # "reset confirm" or second /reset within 30s = execute.
            if args.strip().lower() == "confirm":
                pass  # confirmed via argument
            elif user_id in self._pending_resets:
                elapsed = _time.monotonic() - self._pending_resets.pop(user_id)
                if elapsed > 30:
                    # Confirmation expired — ask again
                    self._pending_resets[user_id] = _time.monotonic()
                    self._bot.send_message(
                        chat_id,
                        "\u26a0\ufe0f Confirmation expired. Send /reset again within 30 seconds "
                        "or use /reset confirm to erase all preferences.",
                    )
                    return {"action": "reset_pending", "user_id": user_id}
                # Confirmed by double /reset within 30s — fall through
            else:
                self._pending_resets[user_id] = _time.monotonic()
                self._bot.send_message(
                    chat_id,
                    "\u26a0\ufe0f <b>This will erase ALL your preferences</b> "
                    "(topics, sources, schedule, bookmarks, tracked stories).\n\n"
                    "Send /reset again within 30 seconds to confirm, "
                    "or /reset confirm to proceed immediately.",
                    parse_mode="HTML",
                )
                return {"action": "reset_pending", "user_id": user_id}

            self._pending_resets.pop(user_id, None)
            self._engine.preferences.reset(user_id)
            self._engine.apply_user_feedback(user_id, "reset preferences")
            self._shown_ids.pop(user_id, None)
            self._last_items.pop(user_id, None)
            self._last_topic.pop(user_id, None)
            self._bot.send_message(chat_id, "All preferences reset to defaults.")
            return {"action": "reset", "user_id": user_id}

        if command == "transparency":
            return self._show_transparency(chat_id, user_id)

        if command == "admin":
            return self._handle_admin(chat_id, user_id, args)

        if command == "approve":
            ac = self._engine.access_control
            msg = ac.approve_user(user_id, args.strip())
            self._bot.send_message(chat_id, msg)
            return {"action": "approve_user", "user_id": user_id, "target": args.strip()}

        if command == "reject":
            ac = self._engine.access_control
            msg = ac.reject_user(user_id, args.strip())
            self._bot.send_message(chat_id, msg)
            return {"action": "reject_user", "user_id": user_id, "target": args.strip()}

        if command == "promote":
            ac = self._engine.access_control
            msg = ac.promote_to_admin(user_id, args.strip())
            self._bot.send_message(chat_id, msg)
            return {"action": "promote_user", "user_id": user_id, "target": args.strip()}

        if command == "demote":
            ac = self._engine.access_control
            msg = ac.demote_from_admin(user_id, args.strip())
            self._bot.send_message(chat_id, msg)
            return {"action": "demote_user", "user_id": user_id, "target": args.strip()}

        if command == "users":
            ac = self._engine.access_control
            counts = ac.get_user_count()
            pending = ac.get_pending_users()
            import html as html_mod
            lines = [
                "<b>User Access Summary</b>",
                f"  Allowed: {counts['allowed']}",
                f"  Admins: {counts['admin']}",
                f"  Pending: {counts['pending']}",
            ]
            if pending:
                lines.append("")
                lines.append("<b>Pending Approval:</b>")
                for uid in pending:
                    lines.append(f"  {html_mod.escape(uid)} — /approve {html_mod.escape(uid)}")
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "users_list", "user_id": user_id}

        if command == "_stale_callback":
            self._bot.send_message(
                chat_id,
                "This button is no longer active. "
                "Run /briefing for a fresh briefing with working controls."
            )
            return {"action": "stale_callback", "user_id": user_id}

        # Unknown command
        import html as html_mod
        self._bot.send_message(chat_id, f"Unknown command: /{html_mod.escape(command)}. Try /help")
        return {"action": "unknown_command", "user_id": user_id, "command": command}

    def _onboard(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Welcome a new user with interactive onboarding flow.

        Starts a 3-step personalization: topics -> role -> detail level.
        Seeds the profile in ~30 seconds instead of requiring iterative /feedback.
        If access control is active, handles registration before onboarding.
        """
        ac = self._engine.access_control
        if not ac.is_allowed(user_id):
            msg = ac.request_access(user_id)
            self._bot.send_message(chat_id, msg)
            # Notify admins of pending request
            pending = ac.get_pending_users()
            if pending and user_id in pending:
                for admin_id in ac._admin_users:
                    if admin_id != user_id:
                        self._bot.send_message(
                            admin_id,
                            f"New access request from user {user_id}.\n"
                            f"Approve: /approve {user_id}\n"
                            f"Reject: /reject {user_id}",
                        )
            if not ac.is_allowed(user_id):
                return {"action": "access_requested", "user_id": user_id}

        self._engine.preferences.get_or_create(user_id)
        self._engine.analytics.record_user_seen(user_id, chat_id)

        # Initialize onboarding state
        self._onboarding[user_id] = OnboardingState()

        text, keyboard = build_welcome_message()
        self._bot.send_message(chat_id, text, reply_markup=keyboard)
        return {"action": "onboard", "user_id": user_id}

    def _handle_onboard_callback(self, chat_id: int | str, user_id: str,
                                  callback_data: str) -> dict[str, Any]:
        """Handle onboarding inline keyboard callbacks.

        Callback data format: "onboard:topic:geopolitics", "onboard:topics_done",
        "onboard:role:investor", "onboard:detail:standard"
        """
        state = self._onboarding.get(user_id)
        if not state:
            # No active onboarding — start fresh
            return self._onboard(chat_id, user_id)

        parts = callback_data.split(":")
        if len(parts) < 2:
            return {"action": "onboard_error", "user_id": user_id}

        action = parts[1] if len(parts) >= 2 else ""
        value = parts[2] if len(parts) >= 3 else ""

        import html as html_mod

        if action == "topic" and value:
            # Toggle topic selection
            if value in state.selected_topics:
                state.selected_topics.remove(value)
            else:
                if len(state.selected_topics) < 5:
                    state.selected_topics.append(value)
                else:
                    self._bot.send_message(chat_id, "Max 5 topics. Deselect one first, or tap 'Done'.")
                    return {"action": "onboard_topic_max", "user_id": user_id}

            # Show updated selection
            if state.selected_topics:
                from newsfeed.delivery.onboarding import TOPIC_OPTIONS
                names = [dict(TOPIC_OPTIONS).get(t, t) for t in state.selected_topics]
                self._bot.send_message(
                    chat_id,
                    f"Selected: <b>{html_mod.escape(', '.join(names))}</b> "
                    f"({len(state.selected_topics)}/5)\n"
                    "Tap more topics or 'Done selecting topics \u2192'"
                )
            return {"action": "onboard_topic_toggle", "user_id": user_id,
                    "topics": list(state.selected_topics)}

        if action == "topics_done":
            if len(state.selected_topics) < 1:
                self._bot.send_message(chat_id, "Please select at least 1 topic.")
                return {"action": "onboard_topics_empty", "user_id": user_id}

            state.step = "role"
            text, keyboard = build_role_message(state.selected_topics)
            self._bot.send_message(chat_id, text, reply_markup=keyboard)
            return {"action": "onboard_topics_done", "user_id": user_id,
                    "topics": list(state.selected_topics)}

        if action == "role" and value:
            state.role = value
            state.step = "detail"
            text, keyboard = build_detail_message(value)
            self._bot.send_message(chat_id, text, reply_markup=keyboard)
            return {"action": "onboard_role", "user_id": user_id, "role": value}

        if action == "detail" and value:
            state.detail_level = value
            state.step = "done"

            # Apply all selections to profile
            effective_weights = apply_onboarding_profile(
                self._engine.preferences,
                user_id,
                state.selected_topics,
                state.role,
                state.detail_level,
            )
            self._persist_prefs()

            # Send completion message
            msg = build_completion_message(
                state.selected_topics, state.role, state.detail_level,
                effective_weights,
            )
            self._bot.send_message(chat_id, msg)

            # Clean up onboarding state
            self._onboarding.pop(user_id, None)

            # Auto-trigger first briefing — don't make user type /briefing
            self._bot.send_message(
                chat_id,
                "<i>Generating your first personalized briefing...</i>"
            )
            self._run_briefing(chat_id, user_id)

            return {"action": "onboard_complete", "user_id": user_id,
                    "topics": state.selected_topics, "role": state.role,
                    "detail": state.detail_level}

        # Unrecognized onboarding callback
        return {"action": "onboard_unknown", "user_id": user_id}

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

        items = self._engine.last_report_items(user_id)
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

        # Build continuity summary from delta tags
        continuity = self._build_continuity_summary(delta_tags, tracked_count)

        # Message 1: Header (ticker + exec summary + geo risks + trends + threads)
        header = formatter.format_header(payload, ticker_bar, tracked_count=tracked_count)
        if continuity:
            header += f"\n{continuity}"
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
            # Explain which filters caused the empty result
            filter_hints = []
            if profile.confidence_min > 0:
                filter_hints.append(f"confidence \u2265 {profile.confidence_min:.0%}")
            if profile.urgency_min:
                filter_hints.append(f"urgency \u2265 {profile.urgency_min}")
            if profile.max_per_source > 0:
                filter_hints.append(f"max {profile.max_per_source}/source")
            if profile.muted_topics:
                filter_hints.append(f"{len(profile.muted_topics)} muted topics")
            if filter_hints:
                hint_str = ", ".join(filter_hints)
                msg = (
                    f"No stories passed your active filters ({hint_str}).\n"
                    f"Try /filter confidence 0 or /feedback reset filters to see more."
                )
            else:
                msg = "No stories matched your current interests. Try /feedback to adjust."
            self._bot.send_message(chat_id, msg)

        # Topic discovery — surface emerging trends the user doesn't track
        if payload.trends and payload.items:
            emerging = [t.topic for t in payload.trends if t.is_emerging]
            user_weights = dict(profile.topic_weights) if profile.topic_weights else dict(topics)
            discovery_msg = formatter.format_topic_discovery(emerging, user_weights)
            if discovery_msg:
                self._bot.send_message(chat_id, discovery_msg)

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
        """Show more items from reserve cache with context, or signal caught-up state."""
        import html as html_mod
        topic = topic_hint.strip().lower().replace(" ", "_") if topic_hint else self._last_topic.get(user_id, "general")
        topic_name = html_mod.escape(topic.replace("_", " ").title())
        seen = self._shown_ids.get(user_id, set())

        more = self._engine.show_more(user_id, topic, seen, limit=5)

        if more:
            # Context label — these are reserve items below the briefing threshold
            lines = [
                f"<b>More on {topic_name}</b> "
                f"<i>(reserve stories \u2014 slightly below briefing threshold)</i>",
                "",
            ]
            for c in more:
                title_esc = html_mod.escape(c.title)
                score = c.composite_score()
                conf_label = f" <i>({score:.0%} confidence)</i>"
                if c.url and not c.url.startswith("https://example.com"):
                    lines.append(
                        f'\u2022 <a href="{html_mod.escape(c.url)}">{title_esc}</a> '
                        f'[{html_mod.escape(c.source)}]{conf_label}'
                    )
                else:
                    lines.append(
                        f"\u2022 {title_esc} [{html_mod.escape(c.source)}]{conf_label}"
                    )
                if c.summary:
                    lines.append(f"  <i>{html_mod.escape(c.summary[:120])}</i>")
                self._shown_ids.setdefault(user_id, set()).add(c.candidate_id)
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "show_more", "user_id": user_id, "count": len(more)}

        # Cache exhausted — "all caught up" moment
        total_seen = len(seen)
        self._bot.send_message(
            chat_id,
            f"\u2705 <b>You're all caught up on {topic_name}!</b>\n\n"
            f"You've seen {total_seen} stories on this topic today.\n"
            f"No new high-confidence items since your last briefing.\n\n"
            "<i>Check back in a few hours for new developments, or:\n"
            "\u2022 /briefing \u2014 Full briefing on all topics\n"
            "\u2022 /quick \u2014 Quick headline scan\n"
            "\u2022 /filter confidence 0.5 \u2014 Lower threshold to see more</i>"
        )
        return {"action": "show_more_caught_up", "user_id": user_id, "seen": total_seen}

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
        """Apply user feedback to preferences with fuzzy matching and rich preview."""
        import html as html_mod

        # Collect known topics for fuzzy matching
        profile = self._engine.preferences.get_or_create(user_id)
        known_topics = set(profile.topic_weights.keys())
        known_topics.update(self._default_topics.keys())

        # Parse with fuzzy matching to catch typos
        parse_result = parse_preference_commands_rich(
            text, known_topics=known_topics,
            deltas=getattr(self._engine, '_preference_deltas', None),
        )

        # Surface unrecognized values immediately
        if parse_result.unrecognized and not parse_result.commands:
            hints = "\n".join(
                f"\u2022 {html_mod.escape(u)}" for u in parse_result.unrecognized
            )
            self._bot.send_message(chat_id, f"\u26a0\ufe0f {hints}")
            return {"action": "feedback_invalid", "user_id": user_id,
                    "errors": parse_result.unrecognized}

        # Apply via engine (use standard path for actual application)
        results = self._engine.apply_user_feedback(
            user_id, text, is_admin=self._is_admin(user_id)
        )

        # Send a concise confirmation of each change applied
        if results:
            summary_lines = []
            for k, v in results.items():
                if not k.startswith("hint:"):
                    summary_lines.append(f"{k} \u2192 {v}")
            if summary_lines:
                confirmation = "\n".join(summary_lines)
                self._bot.send_message(
                    chat_id,
                    f"<b>Confirmed changes:</b>\n{html_mod.escape(confirmation)}",
                )

        # If rich parser corrected topics, re-apply with corrected topics
        if parse_result.corrections and parse_result.commands:
            for cmd in parse_result.commands:
                if cmd.action == "topic_delta" and cmd.topic and cmd.value:
                    delta = float(cmd.value)
                    _, hint = self._engine.preferences.apply_weight_adjustment(
                        user_id, cmd.topic, delta)
                    results[f"topic:{cmd.topic}"] = str(
                        self._engine.preferences.get_or_create(user_id)
                        .topic_weights.get(cmd.topic, 0.0))
                    if hint:
                        results[f"hint:{cmd.topic}"] = hint

        if results:
            # Refresh profile after changes
            profile = self._engine.preferences.get_or_create(user_id)
            lines: list[str] = ["<b>Preferences updated:</b>", ""]

            # Show corrections first so user knows what happened
            for correction in parse_result.corrections:
                lines.append(f"\U0001f504 <i>{html_mod.escape(correction)}</i>")
            if parse_result.corrections:
                lines.append("")

            for key, val in results.items():
                if key.startswith("hint:"):
                    lines.append(f"\u26a0\ufe0f <i>{html_mod.escape(str(val))}</i>")
                else:
                    lines.append(f"\u2022 {html_mod.escape(str(key))} = {html_mod.escape(str(val))}")

            # Show updated topic balance if any topic changes
            topic_changes = [k for k in results if k.startswith("topic:")]
            if topic_changes and profile.topic_weights:
                lines.append("")
                lines.append("<b>Your topic balance:</b>")
                sorted_topics = sorted(profile.topic_weights.items(), key=lambda x: x[1], reverse=True)
                for topic, weight in sorted_topics[:6]:
                    name = html_mod.escape(topic.replace("_", " ").title())
                    bar = "\u2588" * max(1, int(abs(weight) * 10))
                    lines.append(f"  {name}: {weight:.0%} {bar}")
                lines.append("")
                total_items = profile.max_items
                top_topic = sorted_topics[0][0].replace("_", " ").title() if sorted_topics else "general"
                lines.append(
                    f"<i>Next briefing (~{total_items} stories) will favor "
                    f"{html_mod.escape(top_topic)}</i>"
                )

            # Show source weight changes
            source_changes = [k for k in results if k.startswith("source:")]
            if source_changes and profile.source_weights:
                lines.append("")
                lines.append("<b>Source preferences:</b>")
                for src, sw in sorted(profile.source_weights.items(), key=lambda x: -x[1]):
                    label = "\u2191 boosted" if sw > 0 else "\u2193 demoted"
                    lines.append(f"  {html_mod.escape(src)}: {label}")

            # Surface any unrecognized parts alongside successful changes
            if parse_result.unrecognized:
                lines.append("")
                for u in parse_result.unrecognized:
                    lines.append(f"\u26a0\ufe0f <i>{html_mod.escape(u)}</i>")

            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "feedback", "user_id": user_id, "changes": results,
                    "corrections": parse_result.corrections}

        # No preference match — try conversational intent detection
        intent, arg = self._detect_intent(text)

        if intent == "briefing_query":
            import html as html_mod
            self._bot.send_message(chat_id, f"Pulling intelligence on {html_mod.escape(arg)}...")
            return self._run_briefing(chat_id, user_id, topic_hint=arg)

        if intent == "trending":
            return self._show_weekly(chat_id, user_id)

        if intent == "search_query":
            return _h_recall(self._ctx, chat_id, user_id, arg)

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

        import html as html_mod
        source = item.get("source", "")
        topic_name = html_mod.escape(topic.replace("_", " ").title())
        source_name = html_mod.escape(source) if source else ""

        if direction == "up":
            self._engine.apply_user_feedback(user_id, f"more {topic}")
            if source:
                self._engine.preferences.apply_source_weight(user_id, source, 0.3)
            # Show specific impact — make the learning feel immediate
            profile = self._engine.preferences.get_or_create(user_id)
            weight = profile.topic_weights.get(topic, 0.5)
            msg = f"\U0001f44d Got it \u2014 more <b>{topic_name}</b>"
            if source_name:
                msg += f" from {source_name}"
            msg += f"\n<i>Topic weight now {weight:.0%}. Next briefing will reflect this.</i>"
            self._bot.send_message(chat_id, msg)
        elif direction == "down":
            self._engine.apply_user_feedback(user_id, f"less {topic}")
            if source:
                self._engine.preferences.apply_source_weight(user_id, source, -0.3)
            profile = self._engine.preferences.get_or_create(user_id)
            weight = profile.topic_weights.get(topic, 0.5)
            msg = f"\U0001f44e Got it \u2014 less <b>{topic_name}</b>"
            if source_name:
                msg += f" from {source_name}"
            msg += f"\n<i>Topic weight now {weight:.0%}. Next briefing will reflect this.</i>"
            self._bot.send_message(chat_id, msg)
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

    def _show_transparency(self, chat_id: int | str, user_id: str) -> dict[str, Any]:
        """Show pipeline trace for the last briefing — full decision transparency.

        Exposes the audit trail data that the pipeline already tracks:
        - How many candidates were researched and from which agents
        - Intelligence pipeline timing
        - Expert council voting breakdown (agreements, rejections, arbitrations)
        - Editorial review changes
        """
        import html as html_mod

        # Get the most recent request from audit trail
        recent = self._engine.audit.get_recent_requests(limit=1)
        if not recent:
            self._bot.send_message(
                chat_id,
                "No pipeline data available yet. Run /briefing first."
            )
            return {"action": "transparency_empty", "user_id": user_id}

        request_id = recent[0]
        trace = self._engine.audit.get_request_trace(request_id)
        if not trace:
            self._bot.send_message(chat_id, "No trace data for last request.")
            return {"action": "transparency_empty", "user_id": user_id}

        # Parse trace into sections
        research_events = [e for e in trace if e["type"] == "research"]
        vote_events = [e for e in trace if e["type"] == "vote"]
        selection_events = [e for e in trace if e["type"] == "selection"]
        review_events = [e for e in trace if e["type"] == "review"]
        delivery_events = [e for e in trace if e["type"] == "delivery"]

        lines: list[str] = []
        lines.append("<b>Pipeline Transparency Report</b>")
        lines.append("")

        # Research phase
        if research_events:
            total_candidates = sum(e.get("candidate_count", 0) for e in research_events)
            avg_latency = (
                sum(e.get("latency_ms", 0) for e in research_events) / len(research_events)
            ) if research_events else 0
            lines.append(f"<b>Research:</b> {total_candidates} candidates from "
                         f"{len(research_events)} agents ({avg_latency:.0f}ms avg)")
            # Top contributing agents
            top_agents = sorted(research_events, key=lambda e: e.get("candidate_count", 0), reverse=True)[:5]
            for e in top_agents:
                agent = html_mod.escape(e.get("agent_id", "?"))
                count = e.get("candidate_count", 0)
                lines.append(f"  \u2022 {agent}: {count} candidates")

        # Expert council
        if vote_events:
            keeps = sum(1 for v in vote_events if v.get("keep"))
            drops = sum(1 for v in vote_events if not v.get("keep"))
            arbitrated = sum(1 for v in vote_events if v.get("arbitrated"))
            lines.append("")
            lines.append(f"<b>Expert Council:</b> {len(vote_events)} votes "
                         f"({keeps} keep, {drops} drop)")
            if arbitrated:
                lines.append(f"  \u2022 {arbitrated} votes revised through arbitration")

            # Expert disagreement summary
            from collections import defaultdict
            candidate_votes: dict[str, dict] = defaultdict(lambda: {"keep": 0, "drop": 0})
            for v in vote_events:
                cid = v.get("candidate_id", "?")
                if v.get("keep"):
                    candidate_votes[cid]["keep"] += 1
                else:
                    candidate_votes[cid]["drop"] += 1

            contested = [
                (cid, cv) for cid, cv in candidate_votes.items()
                if cv["keep"] > 0 and cv["drop"] > 0
            ]
            if contested:
                lines.append(f"  \u2022 {len(contested)} stories had split votes (experts disagreed)")

        # Selection
        if selection_events:
            selected = sum(1 for s in selection_events if s.get("selected"))
            rejected = sum(1 for s in selection_events if not s.get("selected"))
            lines.append("")
            lines.append(f"<b>Selection:</b> {selected} stories selected, "
                         f"{rejected} filtered out")

        # Editorial review
        if review_events:
            rewrites = sum(1 for r in review_events if r.get("changed"))
            lines.append("")
            lines.append(f"<b>Editorial Review:</b> {rewrites}/{len(review_events)} "
                         f"fields rewritten by review agents")

        # Delivery timing
        if delivery_events:
            elapsed = delivery_events[0].get("total_elapsed_s", 0)
            items = delivery_events[0].get("item_count", 0)
            lines.append("")
            lines.append(f"<b>Delivery:</b> {items} stories in {elapsed:.2f}s total")

        # Last briefing's pipeline_trace from metadata (if available)
        report_items = self._engine.last_report_items(user_id)
        if not report_items and not research_events:
            lines.append("")
            lines.append("<i>Run /briefing to generate pipeline data.</i>")

        lines.append("")
        lines.append("<i>The audit trail tracks every decision. "
                     "This is the reasoning behind your briefing.</i>")

        self._bot.send_message(chat_id, "\n".join(lines))
        return {"action": "transparency", "user_id": user_id}

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
        # Sync cadence to user profile so it survives persistence
        cadence = schedule_type if schedule_type != "off" else "on_demand"
        self._engine.preferences.apply_cadence(user_id, cadence)
        self._persist_prefs(chat_id)
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
                self._delivery_metrics.record_success("telegram")
                # Auto-send email digest if user has email configured
                self._auto_email_digest(user_id)
            except Exception:
                log.exception("Failed to deliver scheduled briefing to user=%s", user_id)
                self._delivery_metrics.record_failure("telegram")
                # Notify the user so they know their briefing didn't arrive
                try:
                    self._bot.send_message(
                        user_id,
                        "\u26a0\ufe0f Your scheduled briefing could not be generated "
                        "due to a temporary issue. You can run /briefing manually, "
                        "or wait for your next scheduled delivery."
                    )
                except Exception:
                    pass  # If we can't even notify, log already captured it

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

    def _persist_prefs(self, chat_id: int | str | None = None) -> bool:
        """Persist preferences immediately. Returns True on success.

        If chat_id is provided and persistence fails, notifies the user
        so they know their change may not survive a restart.
        """
        try:
            self._engine.persist_preferences()
            return True
        except Exception:
            log.exception("Failed to persist preferences")
            if chat_id is not None:
                try:
                    self._bot.send_message(
                        chat_id,
                        "\u26a0\ufe0f Your change was applied but could not be saved to disk. "
                        "It may be lost if the system restarts. Please try again."
                    )
                except Exception:
                    pass
            return False

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
                f"Set with: /timezone America/New_York"
            )
            return {"action": "timezone_show", "user_id": user_id}

        # Validate timezone against zoneinfo database before accepting
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(tz)  # Raises KeyError if invalid
        except (KeyError, Exception):
            import html as html_mod
            self._bot.send_message(
                chat_id,
                f"Unknown timezone <code>{html_mod.escape(tz)}</code>.\n"
                f"Examples: America/New_York, Europe/London, Asia/Tokyo, UTC"
            )
            return {"action": "timezone_invalid", "user_id": user_id}

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
                    suggestions.append(f"{name} is already at max weight \u2014 you clearly love this topic")
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

        # Evict expired alert dedup entries to prevent unbounded growth
        now = time.monotonic()
        self._sent_alerts = {
            k: v for k, v in self._sent_alerts.items()
            if now - v < self._ALERT_COOLDOWN
        }

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
                    # Dedup: skip if same alert was sent recently
                    alert_key = f"{user_id}:georisk:{region}"
                    now = time.monotonic()
                    last_sent = self._sent_alerts.get(alert_key)
                    if last_sent is not None and now - last_sent < self._ALERT_COOLDOWN:
                        continue
                    msg = formatter.format_intelligence_alert("georisk", alert_data)
                    self._bot.send_message(user_id, msg)
                    self._auto_webhook_alert(user_id, "georisk", alert_data)
                    self._sent_alerts[alert_key] = now
                    self._delivery_metrics.record_success("telegram")
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
                    # Dedup: skip if same alert was sent recently
                    alert_key = f"{user_id}:trend:{topic}"
                    now = time.monotonic()
                    last_sent = self._sent_alerts.get(alert_key)
                    if last_sent is not None and now - last_sent < self._ALERT_COOLDOWN:
                        continue
                    msg = formatter.format_intelligence_alert("trend", alert_data)
                    self._bot.send_message(user_id, msg)
                    self._auto_webhook_alert(user_id, "trend", alert_data)
                    self._sent_alerts[alert_key] = now
                    self._delivery_metrics.record_success("telegram")
                    sent += 1

        return sent

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

        # Security: reject CRLF injection and enforce length cap
        if any(c in email for c in ("\r", "\n", "\x00")) or len(email) > 254:
            self._bot.send_message(chat_id, "That doesn't look like a valid email address.")
            return {"action": "email_invalid", "user_id": user_id}
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
            from newsfeed.delivery.webhook import send_webhook_with_detail
            success, error_detail = send_webhook_with_detail(profile.webhook_url, test_payload)
            if success:
                self._bot.send_message(chat_id, "\u2705 Test payload delivered successfully.")
            else:
                import html as html_mod
                safe_detail = html_mod.escape(error_detail)
                self._bot.send_message(
                    chat_id,
                    f"\u274c Delivery failed: {safe_detail}\n"
                    f"Check your endpoint accepts POST with Content-Type: application/json."
                )
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
        report_items = self._engine.last_report_items(user_id)
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

        report_items = self._engine.last_report_items(user_id)
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
            success = digest.send(profile.email, subject, html_body)
            if success:
                log.info("Auto-sent email digest to user=%s", user_id)
                self._delivery_metrics.record_success("email")
            else:
                log.warning("Email digest send returned failure for user=%s", user_id)
                self._delivery_metrics.record_failure("email")
        except Exception:
            log.warning("Auto email digest failed for user=%s", user_id, exc_info=True)
            self._delivery_metrics.record_failure("email")

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

    # Webhook circuit breaker: after N consecutive failures, disable webhook
    # and notify user via Telegram. Prevents hammering dead endpoints.
    _WEBHOOK_MAX_FAILURES = 5
    _webhook_fail_counts: dict[str, int] = {}

    def _auto_webhook_briefing(self, user_id: str, items: list) -> None:
        """Push briefing to user's webhook if configured."""
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.webhook_url or not items:
            return
        # Circuit breaker: skip if already tripped
        if self._webhook_fail_counts.get(user_id, 0) >= self._WEBHOOK_MAX_FAILURES:
            return
        try:
            from newsfeed.delivery.webhook import (
                _detect_platform, format_briefing_payload, send_webhook, validate_webhook_url,
            )
            # Re-validate URL at delivery time (DNS may have changed since setup)
            valid, _err = validate_webhook_url(profile.webhook_url)
            if not valid:
                log.warning("Webhook URL failed re-validation for user=%s: %s", user_id, _err)
                self._record_webhook_failure(user_id, profile)
                return
            platform = _detect_platform(profile.webhook_url)
            payload = format_briefing_payload(user_id, items, platform)
            success = send_webhook(profile.webhook_url, payload)
            if success:
                log.info("Webhook briefing pushed for user=%s", user_id)
                self._webhook_fail_counts.pop(user_id, None)
                self._delivery_metrics.record_success("webhook")
            else:
                self._record_webhook_failure(user_id, profile)
                self._delivery_metrics.record_failure("webhook")
        except Exception:
            log.warning("Webhook briefing failed for user=%s", user_id, exc_info=True)
            self._record_webhook_failure(user_id, profile)
            self._delivery_metrics.record_failure("webhook")

    def _resolve_chat_id(self, user_id: str) -> str | int | None:
        """Resolve a user_id to their most recent chat_id for notifications.

        In Telegram DMs, chat_id == user_id, so we fall back to that if
        analytics doesn't have a record.
        """
        try:
            summary = self._engine.analytics.get_user_summary(user_id)
            if summary and summary.get("chat_id"):
                return summary["chat_id"]
        except Exception:
            pass
        # In Telegram DMs, user_id works as chat_id
        return user_id

    def _record_webhook_failure(self, user_id: str, profile) -> None:
        """Track webhook failures and disable after too many consecutive ones."""
        count = self._webhook_fail_counts.get(user_id, 0) + 1
        self._webhook_fail_counts[user_id] = count
        if count >= self._WEBHOOK_MAX_FAILURES:
            log.warning("Webhook disabled for user=%s after %d consecutive failures", user_id, count)
            chat_id = self._resolve_chat_id(user_id)
            if chat_id:
                try:
                    self._bot.send_message(
                        chat_id,
                        f"\u26a0\ufe0f Your webhook has been disabled after {count} consecutive delivery "
                        f"failures. Briefings will continue via Telegram.\n"
                        f"Fix your endpoint and run /webhook test to re-enable."
                    )
                except Exception:
                    pass
            profile.webhook_url = ""
            self._persist_prefs()

    def _auto_webhook_alert(self, user_id: str, alert_type: str,
                            alert_data: dict) -> None:
        """Push an intelligence alert to user's webhook if configured."""
        profile = self._engine.preferences.get_or_create(user_id)
        if not profile.webhook_url:
            return
        try:
            from newsfeed.delivery.webhook import (
                _detect_platform, format_alert_payload, send_webhook,
                validate_webhook_url,
            )
            # Re-validate URL at delivery time (same as briefing webhook)
            valid, _err = validate_webhook_url(profile.webhook_url)
            if not valid:
                log.warning("Webhook alert URL failed re-validation for user=%s: %s", user_id, _err)
                return
            platform = _detect_platform(profile.webhook_url)
            payload = format_alert_payload(alert_type, alert_data, platform)
            success = send_webhook(profile.webhook_url, payload)
            if success:
                self._delivery_metrics.record_success("webhook")
            else:
                self._delivery_metrics.record_failure("webhook")
        except Exception:
            log.debug("Webhook alert failed for user=%s", user_id, exc_info=True)
            self._delivery_metrics.record_failure("webhook")

    @staticmethod
    def _build_continuity_summary(delta_tags: list[str], tracked_count: int) -> str:
        """Build a one-line continuity header from delta tags.

        Returns something like: "Since last briefing: 6 new, 3 developing, 1 tracked"
        This is the #1 thing a returning user scans for — what changed?
        """
        from collections import Counter
        counts = Counter(delta_tags)
        new = counts.get("new", 0)
        developing = counts.get("developing", 0)
        updated = counts.get("updated", 0)

        if not any([new, developing, updated]):
            return ""

        parts: list[str] = []
        if new:
            parts.append(f"{new} new")
        if developing:
            parts.append(f"{developing} developing")
        if updated:
            parts.append(f"{updated} updated")
        if tracked_count:
            parts.append(f"{tracked_count} tracked")

        return f"<i>Since last briefing: {', '.join(parts)}</i>"

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
        report_items = self._engine.last_report_items(user_id)
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
            _, preset_err = self._engine.preferences.save_preset(user_id, name)
            if preset_err:
                self._bot.send_message(chat_id, preset_err)
                return {"action": "preset_save_error", "user_id": user_id}
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

    def _manage_alert(self, chat_id: int | str, user_id: str,
                      args: str) -> dict[str, Any]:
        """Handle /alert add|list|remove commands for keyword alerts."""
        import html as html_mod

        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        subargs = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "add":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /alert add <keyword or phrase>")
                return {"action": "alert_add_help", "user_id": user_id}
            _, error = self._engine.preferences.add_alert_keyword(user_id, subargs)
            if error:
                self._bot.send_message(chat_id, f"\u274c {html_mod.escape(error)}")
                return {"action": "alert_add_failed", "user_id": user_id}
            self._persist_prefs()
            self._bot.send_message(
                chat_id,
                f"\u2705 Alert set for '<code>{html_mod.escape(subargs.lower()[:50])}</code>'.\n"
                "Matching stories will be priority-boosted and flagged in your briefings."
            )
            return {"action": "alert_added", "user_id": user_id, "keyword": subargs}

        if subcmd == "remove":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /alert remove <keyword>")
                return {"action": "alert_remove_help", "user_id": user_id}
            _, removed = self._engine.preferences.remove_alert_keyword(user_id, subargs)
            if removed:
                self._persist_prefs()
                self._bot.send_message(
                    chat_id,
                    f"\u2705 Alert for '<code>{html_mod.escape(subargs.lower())}</code>' removed."
                )
                return {"action": "alert_removed", "user_id": user_id}
            self._bot.send_message(
                chat_id,
                f"No alert found for '<code>{html_mod.escape(subargs.lower())}</code>'."
            )
            return {"action": "alert_not_found", "user_id": user_id}

        if subcmd == "list":
            profile = self._engine.preferences.get_or_create(user_id)
            keywords = profile.alert_keywords
            if not keywords:
                self._bot.send_message(
                    chat_id,
                    "<b>\U0001f514 Keyword Alerts</b>\n\n"
                    "No alerts set.\n\n"
                    "Add one with: /alert add <keyword or phrase>"
                )
                return {"action": "alert_list_empty", "user_id": user_id}
            lines = [f"<b>\U0001f514 Keyword Alerts</b> ({len(keywords)}/20)", ""]
            for i, kw in enumerate(keywords, 1):
                lines.append(f"{i}. <code>{html_mod.escape(kw)}</code>")
            lines.extend(["", "Remove with: /alert remove <keyword>"])
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "alert_list", "user_id": user_id, "count": len(keywords)}

        # Default: show help
        self._bot.send_message(
            chat_id,
            "<b>\U0001f514 Keyword Alerts</b>\n\n"
            "Get priority-boosted stories when specific keywords appear.\n"
            "Works across all topics and sources.\n\n"
            "<b>Commands:</b>\n"
            "/alert add <keyword> \u2014 Add a keyword alert\n"
            "/alert list \u2014 View active alerts\n"
            "/alert remove <keyword> \u2014 Remove an alert\n\n"
            "<b>Examples:</b>\n"
            "<code>/alert add quantum computing</code>\n"
            "<code>/alert add SEC regulation</code>\n"
            "<code>/alert add TSMC</code>"
        )
        return {"action": "alert_help", "user_id": user_id}

    def _manage_source(self, chat_id: int | str, user_id: str,
                       args: str) -> dict[str, Any]:
        """Handle /source add|list|remove|test commands for custom RSS feeds."""
        import html as html_mod
        from newsfeed.agents.dynamic_sources import (
            MAX_CUSTOM_SOURCES_PER_USER,
            discover_feed,
            validate_source_name,
        )

        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        subargs = parts[1].strip() if len(parts) > 1 else ""

        if subcmd == "add":
            return self._source_add(chat_id, user_id, subargs)

        if subcmd == "list":
            sources = self._engine.preferences.get_custom_sources(user_id)
            if not sources:
                self._bot.send_message(
                    chat_id,
                    "<b>\U0001f4e1 Custom Sources</b>\n\n"
                    "No custom sources added yet.\n\n"
                    "Add one with: /source add https://example.com [name]"
                )
                return {"action": "source_list_empty", "user_id": user_id}
            lines = [
                f"<b>\U0001f4e1 Custom Sources</b> "
                f"({len(sources)}/{MAX_CUSTOM_SOURCES_PER_USER})",
                "",
            ]
            for i, src in enumerate(sources, 1):
                title = html_mod.escape(src.get("feed_title") or src["name"])
                name = html_mod.escape(src["name"])
                topics = ", ".join(src.get("topics", ["general"]))
                lines.append(
                    f"{i}. <b>{title}</b> (<code>{name}</code>)\n"
                    f"   Topics: {html_mod.escape(topics)}\n"
                    f"   Feed: <code>{html_mod.escape(src['feed_url'][:80])}</code>"
                )
            lines.extend([
                "",
                "Remove with: /source remove [name]",
                "Test with: /source test [name]",
            ])
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "source_list", "user_id": user_id, "count": len(sources)}

        if subcmd == "remove":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /source remove [name]")
                return {"action": "source_remove_help", "user_id": user_id}
            name = subargs.split()[0].lower()
            _, removed = self._engine.preferences.remove_custom_source(user_id, name)
            if removed:
                self._persist_prefs()
                self._bot.send_message(
                    chat_id,
                    f"\u2705 Source '<code>{html_mod.escape(name)}</code>' removed."
                )
                return {"action": "source_removed", "user_id": user_id, "name": name}
            self._bot.send_message(
                chat_id,
                f"Source '<code>{html_mod.escape(name)}</code>' not found. "
                "Use /source list to see your sources."
            )
            return {"action": "source_not_found", "user_id": user_id, "name": name}

        if subcmd == "test":
            if not subargs:
                self._bot.send_message(chat_id, "Usage: /source test [name]")
                return {"action": "source_test_help", "user_id": user_id}
            name = subargs.split()[0].lower()
            sources = self._engine.preferences.get_custom_sources(user_id)
            src = next((s for s in sources if s["name"].lower() == name), None)
            if not src:
                self._bot.send_message(
                    chat_id,
                    f"Source '<code>{html_mod.escape(name)}</code>' not found."
                )
                return {"action": "source_test_notfound", "user_id": user_id}
            result = discover_feed(src["feed_url"])
            if result.valid:
                self._bot.send_message(
                    chat_id,
                    f"\u2705 <b>{html_mod.escape(src['name'])}</b> is healthy.\n"
                    f"Feed: {html_mod.escape(result.feed_title or 'untitled')}\n"
                    f"Items: {result.item_count}"
                )
            else:
                self._bot.send_message(
                    chat_id,
                    f"\u26a0\ufe0f <b>{html_mod.escape(src['name'])}</b> "
                    f"may be down.\n{html_mod.escape(result.error)}"
                )
            return {"action": "source_test", "user_id": user_id, "valid": result.valid}

        # Default: show help
        self._bot.send_message(
            chat_id,
            "<b>\U0001f4e1 Custom Source Management</b>\n\n"
            "Add your own RSS feeds to your intelligence pipeline.\n"
            "Custom sources start at low trust and earn credibility over time.\n\n"
            "<b>Commands:</b>\n"
            "/source add https://example.com [name] \u2014 Add a source\n"
            "/source list \u2014 View your custom sources\n"
            "/source remove [name] \u2014 Remove a source\n"
            "/source test [name] \u2014 Check if a source is healthy\n\n"
            "<b>Examples:</b>\n"
            "<code>/source add https://techcrunch.com techcrunch</code>\n"
            "<code>/source add https://feeds.arstechnica.com/arstechnica/index ars</code>\n\n"
            f"You can add up to {MAX_CUSTOM_SOURCES_PER_USER} custom sources."
        )
        return {"action": "source_help", "user_id": user_id}

    # Rate limit for /source add: max 3 source additions per 60 seconds per user.
    _SOURCE_ADD_WINDOW = 60
    _SOURCE_ADD_MAX = 3

    def _source_add(self, chat_id: int | str, user_id: str,
                    args: str) -> dict[str, Any]:
        """Handle /source add <url> [name] — discover, validate, and register a feed."""
        import html as html_mod
        import time as _time
        from urllib.parse import urlparse

        from newsfeed.agents.dynamic_sources import (
            discover_feed,
            validate_source_name,
        )

        # Per-user rate limit for source additions to prevent abuse/DoS.
        if not hasattr(self, "_source_add_times"):
            self._source_add_times: BoundedUserDict[list[float]] = BoundedUserDict(maxlen=500)
        now = _time.monotonic()
        times = self._source_add_times.get(user_id, [])
        times = [t for t in times if now - t < self._SOURCE_ADD_WINDOW]
        if len(times) >= self._SOURCE_ADD_MAX:
            self._bot.send_message(
                chat_id,
                f"\u26a0\ufe0f Rate limited: max {self._SOURCE_ADD_MAX} source additions "
                f"per {self._SOURCE_ADD_WINDOW} seconds. Please wait.",
            )
            return {"action": "source_add_rate_limited", "user_id": user_id}
        times.append(now)
        self._source_add_times[user_id] = times

        if not args:
            self._bot.send_message(
                chat_id,
                "Usage: /source add <url> [name]\n"
                "Example: /source add https://techcrunch.com techcrunch"
            )
            return {"action": "source_add_help", "user_id": user_id}

        parts = args.split(maxsplit=1)
        url = parts[0]
        explicit_name = parts[1].strip().split()[0].lower() if len(parts) > 1 else ""

        # Show a "working" message since feed discovery can take a few seconds
        self._bot.send_message(chat_id, "\U0001f50d Discovering feed...")

        # Discover and probe the feed
        result = discover_feed(url)
        if not result.valid:
            self._bot.send_message(
                chat_id,
                f"\u274c Could not find a valid feed.\n{html_mod.escape(result.error)}"
            )
            return {"action": "source_add_failed", "user_id": user_id, "error": result.error}

        # Derive name from explicit arg, feed title, or hostname
        if explicit_name:
            name = explicit_name
        elif result.feed_title:
            # Sanitize feed title to a valid source name
            import re
            name = re.sub(r"[^a-zA-Z0-9_-]", "", result.feed_title.lower().replace(" ", "-"))[:30]
        else:
            name = (urlparse(url).hostname or "custom").replace("www.", "").split(".")[0]

        if not name:
            name = "custom-feed"

        valid_name, name_error = validate_source_name(name)
        if not valid_name:
            self._bot.send_message(
                chat_id,
                f"\u274c Invalid source name: {html_mod.escape(name_error)}\n"
                "Try: /source add <url> <name>"
            )
            return {"action": "source_add_bad_name", "user_id": user_id}

        # Register the source
        _, error = self._engine.preferences.add_custom_source(
            user_id=user_id,
            name=name,
            feed_url=result.feed_url,
            site_url=url,
            feed_title=result.feed_title,
        )
        if error:
            self._bot.send_message(chat_id, f"\u274c {html_mod.escape(error)}")
            return {"action": "source_add_dup", "user_id": user_id, "error": error}

        self._persist_prefs()

        self._bot.send_message(
            chat_id,
            f"\u2705 <b>Source added!</b>\n\n"
            f"Name: <code>{html_mod.escape(name)}</code>\n"
            f"Feed: {html_mod.escape(result.feed_title or 'untitled')}\n"
            f"Items found: {result.item_count}\n"
            f"URL: <code>{html_mod.escape(result.feed_url[:80])}</code>\n\n"
            "This source will appear in your next briefing with low initial trust. "
            "It earns credibility as its stories get corroborated by established sources."
        )
        return {
            "action": "source_added", "user_id": user_id,
            "name": name, "feed_url": result.feed_url,
        }

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
                val = max(0.0, min(float(value), 1.0))
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
                val = max(0, min(val, 10))
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
                val = max(0.1, min(float(value), 1.0))
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
                val = max(1.5, min(float(value), 10.0))
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

    @staticmethod
    def _categorize_error(exc: Exception) -> str:
        """Turn an exception into a user-friendly message.

        Instead of generic "Something went wrong", tell the user what
        class of problem occurred so they can decide whether to retry,
        wait, or report.
        """
        name = type(exc).__name__
        msg = str(exc)[:100] if str(exc) else ""

        if "timeout" in name.lower() or "timeout" in msg.lower():
            return (
                "\u23f3 The request timed out. News sources may be slow right now. "
                "Please try again in a moment."
            )
        if "connection" in name.lower() or "urlerror" in name.lower():
            return (
                "\U0001f310 Network error \u2014 couldn't reach news sources. "
                "Check your connection and try again."
            )
        if "permission" in name.lower() or "auth" in name.lower():
            return (
                "\U0001f512 Access error. If this persists, contact the bot admin."
            )
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return (
                "\u26a0\ufe0f Something unexpected happened while processing your request. "
                "Try again, or use /help to see available commands."
            )
        return (
            "\u26a0\ufe0f Something went wrong. "
            "Try again in a moment, or use /help for available commands."
        )

    def _is_admin(self, user_id: str) -> bool:
        """Check if user is an admin via AccessControl.

        Delegates to the engine's AccessControl which supports owner,
        configured admins, and runtime-promoted admins.
        """
        return self._engine.access_control.is_admin(user_id)

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
                "/admin briefings [user_id] \u2014 User briefing history\n"
                "/admin health \u2014 Delivery success rates"
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

        if subcmd == "health":
            health = self._delivery_metrics.summary()
            lines = ["<b>Delivery Health</b>", ""]
            for ch in ("telegram", "webhook", "email"):
                h = health[ch]
                rate_pct = h["rate"] * 100
                icon = "\u2705" if rate_pct >= 95 else ("\u26a0\ufe0f" if rate_pct >= 80 else "\u274c")
                lines.append(
                    f"  {icon} {ch}: {h['success']}/{h['total']} "
                    f"({rate_pct:.0f}% success)"
                )
            self._bot.send_message(chat_id, "\n".join(lines))
            return {"action": "admin_health", "user_id": user_id}

        self._bot.send_message(chat_id, f"Unknown admin command: {html_mod.escape(subcmd)}. Try /admin help")
        return {"action": "admin_unknown", "user_id": user_id}
