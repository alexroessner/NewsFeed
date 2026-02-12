from __future__ import annotations

import hashlib

from newsfeed.models.domain import CandidateItem, SourceReliability


# Known tier-1 wire services and outlets with strong editorial standards
_TIER1_SOURCES = frozenset({"reuters", "ap", "bbc", "guardian", "ft"})
_TIER2_SOURCES = frozenset({"x", "reddit", "web"})

_BIAS_PROFILES: dict[str, str] = {
    "reuters": "center",
    "ap": "center",
    "bbc": "center-left",
    "guardian": "left-leaning",
    "ft": "center-right",
    "x": "variable",
    "reddit": "community-driven",
    "web": "unverified",
}


class CredibilityTracker:
    def __init__(self) -> None:
        self._sources: dict[str, SourceReliability] = {}

    def _init_source(self, source_id: str) -> SourceReliability:
        if source_id in _TIER1_SOURCES:
            base = 0.85
        elif source_id in _TIER2_SOURCES:
            base = 0.55
        else:
            base = 0.50
        return SourceReliability(
            source_id=source_id,
            reliability_score=base,
            bias_rating=_BIAS_PROFILES.get(source_id, "unrated"),
            historical_accuracy=base,
            corroboration_rate=0.5,
        )

    def get_source(self, source_id: str) -> SourceReliability:
        if source_id not in self._sources:
            self._sources[source_id] = self._init_source(source_id)
        return self._sources[source_id]

    def record_item(self, item: CandidateItem) -> None:
        sr = self.get_source(item.source)
        sr.total_items_seen += 1

    def record_corroboration(self, source_a: str, source_b: str) -> None:
        for sid in (source_a, source_b):
            sr = self.get_source(sid)
            old = sr.corroboration_rate
            sr.corroboration_rate = round(min(1.0, old + 0.02), 3)

    def score_candidate(self, item: CandidateItem) -> float:
        sr = self.get_source(item.source)
        trust = sr.trust_factor()
        corroboration_bonus = min(0.2, 0.08 * len(item.corroborated_by))
        return min(1.0, item.composite_score() * 0.7 + trust * 0.2 + corroboration_bonus + 0.1 * item.evidence_score)


def detect_cross_corroboration(candidates: list[CandidateItem]) -> list[CandidateItem]:
    title_key_map: dict[str, list[CandidateItem]] = {}
    for c in candidates:
        key = _normalize_title(c.title)
        title_key_map.setdefault(key, []).append(c)

    topic_source_map: dict[str, dict[str, list[CandidateItem]]] = {}
    for c in candidates:
        topic_source_map.setdefault(c.topic, {}).setdefault(c.source, []).append(c)

    for key, group in title_key_map.items():
        sources_in_group = {c.source for c in group}
        if len(sources_in_group) >= 2:
            source_list = sorted(sources_in_group)
            for c in group:
                c.corroborated_by = [s for s in source_list if s != c.source]

    for topic, by_source in topic_source_map.items():
        if len(by_source) >= 3:
            all_sources = sorted(by_source.keys())
            for source, items in by_source.items():
                others = [s for s in all_sources if s != source]
                for c in items:
                    if not c.corroborated_by:
                        c.corroborated_by = others[:2]

    return candidates


def enforce_source_diversity(candidates: list[CandidateItem], max_per_source: int = 3) -> list[CandidateItem]:
    source_counts: dict[str, int] = {}
    diverse: list[CandidateItem] = []
    overflow: list[CandidateItem] = []

    sorted_candidates = sorted(candidates, key=lambda c: c.composite_score(), reverse=True)
    for c in sorted_candidates:
        count = source_counts.get(c.source, 0)
        if count < max_per_source:
            diverse.append(c)
            source_counts[c.source] = count + 1
        else:
            overflow.append(c)

    return diverse + overflow


def _normalize_title(title: str) -> str:
    words = title.lower().split()
    filtered = [w for w in words if len(w) > 3]
    return hashlib.md5(" ".join(filtered[:6]).encode()).hexdigest()[:12]
