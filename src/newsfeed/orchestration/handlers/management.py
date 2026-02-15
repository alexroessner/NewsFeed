"""Story management handlers — /track, /untrack, /tracked, /save, /saved, /unsave.

These commands manage story tracking (cross-session continuity) and bookmarks.
Extracted from CommunicationAgent for separation of concerns.
"""
from __future__ import annotations

import html as html_mod
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from newsfeed.orchestration.handlers import HandlerContext

log = logging.getLogger(__name__)


def handle_track(ctx: HandlerContext, chat_id: int | str,
                 user_id: str, args: str) -> dict[str, Any]:
    """Track a story from the last briefing for cross-session continuity."""
    try:
        story_num = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(chat_id, "Usage: tap the \U0001f4cc Track button on a story card.")
        return {"action": "track_help", "user_id": user_id}

    items = ctx.last_items.get(user_id, [])
    if story_num < 1 or story_num > len(items):
        ctx.bot.send_message(chat_id, "That story is no longer available. Run /briefing first.")
        return {"action": "track_expired", "user_id": user_id}

    item = items[story_num - 1]
    topic = item["topic"]
    headline = item["title"]

    ctx.engine.preferences.track_story(user_id, topic, headline)
    ctx.persist_prefs(chat_id)
    track_count = len(ctx.engine.preferences.get_or_create(user_id).tracked_stories)
    ctx.bot.send_message(
        chat_id,
        f"\U0001f4cc Now tracking: <b>{html_mod.escape(headline)}</b>\n"
        f"You'll see \U0001f4cc badges when new developments appear in future briefings.\n"
        f"View tracked stories: /tracked\n"
        f"<i>Tip: Use /timeline {track_count} to see this story's evolution over time</i>"
    )
    return {"action": "track", "user_id": user_id, "story": story_num}


def handle_tracked(ctx: HandlerContext, chat_id: int | str,
                   user_id: str) -> dict[str, Any]:
    """Show all stories the user is currently tracking."""
    profile = ctx.engine.preferences.get_or_create(user_id)
    tracked = profile.tracked_stories

    if not tracked:
        ctx.bot.send_message(
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
    ctx.bot.send_message(chat_id, "\n".join(lines))
    return {"action": "tracked", "user_id": user_id, "count": len(tracked)}


def handle_untrack(ctx: HandlerContext, chat_id: int | str,
                   user_id: str, args: str) -> dict[str, Any]:
    """Stop tracking a story by its position in /tracked list."""
    try:
        index = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(chat_id, "Usage: /untrack [number] — see /tracked for the list.")
        return {"action": "untrack_help", "user_id": user_id}

    profile = ctx.engine.preferences.get_or_create(user_id)
    if index < 1 or index > len(profile.tracked_stories):
        ctx.bot.send_message(chat_id, f"No tracked story #{index}. See /tracked for the list.")
        return {"action": "untrack_invalid", "user_id": user_id}

    removed = profile.tracked_stories[index - 1]
    ctx.engine.preferences.untrack_story(user_id, index)
    ctx.persist_prefs(chat_id)
    ctx.bot.send_message(
        chat_id,
        f"Stopped tracking: <b>{html_mod.escape(removed['headline'][:80])}</b>"
    )
    return {"action": "untrack", "user_id": user_id, "index": index}


def handle_save(ctx: HandlerContext, chat_id: int | str,
                user_id: str, args: str) -> dict[str, Any]:
    """Bookmark a story from the last briefing."""
    try:
        story_num = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(chat_id, "Usage: tap the \U0001f516 Save button on a story card.")
        return {"action": "save_help", "user_id": user_id}

    items = ctx.last_items.get(user_id, [])
    if story_num < 1 or story_num > len(items):
        ctx.bot.send_message(chat_id, "That story is no longer available. Run /briefing first.")
        return {"action": "save_expired", "user_id": user_id}

    item = items[story_num - 1]
    report_item = ctx.engine.get_report_item(user_id, story_num)
    url = report_item.candidate.url if report_item else ""

    ctx.engine.preferences.save_bookmark(
        user_id,
        title=item["title"],
        source=item.get("source", ""),
        url=url,
        topic=item["topic"],
    )
    ctx.persist_prefs(chat_id)

    bookmark_count = len(ctx.engine.preferences.get_or_create(user_id).bookmarks)
    ctx.bot.send_message(
        chat_id,
        f"\U0001f516 Saved: <b>{html_mod.escape(item['title'][:80])}</b>\n"
        f"View bookmarks: /saved ({bookmark_count} total)\n"
        f"<i>Tip: /export to get all stories as Markdown for your notes app</i>"
    )
    return {"action": "save", "user_id": user_id, "story": story_num}


def handle_saved(ctx: HandlerContext, chat_id: int | str,
                 user_id: str) -> dict[str, Any]:
    """Show all bookmarked stories."""
    profile = ctx.engine.preferences.get_or_create(user_id)
    formatter = ctx.engine.formatter
    card = formatter.format_bookmarks(profile.bookmarks)
    ctx.bot.send_message(chat_id, card)
    return {"action": "saved", "user_id": user_id, "count": len(profile.bookmarks)}


def handle_unsave(ctx: HandlerContext, chat_id: int | str,
                  user_id: str, args: str) -> dict[str, Any]:
    """Remove a bookmark by index."""
    try:
        index = int(args.strip())
    except (ValueError, TypeError):
        ctx.bot.send_message(chat_id, "Usage: /unsave [number] \u2014 see /saved for the list.")
        return {"action": "unsave_help", "user_id": user_id}

    profile = ctx.engine.preferences.get_or_create(user_id)
    if index < 1 or index > len(profile.bookmarks):
        ctx.bot.send_message(chat_id, f"No bookmark #{index}. See /saved for the list.")
        return {"action": "unsave_invalid", "user_id": user_id}

    removed = profile.bookmarks[index - 1]
    ctx.engine.preferences.remove_bookmark(user_id, index)
    ctx.persist_prefs(chat_id)
    ctx.bot.send_message(
        chat_id,
        f"Removed bookmark: <b>{html_mod.escape(removed['title'][:80])}</b>"
    )
    return {"action": "unsave", "user_id": user_id, "index": index}
