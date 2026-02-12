from __future__ import annotations

import hashlib
from typing import Any

from newsfeed.models.domain import CandidateItem, SourceReliability


class CredibilityTracker:
    def __init__(self, intel_cfg: dict[str, Any] | None = None) -> None:
        cfg = intel_cfg or {}
        self._sources: dict[str, SourceReliability] = {}

        tiers = cfg.get("source_tiers", {})
        t1 = tiers.get("tier_1", {})
        t1b = tiers.get("tier_1b", {})
        t2 = tiers.get("tier_2", {})
        t_academic = tiers.get("tier_academic", {})
        self._tier1_sources = frozenset(t1.get("sources", ["reuters", "ap", "bbc", "guardian", "ft"]))
        self._tier1b_sources = frozenset(t1b.get("sources", ["aljazeera"]))
        self._tier2_sources = frozenset(t2.get("sources", ["x", "reddit", "web", "hackernews", "gdelt"]))
        self._academic_sources = frozenset(t_academic.get("sources", ["arxiv"]))
        self._tier1_base = t1.get("base_reliability", 0.85)
        self._tier1b_base = t1b.get("base_reliability", 0.78)
        self._tier2_base = t2.get("base_reliability", 0.55)
        self._academic_base = t_academic.get("base_reliability", 0.72)
        self._unknown_base = tiers.get("unknown_base_reliability", 0.50)
        self._bias_profiles: dict[str, str] = cfg.get("bias_profiles", {
            "reuters": "center", "ap": "center", "bbc": "center-left",
            "guardian": "left-leaning", "ft": "center-right",
            "x": "variable", "reddit": "community-driven", "web": "unverified",
        })
        self._corroboration_increment = cfg.get("corroboration_increment", 0.02)

        scoring = cfg.get("_scoring", {}).get("credibility_weights", {})
        self._w_composite = scoring.get("composite", 0.70)
        self._w_trust = scoring.get("trust", 0.20)
        self._w_evidence = scoring.get("evidence", 0.10)
        self._bonus_per = scoring.get("corroboration_bonus_per_source", 0.08)
        self._bonus_cap = scoring.get("corroboration_bonus_cap", 0.20)

    def _init_source(self, source_id: str) -> SourceReliability:
        if source_id in self._tier1_sources:
            base = self._tier1_base
        elif source_id in self._tier1b_sources:
            base = self._tier1b_base
        elif source_id in self._academic_sources:
            base = self._academic_base
        elif source_id in self._tier2_sources:
            base = self._tier2_base
        else:
            base = self._unknown_base
        return SourceReliability(
            source_id=source_id,
            reliability_score=base,
            bias_rating=self._bias_profiles.get(source_id, "unrated"),
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
            sr.corroboration_rate = round(min(1.0, old + self._corroboration_increment), 3)

    def score_candidate(self, item: CandidateItem) -> float:
        sr = self.get_source(item.source)
        trust = sr.trust_factor()
        corroboration_bonus = min(self._bonus_cap, self._bonus_per * len(item.corroborated_by))
        return min(1.0, item.composite_score() * self._w_composite + trust * self._w_trust + corroboration_bonus + self._w_evidence * item.evidence_score)

    def snapshot(self) -> dict[str, dict]:
        return {sid: {"reliability": sr.reliability_score, "accuracy": sr.historical_accuracy,
                       "corroboration": sr.corroboration_rate, "seen": sr.total_items_seen}
                for sid, sr in self._sources.items()}


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
