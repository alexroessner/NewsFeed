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

    # Synonym expansion for topic matching — a story about "NATO" or "sanctions"
    # should score high for the "geopolitics" topic even if "geopolitics" doesn't
    # appear verbatim.
    _TOPIC_KEYWORDS: dict[str, list[str]] = {
        "geopolitics": [
            "geopolitics", "geopolitical", "diplomacy", "diplomatic", "nato", "sanctions",
            "alliance", "foreign policy", "sovereignty", "territorial", "conflict",
            "bilateral", "multilateral", "summit", "treaty", "ambassador", "military",
            "defense", "defence", "troops", "war", "invasion", "ceasefire",
        ],
        "ai_policy": [
            "ai", "artificial intelligence", "machine learning", "deep learning",
            "regulation", "policy", "governance", "safety", "alignment", "llm",
            "gpt", "claude", "gemini", "openai", "anthropic", "deepmind",
            "autonomous", "generative", "neural", "model",
        ],
        "technology": [
            "technology", "tech", "software", "hardware", "startup", "silicon valley",
            "computing", "semiconductor", "chip", "digital", "platform", "app",
            "cyber", "hack", "data", "cloud", "infrastructure", "api",
        ],
        "markets": [
            "markets", "market", "stock", "stocks", "equity", "bond", "treasury",
            "fed", "federal reserve", "interest rate", "inflation", "gdp",
            "recession", "economy", "economic", "trade", "tariff", "earnings",
            "investor", "financial", "banking", "currency", "forex",
        ],
        "climate": [
            "climate", "carbon", "emissions", "greenhouse", "renewable", "solar",
            "wind", "energy", "epa", "environment", "environmental", "warming",
            "temperature", "fossil", "sustainability", "net zero", "paris agreement",
        ],
        "crypto": [
            "crypto", "cryptocurrency", "bitcoin", "ethereum", "blockchain",
            "defi", "token", "mining", "web3", "nft", "stablecoin",
        ],
        "science": [
            "science", "scientific", "research", "study", "discovery", "physics",
            "biology", "chemistry", "genome", "space", "nasa", "quantum",
            "experiment", "peer review", "journal", "arxiv",
        ],
    }

    def _score_relevance(self, title: str, summary: str, weighted_topics: dict[str, float]) -> float:
        """Score how relevant an article is to the user's weighted topics."""
        text = f"{title} {summary}".lower()
        score = 0.0
        total_weight = sum(weighted_topics.values()) or 1.0
        for topic, weight in weighted_topics.items():
            # Use expanded keywords if available, otherwise fall back to topic name
            keywords = self._TOPIC_KEYWORDS.get(topic, topic.lower().replace("_", " ").split())
            hits = sum(1 for kw in keywords if kw in text)
            if hits:
                # Normalize by keyword count to avoid topics with more synonyms dominating
                hit_rate = hits / len(keywords)
                score += (weight / total_weight) * min(1.0, hit_rate * 3.0 + 0.2)
        return round(min(1.0, score + 0.15), 3)  # baseline relevance of 0.15
