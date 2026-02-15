"""Smart narrative generation — replaces boilerplate with metadata-driven text.

Uses the structured data the pipeline already produces (source tier, urgency,
corroboration, regions, lifecycle, scores) to generate specific, meaningful
"why it matters", "what changed", and "predictive outlook" text for each story
WITHOUT requiring an LLM.

This is the #1 impact improvement identified in human review: the gap between
"impressive prototype" and "daily-use tool" is mostly about content quality.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from newsfeed.intelligence.credibility import CredibilityTracker
    from newsfeed.models.domain import CandidateItem, UserProfile


# ── Human-readable topic names ────────────────────────────────────────────

_TOPIC_DISPLAY = {
    "geopolitics": "geopolitics",
    "ai_policy": "AI policy",
    "technology": "technology",
    "markets": "markets",
    "crypto": "crypto",
    "climate": "climate",
    "defense": "defense",
    "regulation": "regulation",
    "energy": "energy",
    "space": "space",
    "health": "health",
    "science": "science",
    "cybersecurity": "cybersecurity",
    "trade": "trade",
    "economics": "economics",
}


def _topic_name(topic: str) -> str:
    """Human-readable topic label."""
    return _TOPIC_DISPLAY.get(topic, topic.replace("_", " "))


_TIER_LABELS = {
    "tier_1": "major wire service",
    "tier_1b": "established international outlet",
    "tier_academic": "academic/research source",
    "tier_2": "community/social source",
}


def _source_tier_label(source: str, credibility: CredibilityTracker) -> str:
    """Describe source quality in human terms."""
    if source in credibility._tier1_sources:
        return _TIER_LABELS["tier_1"]
    if source in credibility._tier1b_sources:
        return _TIER_LABELS["tier_1b"]
    if source in credibility._academic_sources:
        return _TIER_LABELS["tier_academic"]
    if source in credibility._tier2_sources:
        return _TIER_LABELS["tier_2"]
    return "source"


def _urgency_phrase(candidate: CandidateItem) -> str:
    """Convert urgency enum to natural language."""
    from newsfeed.models.domain import UrgencyLevel
    return {
        UrgencyLevel.CRITICAL: "critical development",
        UrgencyLevel.BREAKING: "breaking development",
        UrgencyLevel.ELEVATED: "notable development",
        UrgencyLevel.ROUTINE: "development",
    }.get(candidate.urgency, "development")


def _region_phrase(regions: list[str]) -> str:
    """Format regions for inline text."""
    if not regions:
        return ""
    display = [r.replace("_", " ").title() for r in regions[:3]]
    if len(display) == 1:
        return display[0]
    if len(display) == 2:
        return f"{display[0]} and {display[1]}"
    return f"{display[0]}, {display[1]}, and {display[2]}"


def _corroboration_phrase(candidate: CandidateItem) -> str:
    """Describe corroboration status."""
    count = len(candidate.corroborated_by)
    if count == 0:
        return ""
    sources = candidate.corroborated_by[:3]
    source_str = ", ".join(s.title() for s in sources)
    if count == 1:
        return f"independently confirmed by {source_str}"
    if count <= 3:
        return f"corroborated by {count} sources ({source_str})"
    return f"corroborated by {count} independent sources"


# ── Main generators ───────────────────────────────────────────────────────


def generate_why(
    candidate: CandidateItem,
    credibility: CredibilityTracker,
    profile: UserProfile | None = None,
) -> str:
    """Generate a specific 'why it matters' sentence using structured metadata.

    Combines: source quality + topic alignment + corroboration + urgency + regions.
    """
    parts: list[str] = []
    topic = _topic_name(candidate.topic)
    source_label = _source_tier_label(candidate.source, credibility)
    urgency = _urgency_phrase(candidate)
    source_name = candidate.source.title()

    # Opening: source + urgency + topic
    from newsfeed.models.domain import UrgencyLevel
    if candidate.urgency in (UrgencyLevel.CRITICAL, UrgencyLevel.BREAKING):
        parts.append(f"{urgency.title()} in {topic} from {source_name} ({source_label})")
    else:
        parts.append(f"This {source_name} report covers a {urgency} in {topic}")

    # Corroboration
    corr = _corroboration_phrase(candidate)
    if corr:
        parts[-1] += f", {corr}"

    # Regions
    region_text = _region_phrase(candidate.regions)
    if region_text:
        parts.append(f"Affects {region_text}")

    # User alignment — only if profile has explicit weight for this topic
    if profile and profile.topic_weights:
        weight = profile.topic_weights.get(candidate.topic, 0.0)
        if weight >= 0.7:
            parts.append("Matches your high-priority interest")
        elif weight >= 0.4:
            parts.append("Aligns with your tracked interests")

    # Evidence quality signal
    sr = credibility.get_source(candidate.source)
    if sr.reliability_score >= 0.8 and candidate.evidence_score >= 0.7:
        parts.append("High-reliability source with strong evidence")
    elif sr.reliability_score < 0.6:
        parts.append("Lower-reliability source — verify independently")

    return ". ".join(parts) + "."


def generate_what_changed(
    candidate: CandidateItem,
    credibility: CredibilityTracker,
) -> str:
    """Generate a specific 'what changed' sentence using lifecycle + corroboration."""
    from newsfeed.models.domain import StoryLifecycle, UrgencyLevel

    parts: list[str] = []

    # Lifecycle-driven opener
    lifecycle_phrases = {
        StoryLifecycle.BREAKING: "New breaking report",
        StoryLifecycle.DEVELOPING: "Developing story with fresh updates",
        StoryLifecycle.ONGOING: "Ongoing situation with new details",
        StoryLifecycle.WANING: "Story activity declining but still relevant",
        StoryLifecycle.RESOLVED: "Situation appears to be resolving",
    }
    parts.append(lifecycle_phrases.get(candidate.lifecycle, "New report"))

    # Corroboration change
    corr_count = len(candidate.corroborated_by)
    if corr_count >= 3:
        parts.append(f"now confirmed across {corr_count} independent sources")
    elif corr_count == 2:
        parts.append("cross-source confirmation strengthening")
    elif corr_count == 1:
        src = candidate.corroborated_by[0].title()
        parts.append(f"secondary reporting from {src}")
    else:
        parts.append("single-source report, awaiting confirmation")

    # Urgency escalation signal
    if candidate.urgency in (UrgencyLevel.BREAKING, UrgencyLevel.CRITICAL):
        parts.append("urgency elevated above baseline")

    # Novelty signal
    if candidate.novelty_score >= 0.8:
        parts.append("high novelty — first appearance in monitoring window")
    elif candidate.novelty_score >= 0.6:
        parts.append("notable new angles emerging")

    return ". ".join(parts) + "."


def generate_outlook(
    candidate: CandidateItem,
    credibility: CredibilityTracker,
) -> str:
    """Generate a specific 'predictive outlook' sentence using prediction signals."""
    from newsfeed.models.domain import UrgencyLevel

    parts: list[str] = []

    # Prediction signal interpretation
    ps = candidate.prediction_signal
    if ps >= 0.7:
        parts.append("Strong forward-looking signals suggest significant near-term developments")
    elif ps >= 0.4:
        parts.append("Moderate predictive signals — situation likely to evolve")
    else:
        parts.append("Limited forward indicators at this time")

    # Urgency trajectory
    if candidate.urgency == UrgencyLevel.CRITICAL:
        parts.append("monitor for rapid escalation")
    elif candidate.urgency == UrgencyLevel.BREAKING:
        parts.append("watch for follow-on developments within hours")
    elif candidate.urgency == UrgencyLevel.ELEVATED:
        parts.append("elevated watch priority for coming days")

    # Evidence strength as confidence qualifier
    if candidate.evidence_score >= 0.8:
        parts.append("assessment backed by strong evidence base")
    elif candidate.evidence_score < 0.4:
        parts.append("limited evidence — outlook may shift rapidly")

    # Market/narrative signal for relevant topics
    market_topics = {"markets", "crypto", "economics", "trade", "energy"}
    if candidate.topic in market_topics and ps >= 0.5:
        parts.append("potential market-moving implications")

    # Corroboration as conviction signal
    corr = len(candidate.corroborated_by)
    if corr >= 3:
        parts.append("high multi-source conviction")

    return ". ".join(parts) + "."


def generate_adjacent_reads(
    candidate: CandidateItem,
    threads: list,
    reserve_candidates: list[CandidateItem] | None = None,
    limit: int = 3,
) -> list[str]:
    """Generate real adjacent read titles from thread siblings and reserve cache.

    Instead of placeholder text like "Context read 1 for geopolitics", returns
    actual story titles from:
    1. Other candidates in the same narrative thread (same-story, different source)
    2. Reserve candidates in the same topic cluster
    """
    reads: list[str] = []
    seen_ids: set[str] = {candidate.candidate_id}

    # Source 1: thread siblings (same narrative, different sources)
    for thread in threads:
        for sibling in thread.candidates:
            if sibling.candidate_id in seen_ids:
                continue
            if sibling.source == candidate.source:
                continue
            # Build a readable recommendation
            title = sibling.title
            if len(title) > 100:
                cut = title[:100].rfind(" ")
                title = title[:cut] + "..." if cut > 40 else title[:97] + "..."
            reads.append(f"{title} [{sibling.source}]")
            seen_ids.add(sibling.candidate_id)
            if len(reads) >= limit:
                return reads

    # Source 2: reserve candidates in the same topic
    if reserve_candidates:
        topic_matches = [
            c for c in reserve_candidates
            if c.topic == candidate.topic and c.candidate_id not in seen_ids
        ]
        # Sort by composite score — show the best reserves
        topic_matches.sort(key=lambda c: c.composite_score(), reverse=True)
        for c in topic_matches:
            title = c.title
            if len(title) > 100:
                cut = title[:100].rfind(" ")
                title = title[:cut] + "..." if cut > 40 else title[:97] + "..."
            reads.append(f"{title} [{c.source}]")
            seen_ids.add(c.candidate_id)
            if len(reads) >= limit:
                return reads

    return reads
