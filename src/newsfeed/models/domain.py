from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

log = logging.getLogger(__name__)

# Strip Unicode control characters that could confuse display:
# - Bidirectional overrides (U+202A-202E, U+2066-2069) reverse text rendering
# - Zero-width chars (U+200B-200F, U+FEFF) can hide content
# - Other C0/C1 controls (except tab, newline, carriage return)
_CONTROL_CHAR_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]"
)


def sanitize_text(text: str) -> str:
    """Normalize Unicode and strip dangerous control characters."""
    text = unicodedata.normalize("NFC", text)
    return _CONTROL_CHAR_RE.sub("", text)

# Module-level scoring config for convenience; engines can also pass config explicitly.
_SCORING_CFG: dict[str, Any] = {}


def configure_scoring(cfg: dict[str, Any]) -> None:
    _SCORING_CFG.clear()
    _SCORING_CFG.update(cfg)


def _get_scoring() -> dict[str, Any]:
    return _SCORING_CFG


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
        labels = _get_scoring().get("confidence_labels", {})
        if self.mid >= labels.get("high_threshold", 0.80):
            return "high confidence"
        if self.mid >= labels.get("moderate_threshold", 0.55):
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
        weights = _get_scoring().get("trust_factor_weights", {})
        w_rel = weights.get("reliability", 0.50)
        w_acc = weights.get("historical_accuracy", 0.30)
        w_cor = weights.get("corroboration", 0.20)
        return w_rel * self.reliability_score + w_acc * self.historical_accuracy + w_cor * self.corroboration_rate


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
    watchlist_crypto: list[str] = field(default_factory=list)
    watchlist_stocks: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    muted_topics: list[str] = field(default_factory=list)
    # Tracked stories: user follows developing narratives across briefings
    # Each entry: {"topic": str, "keywords": [str], "headline": str, "tracked_at": float}
    tracked_stories: list[dict[str, Any]] = field(default_factory=list)
    # Bookmarked stories: saved for later reading
    # Each entry: {"title": str, "source": str, "url": str, "topic": str, "saved_at": float}
    bookmarks: list[dict[str, Any]] = field(default_factory=list)
    # Email address for email digest delivery
    email: str = ""
    # Advanced briefing filters — user-controllable thresholds
    # confidence_min: only show stories with confidence.mid >= this (0.0 = off)
    confidence_min: float = 0.0
    # urgency_min: only show stories at or above this urgency level
    # "routine", "elevated", "breaking", "critical" (empty = off)
    urgency_min: str = ""
    # max_per_source: limit stories from a single source (0 = no limit)
    max_per_source: int = 0
    # Alert sensitivity thresholds — user-configurable
    # geo-risk threshold: alert when risk_level exceeds this (default 0.5)
    alert_georisk_threshold: float = 0.5
    # trend spike threshold: alert when anomaly_score exceeds this (default 3.0)
    alert_trend_threshold: float = 3.0
    # Saved briefing presets — named configurations users can switch between
    # Each entry: {"name": str, "topic_weights": dict, "source_weights": dict,
    #   "tone": str, "format": str, "max_items": int, "regions": list,
    #   "confidence_min": float, "urgency_min": str, "max_per_source": int,
    #   "muted_topics": list}
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Outbound webhook URL for pushing briefings/alerts as structured JSON
    webhook_url: str = ""
    # User-added custom RSS sources — dynamically injected into research pipeline
    # Each entry: {"name": str, "feed_url": str, "site_url": str,
    #   "feed_title": str, "topics": [str], "added_at": float, "items_seen": int}
    custom_sources: list[dict[str, Any]] = field(default_factory=list)
    # Keyword alerts — stories matching these keywords get priority-boosted
    # and flagged in briefings (cross-topic, case-insensitive)
    alert_keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ResearchTask:
    request_id: str
    user_id: str
    prompt: str
    weighted_topics: dict[str, float]


_SAFE_URL_SCHEMES = frozenset({"http", "https", "ftp", ""})


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

    def __post_init__(self) -> None:
        # Normalize Unicode and strip control characters from text fields
        self.title = sanitize_text(self.title)
        self.summary = sanitize_text(self.summary)
        # Clamp scores to [0, 1] — agents may produce slight overshoots
        self.evidence_score = max(0.0, min(1.0, self.evidence_score))
        self.novelty_score = max(0.0, min(1.0, self.novelty_score))
        self.preference_fit = max(0.0, min(1.0, self.preference_fit))
        self.prediction_signal = max(0.0, min(1.0, self.prediction_signal))
        # Enforce max lengths to prevent memory abuse from corrupted feeds
        if len(self.title) > 500:
            self.title = self.title[:500]
        if len(self.summary) > 2000:
            self.summary = self.summary[:2000]
        # Reject dangerous URL schemes at the data layer
        scheme = self.url.split(":", 1)[0].lower().strip() if ":" in self.url else ""
        if scheme not in _SAFE_URL_SCHEMES:
            log.warning("Rejected unsafe URL scheme %r in candidate %s", scheme, self.candidate_id)
            self.url = ""

    def composite_score(self) -> float:
        weights = _get_scoring().get("composite_weights", {})
        w_ev = weights.get("evidence", 0.30)
        w_no = weights.get("novelty", 0.25)
        w_pf = weights.get("preference_fit", 0.30)
        w_ps = weights.get("prediction_signal", 0.15)
        return (
            w_ev * self.evidence_score
            + w_no * self.novelty_score
            + w_pf * self.preference_fit
            + w_ps * self.prediction_signal
        )


def validate_candidate(c: CandidateItem) -> list[str]:
    """Validate candidate data integrity. Returns list of issues found."""
    issues: list[str] = []
    for fname, val in [
        ("evidence_score", c.evidence_score),
        ("novelty_score", c.novelty_score),
        ("preference_fit", c.preference_fit),
        ("prediction_signal", c.prediction_signal),
    ]:
        if not (0.0 <= val <= 1.0):
            issues.append(f"{fname}={val} outside [0, 1]")
    if not c.title.strip():
        issues.append("empty title")
    if not c.source.strip():
        issues.append("empty source")
    if not c.topic.strip():
        issues.append("empty topic")
    return issues


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

        ts_cfg = _get_scoring().get("thread_scoring", {})
        bonus_per = ts_cfg.get("source_bonus_per", 0.05)
        bonus_cap = ts_cfg.get("source_bonus_cap", 0.15)
        source_bonus = min(bonus_cap, bonus_per * self.source_count)

        urgency_map = ts_cfg.get("urgency_bonus", {})
        urgency_bonus = urgency_map.get(self.urgency.value, 0.0)

        return min(1.0, avg + source_bonus + urgency_bonus)


@dataclass(slots=True)
class GeoRiskEntry:
    region: str
    risk_level: float
    previous_level: float = 0.0
    escalation_delta: float = 0.0
    drivers: list[str] = field(default_factory=list)

    def is_escalating(self) -> bool:
        threshold = _get_scoring().get("_georisk_escalation_threshold", 0.05)
        return self.escalation_delta > threshold


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
