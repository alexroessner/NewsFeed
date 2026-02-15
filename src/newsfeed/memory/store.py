from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypeVar

from newsfeed.models.domain import CandidateItem, UserProfile

log = logging.getLogger(__name__)

# ── Bounded per-user cache ───────────────────────────────────────
# Used throughout the engine and communication agent to prevent
# unbounded per-user dict growth when many users interact.

_VT = TypeVar("_VT")


class BoundedUserDict(dict[str, _VT]):
    """A dict that evicts least-recently-used entries when size exceeds a cap.

    Drop-in replacement for ``dict[str, V]`` where keys are user IDs.
    On every __setitem__ the key is moved to the end (most recently used);
    when the population exceeds *maxlen* the oldest entry is evicted.
    """

    __slots__ = ("_maxlen",)

    def __init__(self, maxlen: int = 500, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._maxlen = max(1, maxlen)

    def __setitem__(self, key: str, value: _VT) -> None:
        # Move existing key to end (refresh) or insert at end
        if key in self:
            super().__delitem__(key)
        super().__setitem__(key, value)
        # Evict oldest entries if over cap
        while len(self) > self._maxlen:
            oldest = next(iter(self))
            super().__delitem__(oldest)

    def setdefault(self, key: str, default: _VT = None) -> _VT:  # type: ignore[assignment]
        if key not in self:
            self[key] = default  # type: ignore[assignment]
        return self[key]

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
    # Cap total user profiles to prevent unbounded memory growth.
    # Higher limit than other per-user dicts because preferences are
    # the most important per-user state to preserve.
    MAX_USERS = 5000

    def __init__(self) -> None:
        self._profiles: BoundedUserDict[UserProfile] = BoundedUserDict(maxlen=self.MAX_USERS)

    def get_or_create(self, user_id: str) -> UserProfile:
        if user_id not in self._profiles:
            self._profiles[user_id] = UserProfile(user_id=user_id)
        return self._profiles[user_id]

    MAX_WEIGHTS = 100  # Max distinct topic/source weight entries per user

    def apply_weight_adjustment(self, user_id: str, topic: str, delta: float) -> UserProfile:
        profile = self.get_or_create(user_id)
        current = profile.topic_weights.get(topic, 0.0)
        # Reject new entries if at cap (updates to existing keys are always allowed)
        if topic not in profile.topic_weights and len(profile.topic_weights) >= self.MAX_WEIGHTS:
            return profile
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
        if source not in profile.source_weights and len(profile.source_weights) >= self.MAX_WEIGHTS:
            return profile
        profile.source_weights[source] = round(max(min(current + delta, 2.0), -2.0), 3)
        return profile

    def remove_region(self, user_id: str, region: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if region in profile.regions_of_interest:
            profile.regions_of_interest.remove(region)
        return profile

    MAX_WATCHLIST_SIZE = 50  # Prevent resource exhaustion via unbounded lists

    def set_watchlist(self, user_id: str, crypto: list[str] | None = None,
                     stocks: list[str] | None = None) -> UserProfile:
        profile = self.get_or_create(user_id)
        if crypto is not None:
            profile.watchlist_crypto = crypto[:self.MAX_WATCHLIST_SIZE]
        if stocks is not None:
            profile.watchlist_stocks = stocks[:self.MAX_WATCHLIST_SIZE]
        return profile

    _MAX_TIMEZONE_LEN = 50  # Longest IANA tz is ~32 chars (e.g. "America/Argentina/Buenos_Aires")

    def set_timezone(self, user_id: str, tz: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        tz = tz[:self._MAX_TIMEZONE_LEN]
        profile.timezone = tz
        return profile

    MAX_MUTED_TOPICS = 50

    def mute_topic(self, user_id: str, topic: str) -> UserProfile:
        profile = self.get_or_create(user_id)
        if topic not in profile.muted_topics:
            if len(profile.muted_topics) >= self.MAX_MUTED_TOPICS:
                return profile
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

    _MAX_PRESET_NAME_LEN = 50  # Reasonable limit for a human-typed preset name

    def save_preset(self, user_id: str, name: str) -> UserProfile:
        """Save current preferences as a named preset."""
        name = name[:self._MAX_PRESET_NAME_LEN]
        profile = self.get_or_create(user_id)
        profile.presets[name] = {
            "topic_weights": dict(profile.topic_weights),
            "source_weights": dict(profile.source_weights),
            "tone": profile.tone,
            "format": profile.format,
            "max_items": profile.max_items,
            "regions": list(profile.regions_of_interest),
            "confidence_min": profile.confidence_min,
            "urgency_min": profile.urgency_min,
            "max_per_source": profile.max_per_source,
            "muted_topics": list(profile.muted_topics),
        }
        # Cap at 10 presets — evict oldest (first inserted) if over limit
        while len(profile.presets) > 10:
            first_key = next(iter(profile.presets), None)
            if first_key is None:
                break
            del profile.presets[first_key]
        return profile

    def load_preset(self, user_id: str, name: str) -> UserProfile | None:
        """Load a named preset, replacing current preferences."""
        profile = self.get_or_create(user_id)
        preset = profile.presets.get(name)
        if not preset:
            return None
        profile.topic_weights = dict(preset.get("topic_weights") or {})
        profile.source_weights = dict(preset.get("source_weights") or {})
        profile.tone = str(preset.get("tone") or "concise")
        profile.format = str(preset.get("format") or "bullet")
        profile.max_items = int(preset.get("max_items") or 10)
        profile.regions_of_interest = list(preset.get("regions") or [])
        profile.confidence_min = float(preset.get("confidence_min") or 0.0)
        profile.urgency_min = str(preset.get("urgency_min") or "")
        profile.max_per_source = int(preset.get("max_per_source") or 0)
        profile.muted_topics = list(preset.get("muted_topics") or [])
        return profile

    def delete_preset(self, user_id: str, name: str) -> bool:
        """Delete a named preset. Returns True if it existed."""
        profile = self.get_or_create(user_id)
        if name in profile.presets:
            del profile.presets[name]
            return True
        return False

    def set_filter(self, user_id: str, field: str, value: str) -> UserProfile:
        """Set an advanced briefing filter."""
        profile = self.get_or_create(user_id)
        if field == "confidence":
            profile.confidence_min = max(0.0, min(float(value), 1.0))
        elif field == "urgency":
            valid = {"", "routine", "elevated", "breaking", "critical"}
            if value.lower() in valid:
                profile.urgency_min = value.lower()
        elif field == "max_per_source":
            profile.max_per_source = max(0, min(int(value), 10))
        elif field == "georisk":
            profile.alert_georisk_threshold = max(0.1, min(float(value), 1.0))
        elif field == "trend":
            profile.alert_trend_threshold = max(1.5, min(float(value), 10.0))
        return profile

    # ── Keyword alert management ────────────────────────────────

    _MAX_ALERT_KEYWORDS = 20
    _MAX_KEYWORD_LEN = 50

    def add_alert_keyword(self, user_id: str, keyword: str) -> tuple[UserProfile, str]:
        """Add a keyword alert. Returns (profile, error_message)."""
        profile = self.get_or_create(user_id)
        keyword = keyword.strip().lower()[:self._MAX_KEYWORD_LEN]
        if not keyword or len(keyword) < 2:
            return profile, "Keyword must be at least 2 characters."
        if keyword in profile.alert_keywords:
            return profile, f"Already alerting on '{keyword}'."
        if len(profile.alert_keywords) >= self._MAX_ALERT_KEYWORDS:
            return profile, f"Maximum {self._MAX_ALERT_KEYWORDS} alert keywords reached."
        profile.alert_keywords.append(keyword)
        return profile, ""

    def remove_alert_keyword(self, user_id: str, keyword: str) -> tuple[UserProfile, bool]:
        """Remove a keyword alert. Returns (profile, was_removed)."""
        profile = self.get_or_create(user_id)
        keyword = keyword.strip().lower()
        if keyword in profile.alert_keywords:
            profile.alert_keywords.remove(keyword)
            return profile, True
        return profile, False

    def set_email(self, user_id: str, email: str) -> UserProfile:
        """Set the user's email address for digest delivery."""
        profile = self.get_or_create(user_id)
        profile.email = email.strip()
        return profile

    # ── Custom source management ──────────────────────────────────

    _MAX_CUSTOM_SOURCES = 10

    def add_custom_source(self, user_id: str, name: str, feed_url: str,
                          site_url: str = "", feed_title: str = "",
                          topics: list[str] | None = None) -> tuple[UserProfile, str]:
        """Add a custom RSS source. Returns (profile, error_message)."""
        profile = self.get_or_create(user_id)
        if len(profile.custom_sources) >= self._MAX_CUSTOM_SOURCES:
            return profile, f"Maximum {self._MAX_CUSTOM_SOURCES} custom sources reached."
        # Check for duplicate names
        for src in profile.custom_sources:
            if src["name"].lower() == name.lower():
                return profile, f"Source '{name}' already exists."
        # Check for duplicate feed URLs
        for src in profile.custom_sources:
            if src["feed_url"] == feed_url:
                return profile, f"Feed URL already added as '{src['name']}'."
        profile.custom_sources.append({
            "name": name,
            "feed_url": feed_url,
            "site_url": site_url,
            "feed_title": feed_title,
            "topics": topics or ["general"],
            "added_at": time.time(),
            "items_seen": 0,
        })
        return profile, ""

    def remove_custom_source(self, user_id: str, name: str) -> tuple[UserProfile, bool]:
        """Remove a custom source by name. Returns (profile, was_removed)."""
        profile = self.get_or_create(user_id)
        for i, src in enumerate(profile.custom_sources):
            if src["name"].lower() == name.lower():
                profile.custom_sources.pop(i)
                return profile, True
        return profile, False

    def get_custom_sources(self, user_id: str) -> list[dict]:
        """Get all custom sources for a user."""
        profile = self.get_or_create(user_id)
        return list(profile.custom_sources)

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
        profile.confidence_min = 0.0
        profile.urgency_min = ""
        profile.max_per_source = 0
        profile.alert_georisk_threshold = 0.5
        profile.alert_trend_threshold = 3.0
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
                "confidence_min": p.confidence_min,
                "urgency_min": p.urgency_min,
                "max_per_source": p.max_per_source,
                "alert_georisk_threshold": p.alert_georisk_threshold,
                "alert_trend_threshold": p.alert_trend_threshold,
                "presets": dict(p.presets),
                "webhook_url": p.webhook_url,
                "custom_sources": list(p.custom_sources),
                "alert_keywords": list(p.alert_keywords),
            }
        return result


class CandidateCache:
    # Maximum number of cache slots (user:topic keys) to prevent
    # unbounded memory growth in multi-user deployments.
    _MAX_SLOTS = 500
    # Evict stale entries periodically — every N puts
    _EVICTION_INTERVAL = 20

    def __init__(self, stale_after_minutes: int = 180) -> None:
        self._entries: dict[str, list[CandidateItem]] = {}
        self.stale_after = timedelta(minutes=stale_after_minutes)
        self._eviction_counter = 0

    def key(self, user_id: str, topic: str) -> str:
        return f"{user_id}:{topic}"

    def put(self, user_id: str, topic: str, candidates: list[CandidateItem]) -> None:
        self._entries[self.key(user_id, topic)] = candidates
        self._eviction_counter += 1
        if self._eviction_counter >= self._EVICTION_INTERVAL:
            self._evict_stale()
            self._eviction_counter = 0

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

    def _evict_stale(self) -> None:
        """Remove fully stale entries and enforce max slot cap.

        Runs periodically (triggered by put()) to prevent unbounded growth.
        Entries where ALL candidates are stale are removed entirely.
        If the cache still exceeds _MAX_SLOTS after stale eviction, the
        oldest slots (by newest candidate timestamp) are dropped.
        """
        now = datetime.now(timezone.utc)
        to_remove: list[str] = []
        for cache_key, candidates in self._entries.items():
            if all(now - c.created_at > self.stale_after for c in candidates):
                to_remove.append(cache_key)
        for cache_key in to_remove:
            del self._entries[cache_key]

        # Enforce hard cap by dropping oldest slots
        if len(self._entries) > self._MAX_SLOTS:
            def _newest_ts(cands: list[CandidateItem]) -> datetime:
                return max((c.created_at for c in cands), default=datetime.min.replace(tzinfo=timezone.utc))

            sorted_keys = sorted(self._entries.keys(), key=lambda k: _newest_ts(self._entries[k]))
            overshoot = len(self._entries) - self._MAX_SLOTS
            for k in sorted_keys[:overshoot]:
                del self._entries[k]

        if to_remove:
            log.debug("Cache eviction: removed %d stale slots, %d remaining", len(to_remove), len(self._entries))


class StatePersistence:
    # Only alphanumeric + underscore/hyphen allowed in persistence keys
    _VALID_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str) -> Path:
        """Resolve a persistence key to a safe file path.

        SECURITY: Rejects keys containing path traversal sequences or
        characters outside a strict allowlist.
        """
        if not self._VALID_KEY_RE.match(key):
            raise ValueError(f"Invalid persistence key: {key!r}")
        path = (self.state_dir / f"{key}.json").resolve()
        # Belt-and-suspenders: ensure resolved path is under state_dir
        if not str(path).startswith(str(self.state_dir.resolve())):
            raise ValueError(f"Path traversal blocked for key: {key!r}")
        return path

    def save(self, key: str, data: dict) -> None:
        path = self._safe_path(key)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.rename(path)

    def load(self, key: str) -> dict | None:
        path = self._safe_path(key)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
