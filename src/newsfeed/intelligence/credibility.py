from __future__ import annotations

from collections import defaultdict
from typing import Any

from newsfeed.intelligence.source_tiers import SourceTiers
from newsfeed.models.domain import CandidateItem, SourceReliability


class CredibilityTracker:
    # Cap tracked sources to prevent unbounded growth from custom feeds
    _MAX_SOURCES = 500

    def __init__(self, intel_cfg: dict[str, Any] | None = None) -> None:
        cfg = intel_cfg or {}
        self._sources: dict[str, SourceReliability] = {}

        # Use unified source tiers — single source of truth
        self._tiers = SourceTiers(cfg)
        self._tier1_sources = self._tiers.sources_in_tier("tier_1")
        self._tier1b_sources = self._tiers.sources_in_tier("tier_1b")
        self._tier2_sources = self._tiers.sources_in_tier("tier_2")
        self._academic_sources = self._tiers.sources_in_tier("tier_academic")
        self._unknown_base = 0.50
        self._bias_profiles = {s: self._tiers.bias(s) for s in self._tiers.all_known_sources()}
        self._corroboration_increment = cfg.get("corroboration_increment", 0.02)

        scoring = cfg.get("_scoring", {}).get("credibility_weights", {})
        self._w_composite = scoring.get("composite", 0.80)
        self._w_trust = scoring.get("trust", 0.20)
        self._bonus_per = scoring.get("corroboration_bonus_per_source", 0.08)
        self._bonus_cap = scoring.get("corroboration_bonus_cap", 0.20)

    def _init_source(self, source_id: str) -> SourceReliability:
        base = self._tiers.base_reliability(source_id)
        return SourceReliability(
            source_id=source_id,
            reliability_score=base,
            bias_rating=self._tiers.bias(source_id),
            historical_accuracy=base,
            corroboration_rate=0.5,
        )

    def get_source(self, source_id: str) -> SourceReliability:
        if source_id not in self._sources:
            # Evict least-seen unknown sources when at capacity
            if len(self._sources) >= self._MAX_SOURCES:
                self._evict_least_seen()
            self._sources[source_id] = self._init_source(source_id)
        return self._sources[source_id]

    def _evict_least_seen(self) -> None:
        """Evict the least-active unknown-tier source to stay under cap.

        Known-tier sources (tier_1, tier_1b, tier_2, academic) are never
        evicted — only dynamically discovered sources are candidates.
        """
        known = self._tier1_sources | self._tier1b_sources | self._tier2_sources | self._academic_sources
        evict_candidates = [
            (sid, sr) for sid, sr in self._sources.items()
            if sid not in known
        ]
        if not evict_candidates:
            return
        # Evict the one with fewest total_items_seen
        victim = min(evict_candidates, key=lambda x: x[1].total_items_seen)
        del self._sources[victim[0]]

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
        return min(1.0, item.composite_score() * self._w_composite + trust * self._w_trust + corroboration_bonus)

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

    Optimization: candidates are pre-grouped by topic so only items within
    the same topic are compared, reducing O(n²) to O(sum(k²)) where k
    is the per-topic count — typically a 3-5x reduction.
    """
    # Pre-extract word sets and group by topic for efficient comparison
    word_map: dict[str, set[str]] = {}
    topic_groups: dict[str, list[CandidateItem]] = defaultdict(list)
    for c in candidates:
        # Simulated placeholders have synthetic text — skip them to avoid
        # false corroboration between "Simulated placeholder — reddit agent..."
        # and "Simulated placeholder — reuters agent..." (Jaccard ~0.75).
        if "example.com" in (c.url or ""):
            word_map[c.candidate_id] = set()
        else:
            word_map[c.candidate_id] = _extract_significant_words(f"{c.title} {c.summary}")
        topic_groups[c.topic].append(c)

    # Compare within each topic group — cross-topic items can't corroborate
    for group in topic_groups.values():
        for i, ci in enumerate(group):
            wi = word_map[ci.candidate_id]
            if not wi:
                continue
            corr_sources: set[str] = set()
            for j in range(i + 1, len(group)):
                cj = group[j]
                if ci.source == cj.source:
                    continue
                wj = word_map[cj.candidate_id]
                if not wj:
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
