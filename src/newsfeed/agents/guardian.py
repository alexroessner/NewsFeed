"""Guardian Open Platform API agent.

Requires a free API key from https://open-platform.theguardian.com/access/
Set via config key: api_keys.guardian
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlencode

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_BASE_URL = "https://content.guardianapis.com/search"


class GuardianAgent(ResearchAgent):
    def __init__(self, agent_id: str, mandate: str, api_key: str, timeout: int = 10) -> None:
        super().__init__(agent_id=agent_id, source="guardian", mandate=mandate)
        self._api_key = api_key
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        query = self._build_query(task)
        params = urlencode({
            "q": query,
            "api-key": self._api_key,
            "format": "json",
            "show-fields": "headline,trailText,shortUrl,thumbnail,byline",
            "order-by": "newest",
            "page-size": min(top_k * 2, 50),
        })
        url = f"{_BASE_URL}?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.error("Guardian API request failed: %s", e)
            return []

        results = data.get("response", {}).get("results", [])
        if not results:
            log.info("Guardian returned 0 results for query=%r", query)
            return []

        candidates: list[CandidateItem] = []
        for idx, item in enumerate(results[:top_k]):
            fields = item.get("fields", {})
            title = fields.get("headline") or item.get("webTitle", "")
            summary = fields.get("trailText", "")
            web_url = item.get("webUrl", "")
            section = item.get("sectionId", "general")
            pub_date = item.get("webPublicationDate", "")

            created_at = datetime.now(timezone.utc)
            if pub_date:
                try:
                    created_at = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                except ValueError:
                    pass

            preference_fit = self._score_relevance(title, summary, task.weighted_topics)
            cid = hashlib.sha256(f"{self.agent_id}:{web_url}".encode()).hexdigest()[:16]

            # Content-aware scoring: recency-based novelty + mandate alignment
            now = datetime.now(timezone.utc)
            age_hours = max(0.1, (now - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, summary)
            pred_boost = self._prediction_boost(title, summary)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="guardian",
                summary=summary[:600],
                url=web_url,
                topic=self._map_section_to_topic(section),
                evidence_score=0.80,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.50 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
                regions=self.detect_locations(title, summary),
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        log.info("Guardian agent %s returned %d candidates", self.agent_id, len(candidates))
        return candidates

    def _build_query(self, task: ResearchTask) -> str:
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:3]
        keywords = []
        for topic in top_topics:
            keywords.extend(topic.replace("_", " ").split())
        return " OR ".join(keywords[:5]) if keywords else task.prompt[:100]

    def _map_section_to_topic(self, section: str) -> str:
        mapping = {
            "world": "geopolitics", "politics": "geopolitics",
            "business": "markets", "money": "markets",
            "technology": "technology", "science": "science",
            "environment": "climate", "us-news": "geopolitics",
            "uk-news": "geopolitics", "global-development": "geopolitics",
        }
        return mapping.get(section, section)
