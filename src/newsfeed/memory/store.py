from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsfeed.models.domain import CandidateItem, UserProfile

# Common stop words to exclude from keyword extraction
_STOP_WORDS = frozenset({
    "a", "an", "the", "in", "on", "at", "to", "for", "of", "and", "or",
    "is", "are", "was", "were", "be", "been", "has", "have", "had", "do",
    "does", "did", "will", "would", "could", "should", "may", "might",
    "with", "from", "by", "as", "into", "about", "over", "after", "before",
    "between", "under", "up", "down", "out", "new", "says", "said", "its",
    "it", "that", "this", "but", "not", "no", "what", "how", "why", "who",
    "when", "where", "which", "than", "more", "also", "can", "will", "just",
})


def extract_keywords(headline: str) -> list[str]:
    """Extract meaningful keywords from a headline for tracking."""
    words = re.findall(r"[a-zA-Z]+", headline.lower())
    return [w for w in words if w not in _STOP_WORDS and len(w) > 2]


def match_tracked(story_topic: str, story_title: str,
                  tracked: dict) -> bool:
    """Check if a story matches a tracked item.

    Matches if same topic AND at least 2 keyword overlaps.
    """
    if story_topic != tracked["topic"]:
        return False
    story_words = set(extract_keywords(story_title))
    tracked_words = set(tracked["keywords"])
    return len(story_words & tracked_words) >= 2


