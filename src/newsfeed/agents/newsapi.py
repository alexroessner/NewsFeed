"""NewsAPI.org agent — aggregator covering Reuters, AP, FT, and 150,000+ sources.

Requires a free API key from https://newsapi.org/register
Set via config key: api_keys.newsapi

Note: Free tier is for development only. Production use requires a paid plan.
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_BASE_URL = "https://newsapi.org/v2"

# Map our source names to NewsAPI source IDs
_SOURCE_MAP: dict[str, str] = {
    "reuters": "reuters",
    "ap": "associated-press",
    "ft": "financial-times",
    "bbc": "bbc-news",
    "guardian": "the-guardian-uk",
}


class NewsAPIAgent(ResearchAgent):
    """Agent that fetches news via NewsAPI.org, which aggregates Reuters, AP, FT, etc."""

    def __init__(
        self, agent_id: str, source: str, mandate: str,
        api_key: str, timeout: int = 10,
    ) -> None:
        super().__init__(agent_id=agent_id, source=source, mandate=mandate)
        self._api_key = api_key
        self._timeout = timeout
        self._newsapi_source = _SOURCE_MAP.get(source, "")

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        # Try source-specific endpoint first, then fall back to keyword search
        candidates = self._fetch_from_source(task, top_k)
        if not candidates:
            candidates = self._fetch_everything(task, top_k)
        return candidates

    def _fetch_from_source(self, task: ResearchTask, top_k: int) -> list[CandidateItem]:
        """Fetch top headlines from a specific source."""
        if not self._newsapi_source:
            return []

        params = urlencode({
            "sources": self._newsapi_source,
            "pageSize": min(top_k * 2, 100),
            "apiKey": self._api_key,
        })
        url = f"{_BASE_URL}/top-headlines?{params}"
        return self._fetch_and_parse(url, task, top_k)

    def _fetch_everything(self, task: ResearchTask, top_k: int) -> list[CandidateItem]:
        """Search all sources by keyword."""
        query = self._build_query(task)
        params: dict[str, str | int] = {
            "q": query,
            "sortBy": "publishedAt",
            "pageSize": min(top_k * 2, 100),
            "apiKey": self._api_key,
            "language": "en",
        }
        if self._newsapi_source:
            params["sources"] = self._newsapi_source

        url = f"{_BASE_URL}/everything?{urlencode(params)}"
        return self._fetch_and_parse(url, task, top_k)

    def _fetch_and_parse(self, url: str, task: ResearchTask, top_k: int) -> list[CandidateItem]:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "NewsFeed/1.0",
                "X-Api-Key": self._api_key,
            })
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.error("NewsAPI request failed for %s: %s", self.source, e)
            return []

        if data.get("status") != "ok":
            log.error("NewsAPI error: %s", data.get("message", "unknown"))
            return []

        articles = data.get("articles", [])
        if not articles:
            log.info("NewsAPI returned 0 articles for source=%s", self.source)
            return []

        candidates: list[CandidateItem] = []
        for idx, article in enumerate(articles[:top_k]):
            title = article.get("title") or ""
            description = article.get("description") or ""
            article_url = article.get("url") or ""
            source_name = (article.get("source") or {}).get("name", self.source)
            published_at = article.get("publishedAt", "")

            if not title or title == "[Removed]":
                continue

            created_at = datetime.now(timezone.utc)
            if published_at:
                try:
                    created_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except ValueError:
                    pass

            preference_fit = self._score_relevance(title, description, task.weighted_topics)

            # Source reliability varies by newsapi source
            evidence = self._source_evidence(source_name)
            cid = hashlib.sha256(f"{self.agent_id}:{article_url}".encode()).hexdigest()[:16]

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, description)
            pred_boost = self._prediction_boost(title, description)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source=self.source,
                summary=description[:300],
                url=article_url,
                topic=self._infer_topic(title, description, task),
                evidence_score=evidence,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.50 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        log.info("NewsAPI agent %s (%s) returned %d candidates", self.agent_id, self.source, len(candidates))
        return candidates

    def _build_query(self, task: ResearchTask) -> str:
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:3]
        keywords = []
        for topic in top_topics:
            keywords.extend(topic.replace("_", " ").split())
        return " OR ".join(keywords[:5]) if keywords else task.prompt[:100]

    def _source_evidence(self, source_name: str) -> float:
        high_trust = {"reuters", "associated press", "ap", "financial times", "bbc news"}
        if source_name.lower() in high_trust:
            return 0.85
        return 0.65

    def _infer_topic(self, title: str, description: str, task: ResearchTask) -> str:
        """Infer topic from content, defaulting to the highest-weighted user topic."""
        text = f"{title} {description}".lower()
        best_topic = max(task.weighted_topics, key=task.weighted_topics.get, default="general")
        best_score = 0.0
        for topic, weight in task.weighted_topics.items():
            keywords = topic.lower().replace("_", " ").split()
            hits = sum(1 for kw in keywords if kw in text)
            if hits > best_score:
                best_score = hits
                best_topic = topic
        return best_topic
