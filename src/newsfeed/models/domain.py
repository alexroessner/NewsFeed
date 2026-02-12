from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class StoryLifecycle(Enum):
    DEVELOPING = "developing"
    BREAKING = "breaking"
    ONGOING = "ongoing"
    WANING = "waning"
    RESOLVED = "resolved"


class UrgencyLevel(Enum):
    ROUTINE = "routine"
    ELEVATED = "elevated"
    BREAKING = "breaking"
    CRITICAL = "critical"


class BriefingType(Enum):
    MORNING_DIGEST = "morning_digest"
    BREAKING_ALERT = "breaking_alert"
    EVENING_SUMMARY = "evening_summary"
    DEEP_DIVE = "deep_dive"


@dataclass(slots=True)
class ConfidenceBand:
    low: float
    mid: float
    high: float
    key_assumptions: list[str] = field(default_factory=list)

    def label(self) -> str:
        if self.mid >= 0.8:
            return "high confidence"
        if self.mid >= 0.55:
            return "moderate confidence"
        return "low confidence"


@dataclass(slots=True)
class SourceReliability:
    source_id: str
    reliability_score: float = 0.7
    bias_rating: str = "unrated"
    historical_accuracy: float = 0.7
    corroboration_rate: float = 0.5
    total_items_seen: int = 0

    def trust_factor(self) -> float:
        return 0.5 * self.reliability_score + 0.3 * self.historical_accuracy + 0.2 * self.corroboration_rate


@dataclass(slots=True)
class UserProfile:
    user_id: str
    topic_weights: dict[str, float] = field(default_factory=dict)
    source_weights: dict[str, float] = field(default_factory=dict)
    tone: str = "concise"
    format: str = "bullet"
    max_items: int = 10
    briefing_cadence: str = "on_demand"
    regions_of_interest: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResearchTask:
    request_id: str
    user_id: str
    prompt: str
    weighted_topics: dict[str, float]


@dataclass(slots=True)
class CandidateItem:
    candidate_id: str
    title: str
    source: str
    summary: str
    url: str
    topic: str
    evidence_score: float
    novelty_score: float
    preference_fit: float
    prediction_signal: float
    discovered_by: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    lifecycle: StoryLifecycle = StoryLifecycle.DEVELOPING
    urgency: UrgencyLevel = UrgencyLevel.ROUTINE
    regions: list[str] = field(default_factory=list)
    corroborated_by: list[str] = field(default_factory=list)
    contrarian_signal: str = ""

    def composite_score(self) -> float:
        return (
            0.30 * self.evidence_score
            + 0.25 * self.novelty_score
            + 0.30 * self.preference_fit
            + 0.15 * self.prediction_signal
        )


@dataclass(slots=True)
class DebateVote:
    expert_id: str
    candidate_id: str
    keep: bool
    confidence: float
    rationale: str
    risk_note: str


@dataclass(slots=True)
class DebateRecord:
    votes: list[DebateVote] = field(default_factory=list)


@dataclass(slots=True)
class NarrativeThread:
    thread_id: str
    headline: str
    candidates: list[CandidateItem]
    lifecycle: StoryLifecycle = StoryLifecycle.DEVELOPING
    urgency: UrgencyLevel = UrgencyLevel.ROUTINE
    source_count: int = 0
    confidence: ConfidenceBand | None = None

    def thread_score(self) -> float:
        if not self.candidates:
            return 0.0
        avg = sum(c.composite_score() for c in self.candidates) / len(self.candidates)
        source_bonus = min(0.15, 0.05 * self.source_count)
        urgency_bonus = {
            UrgencyLevel.ROUTINE: 0.0,
            UrgencyLevel.ELEVATED: 0.05,
            UrgencyLevel.BREAKING: 0.15,
            UrgencyLevel.CRITICAL: 0.25,
        }.get(self.urgency, 0.0)
        return min(1.0, avg + source_bonus + urgency_bonus)


@dataclass(slots=True)
class GeoRiskEntry:
    region: str
    risk_level: float
    previous_level: float = 0.0
    escalation_delta: float = 0.0
    drivers: list[str] = field(default_factory=list)

    def is_escalating(self) -> bool:
        return self.escalation_delta > 0.05


@dataclass(slots=True)
class TrendSnapshot:
    topic: str
    velocity: float
    baseline_velocity: float
    anomaly_score: float
    is_emerging: bool = False
    sample_window_minutes: int = 60


@dataclass(slots=True)
class ReportItem:
    candidate: CandidateItem
    why_it_matters: str
    what_changed: str
    predictive_outlook: str
    adjacent_reads: list[str]
    confidence: ConfidenceBand | None = None
    thread_id: str | None = None
    contrarian_note: str = ""


@dataclass(slots=True)
class DeliveryPayload:
    user_id: str
    generated_at: datetime
    items: list[ReportItem]
    metadata: dict[str, Any] = field(default_factory=dict)
    briefing_type: BriefingType = BriefingType.MORNING_DIGEST
    threads: list[NarrativeThread] = field(default_factory=list)
    geo_risks: list[GeoRiskEntry] = field(default_factory=list)
    trends: list[TrendSnapshot] = field(default_factory=list)
