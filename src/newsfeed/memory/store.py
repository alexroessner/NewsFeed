from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsfeed.models.domain import CandidateItem, UserProfile


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

    def reset(self, user_id: str) -> UserProfile:
        """Reset all user preferences to defaults."""
        profile = self.get_or_create(user_id)
        profile.topic_weights.clear()
        profile.source_weights.clear()
        profile.regions_of_interest.clear()
        profile.tone = "concise"
        profile.format = "bullet"
        profile.max_items = 10
        profile.briefing_cadence = "on_demand"
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
