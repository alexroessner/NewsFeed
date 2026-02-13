"""France 24 RSS agent — free English-language feeds.

No API key required. France 24 provides a continental European perspective
on global affairs, with particularly strong coverage of Africa, Middle East,
and francophone world events often underrepresented in Anglo-American media.
"""
from __future__ import annotations

from newsfeed.agents.rss_generic import GenericRSSAgent

_FEEDS: dict[str, str] = {
    "top": "https://www.france24.com/en/rss",
    "africa": "https://www.france24.com/en/africa/rss",
    "middle_east": "https://www.france24.com/en/middle-east/rss",
    "europe": "https://www.france24.com/en/europe/rss",
    "americas": "https://www.france24.com/en/americas/rss",
    "asia_pacific": "https://www.france24.com/en/asia-pacific/rss",
}

_TOPIC_MAP: dict[str, str] = {
    "top": "geopolitics",
    "africa": "geopolitics",
    "middle_east": "geopolitics",
    "europe": "geopolitics",
    "americas": "geopolitics",
    "asia_pacific": "geopolitics",
}


class France24Agent(GenericRSSAgent):
    """Fetches news from France 24's English RSS feeds.

    Provides a European continental and francophone perspective on global
    events. Especially valuable for Africa, Middle East, and EU policy
    coverage that complements Anglo-American wire services.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(
            agent_id=agent_id,
            source="france24",
            mandate=mandate,
            feeds=_FEEDS,
            topic_map=_TOPIC_MAP,
            evidence_baseline=0.74,       # tier-1b: established intl broadcaster
            prediction_baseline=0.40,
            timeout=timeout,
            max_feeds=4,
        )
