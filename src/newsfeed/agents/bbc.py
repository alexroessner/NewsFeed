"""BBC News RSS feed agent.

No API key required — uses free public RSS feeds.
Feeds: https://www.bbc.co.uk/news/10628494
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")

_FEEDS: dict[str, str] = {
    "top": "https://feeds.bbci.co.uk/news/rss.xml",
    "world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "science": "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "health": "https://feeds.bbci.co.uk/news/health/rss.xml",
    "asia": "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "europe": "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "middle_east": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "africa": "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "us_canada": "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
}

_FEED_TOPIC_MAP: dict[str, str] = {
    "top": "general",
    "world": "geopolitics",
    "business": "markets",
    "technology": "technology",
    "science": "science",
    "politics": "geopolitics",
    "health": "health",
    "asia": "geopolitics",
    "europe": "geopolitics",
    "middle_east": "geopolitics",
    "africa": "geopolitics",
    "us_canada": "geopolitics",
}


class BBCAgent(ResearchAgent):
    def __init__(self, agent_id: str, mandate: str, feeds: list[str] | None = None, timeout: int = 10) -> None:
        super().__init__(agent_id=agent_id, source="bbc", mandate=mandate)
        self._feed_names = feeds or ["top", "world", "business", "technology"]
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        # Pick feeds most relevant to user's topics
        relevant_feeds = self._select_feeds(task.weighted_topics)
        all_items: list[CandidateItem] = []

        for feed_name in relevant_feeds:
            url = _FEEDS.get(feed_name)
            if not url:
                continue
            items = self._fetch_feed(feed_name, url, task)
            all_items.extend(items)

        # Dedupe by title
        seen_titles: set[str] = set()
        deduped: list[CandidateItem] = []
        for item in all_items:
            key = item.title.lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                deduped.append(item)

        deduped.sort(key=lambda c: c.composite_score(), reverse=True)
        result = deduped[:top_k]
        log.info("BBC agent %s returned %d candidates from %d feeds", self.agent_id, len(result), len(relevant_feeds))
        return result

    def _select_feeds(self, weighted_topics: dict[str, float]) -> list[str]:
        """Select feeds most relevant to user's weighted topics."""
        feed_scores: dict[str, float] = {}
        for feed_name, topic in _FEED_TOPIC_MAP.items():
            if feed_name not in self._feed_names and feed_name != "top":
                continue
            for user_topic, weight in weighted_topics.items():
                if any(kw in topic for kw in user_topic.lower().replace("_", " ").split()):
                    feed_scores[feed_name] = feed_scores.get(feed_name, 0) + weight

        # Always include 'top' and add highest-scoring feeds
        selected = ["top"]
        for feed_name, _ in sorted(feed_scores.items(), key=lambda x: x[1], reverse=True):
            if feed_name not in selected:
                selected.append(feed_name)
            if len(selected) >= 3:
                break

        return selected

    def _fetch_feed(self, feed_name: str, url: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read()
        except (URLError, OSError) as e:
            log.error("BBC RSS fetch failed for %s: %s", feed_name, e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("BBC RSS parse failed for %s: %s", feed_name, e)
            return []

        candidates: list[CandidateItem] = []
        topic = _FEED_TOPIC_MAP.get(feed_name, "general")

        for idx, item in enumerate(root.iter("item")):
            title_el = item.find("title")
            desc_el = item.find("description")
            link_el = item.find("link")
            pub_date_el = item.find("pubDate")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            summary = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""

            if not title:
                continue

            # Unescape entities first, then strip any resulting HTML tags
            title = self._strip_html(html.unescape(title))
            summary = self._strip_html(html.unescape(summary))

            created_at = datetime.now(timezone.utc)
            if pub_date_el is not None and pub_date_el.text:
                try:
                    created_at = parsedate_to_datetime(pub_date_el.text)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

            preference_fit = self._score_relevance(title, summary, task.weighted_topics)
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, summary)
            pred_boost = self._prediction_boost(title, summary)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="bbc",
                summary=summary[:600],
                url=link,
                topic=topic,
                evidence_score=0.82,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.45 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
                regions=self.detect_locations(title, summary),
            ))

        return candidates

    @staticmethod
    def _strip_html(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()
