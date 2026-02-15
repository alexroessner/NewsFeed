"""arXiv agent — academic preprint search via the free Atom API.

No API key required. Docs: https://info.arxiv.org/help/api/index.html
Rate limit: 1 request per 3 seconds recommended.

Specializes in AI/ML, physics, economics, and quantitative research
that often foreshadows industry developments by weeks or months.
"""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_API_BASE = "http://export.arxiv.org/api/query"

# Atom namespace
_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Topic to arXiv category mapping
_TOPIC_CATEGORIES: dict[str, list[str]] = {
    "ai_policy": ["cs.AI", "cs.CL", "cs.LG", "cs.CV"],
    "technology": ["cs.SE", "cs.CR", "cs.DC", "cs.NI"],
    "science": ["physics", "q-bio", "math"],
    "markets": ["q-fin", "econ", "stat"],
    "climate": ["physics.ao-ph", "physics.geo-ph"],
    "crypto": ["cs.CR"],
    "health": ["q-bio", "cs.AI"],
}


class ArXivAgent(ResearchAgent):
    """Fetches recent preprints from arXiv Atom API.

    Provides early-signal intelligence on AI/ML breakthroughs, policy-relevant
    research, and quantitative findings before they hit mainstream coverage.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 12) -> None:
        super().__init__(agent_id=agent_id, source="arxiv", mandate=mandate)
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        query = self._build_query(task)
        url = f"{_API_BASE}?search_query={quote_plus(query)}&sortBy=submittedDate&sortOrder=descending&max_results={top_k * 2}"

        _MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MB
        try:
            req = Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urlopen(req, timeout=self._timeout) as resp:
                xml_data = resp.read(_MAX_FEED_BYTES + 1)
            if len(xml_data) > _MAX_FEED_BYTES:
                log.warning("arXiv API response too large (%d bytes), skipping", len(xml_data))
                return []
        except (URLError, OSError) as e:
            log.error("arXiv API fetch failed: %s", e)
            return []

        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError as e:
            log.error("arXiv XML parse failed: %s", e)
            return []

        candidates: list[CandidateItem] = []

        for idx, entry in enumerate(root.findall("atom:entry", _NS)):
            title_el = entry.find("atom:title", _NS)
            summary_el = entry.find("atom:summary", _NS)
            id_el = entry.find("atom:id", _NS)
            published_el = entry.find("atom:published", _NS)

            title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
            summary = summary_el.text.strip().replace("\n", " ") if summary_el is not None and summary_el.text else ""
            paper_url = id_el.text.strip() if id_el is not None and id_el.text else ""

            if not title:
                continue

            created_at = datetime.now(timezone.utc)
            if published_el is not None and published_el.text:
                try:
                    created_at = datetime.fromisoformat(published_el.text.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Extract categories for topic inference
            categories = []
            for cat_el in entry.findall("arxiv:primary_category", _NS):
                term = cat_el.get("term", "")
                if term:
                    categories.append(term)

            # Authors
            authors = []
            for author_el in entry.findall("atom:author", _NS):
                name_el = author_el.find("atom:name", _NS)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            preference_fit = self._score_relevance(title, summary, task.weighted_topics)
            topic = self._categories_to_topic(categories, task)
            cid = hashlib.sha256(f"{self.agent_id}:{paper_url}".encode()).hexdigest()[:16]

            author_note = f"by {', '.join(authors[:3])}" if authors else ""
            cat_note = f"[{', '.join(categories[:2])}]" if categories else ""

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.5, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, summary)
            pred_boost = self._prediction_boost(title, summary)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title[:200],
                source="arxiv",
                summary=f"{cat_note} {summary[:250]} {author_note}".strip(),
                url=paper_url,
                topic=topic,
                evidence_score=0.72,  # Preprints: high novelty, moderate evidence pre-peer-review
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.60 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        result = candidates[:top_k]
        log.info("arXiv agent %s returned %d candidates", self.agent_id, len(result))
        return result

    def _build_query(self, task: ResearchTask) -> str:
        """Build arXiv query from weighted topics."""
        parts: list[str] = []
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:3]

        for topic in top_topics:
            cats = _TOPIC_CATEGORIES.get(topic, [])
            if cats:
                cat_query = " OR ".join(f"cat:{c}" for c in cats[:3])
                parts.append(f"({cat_query})")
            else:
                keywords = topic.replace("_", " ").split()
                for kw in keywords[:2]:
                    parts.append(f'all:"{kw}"')

        return " OR ".join(parts) if parts else f'all:"{task.prompt[:50]}"'

    def _categories_to_topic(self, categories: list[str], task: ResearchTask) -> str:
        """Map arXiv categories to internal topics."""
        for cat in categories:
            for topic, topic_cats in _TOPIC_CATEGORIES.items():
                if any(cat.startswith(tc) for tc in topic_cats):
                    return topic
        return max(task.weighted_topics, key=task.weighted_topics.get, default="science")
