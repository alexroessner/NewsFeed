"""Generic RSS feed agent — configurable base for any standard RSS 2.0 source.

Handles the common RSS pipeline: HTTP fetch, XML parse, date handling,
relevance scoring, deduplication, and candidate ranking. Source-specific
agents provide feed URLs, topic mappings, and scoring baselines.
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


class GenericRSSAgent(ResearchAgent):
    """Configurable RSS feed agent.

    Parameters
    ----------
    agent_id : str
        Unique identifier for this agent instance.
    source : str
        Source name (e.g. "npr", "cnbc").
    mandate : str
        Agent mandate text.
    feeds : dict[str, str]
        Mapping of feed_name -> feed_url.
    topic_map : dict[str, str]
        Mapping of feed_name -> topic category.
    evidence_baseline : float
        Base evidence score for this source (reflects source tier).
    prediction_baseline : float
        Base prediction signal score.
    timeout : int
        HTTP request timeout in seconds.
    max_feeds : int
        Maximum number of feeds to fetch per run.
    """

    def __init__(
        self,
        agent_id: str,
        source: str,
        mandate: str,
        feeds: dict[str, str],
        topic_map: dict[str, str],
        evidence_baseline: float = 0.75,
        prediction_baseline: float = 0.42,
        timeout: int = 10,
        max_feeds: int = 4,
    ) -> None:
        super().__init__(agent_id=agent_id, source=source, mandate=mandate)
        self._feeds = feeds
        self._topic_map = topic_map
        self._evidence_baseline = evidence_baseline
        self._prediction_baseline = prediction_baseline
        self._timeout = timeout
        self._max_feeds = max_feeds

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        relevant_feeds = self._select_feeds(task.weighted_topics)
        all_items: list[CandidateItem] = []

        for feed_name in relevant_feeds:
            url = self._feeds.get(feed_name)
            if not url:
                continue
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
        log.info(
            "%s agent %s returned %d candidates from %d feeds",
            self.source, self.agent_id, len(result), len(relevant_feeds),
        )
        return result

    def _select_feeds(self, weighted_topics: dict[str, float]) -> list[str]:
        """Pick feeds most relevant to user's weighted topics."""
        feed_scores: dict[str, float] = {}
        for feed_name, topic in self._topic_map.items():
            for user_topic, weight in weighted_topics.items():
                if any(kw in topic for kw in user_topic.lower().replace("_", " ").split()):
                    feed_scores[feed_name] = feed_scores.get(feed_name, 0) + weight

        # Always include first feed (typically "top"), plus highest scoring
        first_feed = next(iter(self._feeds), None)
        selected: list[str] = []
        if first_feed:
            selected.append(first_feed)

        for feed_name, _ in sorted(feed_scores.items(), key=lambda x: x[1], reverse=True):
            if feed_name not in selected:
                selected.append(feed_name)
            if len(selected) >= self._max_feeds:
                break

        # If we still have room, add remaining feeds
        if len(selected) < self._max_feeds:
            for feed_name in self._feeds:
                if feed_name not in selected:
                    selected.append(feed_name)
                if len(selected) >= self._max_feeds:
                    break

        return selected

    def _fetch_feed(self, feed_name: str, url: str, task: ResearchTask) -> list[CandidateItem]:
        try:
            req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read()
        except (URLError, OSError) as e:
            log.error("%s RSS fetch failed for %s: %s", self.source, feed_name, e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("%s RSS parse failed for %s: %s", self.source, feed_name, e)
            return []

        candidates: list[CandidateItem] = []
        topic = self._topic_map.get(feed_name, "general")

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

            title = html.unescape(self._strip_html(title))
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
            cid = hashlib.sha256(f"{self.agent_id}:{link}".encode()).hexdigest()[:16]

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source=self.source,
                summary=summary[:300],
                url=link,
                topic=topic,
                evidence_score=self._evidence_baseline,
                novelty_score=round(max(0.3, 1.0 - idx * 0.05), 3),
                preference_fit=preference_fit,
                prediction_signal=self._prediction_baseline,
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        return candidates

    @staticmethod
    def _strip_html(text: str) -> str:
        return _HTML_TAG_RE.sub("", text).strip()
