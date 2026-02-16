"""Telegram Bot communication agent — handles message relay, commands, and scheduling.

Requires a bot token from https://t.me/BotFather
Set via config key: api_keys.telegram_bot_token

Features:
- Send formatted briefings to users/channels
- Process incoming commands (/briefing, /more, /feedback, /settings, /status)
- Schedule automatic briefings (morning, evening, breaking alerts)
- Handle user feedback and preference updates in real-time
- Support for inline keyboards for interactive feedback
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from newsfeed.memory.store import BoundedUserDict

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"

# Telegram message limit
_MAX_MESSAGE_LENGTH = 4096

# Command definitions for BotFather
BOT_COMMANDS = [
    {"command": "start", "description": "Welcome message and quick-start guide"},
    {"command": "briefing", "description": "Get your personalized intelligence briefing"},
    {"command": "quick", "description": "Quick headlines-only scan of current stories"},
    {"command": "deep_dive", "description": "Extended briefing with more items (e.g. /deep_dive AI)"},
    {"command": "more", "description": "Show more stories on a topic (e.g. /more geopolitics)"},
    {"command": "feedback", "description": "Adjust preferences (e.g. /feedback more AI, less crypto)"},
    {"command": "settings", "description": "View and update your profile settings"},
    {"command": "topics", "description": "List available topics and your current weights"},
    {"command": "schedule", "description": "Set briefing schedule (e.g. /schedule morning 08:00)"},
    {"command": "status", "description": "System status and last briefing info"},
    {"command": "reset", "description": "Reset all preferences to defaults"},
    {"command": "watchlist", "description": "Set market watchlist (e.g. /watchlist crypto BTC ETH)"},
    {"command": "timezone", "description": "Set your timezone (e.g. /timezone US/Eastern)"},
    {"command": "mute", "description": "Mute a topic (e.g. /mute crypto)"},
    {"command": "unmute", "description": "Unmute a topic (e.g. /unmute crypto)"},
    {"command": "tracked", "description": "View stories you're tracking"},
    {"command": "untrack", "description": "Stop tracking a story (e.g. /untrack 1)"},
    {"command": "sitrep", "description": "Situation Report \u2014 single-document intelligence summary"},
    {"command": "diff", "description": "Compare current briefing vs previous briefing"},
    {"command": "entities", "description": "Key people, orgs, and countries across stories"},
    {"command": "compare", "description": "Compare sources on a story (e.g. /compare 2)"},
    {"command": "recall", "description": "Search past briefings (e.g. /recall AI regulation)"},
    {"command": "insights", "description": "View your preference profile and auto-adjustments"},
    {"command": "weekly", "description": "Weekly intelligence digest and coverage summary"},
    {"command": "timeline", "description": "Story evolution timeline (e.g. /timeline 1)"},
    {"command": "save", "description": "Bookmark a story (e.g. /save 2)"},
    {"command": "saved", "description": "View your bookmarked stories"},
    {"command": "unsave", "description": "Remove a bookmark (e.g. /unsave 1)"},
    {"command": "email", "description": "Set email for digest delivery (e.g. /email user@example.com)"},
    {"command": "digest", "description": "Send email digest of your latest briefing"},
    {"command": "export", "description": "Export last briefing as Markdown"},
    {"command": "stats", "description": "View your personal engagement analytics"},
    {"command": "webhook", "description": "Set webhook URL for Slack/Discord/custom delivery"},
    {"command": "alert", "description": "Keyword alerts (e.g. /alert add quantum computing)"},
    {"command": "source", "description": "Manage custom RSS sources (e.g. /source add https://...)"},
    {"command": "sources", "description": "View source reliability, bias, and trust ratings"},
    {"command": "filter", "description": "Set briefing filters (e.g. /filter confidence 0.7)"},
    {"command": "preset", "description": "Save/load briefing presets (e.g. /preset save Work)"},
    {"command": "transparency", "description": "Pipeline trace \u2014 see how your briefing was built"},
    {"command": "help", "description": "Show available commands and usage"},
]


class TelegramBot:
    """Telegram Bot API client for the NewsFeed communication agent.

    Handles all communication between the system and Telegram users,
    including sending briefings, processing commands, and managing
    interactive feedback via inline keyboards.
    """

    def __init__(self, bot_token: str, timeout: int = 10) -> None:
        self._token = bot_token.strip()
        self._base_url = _API_BASE.format(token=self._token)
        self._timeout = timeout
        self._offset: int = 0  # For long polling

    # ──────────────────────────────────────────────────────────────
    # Core API methods
    # ──────────────────────────────────────────────────────────────

    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # seconds

    def _api_call(self, method: str, params: dict | None = None, data: dict | None = None) -> dict:
        """Make a Telegram Bot API call with retry on 429 rate limits."""
        url = f"{self._base_url}/{method}"

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                if data:
                    body = json.dumps(data).encode("utf-8")
                    req = urllib.request.Request(
                        url,
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                elif params:
                    call_url = f"{url}?{urlencode(params)}"
                    req = urllib.request.Request(call_url)
                else:
                    req = urllib.request.Request(url)

                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))

                if not result.get("ok"):
                    log.error("Telegram API error: %s", result.get("description", "unknown"))
                    return {}

                return result.get("result", {})

            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < self._MAX_RETRIES:
                    # Respect Retry-After header if present, otherwise exponential backoff
                    retry_after = None
                    try:
                        err_body = json.loads(e.read().decode("utf-8"))
                        retry_after = err_body.get("parameters", {}).get("retry_after")
                    except (json.JSONDecodeError, OSError):
                        pass
                    delay = retry_after if retry_after else self._RETRY_BASE_DELAY * (2 ** attempt)
                    delay = min(delay, 30)  # cap at 30s
                    log.warning("Telegram 429 rate limit on %s, retrying in %.1fs (attempt %d/%d)",
                                method, delay, attempt + 1, self._MAX_RETRIES)
                    time.sleep(delay)
                    continue
                log.error("Telegram API call %s failed: %s", method, e)
                return {}
            except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as e:
                log.error("Telegram API call %s failed: %s", method, e)
                return {}
        return {}

    def get_me(self) -> dict:
        """Verify bot token and get bot info."""
        return self._api_call("getMe")

    def set_commands(self) -> bool:
        """Register bot commands with BotFather."""
        result = self._api_call("setMyCommands", data={"commands": BOT_COMMANDS})
        return bool(result)

    # ──────────────────────────────────────────────────────────────
    # Message sending
    # ──────────────────────────────────────────────────────────────

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> dict:
        """Send a text message to a chat.

        Automatically splits messages that exceed Telegram's 4096 char limit.
        """
        if len(text) <= _MAX_MESSAGE_LENGTH:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            }
            if reply_markup:
                payload["reply_markup"] = reply_markup
            return self._api_call("sendMessage", data=payload)

        # Split long messages
        chunks = self._split_message(text)
        last_result = {}
        for i, chunk in enumerate(chunks):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            }
            # Only attach reply markup to the last chunk
            if reply_markup and i == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            last_result = self._api_call("sendMessage", data=payload)
        return last_result

    def send_briefing(self, chat_id: int | str, formatted_text: str,
                      item_count: int = 0) -> dict:
        """Send a formatted briefing with clean action buttons.

        Per-item rating is available via the 'Rate Stories' button on demand.
        """
        rows: list[list[dict]] = [
            [
                {"text": "\u25b6 More", "callback_data": "cmd:more"},
                {"text": "\U0001f50d Deep Dive", "callback_data": "cmd:deep_dive"},
            ],
            [
                {"text": "\U0001f44d More like this", "callback_data": "pref:more_similar"},
                {"text": "\U0001f44e Less like this", "callback_data": "pref:less_similar"},
            ],
            [
                {"text": "\u2b50 Rate Stories", "callback_data": "cmd:rate_prompt"},
                {"text": "\u2699 Settings", "callback_data": "cmd:settings"},
            ],
        ]
        keyboard = {"inline_keyboard": rows}
        return self.send_message(chat_id, formatted_text, reply_markup=keyboard)

    def send_story_card(
        self,
        chat_id: int | str,
        text: str,
        story_index: int = 0,
        is_tracked: bool = False,
    ) -> dict:
        """Send a single story card with feedback, deep dive, and track buttons."""
        track_label = "\U0001f4cc Tracked" if is_tracked else "\U0001f4cc Track"
        rows: list[list[dict]] = [
            [
                {"text": "\U0001f44d", "callback_data": f"rate:{story_index}:up"},
                {"text": "\U0001f44e", "callback_data": f"rate:{story_index}:down"},
                {"text": "\U0001f50d Dive deeper", "callback_data": f"dive:{story_index}"},
            ],
            [
                {"text": track_label, "callback_data": f"track:{story_index}"},
                {"text": "\U0001f516 Save", "callback_data": f"save:{story_index}"},
                {"text": "\U0001f50e Compare", "callback_data": f"compare:{story_index}"},
            ],
        ]
        keyboard = {"inline_keyboard": rows}
        return self.send_message(chat_id, text, reply_markup=keyboard)

    def send_closing(
        self,
        chat_id: int | str,
        text: str,
    ) -> dict:
        """Send the closing message with action buttons."""
        rows: list[list[dict]] = [
            [
                {"text": "\u25b6 More", "callback_data": "cmd:more"},
                {"text": "\U0001f50d Deep Dive", "callback_data": "cmd:deep_dive"},
            ],
            [
                {"text": "\u2699 Settings", "callback_data": "cmd:settings"},
                {"text": "\U0001f4ac Feedback", "callback_data": "cmd:feedback"},
            ],
        ]
        keyboard = {"inline_keyboard": rows}
        return self.send_message(chat_id, text, reply_markup=keyboard)

    def send_quick_briefing(self, chat_id: int | str, formatted_text: str,
                            item_count: int = 0) -> dict:
        """Send a quick-scan briefing with compact action buttons."""
        rows: list[list[dict]] = [
            [
                {"text": "\U0001f4cb Full briefing", "callback_data": "cmd:briefing"},
                {"text": "\U0001f50d Deep Dive", "callback_data": "cmd:deep_dive"},
            ],
            [
                {"text": "\u2b50 Rate Stories", "callback_data": "cmd:rate_prompt"},
                {"text": "\U0001f4dd Export", "callback_data": "cmd:export"},
            ],
        ]
        keyboard = {"inline_keyboard": rows}
        return self.send_message(chat_id, formatted_text, reply_markup=keyboard)

    def send_breaking_alert(self, chat_id: int | str, formatted_text: str) -> dict:
        """Send a breaking alert with urgency formatting."""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Full briefing", "callback_data": "cmd:briefing"},
                    {"text": "Mute 1h", "callback_data": "mute:60"},
                ],
            ]
        }
        return self.send_message(chat_id, formatted_text, reply_markup=keyboard)

    def answer_callback(self, callback_query_id: str, text: str = "") -> dict:
        """Answer a callback query from inline keyboard."""
        return self._api_call("answerCallbackQuery", data={
            "callback_query_id": callback_query_id,
            "text": text,
        })

    # ──────────────────────────────────────────────────────────────
    # Update polling
    # ──────────────────────────────────────────────────────────────

    def get_updates(self, timeout: int = 30) -> list[dict]:
        """Long-poll for updates."""
        result = self._api_call("getUpdates", data={
            "offset": self._offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        })
        updates = result if isinstance(result, list) else []
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    def parse_command(self, update: dict) -> dict[str, Any] | None:
        """Parse an update into a structured command.

        Returns dict with keys: type, chat_id, user_id, command, args, text
        or None if not a recognized command.
        """
        # Handle callback queries (inline keyboard)
        if "callback_query" in update:
            cb = update["callback_query"]
            data = cb.get("data", "")
            chat_id = cb.get("message", {}).get("chat", {}).get("id")
            user_id = str(cb.get("from", {}).get("id", ""))

            self.answer_callback(cb.get("id", ""), "Processing...")

            if data.startswith("cmd:"):
                return {
                    "type": "command",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": data[4:],
                    "args": "",
                    "text": "",
                }
            if data.startswith("pref:"):
                return {
                    "type": "preference",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": data[5:],
                    "args": "",
                    "text": "",
                }
            if data.startswith("mute:"):
                return {
                    "type": "mute",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": "mute",
                    "args": data[5:],
                    "text": "",
                }
            if data.startswith("rate:"):
                return {
                    "type": "rate",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": data,  # "rate:N:up" or "rate:N:down"
                    "args": "",
                    "text": "",
                }
            if data.startswith("dive:"):
                return {
                    "type": "command",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": "deep_dive",
                    "args": data[5:],  # story index number
                    "text": "",
                }
            if data.startswith("track:"):
                return {
                    "type": "command",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": "track",
                    "args": data[6:],  # story index number
                    "text": "",
                }
            if data.startswith("save:"):
                return {
                    "type": "command",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": "save",
                    "args": data[5:],  # story index number
                    "text": "",
                }
            if data.startswith("compare:"):
                return {
                    "type": "command",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": "compare",
                    "args": data[8:],  # story index number
                    "text": "",
                }
            if data.startswith("onboard:"):
                return {
                    "type": "onboard",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "command": data,  # "onboard:topic:X" or "onboard:role:X" etc.
                    "args": "",
                    "text": "",
                }
            # Unknown callback — log and surface as stale action
            log.warning("Unknown callback_data: %r from user=%s", data, user_id)
            return {
                "type": "command",
                "chat_id": chat_id,
                "user_id": user_id,
                "command": "_stale_callback",
                "args": data,
                "text": "",
            }

        # Handle text messages
        msg = update.get("message", {})
        text = msg.get("text", "")
        chat_id = msg.get("chat", {}).get("id")
        user_id = str(msg.get("from", {}).get("id", ""))

        if not text or not chat_id:
            return None

        # Parse /command args
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command = parts[0].lstrip("/").split("@")[0]  # Handle @botname suffix
            args = parts[1] if len(parts) > 1 else ""
            return {
                "type": "command",
                "chat_id": chat_id,
                "user_id": user_id,
                "command": command,
                "args": args.strip(),
                "text": text,
            }

        # Natural language feedback
        return {
            "type": "feedback",
            "chat_id": chat_id,
            "user_id": user_id,
            "command": "",
            "args": "",
            "text": text,
        }

    # ──────────────────────────────────────────────────────────────
    # Formatting helpers
    # ──────────────────────────────────────────────────────────────

    def format_settings(self, profile: dict) -> str:
        """Format user settings for display."""
        lines = ["<b>\u2699 Your NewsFeed Settings</b>", ""]

        lines.append(f"\u2022 Tone: <code>{html_mod.escape(str(profile.get('tone', 'concise')))}</code>")
        lines.append(f"\u2022 Format: <code>{html_mod.escape(str(profile.get('format', 'bullet')))}</code>")
        lines.append(f"\u2022 Max items: <code>{profile.get('max_items', 10)}</code>")
        lines.append(f"\u2022 Cadence: <code>{html_mod.escape(str(profile.get('cadence', 'on_demand')))}</code>")
        lines.append(f"\u2022 Timezone: <code>{html_mod.escape(str(profile.get('timezone', 'UTC')))}</code>")

        schedule = profile.get("schedule")
        if schedule:
            lines.append(f"\u2022 Schedule: <code>{html_mod.escape(str(schedule))}</code>")

        topics = profile.get("topic_weights", {})
        if topics:
            lines.append("")
            lines.append("<b>Topic Weights:</b>")
            for topic, weight in sorted(topics.items(), key=lambda x: x[1], reverse=True):
                bar = "\u2588" * max(1, int(abs(weight) * 10))
                sign = "+" if weight > 0 else ""
                lines.append(f"  {html_mod.escape(topic)}: {sign}{weight:.1f} {bar}")

        source_weights = profile.get("source_weights", {})
        if source_weights:
            lines.append("")
            lines.append("<b>Source Preferences:</b>")
            for src, sw in sorted(source_weights.items(), key=lambda x: -x[1]):
                label = "boosted" if sw > 0 else "demoted"
                lines.append(f"  {html_mod.escape(src)}: {label} ({sw:+.1f})")

        regions = profile.get("regions", [])
        if regions:
            lines.append("")
            lines.append(f"<b>Regions:</b> {', '.join(html_mod.escape(r) for r in regions)}")

        muted = profile.get("muted_topics", [])
        if muted:
            lines.append(f"<b>Muted:</b> {', '.join(html_mod.escape(m) for m in muted)}")

        # Advanced filters
        conf_min = profile.get("confidence_min", 0)
        urg_min = profile.get("urgency_min", "")
        mps = profile.get("max_per_source", 0)
        if conf_min or urg_min or mps:
            lines.append("")
            lines.append("<b>Briefing Filters:</b>")
            if conf_min:
                lines.append(f"  Confidence: \u2265 {conf_min:.0%}")
            if urg_min:
                lines.append(f"  Urgency: \u2265 {urg_min}")
            if mps:
                lines.append(f"  Max per source: {mps}")

        # Alert thresholds (only show if non-default)
        geo_t = profile.get("alert_georisk_threshold", 0.5)
        trend_t = profile.get("alert_trend_threshold", 3.0)
        if geo_t != 0.5 or trend_t != 3.0:
            lines.append("")
            lines.append("<b>Alert Sensitivity:</b>")
            if geo_t != 0.5:
                lines.append(f"  Geo-risk at: {geo_t:.0%}")
            if trend_t != 3.0:
                lines.append(f"  Trend spike at: {trend_t:.1f}x")

        # Presets
        presets = profile.get("presets", {})
        if presets:
            lines.append("")
            lines.append(f"<b>Saved Presets:</b> {', '.join(html_mod.escape(k) for k in presets.keys())}")

        # Delivery channels
        email = profile.get("email", "")
        webhook = profile.get("webhook_url", "")
        if email or webhook:
            lines.append("")
            lines.append("<b>Delivery Channels:</b>")
            lines.append(f"  Telegram: active")
            if email:
                lines.append(f"  Email: {html_mod.escape(email)}")
            if webhook:
                lines.append(f"  Webhook: {html_mod.escape(webhook[:50])}...")

        crypto = profile.get("watchlist_crypto", [])
        stocks = profile.get("watchlist_stocks", [])
        if crypto or stocks:
            lines.append("")
            lines.append("<b>Market Watchlist:</b>")
            if crypto:
                lines.append(f"  Crypto: {html_mod.escape(', '.join(c.upper() for c in crypto))}")
            if stocks:
                lines.append(f"  Stocks: {html_mod.escape(', '.join(stocks))}")

        return "\n".join(lines)

    def format_help(self) -> str:
        """Format help message."""
        lines = [
            "<b>\U0001f4e1 NewsFeed Intelligence Bot</b>",
            "",
            "<b>Commands:</b>",
            "",
        ]
        for cmd in BOT_COMMANDS:
            lines.append(f"/{cmd['command']} \u2014 {cmd['description']}")

        lines.extend([
            "",
            "<b>Natural language (just type):</b>",
            "\u2022 <code>What's happening with AI?</code> \u2014 Topic briefing",
            "\u2022 <code>Find stories about regulation</code> \u2014 Search history",
            "\u2022 <code>What's trending?</code> \u2014 Weekly trends",
            "",
            "<b>Preferences (just type):</b>",
            "\u2022 <code>more geopolitics</code> / <code>less crypto</code>",
            "\u2022 <code>tone: analyst</code> / <code>format: sections</code>",
            "\u2022 <code>prefer reuters</code> / <code>demote reddit</code>",
            "\u2022 <code>region: middle_east</code> / <code>max: 15</code>",
        ])

        return "\n".join(lines)

    def format_status(self, engine_info: dict) -> str:
        """Format system status."""
        lines = [
            "<b>System Status</b>",
            "",
            f"Agents: {engine_info.get('agent_count', '?')} active",
            f"Experts: {engine_info.get('expert_count', '?')} in council",
            f"Intelligence stages: {engine_info.get('stage_count', '?')} enabled",
            f"Last briefing: {engine_info.get('last_briefing', 'none')}",
            f"Cache entries: {engine_info.get('cache_entries', '?')}",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────

    def _split_message(self, text: str) -> list[str]:
        """Split a long message into chunks at paragraph boundaries."""
        chunks: list[str] = []
        current = ""

        for line in text.split("\n"):
            if len(current) + len(line) + 1 > _MAX_MESSAGE_LENGTH - 50:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line

        if current:
            chunks.append(current)

        return chunks or [text[:_MAX_MESSAGE_LENGTH]]


class BriefingScheduler:
    """Manages scheduled briefing delivery.

    Tracks per-user schedules and triggers briefing generation
    at configured times. Converts user-local schedule times to UTC
    using their configured timezone. Also handles mute suppression.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # All per-user dicts use BoundedUserDict to cap memory at 1000
        # users with LRU eviction — prevents unbounded growth.
        self._schedules: BoundedUserDict[dict[str, Any]] = BoundedUserDict(maxlen=1000)
        self._last_sent: BoundedUserDict[float] = BoundedUserDict(maxlen=1000)
        self._muted_until: BoundedUserDict[float] = BoundedUserDict(maxlen=1000)
        self._user_timezones: BoundedUserDict[str] = BoundedUserDict(maxlen=1000)

    def set_user_timezone(self, user_id: str, tz: str) -> None:
        """Update the cached timezone for a user."""
        self._user_timezones[user_id] = tz

    def _user_local_time(self, user_id: str) -> str:
        """Get current HH:MM in the user's timezone."""
        tz_name = self._user_timezones.get(user_id, "UTC")
        try:
            from zoneinfo import ZoneInfo
            user_tz = ZoneInfo(tz_name)
            return datetime.now(user_tz).strftime("%H:%M")
        except (ImportError, KeyError):
            # Invalid timezone or zoneinfo unavailable — fall back to UTC
            return datetime.now(timezone.utc).strftime("%H:%M")

    def set_schedule(self, user_id: str, schedule_type: str, time_str: str = "") -> str:
        """Set a briefing schedule for a user.

        schedule_type: 'morning', 'evening', 'realtime', 'off'
        time_str: optional HH:MM in the user's local timezone
        """
        schedule_times = {
            "morning": "08:00",
            "evening": "18:00",
        }

        if schedule_type == "off":
            self._schedules.pop(user_id, None)
            return "Scheduled briefings disabled."

        if schedule_type == "realtime":
            self._schedules[user_id] = {"type": "realtime", "time": ""}
            return "Breaking alerts enabled \u2014 you'll receive real-time notifications."

        target_time = time_str or schedule_times.get(schedule_type, "08:00")
        self._schedules[user_id] = {"type": schedule_type, "time": target_time}
        tz_name = self._user_timezones.get(user_id, "UTC")
        return f"Briefing scheduled: {schedule_type} at {target_time} ({tz_name})."

    def get_due_users(self) -> list[str]:
        """Get list of user IDs whose briefings are due now.

        Compares the scheduled time against each user's local timezone,
        so a user in US/Eastern with schedule "08:00" gets their briefing
        at 8 AM Eastern, not 8 AM UTC.

        Thread-safe: uses a lock to prevent duplicate sends from concurrent
        callers (e.g. multiple polling threads or webhook handlers).
        """
        due: list[str] = []

        with self._lock:
            for user_id, schedule in self._schedules.items():
                if self.is_muted(user_id):
                    continue

                if schedule["type"] == "realtime":
                    continue

                user_now = self._user_local_time(user_id)
                if schedule["time"] == user_now:
                    last = self._last_sent.get(user_id, 0)
                    if time.time() - last > 120:
                        due.append(user_id)
                        self._last_sent[user_id] = time.time()

        return due

    def mute(self, user_id: str, minutes: int) -> str:
        """Mute all alerts for a user for the given number of minutes."""
        minutes = max(1, min(minutes, 1440))  # clamp 1 min to 24 hours
        self._muted_until[user_id] = time.time() + (minutes * 60)
        return f"Alerts muted for {minutes} minutes."

    def is_muted(self, user_id: str) -> bool:
        """Check if a user is currently muted."""
        until = self._muted_until.get(user_id, 0)
        if until and time.time() < until:
            return True
        # Expired — clean up
        self._muted_until.pop(user_id, None)
        return False

    def should_send_breaking(self, user_id: str) -> bool:
        """Check if a user should receive breaking alerts."""
        if self.is_muted(user_id):
            return False
        schedule = self._schedules.get(user_id)
        if not schedule:
            return True  # Default: send breaking alerts
        return schedule["type"] in ("realtime", "morning", "evening")

    def snapshot(self) -> dict:
        return {
            "schedules": dict(self._schedules),
            "timezones": dict(self._user_timezones),
            "muted": {uid: until for uid, until in self._muted_until.items() if time.time() < until},
        }

    def restore(self, data: dict) -> int:
        """Restore scheduler state from a persisted snapshot.

        Returns the number of schedules restored.
        """
        restored = 0
        schedules = data.get("schedules")
        if isinstance(schedules, dict):
            for uid, sched in schedules.items():
                if isinstance(uid, str) and isinstance(sched, dict):
                    stype = sched.get("type", "")
                    stime = sched.get("time", "")
                    if stype in ("morning", "evening", "realtime"):
                        self._schedules[uid] = {"type": stype, "time": stime}
                        restored += 1
        timezones = data.get("timezones")
        if isinstance(timezones, dict):
            for uid, tz in timezones.items():
                if isinstance(uid, str) and isinstance(tz, str) and len(tz) <= 40:
                    self._user_timezones[uid] = tz
        return restored
