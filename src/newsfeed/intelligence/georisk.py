from __future__ import annotations

import hashlib
from collections import defaultdict

from newsfeed.models.domain import CandidateItem, GeoRiskEntry, UrgencyLevel

_REGION_KEYWORDS: dict[str, list[str]] = {
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

_ESCALATION_KEYWORDS = frozenset({
    "war", "invasion", "sanctions", "military", "nuclear", "missile",
    "conflict", "coup", "blockade", "mobilization", "escalation",
    "strike", "attack", "troops", "deployment",
})

_DEESCALATION_KEYWORDS = frozenset({
    "ceasefire", "peace", "treaty", "negotiations", "diplomacy",
    "withdrawal", "agreement", "talks", "summit", "cooperation",
})


class GeoRiskIndex:
    def __init__(self) -> None:
        self._history: dict[str, float] = {}

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
            previous = self._history.get(region, 0.3)
            delta = round(risk_level - previous, 3)
            drivers = self._extract_drivers(items)

            self._history[region] = risk_level

            entries.append(GeoRiskEntry(
                region=region,
                risk_level=round(risk_level, 3),
                previous_level=round(previous, 3),
                escalation_delta=delta,
                drivers=drivers[:5],
            ))

        entries.sort(key=lambda e: e.risk_level, reverse=True)
        return entries

    def _detect_regions(self, item: CandidateItem) -> list[str]:
        text = f"{item.title} {item.summary} {item.topic}".lower()
        regions = []
        for region, keywords in _REGION_KEYWORDS.items():
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
                urgency_factor = max(urgency_factor, 0.3)
            elif c.urgency == UrgencyLevel.BREAKING:
                urgency_factor = max(urgency_factor, 0.2)
            elif c.urgency == UrgencyLevel.ELEVATED:
                urgency_factor = max(urgency_factor, 0.1)

        escalation = 0.0
        for c in items:
            text = f"{c.title} {c.summary}".lower()
            words = set(text.split())
            esc_hits = len(words & _ESCALATION_KEYWORDS)
            deesc_hits = len(words & _DEESCALATION_KEYWORDS)
            escalation += (esc_hits - deesc_hits) * 0.03

        volume_factor = min(0.15, len(items) * 0.02)

        return min(1.0, max(0.0, base * 0.4 + urgency_factor + escalation + volume_factor))

    def _extract_drivers(self, items: list[CandidateItem]) -> list[str]:
        drivers = []
        sources = {c.source for c in items}
        if len(sources) >= 3:
            drivers.append(f"Multi-source coverage ({len(sources)} outlets)")

        for c in sorted(items, key=lambda c: c.composite_score(), reverse=True)[:3]:
            text = f"{c.title} {c.summary}".lower()
            words = set(text.split())
            if words & _ESCALATION_KEYWORDS:
                drivers.append(f"Escalation signal: {c.title[:60]}")
            elif words & _DEESCALATION_KEYWORDS:
                drivers.append(f"De-escalation signal: {c.title[:60]}")
            else:
                drivers.append(f"Activity: {c.title[:60]}")

        return drivers
