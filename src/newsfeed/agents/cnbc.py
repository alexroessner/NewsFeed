"""CNBC RSS agent — free public feeds for markets and business news.

No API key required. CNBC provides real-time market commentary, economic
data coverage, and business analysis. Especially strong on US markets,
central bank policy, and earnings.
"""
from __future__ import annotations

from newsfeed.agents.rss_generic import GenericRSSAgent

_FEEDS: dict[str, str] = {
    "top": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "world": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362",
    "business": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
    "technology": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
}

_TOPIC_MAP: dict[str, str] = {
    "top": "markets",
    "world": "geopolitics",
    "business": "markets",
    "technology": "technology",
    "economy": "markets",
}


class CNBCAgent(GenericRSSAgent):
    """Fetches news from CNBC's public RSS feeds.

    Provides real-time market-moving signals, earnings coverage,
    and economic policy analysis. Strong complement to FT and Reuters
    for financial and business intelligence.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(
            agent_id=agent_id,
            source="cnbc",
            mandate=mandate,
            feeds=_FEEDS,
            topic_map=_TOPIC_MAP,
            evidence_baseline=0.76,       # tier-1b: solid business journalism
            prediction_baseline=0.46,     # higher prediction for markets
            timeout=timeout,
            max_feeds=3,
        )
