from __future__ import annotations

import hashlib
from collections import defaultdict

from newsfeed.models.domain import (
    CandidateItem,
    ConfidenceBand,
    NarrativeThread,
    StoryLifecycle,
    UrgencyLevel,
)


class StoryClustering:
    def __init__(self, similarity_threshold: float = 0.6, cross_source_factor: float = 0.7) -> None:
        self.similarity_threshold = similarity_threshold
        self.cross_source_factor = cross_source_factor

    def cluster(self, candidates: list[CandidateItem]) -> list[NarrativeThread]:
        by_topic: dict[str, list[CandidateItem]] = defaultdict(list)
        for c in candidates:
            by_topic[c.topic].append(c)

        # Pre-compute composite scores once for all candidates.
        # This avoids redundant dict-lookup + arithmetic across sorting,
        # max(), confidence computation, and thread scoring (~4x per item).
        score_cache: dict[str, float] = {c.candidate_id: c.composite_score() for c in candidates}

        threads: list[NarrativeThread] = []
        for topic, items in by_topic.items():
            sub_clusters = self._cluster_within_topic(items, score_cache)
            for idx, cluster_items in enumerate(sub_clusters):
                sources = {c.source for c in cluster_items}
                best = max(cluster_items, key=lambda c: score_cache[c.candidate_id])

                urgency = self._aggregate_urgency(cluster_items)
                lifecycle = self._aggregate_lifecycle(cluster_items)
                confidence = self._compute_confidence(cluster_items, score_cache)

                thread_id = hashlib.sha256(
                    f"{topic}:{idx}:{best.candidate_id}".encode()
                ).hexdigest()[:12]

                threads.append(NarrativeThread(
                    thread_id=thread_id,
                    headline=best.title,
                    candidates=sorted(cluster_items, key=lambda c: score_cache[c.candidate_id], reverse=True),
                    lifecycle=lifecycle,
                    urgency=urgency,
                    source_count=len(sources),
                    confidence=confidence,
                ))

        threads.sort(key=lambda t: t.thread_score(), reverse=True)
        return threads

    def _cluster_within_topic(self, items: list[CandidateItem],
                              score_cache: dict[str, float]) -> list[list[CandidateItem]]:
        if len(items) <= 1:
            return [items] if items else []

        clusters: list[list[CandidateItem]] = []
        assigned: set[str] = set()

        sorted_items = sorted(items, key=lambda c: score_cache[c.candidate_id], reverse=True)

        for item in sorted_items:
            if item.candidate_id in assigned:
                continue

            cluster = [item]
            assigned.add(item.candidate_id)

            for other in sorted_items:
                if other.candidate_id in assigned:
                    continue
                if self._are_similar(item, other):
                    cluster.append(other)
                    assigned.add(other.candidate_id)

            clusters.append(cluster)

        return clusters

    def _are_similar(self, a: CandidateItem, b: CandidateItem) -> bool:
        if a.topic != b.topic:
            return False

        words_a = set(a.title.lower().split())
        words_b = set(b.title.lower().split())
        if not words_a or not words_b:
            return False
        overlap = len(words_a & words_b) / max(len(words_a | words_b), 1)

        if a.source == b.source:
            return overlap >= self.similarity_threshold
        return overlap >= (self.similarity_threshold * self.cross_source_factor)

    def _aggregate_urgency(self, items: list[CandidateItem]) -> UrgencyLevel:
        priority = {
            UrgencyLevel.CRITICAL: 4,
            UrgencyLevel.BREAKING: 3,
            UrgencyLevel.ELEVATED: 2,
            UrgencyLevel.ROUTINE: 1,
        }
        return max((c.urgency for c in items), key=lambda u: priority.get(u, 0), default=UrgencyLevel.ROUTINE)

    def _aggregate_lifecycle(self, items: list[CandidateItem]) -> StoryLifecycle:
        priority = {
            StoryLifecycle.BREAKING: 5,
            StoryLifecycle.DEVELOPING: 4,
            StoryLifecycle.ONGOING: 3,
            StoryLifecycle.WANING: 2,
            StoryLifecycle.RESOLVED: 1,
        }
        return max((c.lifecycle for c in items), key=lambda l: priority.get(l, 0), default=StoryLifecycle.DEVELOPING)

    def _compute_confidence(self, items: list[CandidateItem],
                            score_cache: dict[str, float] | None = None) -> ConfidenceBand:
        if not items:
            return ConfidenceBand(low=0.0, mid=0.0, high=0.0, key_assumptions=["No items in cluster"])
        scores = [score_cache[c.candidate_id] if score_cache else c.composite_score() for c in items]
        sources = {c.source for c in items}
        avg = sum(scores) / len(scores)
        spread = max(scores) - min(scores) if len(scores) > 1 else 0.1

        assumptions = []
        if len(sources) >= 2:
            assumptions.append(f"Corroborated across {len(sources)} sources")
        else:
            assumptions.append("Single-source reporting")

        if any(c.corroborated_by for c in items):
            assumptions.append("Cross-reference confirmation detected")

        return ConfidenceBand(
            low=round(max(0.0, avg - spread - 0.1), 3),
            mid=round(avg, 3),
            high=round(min(1.0, avg + spread + 0.1), 3),
            key_assumptions=assumptions,
        )
