from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from newsfeed.models.domain import CandidateItem, TrendSnapshot


class TrendDetector:
    # Cap tracked topics — evict lowest-velocity topics when exceeded
    _MAX_TOPICS = 200

    def __init__(self, window_minutes: int = 60, anomaly_threshold: float = 2.0, baseline_decay: float = 0.8) -> None:
        self.window = timedelta(minutes=window_minutes)
        self.window_minutes = window_minutes
        self.anomaly_threshold = anomaly_threshold
        self.baseline_decay = baseline_decay
        self._baseline: dict[str, float] = {}

    def analyze(self, candidates: list[CandidateItem]) -> list[TrendSnapshot]:
        now = datetime.now(timezone.utc)

        topic_counts: dict[str, int] = defaultdict(int)
        topic_recent: dict[str, int] = defaultdict(int)
        topic_scores: dict[str, list[float]] = defaultdict(list)

        for c in candidates:
            topic_counts[c.topic] += 1
            topic_scores[c.topic].append(c.composite_score())
            if now - c.created_at <= self.window:
                topic_recent[c.topic] += 1

        snapshots: list[TrendSnapshot] = []
        for topic in topic_counts:
            total = topic_counts[topic]
            recent = topic_recent[topic]
            velocity = recent / max(total, 1)

            baseline = self._baseline.get(topic, 0.3)
            anomaly_score = velocity / max(baseline, 0.01)
            is_emerging = anomaly_score >= self.anomaly_threshold and total >= 2

            self._baseline[topic] = round(
                baseline * self.baseline_decay + velocity * (1 - self.baseline_decay), 4
            )

            snapshots.append(TrendSnapshot(
                topic=topic,
                velocity=round(velocity, 3),
                baseline_velocity=round(baseline, 3),
                anomaly_score=round(anomaly_score, 3),
                is_emerging=is_emerging,
                sample_window_minutes=self.window_minutes,
            ))

        # Evict stale topics when baseline grows too large
        if len(self._baseline) > self._MAX_TOPICS:
            # Drop the topics with lowest baseline velocity (least active)
            sorted_topics = sorted(self._baseline.items(), key=lambda kv: kv[1])
            excess = len(self._baseline) - self._MAX_TOPICS
            for topic_key, _ in sorted_topics[:excess]:
                del self._baseline[topic_key]

        snapshots.sort(key=lambda t: t.anomaly_score, reverse=True)
        return snapshots

    def get_emerging_topics(self, candidates: list[CandidateItem]) -> list[str]:
        snapshots = self.analyze(candidates)
        return [s.topic for s in snapshots if s.is_emerging]

    def snapshot(self) -> dict[str, float]:
        return dict(self._baseline)
