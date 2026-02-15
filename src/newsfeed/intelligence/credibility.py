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

    def get_all_sources_by_tier(self) -> dict[str, list[str]]:
        """Return all known source IDs grouped by tier."""
        result: dict[str, list[str]] = {
            "tier_1": sorted(self._tier1_sources),
            "tier_1b": sorted(self._tier1b_sources),
            "tier_academic": sorted(self._academic_sources),
            "tier_2": sorted(self._tier2_sources),
        }
        # Include any sources seen that aren't in a known tier
        known = self._tier1_sources | self._tier1b_sources | self._academic_sources | self._tier2_sources
        unknown = sorted(set(self._sources.keys()) - known)
        if unknown:
            result["unknown"] = unknown
        return result

    def snapshot(self) -> dict[str, dict]:
        return {sid: {"reliability": sr.reliability_score, "accuracy": sr.historical_accuracy,
                       "corroboration": sr.corroboration_rate, "seen": sr.total_items_seen}
                for sid, sr in self._sources.items()}


def detect_cross_corroboration(candidates: list[CandidateItem]) -> list[CandidateItem]:
    """Detect cross-source corroboration using content similarity.

    Two items corroborate each other when they cover the SAME story from
    DIFFERENT sources.  We measure this via keyword overlap (Jaccard
    similarity) between title+summary text, requiring a threshold of shared
    significant words.  Topic-level matching alone is NOT sufficient — simply
    covering the same broad topic (e.g. geopolitics) is not corroboration.
    """
    word_sets: list[tuple[CandidateItem, set[str]]] = []
    for c in candidates:
        # Simulated placeholders have synthetic text — skip them to avoid
        # false corroboration between "Simulated placeholder — reddit agent..."
        # and "Simulated placeholder — reuters agent..." (Jaccard ~0.75).
        if "example.com" in (c.url or ""):
            word_sets.append((c, set()))
            continue
        words = _extract_significant_words(f"{c.title} {c.summary}")
        word_sets.append((c, words))

    # Compare every pair; only mark if from different sources and similar enough
    for i, (ci, wi) in enumerate(word_sets):
        corr_sources: set[str] = set()
        for j, (cj, wj) in enumerate(word_sets):
            if i >= j:
                continue
            if ci.source == cj.source:
                continue
            similarity = _jaccard(wi, wj)
            if similarity >= 0.25:
                corr_sources.add(cj.source)
                # Also mark the other direction
                if ci.source not in (cj.corroborated_by or []):
                    cj.corroborated_by = list(set(cj.corroborated_by or []) | {ci.source})
        if corr_sources:
            ci.corroborated_by = list(set(ci.corroborated_by or []) | corr_sources)

    return candidates


def enforce_source_diversity(candidates: list[CandidateItem], max_per_source: int = 3) -> list[CandidateItem]:
    """Keep at most max_per_source items per source, dropping the rest.

    Items are sorted by composite score first, so the best items from each
    source survive.  Overflow items are discarded entirely — they previously
    were appended back, defeating the purpose of the filter.
    """
    source_counts: dict[str, int] = {}
    diverse: list[CandidateItem] = []

    sorted_candidates = sorted(candidates, key=lambda c: c.composite_score(), reverse=True)
    for c in sorted_candidates:
        count = source_counts.get(c.source, 0)
        if count < max_per_source:
            diverse.append(c)
            source_counts[c.source] = count + 1

    return diverse


_STOP_WORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "has", "her",
    "was", "one", "our", "out", "his", "its", "from", "they", "been", "have",
    "this", "that", "with", "will", "each", "make", "like", "into", "over",
    "such", "than", "them", "some", "what", "when", "who", "how", "about",
    "more", "also", "after", "says", "said", "new", "could", "would", "been",
    "most", "just", "being", "other", "very", "still", "should", "here",
    "simulated", "signal", "candidate", "insight", "generated", "placeholder",
})


def _extract_significant_words(text: str) -> set[str]:
    """Extract significant content words from text for similarity matching."""
    import re
    words = re.findall(r"[a-z]{3,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0
