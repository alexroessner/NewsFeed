"""Al Jazeera RSS agent — free public RSS feeds.

No API key required. Al Jazeera provides strong coverage of Middle East,
Africa, Asia, and global south perspectives often underrepresented in
Western-centric feeds.
"""
from __future__ import annotations

import hashlib
import html
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_FEEDS: dict[str, str] = {
    "top": "https://www.aljazeera.com/xml/rss/all.xml",
    "middle_east": "https://www.aljazeera.com/xml/rss/all.xml",
    "economy": "https://www.aljazeera.com/xml/rss/all.xml",
}

_FEED_TOPIC_MAP: dict[str, str] = {
    "top": "geopolitics",
    "middle_east": "geopolitics",
    "economy": "markets",
}

# Region keywords for auto-tagging
_REGION_KEYWORDS: dict[str, list[str]] = {
    "middle_east": ["gaza", "israel", "iran", "syria", "iraq", "lebanon", "yemen", "saudi", "qatar", "hamas", "hezbollah"],
    "africa": ["sudan", "ethiopia", "nigeria", "kenya", "sahel", "congo", "somalia", "libya"],
    "south_asia": ["india", "pakistan", "bangladesh", "kashmir", "delhi", "modi"],
    "east_asia": ["china", "taiwan", "beijing", "xinjiang", "uyghur"],
    "europe": ["ukraine", "russia", "nato", "eu", "moscow", "kyiv"],
}


class AlJazeeraAgent(ResearchAgent):
    """Fetches news from Al Jazeera RSS feeds.

    Provides global south perspective and strong Middle East/Africa
    coverage that complements Western wire services.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 10) -> None:
        super().__init__(agent_id=agent_id, source="aljazeera", mandate=mandate)
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        all_items: list[CandidateItem] = []

        for feed_name, url in _FEEDS.items():
            items = self._fetch_feed(feed_name, url, task)
            all_items.extend(items)

        # Dedupe by title
        seen: set[str] = set()
        deduped: list[CandidateItem] = []
        for item in all_items:
            key = item.title.lower().strip()
            if key not in seen:
                seen.add(key)
                deduped.append(item)

        deduped.sort(key=lambda c: c.composite_score(), reverse=True)
        result = deduped[:top_k]
        log.info("AlJazeera agent %s returned %d candidates", self.agent_id, len(result))
        return result

    def _fetch_feed(self, feed_name: str, url: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read()
        except (URLError, OSError) as e:
            log.error("AlJazeera RSS fetch failed for %s: %s", feed_name, e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("AlJazeera RSS parse failed for %s: %s", feed_name, e)
            return []

        candidates: list[CandidateItem] = []
        topic = _FEED_TOPIC_MAP.get(feed_name, "geopolitics")

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

            # Decode HTML entities and strip tags
            title = html.unescape(title)
            summary = html.unescape(self._strip_html(summary))

            created_at = datetime.now(timezone.utc)
            if pub_date_el is not None and pub_date_el.text:
                try:
                    created_at = parsedate_to_datetime(pub_date_el.text)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass

            preference_fit = self._score_relevance(title, summary, task.weighted_topics)
            regions = self._detect_regions(title, summary)
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, summary)
            pred_boost = self._prediction_boost(title, summary)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="aljazeera",
                summary=summary[:300],
                url=link,
                topic=topic,
                evidence_score=0.78,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.48 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
                regions=regions,
            ))

        return candidates

    def _detect_regions(self, title: str, summary: str) -> list[str]:
        text = f"{title} {summary}".lower()
        detected: list[str] = []
        for region, keywords in _REGION_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                detected.append(region)
        return detected

    def _strip_html(self, text: str) -> str:
        import re
        return re.sub(r"<[^>]+>", "", text).strip()
