"""Analysis command handlers — /diff, /entities, /compare, /recall, /insights, /weekly, /timeline.

These commands analyze briefing data, compare sources, search history,
and provide intelligence insights. Extracted from CommunicationAgent
to keep it focused on routing and lifecycle management.
"""
from __future__ import annotations

import html as html_mod
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from newsfeed.orchestration.handlers import HandlerContext

log = logging.getLogger(__name__)


def handle_deep_dive_story(ctx: HandlerContext, chat_id: int | str,
                            user_id: str, story_num: int) -> dict[str, Any]:
    """Deep dive into a specific story from the last briefing."""
    item = ctx.engine.get_report_item(user_id, story_num)
    if not item:
        ctx.bot.send_message(
            chat_id,
            f"Story #{story_num} not found. Run /briefing first."
        )
        return {"action": "deep_dive_not_found", "user_id": user_id}

    formatter = ctx.engine.formatter
    card = formatter.format_deep_dive(item, story_num)
    ctx.bot.send_message(chat_id, card)

    # Also show source comparison if thread data is available
    _, others = ctx.engine.get_story_thread(user_id, story_num)
    if others:
        comp = formatter.format_comparison(item, others, story_num)
        ctx.bot.send_message(chat_id, comp)

    return {"action": "deep_dive_story", "user_id": user_id, "story": story_num}


def handle_compare(ctx: HandlerContext, chat_id: int | str,
                   user_id: str, args: str) -> dict[str, Any]:
    """Show how different sources cover the same story."""
    try:
        story_num = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(chat_id, "Usage: /compare [story number]")
        return {"action": "compare_help", "user_id": user_id}

    item, others = ctx.engine.get_story_thread(user_id, story_num)
    if not item:
        ctx.bot.send_message(
            chat_id,
            f"Story #{story_num} not found. Run /briefing first."
        )
        return {"action": "compare_not_found", "user_id": user_id}

    formatter = ctx.engine.formatter
    card = formatter.format_comparison(item, others, story_num)
    ctx.bot.send_message(chat_id, card)
    return {"action": "compare", "user_id": user_id, "story": story_num,
            "source_count": 1 + len(others)}


def handle_recall(ctx: HandlerContext, chat_id: int | str,
                  user_id: str, args: str) -> dict[str, Any]:
    """Search past briefing history for a keyword."""
    keyword = args.strip()
    if not keyword:
        ctx.bot.send_message(
            chat_id,
            "Usage: /recall [keyword]\n"
            "Example: /recall AI regulation"
        )
        return {"action": "recall_help", "user_id": user_id}

    items = ctx.engine.analytics.search_briefing_items(user_id, keyword)
    formatter = ctx.engine.formatter
    card = formatter.format_recall(keyword, items)
    ctx.bot.send_message(chat_id, card)
    return {"action": "recall", "user_id": user_id, "keyword": keyword,
            "results": len(items)}


def handle_timeline(ctx: HandlerContext, chat_id: int | str,
                    user_id: str, args: str) -> dict[str, Any]:
    """Show timeline of a tracked story's evolution from analytics."""
    try:
        index = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(
            chat_id,
            "Usage: /timeline [number] \u2014 where number is from /tracked list."
        )
        return {"action": "timeline_help", "user_id": user_id}

    profile = ctx.engine.preferences.get_or_create(user_id)
    if index < 1 or index > len(profile.tracked_stories):
        ctx.bot.send_message(chat_id, f"No tracked story #{index}. See /tracked for the list.")
        return {"action": "timeline_invalid", "user_id": user_id}

    tracked = profile.tracked_stories[index - 1]
    items = ctx.engine.analytics.get_story_timeline(
        user_id, tracked["topic"], tracked["keywords"],
    )

    formatter = ctx.engine.formatter
    card = formatter.format_timeline(tracked["headline"], items)
    ctx.bot.send_message(chat_id, card)
    return {"action": "timeline", "user_id": user_id, "index": index,
            "results": len(items)}
