"""Outbound webhook delivery for briefings and alerts.

Sends structured JSON payloads to user-configured webhook URLs.
Compatible with Slack Incoming Webhooks, Discord Webhooks, and any
generic endpoint that accepts JSON POST requests.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Allowed URL schemes for webhook endpoints
_ALLOWED_SCHEMES = frozenset({"https"})
_MAX_URL_LEN = 512


def validate_webhook_url(url: str) -> tuple[bool, str]:
    """Validate a webhook URL. Returns (valid, error_message)."""
    if not url:
        return False, "URL is required"
    if len(url) > _MAX_URL_LEN:
        return False, f"URL too long (max {_MAX_URL_LEN} characters)"
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, "Only HTTPS URLs are allowed"
    if not parsed.hostname:
        return False, "URL must include a hostname"
    # Block localhost/private IPs
    hostname = parsed.hostname.lower()
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False, "Localhost URLs are not allowed"
    if hostname.startswith("10.") or hostname.startswith("192.168."):
        return False, "Private network URLs are not allowed"
    return True, ""


def _detect_platform(url: str) -> str:
    """Detect webhook platform from URL for formatting hints."""
    hostname = urlparse(url).hostname or ""
    if "slack" in hostname or "hooks.slack.com" in hostname:
        return "slack"
    if "discord" in hostname or "discordapp" in hostname:
        return "discord"
    return "generic"


def format_briefing_payload(user_id: str, items: list,
                            platform: str = "generic") -> dict[str, Any]:
    """Format briefing items as a structured JSON payload."""
    now = datetime.now(timezone.utc).isoformat()

    stories = []
    for item in items:
        c = item.candidate
        story: dict[str, Any] = {
            "title": c.title,
            "source": c.source,
            "topic": c.topic,
            "url": c.url or "",
            "summary": c.summary,
            "urgency": c.urgency,
        }
        if c.confidence:
            story["confidence"] = {
                "low": c.confidence.low,
                "mid": c.confidence.mid,
                "high": c.confidence.high,
            }
        if c.corroborated_by:
            story["corroborated_by"] = c.corroborated_by
        stories.append(story)

    payload: dict[str, Any] = {
        "type": "briefing",
        "user_id": user_id,
        "generated_at": now,
        "story_count": len(stories),
        "stories": stories,
    }

    # Slack-compatible formatting
    if platform == "slack":
        text_lines = [f"*Intelligence Briefing* \u2014 {len(stories)} stories"]
        for i, s in enumerate(stories[:10], 1):
            urgency_icon = {"critical": "\U0001f534", "breaking": "\U0001f7e0",
                            "elevated": "\U0001f7e1"}.get(s.get("urgency", ""), "\u26aa")
            title = s["title"]
            url = s.get("url", "")
            link = f"<{url}|{title}>" if url and "example.com" not in url else title
            text_lines.append(f"{urgency_icon} {i}. {link} _[{s['source']}]_")
        payload = {"text": "\n".join(text_lines)}

    elif platform == "discord":
        embeds = []
        for s in stories[:10]:
            embed: dict[str, Any] = {
                "title": s["title"],
                "description": s.get("summary", "")[:200],
                "color": {"critical": 0xFF0000, "breaking": 0xFF8800,
                          "elevated": 0xFFCC00}.get(s.get("urgency", ""), 0x888888),
            }
            url = s.get("url", "")
            if url and "example.com" not in url:
                embed["url"] = url
            embed["footer"] = {"text": f"{s['source']} \u00b7 {s.get('topic', '')}"}
            embeds.append(embed)
        payload = {
            "content": f"**Intelligence Briefing** \u2014 {len(stories)} stories",
            "embeds": embeds,
        }

    return payload


def format_alert_payload(alert_type: str, alert_data: dict,
                         platform: str = "generic") -> dict[str, Any]:
    """Format an intelligence alert as a structured JSON payload."""
    now = datetime.now(timezone.utc).isoformat()

    payload: dict[str, Any] = {
        "type": "alert",
        "alert_type": alert_type,
        "generated_at": now,
        **alert_data,
    }

    if platform == "slack":
        if alert_type == "georisk":
            region = alert_data.get("region", "Unknown")
            risk = alert_data.get("risk_level", 0)
            payload = {"text": f"\u26a0\ufe0f *Geo-Risk Alert*: {region} risk at {risk:.0%}"}
        elif alert_type == "trend":
            topic = alert_data.get("topic", "Unknown")
            score = alert_data.get("anomaly_score", 0)
            payload = {"text": f"\U0001f4c8 *Trend Spike*: {topic} at {score:.1f}x baseline"}

    elif platform == "discord":
        if alert_type == "georisk":
            region = alert_data.get("region", "Unknown")
            risk = alert_data.get("risk_level", 0)
            payload = {
                "content": f"\u26a0\ufe0f **Geo-Risk Alert**: {region} risk at {risk:.0%}",
            }
        elif alert_type == "trend":
            topic = alert_data.get("topic", "Unknown")
            score = alert_data.get("anomaly_score", 0)
            payload = {
                "content": f"\U0001f4c8 **Trend Spike**: {topic} at {score:.1f}x baseline",
            }

    return payload


def send_webhook(url: str, payload: dict[str, Any],
                 timeout: float = 10.0) -> bool:
    """Send a JSON payload to a webhook URL. Returns True on success."""
    import urllib.request
    import urllib.error

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        log.warning("Webhook HTTP error: %s %s", exc.code, exc.reason)
        return False
    except Exception:
        log.debug("Webhook delivery failed", exc_info=True)
        return False
