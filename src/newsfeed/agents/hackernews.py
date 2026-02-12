"""Hacker News agent — uses the free Firebase-backed HN API.

No API key required. Docs: https://github.com/HackerNewsAPI/HN-API
Endpoints: https://hacker-news.firebaseio.com/v0/
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

from newsfeed.agents.base import ResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask

log = logging.getLogger(__name__)

_API_BASE = "https://hacker-news.firebaseio.com/v0"


class HackerNewsAgent(ResearchAgent):
    """Fetches top and best stories from Hacker News.

    Specializes in technology, startups, AI/ML, science, and engineering
    discussion. Uses the official HN API (free, no key needed).
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 8) -> None:
        super().__init__(agent_id=agent_id, source="hackernews", mandate=mandate)
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        story_ids = self._fetch_story_ids()
        if not story_ids:
            return []

        # Fetch more than needed to allow filtering
        fetch_count = min(top_k * 3, len(story_ids), 30)
        candidates: list[CandidateItem] = []

        for sid in story_ids[:fetch_count]:
            item = self._fetch_item(sid)
            if not item:
                continue

            title = item.get("title", "")
            url = item.get("url", f"https://news.ycombinator.com/item?id={sid}")
            score = item.get("score", 0)
            descendants = item.get("descendants", 0)
            created_utc = item.get("time", 0)
            item_type = item.get("type", "story")

            if not title or item_type != "story":
                continue

            created_at = datetime.now(timezone.utc)
            if created_utc:
                try:
                    created_at = datetime.fromtimestamp(created_utc, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            # HN scores: 100+ is notable, 500+ is major, 1000+ is viral
            evidence = round(min(1.0, score / 800 + 0.35), 3)
            preference_fit = self._score_relevance(title, "", task.weighted_topics)
            # Discussion depth as prediction signal
            prediction = round(min(1.0, descendants / 500 + 0.25), 3)

            cid = hashlib.sha256(f"{self.agent_id}:{sid}".encode()).hexdigest()[:16]

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="hackernews",
                summary=f"HN score {score}, {descendants} comments",
                url=url,
                topic=self._infer_topic(title, task),
                evidence_score=evidence,
                novelty_score=round(max(0.4, 1.0 - len(candidates) * 0.06), 3),
                preference_fit=preference_fit,
                prediction_signal=prediction,
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        result = candidates[:top_k]
        log.info("HackerNews agent %s returned %d candidates", self.agent_id, len(result))
        return result

    def _fetch_story_ids(self) -> list[int]:
        """Fetch top story IDs (topstories endpoint returns up to 500)."""
        url = f"{_API_BASE}/topstories.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.error("HN topstories fetch failed: %s", e)
            return []

    def _fetch_item(self, item_id: int) -> dict | None:
        url = f"{_API_BASE}/item/{item_id}.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.warning("HN item %d fetch failed: %s", item_id, e)
            return None

    def _infer_topic(self, title: str, task: ResearchTask) -> str:
        text = title.lower()
        # Order matters: more specific topics checked first
        topic_keywords = {
            "ai_policy": ["openai", "anthropic", "deepmind", "gpt", "llm", "machine learning", "ai regulation", "ai safety"],
            "crypto": ["bitcoin", "ethereum", "crypto", "blockchain", "web3"],
            "climate": ["climate", "energy", "solar", "nuclear", "carbon"],
            "markets": ["ipo", "valuation", "startup", "funding", "revenue", "stock", "market", "vc"],
            "science": ["research", "paper", "study", "physics", "biology", "nature", "science"],
            "technology": ["gpu", "chip", "software", "api", "open source", "programming", "rust", "python", "cyber"],
        }
        for topic, keywords in topic_keywords.items():
            if any(kw in text for kw in keywords):
                return topic
        return max(task.weighted_topics, key=task.weighted_topics.get, default="technology")
