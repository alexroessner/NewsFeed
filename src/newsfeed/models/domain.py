from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class UserProfile:
    user_id: str
    topic_weights: dict[str, float] = field(default_factory=dict)
    source_weights: dict[str, float] = field(default_factory=dict)
    tone: str = "concise"
    format: str = "bullet"
    max_items: int = 10


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

    def composite_score(self) -> float:
        return (
            0.30 * self.evidence_score
            + 0.25 * self.novelty_score
            + 0.30 * self.preference_fit
            + 0.15 * self.prediction_signal
        )


@dataclass(slots=True)
class ReportItem:
    candidate: CandidateItem
    why_it_matters: str
    what_changed: str
    predictive_outlook: str
    adjacent_reads: list[str]


@dataclass(slots=True)
class DeliveryPayload:
    user_id: str
    generated_at: datetime
    items: list[ReportItem]
    metadata: dict[str, Any] = field(default_factory=dict)