class PreferenceStore:
    def __init__(self) -> None:
        self._profiles: dict[str, UserProfile] = {}

    def get_or_create(self, user_id: str) -> UserProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = UserProfile(user_id=user_id)
        return self._profiles[user_id]

    def apply_weight_adjustment(self, user_id: str, topic: str, delta: float) -> UserProfile:
        profile = self.get_or_create(user_id)
        current = profile.topic_weights.get(topic, 0.0)
        profile.topic_weights[topic] = round(max(min(current + delta, 1.0), -1.0), 3)
        return profile

    def apply_style_update(self, user_id: str, tone: str | None = None, fmt: str | None = None) -> UserProfile:
        profile = self.get_or_create(user_id)
        if tone:
            profile.tone = tone
        if fmt:
            profile.format = fmt
        return profile

    def apply_region(self, user_id: str, region: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if region not in profile.regions_of_interest:
            profile.regions_of_interest.append(region)
        return profile

    def apply_cadence(self, user_id: str, cadence: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        profile.briefing_cadence = cadence
        return profile

    def apply_max_items(self, user_id: str, max_items: int) -> UserProfile:
        profile = self.get_or_create(user_id)
        profile.max_items = max(1, min(max_items, 50))
        return profile

    def apply_source_weight(self, user_id: str, source: str, delta: float) -> UserProfile:
        profile = self.get_or_create(user_id)
        current = profile.source_weights.get(source, 0.0)
        profile.source_weights[source] = round(max(min(current + delta, 2.0), -2.0), 3)
        return profile

    def remove_region(self, user_id: str, region: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if region in profile.regions_of_interest:
            profile.regions_of_interest.remove(region)
        return profile

    def set_watchlist(self, user_id: str, crypto: list[str] | None = None,
                     stocks: list[str] | None = None) -> UserProfile:
        profile = self.get_or_create(user_id)
        if crypto is not None:
            profile.watchlist_crypto = crypto
        if stocks is not None:
            profile.watchlist_stocks = stocks
        return profile

    def set_timezone(self, user_id: str, tz: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        profile.timezone = tz
        return profile

    def mute_topic(self, user_id: str, topic: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if topic not in profile.muted_topics:
            profile.muted_topics.append(topic)
        return profile

    def unmute_topic(self, user_id: str, topic: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if topic in profile.muted_topics:
            profile.muted_topics.remove(topic)
        return profile

    def track_story(self, user_id: str, topic: str,
                    headline: str) -> UserProfile:
        """Track a story for cross-briefing continuity."""
        profile = self.get_or_create(user_id)
        keywords = extract_keywords(headline)
        if not keywords:
            return profile
        # Avoid duplicates — check if already tracking similar story
        for existing in profile.tracked_stories:
            if match_tracked(topic, headline, existing):
                return profile  # already tracking
        profile.tracked_stories.append({
            "topic": topic,
            "keywords": keywords,
            "headline": headline,
            "tracked_at": time.time(),
        })
        # Cap at 20 tracked stories
        if len(profile.tracked_stories) > 20:
            profile.tracked_stories = profile.tracked_stories[-20:]
        return profile

    def untrack_story(self, user_id: str, index: int) -> UserProfile:
        """Remove a tracked story by 1-based index."""
        profile = self.get_or_create(user_id)
        if 1 <= index <= len(profile.tracked_stories):
            profile.tracked_stories.pop(index - 1)
        return profile

    def save_bookmark(self, user_id: str, title: str, source: str,
                      url: str, topic: str) -> UserProfile:
        """Save a story bookmark."""
        profile = self.get_or_create(user_id)
        # Avoid exact title duplicates
        for existing in profile.bookmarks:
            if existing["title"] == title:
                return profile
        profile.bookmarks.append({
            "title": title,
            "source": source,
            "url": url,
            "topic": topic,
            "saved_at": time.time(),
        })
        # Cap at 50 bookmarks
        if len(profile.bookmarks) > 50:
            profile.bookmarks = profile.bookmarks[-50:]
        return profile

    def remove_bookmark(self, user_id: str, index: int) -> UserProfile:
        """Remove a bookmark by 1-based index."""
        profile = self.get_or_create(user_id)
        if 1 <= index <= len(profile.bookmarks):
            profile.bookmarks.pop(index - 1)
        return profile

    def set_email(self, user_id: str, email: str) -> UserProfile:
        """Set the user's email address for digest delivery."""
        profile = self.get_or_create(user_id)
        profile.email = email.strip()
        return profile

    def reset(self, user_id: str) -> UserProfile:
        """Reset all user preferences to defaults."""
        profile = self.get_or_create(user_id)
        profile.topic_weights.clear()
        profile.source_weights.clear()
        profile.regions_of_interest.clear()
        profile.muted_topics.clear()
        profile.tone = "concise"
        profile.format = "bullet"
        profile.max_items = 10
        profile.briefing_cadence = "on_demand"
        profile.timezone = "UTC"
        # Keep watchlists, tracked stories, bookmarks, and email on reset — those are data, not weights
        return profile

    def snapshot(self) -> dict[str, dict]:
        result = {}
        for uid, p in self._profiles.items():
            result[uid] = {
                "topic_weights": dict(p.topic_weights),
                "source_weights": dict(p.source_weights),
                "tone": p.tone,
                "format": p.format,
                "max_items": p.max_items,
                "cadence": p.briefing_cadence,
                "regions": list(p.regions_of_interest),
                "watchlist_crypto": list(p.watchlist_crypto),
                "watchlist_stocks": list(p.watchlist_stocks),
                "timezone": p.timezone,
                "muted_topics": list(p.muted_topics),
                "tracked_stories": list(p.tracked_stories),
                "bookmarks": list(p.bookmarks),
                "email": p.email,
            }
        return result


class CandidateCache:
    def __init__(self, stale_after_minutes: int = 180) -> None:
        self._entries: dict[str, list[CandidateItem]] = {}
        self.stale_after = timedelta(minutes=stale_after_minutes)

    def key(self, user_id: str, topic: str) -> str:
        return f"{user_id}:{topic}"

    def put(self, user_id: str, topic: str, candidates: list[CandidateItem]) -> None:
        self._entries[self.key(user_id, topic)] = candidates

    def get_fresh(self, user_id: str, topic: str) -> list[CandidateItem]:
        now = datetime.now(timezone.utc)
        fresh: list[CandidateItem] = []
        for c in self._entries.get(self.key(user_id, topic), []):
            if now - c.created_at <= self.stale_after:
                fresh.append(c)
        return fresh

    def get_all_fresh(self, user_id: str) -> list[CandidateItem]:
        """Get all fresh candidates across all topics for a user."""
        now = datetime.now(timezone.utc)
        prefix = f"{user_id}:"
        fresh: list[CandidateItem] = []
        for key, candidates in self._entries.items():
            if key.startswith(prefix):
                for c in candidates:
                    if now - c.created_at <= self.stale_after:
                        fresh.append(c)
        return fresh

    def get_more(self, user_id: str, topic: str, already_seen_ids: set[str], limit: int) -> list[CandidateItem]:
        candidates = self.get_fresh(user_id, topic)
        unseen = [replace(c) for c in candidates if c.candidate_id not in already_seen_ids]
        unseen.sort(key=lambda c: c.composite_score(), reverse=True)
        return unseen[:limit]


class StatePersistence:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save(self, key: str, data: dict) -> None:
        path = self.state_dir / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.rename(path)

    def load(self, key: str) -> dict | None:
        path = self.state_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
