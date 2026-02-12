from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from newsfeed.models.domain import CandidateItem, StoryLifecycle, UrgencyLevel


_URGENCY_KEYWORDS = frozenset({
    "breaking", "crisis", "war", "attack", "emergency", "collapse",
    "invasion", "coup", "assassination", "catastrophe", "pandemic",
    "shutdown", "explosion", "sanctions", "ceasefire", "martial_law",
})

_ELEVATED_KEYWORDS = frozenset({
    "escalation", "tension", "warning", "alert", "surge", "protest",
    "election", "summit", "treaty", "regulation", "volatility",
    "disruption", "shortage", "scandal", "indictment",
})


class BreakingDetector:
    def __init__(self, velocity_window_minutes: int = 30, breaking_source_threshold: int = 3) -> None:
        self.velocity_window = timedelta(minutes=velocity_window_minutes)
        self.breaking_source_threshold = breaking_source_threshold

    def assess(self, candidates: list[CandidateItem]) -> list[CandidateItem]:
        now = datetime.now(timezone.utc)

        topic_velocity = self._compute_velocity(candidates, now)

        for c in candidates:
            keyword_urgency = self._keyword_urgency(c)
            velocity_urgency = self._velocity_urgency(c.topic, topic_velocity)
            source_urgency = self._source_count_urgency(c, candidates)
            recency_urgency = self._recency_urgency(c, now)

            final = max(keyword_urgency, velocity_urgency, source_urgency, recency_urgency,
                        key=lambda u: _urgency_rank(u))
            c.urgency = final
            c.lifecycle = self._infer_lifecycle(c, topic_velocity)

        return candidates

    def _compute_velocity(self, candidates: list[CandidateItem], now: datetime) -> dict[str, float]:
        topic_recent: dict[str, int] = defaultdict(int)
        topic_total: dict[str, int] = defaultdict(int)

        for c in candidates:
            topic_total[c.topic] += 1
            if now - c.created_at <= self.velocity_window:
                topic_recent[c.topic] += 1

        velocity: dict[str, float] = {}
        for topic in topic_total:
            total = topic_total[topic]
            recent = topic_recent[topic]
            velocity[topic] = recent / max(total, 1)

        return velocity

    def _keyword_urgency(self, item: CandidateItem) -> UrgencyLevel:
        text = f"{item.title} {item.summary}".lower()
        words = set(text.split())

        if words & _URGENCY_KEYWORDS:
            return UrgencyLevel.BREAKING
        if words & _ELEVATED_KEYWORDS:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _velocity_urgency(self, topic: str, velocity: dict[str, float]) -> UrgencyLevel:
        v = velocity.get(topic, 0.0)
        if v >= 0.8:
            return UrgencyLevel.CRITICAL
        if v >= 0.5:
            return UrgencyLevel.BREAKING
        if v >= 0.3:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _source_count_urgency(self, item: CandidateItem, all_candidates: list[CandidateItem]) -> UrgencyLevel:
        topic_sources = {c.source for c in all_candidates if c.topic == item.topic}
        if len(topic_sources) >= self.breaking_source_threshold + 2:
            return UrgencyLevel.BREAKING
        if len(topic_sources) >= self.breaking_source_threshold:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _recency_urgency(self, item: CandidateItem, now: datetime) -> UrgencyLevel:
        age = now - item.created_at
        if age <= timedelta(minutes=5):
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _infer_lifecycle(self, item: CandidateItem, velocity: dict[str, float]) -> StoryLifecycle:
        v = velocity.get(item.topic, 0.0)
        if item.urgency in (UrgencyLevel.CRITICAL, UrgencyLevel.BREAKING):
            return StoryLifecycle.BREAKING
        if v >= 0.3:
            return StoryLifecycle.DEVELOPING
        if item.novelty_score < 0.3:
            return StoryLifecycle.WANING
        return StoryLifecycle.ONGOING


def _urgency_rank(level: UrgencyLevel) -> int:
    return {
        UrgencyLevel.ROUTINE: 0,
        UrgencyLevel.ELEVATED: 1,
        UrgencyLevel.BREAKING: 2,
        UrgencyLevel.CRITICAL: 3,
    }.get(level, 0)
