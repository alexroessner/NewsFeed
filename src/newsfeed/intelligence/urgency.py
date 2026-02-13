from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from newsfeed.models.domain import CandidateItem, StoryLifecycle, UrgencyLevel


_DEFAULT_BREAKING_KEYWORDS = frozenset({
    "breaking", "crisis", "war", "attack", "emergency", "collapse",
    "invasion", "coup", "assassination", "catastrophe", "pandemic",
    "shutdown", "explosion", "sanctions", "ceasefire", "martial_law",
})

_DEFAULT_ELEVATED_KEYWORDS = frozenset({
    "escalation", "tension", "warning", "alert", "surge", "protest",
    "election", "summit", "treaty", "regulation", "volatility",
    "disruption", "shortage", "scandal", "indictment",
})


class BreakingDetector:
    def __init__(
        self,
        velocity_window_minutes: int = 30,
        breaking_source_threshold: int = 3,
        urgency_keywords_cfg: dict[str, list[str]] | None = None,
        velocity_thresholds: dict[str, float] | None = None,
        recency_elevated_minutes: int = 5,
        waning_novelty_threshold: float = 0.3,
    ) -> None:
        self.velocity_window = timedelta(minutes=velocity_window_minutes)
        self.breaking_source_threshold = breaking_source_threshold
        self.recency_window = timedelta(minutes=recency_elevated_minutes)
        self.waning_novelty_threshold = waning_novelty_threshold

        kw = urgency_keywords_cfg or {}
        self._breaking_keywords = frozenset(kw.get("breaking", [])) or _DEFAULT_BREAKING_KEYWORDS
        self._elevated_keywords = frozenset(kw.get("elevated", [])) or _DEFAULT_ELEVATED_KEYWORDS

        vt = velocity_thresholds or {}
        self._v_critical = vt.get("critical", 0.8)
        self._v_breaking = vt.get("breaking", 0.5)
        self._v_elevated = vt.get("elevated", 0.3)

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
        """Compute topic velocity — fraction of items that appeared recently.

        Items with example.com URLs (simulated placeholders) are excluded from
        velocity calculation since their timestamps are synthetic.
        """
        topic_recent: dict[str, int] = defaultdict(int)
        topic_total: dict[str, int] = defaultdict(int)

        for c in candidates:
            # Skip simulated items — they have default timestamps that inflate velocity
            if "example.com" in (c.url or ""):
                continue
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

        if words & self._breaking_keywords:
            return UrgencyLevel.BREAKING
        if words & self._elevated_keywords:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _velocity_urgency(self, topic: str, velocity: dict[str, float]) -> UrgencyLevel:
        v = velocity.get(topic, 0.0)
        if v >= self._v_critical:
            return UrgencyLevel.CRITICAL
        if v >= self._v_breaking:
            return UrgencyLevel.BREAKING
        if v >= self._v_elevated:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _source_count_urgency(self, item: CandidateItem, all_candidates: list[CandidateItem]) -> UrgencyLevel:
        """Check how many independent sources corroborate THIS specific story.

        Uses the corroborated_by field (set by detect_cross_corroboration which
        runs before urgency in the pipeline) rather than counting all sources
        covering the same broad topic.
        """
        corroborating = len(item.corroborated_by) if item.corroborated_by else 0
        if corroborating >= self.breaking_source_threshold + 1:
            return UrgencyLevel.BREAKING
        if corroborating >= self.breaking_source_threshold:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _recency_urgency(self, item: CandidateItem, now: datetime) -> UrgencyLevel:
        age = now - item.created_at
        if age <= self.recency_window:
            return UrgencyLevel.ELEVATED
        return UrgencyLevel.ROUTINE

    def _infer_lifecycle(self, item: CandidateItem, velocity: dict[str, float]) -> StoryLifecycle:
        v = velocity.get(item.topic, 0.0)
        if item.urgency in (UrgencyLevel.CRITICAL, UrgencyLevel.BREAKING):
            return StoryLifecycle.BREAKING
        if v >= self._v_elevated:
            return StoryLifecycle.DEVELOPING
        if item.novelty_score < self.waning_novelty_threshold:
            return StoryLifecycle.WANING
        return StoryLifecycle.ONGOING


def _urgency_rank(level: UrgencyLevel) -> int:
    return {
        UrgencyLevel.ROUTINE: 0,
        UrgencyLevel.ELEVATED: 1,
        UrgencyLevel.BREAKING: 2,
        UrgencyLevel.CRITICAL: 3,
    }.get(level, 0)
