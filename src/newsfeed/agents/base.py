from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)


class ResearchAgent(ABC):
    """Base class for all research agents (simulated and real)."""

    def __init__(self, agent_id: str, source: str, mandate: str) -> None:
        self.agent_id = agent_id
        self.source = source
        self.mandate = mandate

    @abstractmethod
    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        """Fetch and return candidate items for the given task."""

    async def run_async(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        """Async wrapper — subclasses can override for true async I/O."""
        await asyncio.sleep(0)
        return self.run(task, top_k=top_k)

    def _score_relevance(self, title: str, summary: str, weighted_topics: dict[str, float]) -> float:
        """Score how relevant an article is to the user's weighted topics."""
        text = f"{title} {summary}".lower()
        score = 0.0
        total_weight = sum(weighted_topics.values()) or 1.0
        for topic, weight in weighted_topics.items():
            keywords = topic.lower().replace("_", " ").split()
            hits = sum(1 for kw in keywords if kw in text)
            if hits:
                score += (weight / total_weight) * min(1.0, hits * 0.4)
        return round(min(1.0, score + 0.2), 3)  # baseline relevance of 0.2
