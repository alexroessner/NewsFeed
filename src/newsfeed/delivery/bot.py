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

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

log = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"

# Telegram message limit
_MAX_MESSAGE_LENGTH = 4096

# Command definitions for BotFather
BOT_COMMANDS = [
    {"command": "briefing", "description": "Get your personalized intelligence briefing"},
    {"command": "more", "description": "Show more stories on a topic (e.g. /more geopolitics)"},
    {"command": "feedback", "description": "Adjust preferences (e.g. /feedback more AI, less crypto)"},
    {"command": "settings", "description": "View and update your profile settings"},
    {"command": "status", "description": "System status and last briefing info"},
    {"command": "topics", "description": "List available topics and your current weights"},
    {"command": "schedule", "description": "Set briefing schedule (e.g. /schedule morning 08:00)"},
    {"command": "help", "description": "Show available commands and usage"},
]


class TelegramBot:
    """Telegram Bot API client for the NewsFeed communication agent.

    Handles all communication between the system and Telegram users,
    including sending briefings, processing commands, and managing
    interactive feedback via inline keyboards.
    """

    def __init__(self, bot_token: str, timeout: int = 10) -> None:
        self._token = bot_token
        self._base_url = _API_BASE.format(token=bot_token)
        self._timeout = timeout
        self._offset: int = 0  # For long polling

    # ──────────────────────────────────────────────────────────────
    # Core API methods
    # ──────────────────────────────────────────────────────────────

    def _api_call(self, method: str, params: dict | None = None, data: dict | None = None) -> dict:
        """Make a Telegram Bot API call."""
        url = f"{self._base_url}/{method}"

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
                url = f"{url}?{urlencode(params)}"
                req = urllib.request.Request(url)
            else:
                req = urllib.request.Request(url)

            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            if not result.get("ok"):
                log.error("Telegram API error: %s", result.get("description", "unknown"))
                return {}

            return result.get("result", {})

        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.error("Telegram API call %s failed: %s", method, e)
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

    def send_briefing(self, chat_id: int | str, formatted_text: str) -> dict:
        """Send a formatted briefing with feedback buttons."""
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "More stories", "callback_data": "cmd:more"},
                    {"text": "Deeper analysis", "callback_data": "cmd:deep_dive"},
                ],
                [
                    {"text": "More like this", "callback_data": "pref:more_similar"},
                    {"text": "Less like this", "callback_data": "pref:less_similar"},
                ],
                [
                    {"text": "Settings", "callback_data": "cmd:settings"},
                ],
            ]
        }
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
            return None

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
        lines = ["<b>Your NewsFeed Settings</b>", ""]
        lines.append(f"Tone: <code>{profile.get('tone', 'concise')}</code>")
        lines.append(f"Format: <code>{profile.get('format', 'bullet')}</code>")
        lines.append(f"Max items: <code>{profile.get('max_items', 10)}</code>")
        lines.append(f"Cadence: <code>{profile.get('cadence', 'on_demand')}</code>")

        topics = profile.get("topic_weights", {})
        if topics:
            lines.append("")
            lines.append("<b>Topic Weights:</b>")
            for topic, weight in sorted(topics.items(), key=lambda x: x[1], reverse=True):
                bar = "█" * int(abs(weight) * 10)
                sign = "+" if weight > 0 else ""
                lines.append(f"  {topic}: {sign}{weight:.1f} {bar}")

        regions = profile.get("regions", [])
        if regions:
            lines.append("")
            lines.append(f"<b>Regions:</b> {', '.join(regions)}")

        return "\n".join(lines)

    def format_help(self) -> str:
        """Format help message."""
        lines = [
            "<b>NewsFeed Intelligence Bot</b>",
            "",
            "Available commands:",
            "",
        ]
        for cmd in BOT_COMMANDS:
            lines.append(f"/{cmd['command']} — {cmd['description']}")

        lines.extend([
            "",
            "<b>Feedback Examples:</b>",
            "• <code>more geopolitics</code> — Increase geopolitics weight",
            "• <code>less crypto</code> — Decrease crypto weight",
            "• <code>tone: analyst</code> — Switch to analyst tone",
            "• <code>format: sections</code> — Switch to sections format",
            "• <code>region: middle_east</code> — Add region of interest",
            "• <code>cadence: morning</code> — Set daily morning briefing",
            "• <code>max: 15</code> — Set max items per briefing",
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
    at configured times.
    """

    def __init__(self) -> None:
        self._schedules: dict[str, dict[str, Any]] = {}
        self._last_sent: dict[str, float] = {}

    def set_schedule(self, user_id: str, schedule_type: str, time_str: str = "") -> str:
        """Set a briefing schedule for a user.

        schedule_type: 'morning', 'evening', 'realtime', 'off'
        time_str: optional HH:MM for morning/evening
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
            return "Breaking alerts enabled — you'll receive real-time notifications."

        target_time = time_str or schedule_times.get(schedule_type, "08:00")
        self._schedules[user_id] = {"type": schedule_type, "time": target_time}
        return f"Briefing scheduled: {schedule_type} at {target_time} UTC."

    def get_due_users(self) -> list[str]:
        """Get list of user IDs whose briefings are due now."""
        now = datetime.now(timezone.utc)
        current_time = now.strftime("%H:%M")
        due: list[str] = []

        for user_id, schedule in self._schedules.items():
            if schedule["type"] == "realtime":
                continue  # Realtime users get breaking alerts only

            if schedule["time"] == current_time:
                # Avoid duplicate sends within same minute
                last = self._last_sent.get(user_id, 0)
                if time.time() - last > 120:
                    due.append(user_id)
                    self._last_sent[user_id] = time.time()

        return due

    def should_send_breaking(self, user_id: str) -> bool:
        """Check if a user should receive breaking alerts."""
        schedule = self._schedules.get(user_id)
        if not schedule:
            return True  # Default: send breaking alerts
        return schedule["type"] in ("realtime", "morning", "evening")

    def snapshot(self) -> dict:
        return dict(self._schedules)
