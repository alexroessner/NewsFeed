from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)


class ResearchAgent(ABC):
    """Base class for all research agents (simulated and real)."""

    # Stop words excluded from mandate keyword extraction
    _STOP = frozenset({
        "the", "and", "for", "from", "with", "that", "this", "are", "has", "have",
        "been", "its", "their", "which", "into", "also", "than", "more", "most",
        "other", "all", "any", "each", "but", "not", "can", "may", "will",
        "about", "across", "between", "through", "when", "where", "how",
        "monitor", "track", "surface", "mine", "scan", "map", "capture",
        "leverage", "sweep", "check", "detect", "identify", "key",
    })

    # Forward-looking language for prediction signal
    _FORWARD_KW = frozenset({
        "forecast", "predict", "expect", "outlook", "ahead", "future",
        "plan", "announce", "upcoming", "next", "target", "proposal",
        "guidance", "estimate", "proposed", "pending", "launch",
    })

    def __init__(self, agent_id: str, source: str, mandate: str) -> None:
        self.agent_id = agent_id
        self.source = source
        self.mandate = mandate
        # Extract mandate keywords for content-aware scoring
        self._mandate_kw = self._extract_mandate_kw()

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

    def _extract_mandate_kw(self) -> list[str]:
        """Extract content-matching keywords from the agent's mandate."""
        words: set[str] = set()
        for token in self.mandate.lower().replace("-", " ").split():
            cleaned = token.strip(",.;:()[]{}\"'")
            if len(cleaned) >= 3 and cleaned not in self._STOP:
                words.add(cleaned)
        return sorted(words)

    def _mandate_boost(self, title: str, summary: str) -> float:
        """Score how well an article matches this agent's specific mandate."""
        if not self._mandate_kw:
            return 0.0
        text = f"{title} {summary}".lower()
        hits = sum(1 for kw in self._mandate_kw if kw in text)
        return round(min(0.15, hits / max(len(self._mandate_kw), 1) * 0.3), 3)

    def _prediction_boost(self, title: str, summary: str) -> float:
        """Detect forward-looking language for prediction signal enhancement."""
        text = f"{title} {summary}".lower()
        hits = sum(1 for kw in self._FORWARD_KW if kw in text)
        return round(min(0.12, hits * 0.03), 3)

    # ── Location detection ──────────────────────────────────────────
    # Maps keywords → location tags at city, country, region, or continent level.
    # Used by all agents to auto-tag stories with geographic context.

    _LOCATION_MAP: dict[str, str] = {
        # Cities
        "washington": "Washington, US", "beijing": "Beijing, China",
        "moscow": "Moscow, Russia", "kyiv": "Kyiv, Ukraine",
        "london": "London, UK", "paris": "Paris, France",
        "berlin": "Berlin, Germany", "tokyo": "Tokyo, Japan",
        "jerusalem": "Jerusalem, Israel", "tehran": "Tehran, Iran",
        "taipei": "Taipei, Taiwan", "new delhi": "New Delhi, India",
        "delhi": "Delhi, India", "mumbai": "Mumbai, India",
        "brussels": "Brussels, Belgium", "geneva": "Geneva, Switzerland",
        "davos": "Davos, Switzerland",
        "cairo": "Cairo, Egypt", "riyadh": "Riyadh, Saudi Arabia",
        "istanbul": "Istanbul, Turkey", "kabul": "Kabul, Afghanistan",
        "hong kong": "Hong Kong", "singapore": "Singapore",
        "seoul": "Seoul, South Korea", "pyongyang": "Pyongyang, North Korea",
        "baghdad": "Baghdad, Iraq", "damascus": "Damascus, Syria",
        "nairobi": "Nairobi, Kenya", "addis ababa": "Addis Ababa, Ethiopia",
        "silicon valley": "Silicon Valley, US",
        "wall street": "Wall Street, US",
        # Countries
        "united states": "United States", "china": "China",
        "russia": "Russia", "ukraine": "Ukraine", "israel": "Israel",
        "palestine": "Palestine", "iran": "Iran", "iraq": "Iraq",
        "syria": "Syria", "yemen": "Yemen", "lebanon": "Lebanon",
        "saudi arabia": "Saudi Arabia", "turkey": "Turkey",
        "india": "India", "pakistan": "Pakistan",
        "taiwan": "Taiwan", "japan": "Japan", "south korea": "South Korea",
        "north korea": "North Korea", "australia": "Australia",
        "brazil": "Brazil", "mexico": "Mexico", "canada": "Canada",
        "germany": "Germany", "france": "France",
        "united kingdom": "United Kingdom",
        "nigeria": "Nigeria", "ethiopia": "Ethiopia",
        "kenya": "Kenya", "sudan": "Sudan", "somalia": "Somalia",
        "congo": "Congo", "libya": "Libya", "egypt": "Egypt",
        "south africa": "South Africa", "afghanistan": "Afghanistan",
        "myanmar": "Myanmar", "venezuela": "Venezuela", "colombia": "Colombia",
        # Regions and blocs
        "nato": "NATO", "european union": "European Union",
        "middle east": "Middle East", "sahel": "Sahel",
        "gaza": "Gaza", "west bank": "West Bank",
        "kashmir": "Kashmir", "xinjiang": "Xinjiang",
        "crimea": "Crimea", "donbas": "Donbas",
        "arctic": "Arctic", "south china sea": "South China Sea",
        "korean peninsula": "Korean Peninsula",
        "persian gulf": "Persian Gulf", "horn of africa": "Horn Of Africa",
        "baltic": "Baltic States",
        # Continents
        "europe": "Europe", "asia": "Asia", "africa": "Africa",
        "americas": "Americas", "latin america": "Latin America",
        "south asia": "South Asia", "east asia": "East Asia",
        "southeast asia": "Southeast Asia", "central asia": "Central Asia",
        "north america": "North America",
    }

    def detect_locations(self, title: str, summary: str) -> list[str]:
        """Detect geographic locations at city/country/region/continent level."""
        text = f"{title} {summary}".lower()
        seen: set[str] = set()
        locations: list[str] = []
        for keyword, location in self._LOCATION_MAP.items():
            if keyword in text and location not in seen:
                seen.add(location)
                locations.append(location)
            if len(locations) >= 5:
                break
        return locations
