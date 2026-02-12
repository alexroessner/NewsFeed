"""Orchestrator agent — request lifecycle management, brief compilation, and capability routing.

The orchestrator is the central planner and message router (Layer 1 in the vision).
It translates user intent + profile memory into machine-readable research briefs,
assigns tasks to specialist agents based on capabilities, and maintains lifecycle
state for each request.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from newsfeed.models.domain import CandidateItem, ResearchTask, UserProfile

log = logging.getLogger(__name__)


class RequestStage(Enum):
    """Lifecycle stages for a request — observable state machine."""
    QUEUED = "queued"
    COMPILING_BRIEF = "compiling_brief"
    RESEARCHING = "researching"
    ENRICHING = "enriching"
    EXPERT_REVIEW = "expert_review"
    EDITORIAL_REVIEW = "editorial_review"
    FORMATTING = "formatting"
    DELIVERING = "delivering"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(slots=True)
class RequestLifecycle:
    """Tracks a single request through its lifecycle stages."""
    request_id: str
    user_id: str
    stage: RequestStage = RequestStage.QUEUED
    created_at: float = field(default_factory=time.monotonic)
    stage_times: dict[str, float] = field(default_factory=dict)
    stage_entered_at: float = field(default_factory=time.monotonic)
    candidate_count: int = 0
    selected_count: int = 0
    error: str = ""

    def advance(self, new_stage: RequestStage) -> None:
        """Move to the next lifecycle stage, recording timing."""
        now = time.monotonic()
        self.stage_times[self.stage.value] = round(now - self.stage_entered_at, 4)
        self.stage = new_stage
        self.stage_entered_at = now

    def fail(self, error: str) -> None:
        self.error = error
        self.advance(RequestStage.FAILED)

    def total_elapsed(self) -> float:
        return round(time.monotonic() - self.created_at, 4)

    def snapshot(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "stage": self.stage.value,
            "elapsed_s": self.total_elapsed(),
            "stage_times": dict(self.stage_times),
            "candidates": self.candidate_count,
            "selected": self.selected_count,
            "error": self.error,
        }


# ──────────────────────────────────────────────────────────────────────
# Agent capability routing
# ──────────────────────────────────────────────────────────────────────

# Maps topics to the agent sources most capable of covering them
_TOPIC_CAPABILITIES: dict[str, list[str]] = {
    "geopolitics": ["reuters", "ap", "bbc", "guardian", "ft", "aljazeera", "gdelt", "x", "reddit", "web"],
    "ai_policy": ["arxiv", "hackernews", "x", "reddit", "guardian", "web", "reuters", "bbc"],
    "technology": ["hackernews", "arxiv", "x", "reddit", "web", "guardian", "bbc"],
    "markets": ["ft", "reuters", "x", "web", "reddit", "hackernews", "bbc"],
    "crypto": ["x", "reddit", "web", "hackernews", "ft"],
    "climate": ["guardian", "bbc", "reuters", "ap", "web", "reddit", "arxiv"],
    "science": ["arxiv", "hackernews", "guardian", "bbc", "reddit", "web"],
    "middle_east": ["aljazeera", "bbc", "reuters", "ap", "guardian", "gdelt", "x"],
    "africa": ["aljazeera", "bbc", "reuters", "gdelt", "guardian", "web"],
}

# Source reliability for brief weighting
_SOURCE_PRIORITY: dict[str, float] = {
    "reuters": 0.95, "ap": 0.93, "bbc": 0.90, "guardian": 0.88, "ft": 0.90,
    "aljazeera": 0.80, "arxiv": 0.78, "hackernews": 0.65, "reddit": 0.58,
    "x": 0.55, "gdelt": 0.60, "web": 0.50,
}


class OrchestratorAgent:
    """Central planner and message router for the intelligence pipeline.

    Responsibilities:
    1. Compile weighted research briefs from user profile + prompt + session constraints
    2. Route tasks to research agents based on topic-capability matching
    3. Maintain lifecycle state for each request
    4. Track request metrics for system optimization
    """

    agent_id = "orchestrator_agent"

    def __init__(self, agent_configs: list[dict], pipeline_cfg: dict) -> None:
        self._agent_configs = agent_configs
        self._limits = pipeline_cfg.get("limits", {})
        self._default_max_items = self._limits.get("default_max_items", 10)
        self._top_k = self._limits.get("top_discoveries_per_research_agent", 5)

        # Active lifecycle tracking (most recent per user)
        self._active_requests: dict[str, RequestLifecycle] = {}
        # Completed request history for metrics
        self._completed: list[dict] = []
        self._max_history = 100

    def compile_brief(self, user_id: str, prompt: str, profile: UserProfile,
                      max_items: int | None = None) -> tuple[ResearchTask, RequestLifecycle]:
        """Compile a weighted research brief from user intent and profile.

        Returns a ResearchTask and a RequestLifecycle tracker.
        """
        request_id = f"req-{int(datetime.now(timezone.utc).timestamp())}-{user_id[:8]}"
        lifecycle = RequestLifecycle(request_id=request_id, user_id=user_id)
        lifecycle.advance(RequestStage.COMPILING_BRIEF)

        # Build weighted topics from profile + prompt analysis
        weighted_topics = dict(profile.topic_weights) if profile.topic_weights else {}

        # Ensure at least default topics if user has no weights
        if not weighted_topics:
            weighted_topics = {
                "geopolitics": 0.8,
                "ai_policy": 0.7,
                "technology": 0.6,
                "markets": 0.5,
            }

        # Boost topics mentioned in the prompt
        prompt_lower = prompt.lower()
        for topic in list(weighted_topics.keys()) + list(_TOPIC_CAPABILITIES.keys()):
            keywords = topic.lower().replace("_", " ").split()
            if any(kw in prompt_lower for kw in keywords):
                weighted_topics[topic] = min(1.0, weighted_topics.get(topic, 0.3) + 0.3)

        # Apply region-based topic boosts
        for region in profile.regions_of_interest:
            if region in _TOPIC_CAPABILITIES:
                for source in _TOPIC_CAPABILITIES[region][:3]:
                    # Boost topics that this region is relevant to
                    weighted_topics[region] = min(1.0, weighted_topics.get(region, 0.3) + 0.2)

        task = ResearchTask(
            request_id=request_id,
            user_id=user_id,
            prompt=prompt,
            weighted_topics=weighted_topics,
        )

        self._active_requests[user_id] = lifecycle
        log.info(
            "Brief compiled: request=%s topics=%s max_items=%s",
            request_id,
            {k: round(v, 2) for k, v in sorted(weighted_topics.items(), key=lambda x: x[1], reverse=True)[:5]},
            max_items or self._default_max_items,
        )

        return task, lifecycle

    def select_agents(self, task: ResearchTask) -> list[dict]:
        """Select and prioritize research agents based on task topic capabilities.

        Returns agent configs ordered by relevance to the task's weighted topics.
        This allows the engine to optionally limit the agent set for focused requests.
        """
        # Score each agent by topic-capability match
        agent_scores: list[tuple[dict, float]] = []
        top_topics = sorted(task.weighted_topics.items(), key=lambda x: x[1], reverse=True)[:5]

        for agent_cfg in self._agent_configs:
            source = agent_cfg.get("source", "")
            score = 0.0

            for topic, weight in top_topics:
                capable_sources = _TOPIC_CAPABILITIES.get(topic, [])
                if source in capable_sources:
                    # Position in capability list matters
                    position_bonus = 1.0 - (capable_sources.index(source) / max(len(capable_sources), 1)) * 0.3
                    score += weight * position_bonus

            # Add base source priority
            score += _SOURCE_PRIORITY.get(source, 0.50) * 0.1

            agent_scores.append((agent_cfg, score))

        # Sort by relevance score
        agent_scores.sort(key=lambda x: x[1], reverse=True)
        selected = [cfg for cfg, _ in agent_scores]

        log.info(
            "Agent routing: %d agents selected, top-3: %s",
            len(selected),
            [f"{a['id']}({s:.2f})" for a, s in agent_scores[:3]],
        )

        return selected

    def record_research_results(self, lifecycle: RequestLifecycle, candidate_count: int) -> None:
        """Record research phase results."""
        lifecycle.candidate_count = candidate_count
        lifecycle.advance(RequestStage.ENRICHING)

    def record_selection(self, lifecycle: RequestLifecycle, selected_count: int) -> None:
        """Record expert selection results."""
        lifecycle.selected_count = selected_count
        lifecycle.advance(RequestStage.EDITORIAL_REVIEW)

    def record_completion(self, lifecycle: RequestLifecycle) -> None:
        """Record request completion and archive metrics."""
        lifecycle.advance(RequestStage.COMPLETE)
        snapshot = lifecycle.snapshot()
        self._completed.append(snapshot)
        if len(self._completed) > self._max_history:
            self._completed = self._completed[-self._max_history:]
        log.info(
            "Request %s complete: %d candidates → %d selected in %.2fs",
            lifecycle.request_id, lifecycle.candidate_count,
            lifecycle.selected_count, lifecycle.total_elapsed(),
        )

    def get_lifecycle(self, user_id: str) -> RequestLifecycle | None:
        """Get the active lifecycle for a user."""
        return self._active_requests.get(user_id)

    def metrics(self) -> dict[str, Any]:
        """Return aggregate orchestrator metrics."""
        if not self._completed:
            return {"total_requests": 0}

        times = [r["elapsed_s"] for r in self._completed]
        candidates = [r["candidates"] for r in self._completed]
        selected = [r["selected"] for r in self._completed]

        return {
            "total_requests": len(self._completed),
            "avg_elapsed_s": round(sum(times) / len(times), 3),
            "avg_candidates": round(sum(candidates) / len(candidates), 1),
            "avg_selected": round(sum(selected) / len(selected), 1),
            "failed_count": sum(1 for r in self._completed if r.get("error")),
        }
