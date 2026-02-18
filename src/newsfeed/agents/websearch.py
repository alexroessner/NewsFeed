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
from newsfeed.agents._xml_safe import ParseError, safe_fromstring
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
        sorted_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)

        # Each web agent uses a different search strategy so they don't return
        # identical results.  Strategy is selected based on agent_id suffix.
        if "verification" in self.agent_id:
            # Verification agent: keyword search for specific claims/names
            query = self._build_verification_query(task)
            search_url = _GNEWS_SEARCH.format(query=quote_plus(query))
            all_items.extend(self._fetch_rss(search_url, "general", task))
        elif "emerging" in self.agent_id:
            # Emerging agent: lower-weighted topics that might be developing
            tail_topics = sorted_topics[2:5] if len(sorted_topics) > 2 else sorted_topics
            for topic in tail_topics:
                if topic in _GNEWS_TOPICS:
                    all_items.extend(self._fetch_rss(_GNEWS_TOPICS[topic], topic, task))
            if not all_items:
                query = " ".join(t.replace("_", " ") for t in tail_topics[:3])
                search_url = _GNEWS_SEARCH.format(query=quote_plus(query))
                all_items.extend(self._fetch_rss(search_url, "general", task))
        else:
            # Discovery agent (default): top-priority topic feeds
            top_topics = sorted_topics[:2]
            for topic in top_topics:
                if topic in _GNEWS_TOPICS:
                    all_items.extend(self._fetch_rss(_GNEWS_TOPICS[topic], topic, task))
            # Plus keyword search for the prompt itself
            if task.prompt:
                search_url = _GNEWS_SEARCH.format(query=quote_plus(task.prompt[:60]))
                all_items.extend(self._fetch_rss(search_url, "general", task))

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

    _MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB

    def _fetch_rss(self, url: str, default_topic: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsFeed/1.0)",
            })
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read(self._MAX_FEED_BYTES + 1)
            if len(xml_data) > self._MAX_FEED_BYTES:
                log.warning("Google News RSS response too large (%d bytes), skipping", len(xml_data))
                return []
        except (URLError, OSError) as e:
            log.error("Google News RSS fetch failed: %s", e)
            return []

        try:
            root = safe_fromstring(xml_data)
        except ParseError as e:
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

            # Clean HTML entities and tags from title and summary.
            # Unescape first (so encoded tags become real tags), then strip.
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
            topic = self._infer_topic(title, summary, task, default_topic)
            evidence = self._source_evidence(source_name)
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, summary)
            pred_boost = self._prediction_boost(title, summary)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title[:200],
                source="web",
                summary=f"via {source_name}: {summary[:250]}",
                url=link,
                topic=topic,
                evidence_score=evidence,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.40 + pred_boost), 3),
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

    def _build_verification_query(self, task: ResearchTask) -> str:
        """Build a cross-reference search query for fact-checking angles."""
        top_topic = max(task.weighted_topics, key=task.weighted_topics.get, default="news")
        # Search for analysis and fact-check content, not just headlines
        verification_terms = {
            "geopolitics": "geopolitical analysis latest developments",
            "ai_policy": "AI regulation policy update analysis",
            "technology": "technology industry analysis latest",
            "markets": "financial markets analysis economic outlook",
            "climate": "climate policy analysis environment update",
            "crypto": "cryptocurrency regulation analysis",
        }
        return verification_terms.get(top_topic, f"{top_topic.replace('_', ' ')} analysis latest")

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
