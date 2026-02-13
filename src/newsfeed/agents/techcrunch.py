"""TechCrunch RSS agent — free public feed for technology and startup news.

No API key required. TechCrunch provides focused coverage of startups,
venture capital, product launches, and technology industry developments.
Complements HackerNews (community) with editorial tech coverage.
"""
from __future__ import annotations

from newsfeed.agents.rss_generic import GenericRSSAgent

_FEEDS: dict[str, str] = {
    "top": "https://techcrunch.com/feed/",
}

_TOPIC_MAP: dict[str, str] = {
    "top": "technology",
}


class TechCrunchAgent(GenericRSSAgent):
    """Fetches news from TechCrunch's RSS feed.

    Provides editorial technology coverage with emphasis on startups,
    funding rounds, product launches, and industry trends. Complements
    HackerNews community signals with professional tech journalism.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(
            agent_id=agent_id,
            source="techcrunch",
            mandate=mandate,
            feeds=_FEEDS,
            topic_map=_TOPIC_MAP,
            evidence_baseline=0.70,       # tier-2: good tech journalism
            prediction_baseline=0.44,     # decent on tech trends
            timeout=timeout,
            max_feeds=1,
        )
