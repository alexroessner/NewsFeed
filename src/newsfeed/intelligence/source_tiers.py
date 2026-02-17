"""Unified source tier definitions — single source of truth.

All source reliability tiers, bias profiles, and priority scores are defined
here and loaded from config/pipelines.json at startup. No other module should
hardcode source tier data.

Usage:
    tiers = SourceTiers(intel_cfg)
    base = tiers.base_reliability("reuters")  # 0.85
    priority = tiers.priority("reuters")       # 0.95
    tier = tiers.tier_name("reuters")          # "tier_1"
"""
from __future__ import annotations

from typing import Any


# Fallback values if config is missing (bootstrapping/testing only)
_DEFAULT_TIERS = {
    "tier_1": {"sources": ["reuters", "ap", "bbc", "guardian", "ft"], "base_reliability": 0.85},
    "tier_1b": {"sources": ["aljazeera"], "base_reliability": 0.78},
    "tier_academic": {"sources": ["arxiv"], "base_reliability": 0.72},
    "tier_2": {"sources": ["x", "reddit", "web", "hackernews", "gdelt"], "base_reliability": 0.55},
}

_DEFAULT_BIAS = {
    "reuters": "center", "ap": "center", "bbc": "center-left",
    "guardian": "left-leaning", "ft": "center-right",
    "aljazeera": "center", "x": "variable", "reddit": "community-driven",
    "web": "unverified", "arxiv": "academic", "hackernews": "tech-community",
    "gdelt": "event-based",
}

_DEFAULT_PRIORITY = {
    "reuters": 0.95, "ap": 0.93, "bbc": 0.90, "guardian": 0.88, "ft": 0.90,
    "aljazeera": 0.80, "arxiv": 0.78, "hackernews": 0.65, "reddit": 0.58,
    "x": 0.55, "gdelt": 0.60, "web": 0.50,
}


class SourceTiers:
    """Unified source tier registry loaded from pipeline config."""

    def __init__(self, intel_cfg: dict[str, Any] | None = None) -> None:
        cfg = intel_cfg or {}
        tiers = cfg.get("source_tiers", _DEFAULT_TIERS)

        # Build lookup tables
        self._source_to_tier: dict[str, str] = {}
        self._tier_base: dict[str, float] = {}
        self._tier_sources: dict[str, frozenset[str]] = {}

        for tier_name, tier_data in tiers.items():
            if not isinstance(tier_data, dict):
                continue
            sources = tier_data.get("sources", [])
            base = tier_data.get("base_reliability", 0.50)
            self._tier_base[tier_name] = base
            self._tier_sources[tier_name] = frozenset(sources)
            for src in sources:
                self._source_to_tier[src] = tier_name

        self._unknown_base = tiers.get("unknown_base_reliability", 0.50) if isinstance(tiers, dict) else 0.50
        self._bias_profiles = cfg.get("bias_profiles", _DEFAULT_BIAS)
        self._priority = cfg.get("source_priority", _DEFAULT_PRIORITY)

    def tier_name(self, source_id: str) -> str:
        """Return the tier name for a source, or 'unknown'."""
        return self._source_to_tier.get(source_id, "unknown")

    def base_reliability(self, source_id: str) -> float:
        """Return the base reliability score for a source."""
        tier = self._source_to_tier.get(source_id)
        if tier:
            return self._tier_base.get(tier, self._unknown_base)
        return self._unknown_base

    def priority(self, source_id: str) -> float:
        """Return the priority score for a source (used by orchestrator)."""
        return self._priority.get(source_id, 0.50)

    def bias(self, source_id: str) -> str:
        """Return the bias rating for a source."""
        return self._bias_profiles.get(source_id, "unrated")

    def sources_in_tier(self, tier_name: str) -> frozenset[str]:
        """Return all sources in a given tier."""
        return self._tier_sources.get(tier_name, frozenset())

    def all_tiers(self) -> dict[str, list[str]]:
        """Return all tier names and their sources."""
        return {name: sorted(sources) for name, sources in self._tier_sources.items()}

    def is_known(self, source_id: str) -> bool:
        """Check if a source is in any known tier."""
        return source_id in self._source_to_tier

    def all_known_sources(self) -> frozenset[str]:
        """Return set of all known source IDs across all tiers."""
        return frozenset(self._source_to_tier.keys())
