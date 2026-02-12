"""Web search agent — combines Google News RSS and DuckDuckGo Lite.

Google News RSS: No API key required, free public RSS feeds.
DuckDuckGo Lite: No API key required, HTML scraping of lite.duckduckgo.com.

This agent provides broad open-web discovery and cross-reference search
capability, essential for verification and emerging topic detection.
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
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

# Google News RSS endpoints (no key needed)
_GNEWS_SEARCH = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
_GNEWS_TOPICS = {
    "geopolitics": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFZxYUdjU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",
    "technology": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",
    "markets": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",
    "science": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en",
    "health": "https://news.google.com/rss/topics/CAAqIQgKIhtDQkFTRGdvSUwyMHZNR3QwTlRFU0FtVnVLQUFQAQ?hl=en-US&gl=US&ceid=US:en",
}


class WebSearchAgent(ResearchAgent):
    """Broad open-web discovery via Google News RSS feeds.

    Combines topic-specific feeds with keyword search to surface
    stories from thousands of sources that individual agents might miss.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(agent_id=agent_id, source="web", mandate=mandate)
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        all_items: list[CandidateItem] = []

        # Strategy 1: Topic-specific feeds
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:2]
        for topic in top_topics:
            if topic in _GNEWS_TOPICS:
                items = self._fetch_rss(_GNEWS_TOPICS[topic], topic, task)
                all_items.extend(items)

        # Strategy 2: Keyword search
        query = self._build_query(task)
        search_url = _GNEWS_SEARCH.format(query=quote_plus(query))
        search_items = self._fetch_rss(search_url, "general", task)
        all_items.extend(search_items)

        # Dedupe by title
        seen: set[str] = set()
        deduped: list[CandidateItem] = []
        for item in all_items:
            key = item.title.lower().strip()[:60]
            if key not in seen:
                seen.add(key)
                deduped.append(item)

        deduped.sort(key=lambda c: c.composite_score(), reverse=True)
        result = deduped[:top_k]
        log.info("WebSearch agent %s returned %d candidates", self.agent_id, len(result))
        return result

    def _fetch_rss(self, url: str, default_topic: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsFeed/1.0)",
            })
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read()
        except (URLError, OSError) as e:
            log.error("Google News RSS fetch failed: %s", e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("Google News RSS parse failed: %s", e)
            return []

        candidates: list[CandidateItem] = []

        for idx, item in enumerate(root.iter("item")):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            pub_date_el = item.find("pubDate")
            source_el = item.find("source")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            summary = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
            source_name = source_el.text.strip() if source_el is not None and source_el.text else "web"

            if not title:
                continue

            # Clean HTML entities from title and summary
            title = html.unescape(title)
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
            topic = self._infer_topic(title, summary, task, default_topic)
            evidence = self._source_evidence(source_name)
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title[:200],
                source="web",
                summary=f"via {source_name}: {summary[:250]}",
                url=link,
                topic=topic,
                evidence_score=evidence,
                novelty_score=round(max(0.3, 1.0 - idx * 0.06), 3),
                preference_fit=preference_fit,
                prediction_signal=0.40,
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        return candidates

    def _build_query(self, task: ResearchTask) -> str:
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:3]
        keywords = []
        for topic in top_topics:
            keywords.extend(topic.replace("_", " ").split())
        return " ".join(keywords[:5]) if keywords else task.prompt[:80]

    def _source_evidence(self, source_name: str) -> float:
        """Estimate evidence quality based on source name."""
        high_trust = {"reuters", "associated press", "bbc news", "the guardian", "financial times", "ap news", "npr"}
        mid_trust = {"cnn", "al jazeera", "the economist", "washington post", "new york times", "bloomberg"}
        name_lower = source_name.lower()
        if any(s in name_lower for s in high_trust):
            return 0.82
        if any(s in name_lower for s in mid_trust):
            return 0.72
        return 0.55

    def _infer_topic(self, title: str, summary: str, task: ResearchTask, default: str) -> str:
        text = f"{title} {summary}".lower()
        best_topic = default
        best_score = 0.0
        for topic, weight in task.weighted_topics.items():
            keywords = topic.lower().replace("_", " ").split()
            hits = sum(1 for kw in keywords if kw in text)
            if hits > best_score:
                best_score = hits
                best_topic = topic
        return best_topic

    def _strip_html(self, text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()
