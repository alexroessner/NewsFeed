"""Nature RSS agent — free public feed from Nature journal.

No API key required. Nature provides the highest-tier science and research
coverage — peer-reviewed breakthroughs, policy-relevant findings, and
editorial analysis from the world's most cited scientific journal.

Note: Nature uses RDF/RSS 1.0 format with XML namespaces, not standard
RSS 2.0, so this agent overrides the feed parser.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from newsfeed.agents.rss_generic import GenericRSSAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_FEEDS: dict[str, str] = {
    "top": "https://www.nature.com/nature.rss",
}

_TOPIC_MAP: dict[str, str] = {
    "top": "science",
}

# RDF/RSS 1.0 namespaces used by Nature
_NS = {
    "rss": "http://purl.org/rss/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
}

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class NatureAgent(GenericRSSAgent):
    """Fetches news from Nature journal's RSS feed.

    Provides peer-reviewed science coverage and editorial analysis.
    Complements arXiv preprints with validated, published findings
    and Nature's editorial perspective on research significance.

    Overrides _fetch_feed to handle Nature's RDF/RSS 1.0 format
    which uses XML namespaces (dc:date, content:encoded, etc.).
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(
            agent_id=agent_id,
            source="nature",
            mandate=mandate,
            feeds=_FEEDS,
            topic_map=_TOPIC_MAP,
            evidence_baseline=0.88,       # tier-academic: gold-standard science
            prediction_baseline=0.35,     # science is slow-moving
            timeout=timeout,
            max_feeds=1,
        )

    def _fetch_feed(self, feed_name: str, url: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read()
        except (URLError, OSError) as e:
            log.error("Nature RSS fetch failed for %s: %s", feed_name, e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("Nature RSS parse failed for %s: %s", feed_name, e)
            return []

        candidates: list[CandidateItem] = []
        topic = self._topic_map.get(feed_name, "science")

        # Nature uses namespaced <item> tags in RDF format
        for idx, item in enumerate(root.iter(f"{{{_NS['rss']}}}item")):
            title_el = item.find(f"{{{_NS['rss']}}}title")
            if title_el is None:
                title_el = item.find("title")
            link_el = item.find(f"{{{_NS['rss']}}}link")
            if link_el is None:
                link_el = item.find("link")
            content_el = item.find(f"{{{_NS['content']}}}encoded")
            date_el = item.find(f"{{{_NS['dc']}}}date")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""

            # Extract summary from content:encoded (unescape then strip HTML)
            summary = ""
            if content_el is not None and content_el.text:
                summary = content_el.text.strip()

            if not title:
                continue

            # Unescape entities first, then strip any resulting HTML tags
            title = _HTML_TAG_RE.sub("", html.unescape(title)).strip()
            summary = _HTML_TAG_RE.sub("", html.unescape(summary)).strip()

            created_at = datetime.now(timezone.utc)
            if date_el is not None and date_el.text:
                try:
                    created_at = datetime.fromisoformat(date_el.text.replace("Z", "+00:00"))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

            preference_fit = self._score_relevance(title, summary, task.weighted_topics)
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="nature",
                summary=summary[:600],
                url=link,
                topic=topic,
                evidence_score=self._evidence_baseline,
                novelty_score=round(max(0.3, 1.0 - idx * 0.05), 3),
                preference_fit=preference_fit,
                prediction_signal=self._prediction_baseline,
                discovered_by=self.agent_id,
                created_at=created_at,
                regions=self.detect_locations(title, summary),
            ))

        return candidates
