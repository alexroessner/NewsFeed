"""NPR RSS agent — free public feeds from National Public Radio.

No API key required. NPR provides strong US domestic and international
coverage with rigorous editorial standards. Seven topic-specific feeds.
"""
from __future__ import annotations

from newsfeed.agents.rss_generic import GenericRSSAgent

_FEEDS: dict[str, str] = {
    "top": "https://feeds.npr.org/1001/rss.xml",
    "world": "https://feeds.npr.org/1004/rss.xml",
    "politics": "https://feeds.npr.org/1014/rss.xml",
    "business": "https://feeds.npr.org/1006/rss.xml",
    "technology": "https://feeds.npr.org/1019/rss.xml",
    "science": "https://feeds.npr.org/1007/rss.xml",
    "health": "https://feeds.npr.org/1128/rss.xml",
}

_TOPIC_MAP: dict[str, str] = {
    "top": "general",
    "world": "geopolitics",
    "politics": "geopolitics",
    "business": "markets",
    "technology": "technology",
    "science": "science",
    "health": "health",
}


class NPRAgent(GenericRSSAgent):
    """Fetches news from NPR's public RSS feeds.

    NPR provides credible, in-depth US and international coverage with
    a public-interest editorial mandate. Complements wire services with
    stronger narrative context and explanatory journalism.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(
            agent_id=agent_id,
            source="npr",
            mandate=mandate,
            feeds=_FEEDS,
            topic_map=_TOPIC_MAP,
            evidence_baseline=0.80,       # tier-1b: strong editorial standards
            prediction_baseline=0.40,
            timeout=timeout,
            max_feeds=4,
        )
