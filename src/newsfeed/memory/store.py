from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
