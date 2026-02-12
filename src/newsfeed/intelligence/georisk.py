from __future__ import annotations

from collections import defaultdict
from typing import Any

from newsfeed.models.domain import CandidateItem, GeoRiskEntry, UrgencyLevel

_DEFAULT_REGIONS: dict[str, list[str]] = {
    "east_asia": ["china", "taiwan", "japan", "korea", "beijing", "tokyo", "seoul", "pyongyang"],
    "south_asia": ["india", "pakistan", "bangladesh", "sri_lanka", "delhi", "islamabad"],
    "middle_east": ["iran", "israel", "saudi", "yemen", "syria", "iraq", "gaza", "lebanon", "tehran"],
    "europe": ["eu", "nato", "ukraine", "russia", "germany", "france", "uk", "brussels", "moscow", "kyiv"],
    "africa": ["nigeria", "ethiopia", "kenya", "south_africa", "sahel", "sudan", "congo"],
    "americas": ["us", "usa", "brazil", "mexico", "canada", "washington", "congress", "fed"],
    "southeast_asia": ["asean", "philippines", "vietnam", "indonesia", "myanmar", "thailand"],
    "central_asia": ["kazakhstan", "uzbekistan", "turkmenistan", "afghanistan", "taliban"],
    "arctic": ["arctic", "greenland", "svalbard", "northern_passage"],
}

_DEFAULT_ESCALATION = frozenset({
    "war", "invasion", "sanctions", "military", "nuclear", "missile",
    "conflict", "coup", "blockade", "mobilization", "escalation",
    "strike", "attack", "troops", "deployment",
})

_DEFAULT_DEESCALATION = frozenset({
    "ceasefire", "peace", "treaty", "negotiations", "diplomacy",
    "withdrawal", "agreement", "talks", "summit", "cooperation",
})


class GeoRiskIndex:
    def __init__(self, georisk_cfg: dict[str, Any] | None = None) -> None:
        cfg = georisk_cfg or {}
        self._history: dict[str, float] = {}
        self._regions: dict[str, list[str]] = cfg.get("regions", _DEFAULT_REGIONS)
        self._escalation_keywords = frozenset(cfg.get("escalation_keywords", [])) or _DEFAULT_ESCALATION
        self._deescalation_keywords = frozenset(cfg.get("deescalation_keywords", [])) or _DEFAULT_DEESCALATION
        self._default_previous = cfg.get("default_previous_risk", 0.3)
        self._max_drivers = cfg.get("max_drivers", 5)

        rw = cfg.get("risk_weights", {})
        self._w_base = rw.get("base", 0.4)
        self._w_esc_per_kw = rw.get("escalation_per_keyword", 0.03)
        self._w_vol_per = rw.get("volume_per_item", 0.02)
        self._w_vol_cap = rw.get("volume_cap", 0.15)

        uf = cfg.get("urgency_risk_factor", {})
        self._uf_critical = uf.get("critical", 0.3)
        self._uf_breaking = uf.get("breaking", 0.2)
        self._uf_elevated = uf.get("elevated", 0.1)

    def assess(self, candidates: list[CandidateItem]) -> list[GeoRiskEntry]:
        region_items: dict[str, list[CandidateItem]] = defaultdict(list)

        for c in candidates:
            detected_regions = self._detect_regions(c)
            c.regions = detected_regions
            for region in detected_regions:
                region_items[region].append(c)

        entries: list[GeoRiskEntry] = []
        for region, items in region_items.items():
            risk_level = self._compute_risk(items)
            previous = self._history.get(region, self._default_previous)
            delta = round(risk_level - previous, 3)
            drivers = self._extract_drivers(items)

            self._history[region] = risk_level

            entries.append(GeoRiskEntry(
                region=region,
                risk_level=round(risk_level, 3),
                previous_level=round(previous, 3),
                escalation_delta=delta,
                drivers=drivers[:self._max_drivers],
            ))

        entries.sort(key=lambda e: e.risk_level, reverse=True)
        return entries

    def _detect_regions(self, item: CandidateItem) -> list[str]:
        text = f"{item.title} {item.summary} {item.topic}".lower()
        regions = []
        for region, keywords in self._regions.items():
            if any(kw in text for kw in keywords):
                regions.append(region)
        return regions or ["global"]

    def _compute_risk(self, items: list[CandidateItem]) -> float:
        if not items:
            return 0.0

        base = sum(c.composite_score() for c in items) / len(items)

        urgency_factor = 0.0
        for c in items:
            if c.urgency == UrgencyLevel.CRITICAL:
                urgency_factor = max(urgency_factor, self._uf_critical)
            elif c.urgency == UrgencyLevel.BREAKING:
                urgency_factor = max(urgency_factor, self._uf_breaking)
            elif c.urgency == UrgencyLevel.ELEVATED:
                urgency_factor = max(urgency_factor, self._uf_elevated)

        escalation = 0.0
        for c in items:
            text = f"{c.title} {c.summary}".lower()
            words = set(text.split())
            esc_hits = len(words & self._escalation_keywords)
            deesc_hits = len(words & self._deescalation_keywords)
            escalation += (esc_hits - deesc_hits) * self._w_esc_per_kw

        volume_factor = min(self._w_vol_cap, len(items) * self._w_vol_per)

        return min(1.0, max(0.0, base * self._w_base + urgency_factor + escalation + volume_factor))

    def _extract_drivers(self, items: list[CandidateItem]) -> list[str]:
        drivers = []
        sources = {c.source for c in items}
        if len(sources) >= 3:
            drivers.append(f"Multi-source coverage ({len(sources)} outlets)")

        for c in sorted(items, key=lambda c: c.composite_score(), reverse=True)[:3]:
            text = f"{c.title} {c.summary}".lower()
            words = set(text.split())
            if words & self._escalation_keywords:
                drivers.append(f"Escalation signal: {c.title[:60]}")
            elif words & self._deescalation_keywords:
                drivers.append(f"De-escalation signal: {c.title[:60]}")
            else:
                drivers.append(f"Activity: {c.title[:60]}")

        return drivers

    def snapshot(self) -> dict[str, float]:
        return dict(self._history)
