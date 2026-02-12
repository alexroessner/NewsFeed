"""X (Twitter) API v2 agent — real-time social signal monitoring.

Requires a Bearer Token from https://developer.x.com/en/portal/dashboard
Free tier: 500K tweets read/month, search limited to 7 days.
Basic tier ($100/mo): 10K tweets read/month with full search.

Set via config key: api_keys.x_bearer_token
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

_API_BASE = "https://api.twitter.com/2"

# High-signal accounts by vertical (curated for quality, not volume)
_TOPIC_ACCOUNTS: dict[str, list[str]] = {
    "geopolitics": ["ReutersWorld", "BBCWorld", "AP", "ForeignAffairs", "CFaborAmong"],
    "ai_policy": ["AnthropicAI", "OpenAI", "DeepMind", "ylecun", "kaboré"],
    "technology": ["TechCrunch", "veraborge", "WIRED", "aaborstrom"],
    "markets": ["markets", "FT", "business", "WSJ", "economics"],
    "crypto": ["coindesk", "ethereum", "VitalikButerin"],
    "climate": ["CarbonBrief", "IPCC_CH", "ClimateHome"],
    "science": ["Nature", "ScienceMagazine", "NewEnglandJournal"],
    "health": ["WHO", "CDCgov", "TheLancet"],
}

# Keywords for keyword-based search when no accounts match
_TOPIC_SEARCH_TERMS: dict[str, str] = {
    "geopolitics": "geopolitics OR sanctions OR diplomacy OR conflict -is:retweet lang:en",
    "ai_policy": "AI regulation OR LLM OR artificial intelligence policy -is:retweet lang:en",
    "technology": "technology breakthrough OR cybersecurity OR open source -is:retweet lang:en",
    "markets": "markets OR economy OR federal reserve OR GDP -is:retweet lang:en",
    "crypto": "cryptocurrency OR bitcoin OR ethereum regulation -is:retweet lang:en",
    "climate": "climate change OR renewable energy OR carbon emissions -is:retweet lang:en",
    "science": "scientific breakthrough OR research paper OR discovery -is:retweet lang:en",
    "health": "pandemic OR health policy OR WHO OR medical breakthrough -is:retweet lang:en",
}


class XTwitterAgent(ResearchAgent):
    """Monitors X (Twitter) for high-signal posts using the v2 API.

    Combines curated account monitoring with keyword search to surface
    breaking developments, expert commentary, and crowd sentiment shifts.
    Posts are scored by engagement metrics (likes, retweets, quotes)
    as evidence proxies.
    """

    def __init__(
        self,
        agent_id: str,
        mandate: str,
        bearer_token: str,
        timeout: int = 10,
    ) -> None:
        super().__init__(agent_id=agent_id, source="x", mandate=mandate)
        self._bearer_token = bearer_token
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        query = self._build_search_query(task)
        tweets = self._search_recent(query, max_results=min(top_k * 3, 100))

        if not tweets:
            log.info("X agent %s: no tweets found for query", self.agent_id)
            return []

        candidates: list[CandidateItem] = []
        seen_texts: set[str] = set()

        for idx, tweet in enumerate(tweets):
            text = tweet.get("text", "")
            tweet_id = tweet.get("id", "")
            created_str = tweet.get("created_at", "")
            metrics = tweet.get("public_metrics", {})

            if not text or len(text) < 30:
                continue

            # Dedupe by first 80 chars normalized
            text_key = text[:80].lower().strip()
            if text_key in seen_texts:
                continue
            seen_texts.add(text_key)

            likes = metrics.get("like_count", 0)
            retweets = metrics.get("retweet_count", 0)
            replies = metrics.get("reply_count", 0)
            quotes = metrics.get("quote_count", 0)
            engagement = likes + retweets * 2 + replies + quotes * 3

            created_at = datetime.now(timezone.utc)
            if created_str:
                try:
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            # Engagement as evidence proxy: 100+ is notable, 1K+ is significant
            evidence = round(min(1.0, engagement / 2000 + 0.30), 3)
            novelty = round(max(0.3, 1.0 - idx * 0.07), 3)
            preference_fit = self._score_relevance(text, "", task.weighted_topics)
            # Discussion intensity as prediction signal
            prediction = round(min(1.0, (replies + quotes) / 500 + 0.25), 3)

            tweet_url = f"https://x.com/i/status/{tweet_id}"
            cid = hashlib.sha256(f"{self.agent_id}:{tweet_id}".encode()).hexdigest()[:16]

            # Extract title (first sentence or first 120 chars)
            title = self._extract_title(text)

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="x",
                summary=text[:300],
                url=tweet_url,
                topic=self._infer_topic(text, task),
                evidence_score=evidence,
                novelty_score=novelty,
                preference_fit=preference_fit,
                prediction_signal=prediction,
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        result = candidates[:top_k]
        log.info("X agent %s returned %d candidates", self.agent_id, len(result))
        return result

    def _search_recent(self, query: str, max_results: int = 30) -> list[dict]:
        """Search recent tweets using v2 /tweets/search/recent endpoint."""
        params = urlencode({
            "query": query,
            "max_results": str(min(max_results, 100)),
            "tweet.fields": "created_at,public_metrics,author_id,lang",
            "sort_order": "relevancy",
        })
        url = f"{_API_BASE}/tweets/search/recent?{params}"

        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "User-Agent": "NewsFeed/1.0",
            })
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.error("X API search failed: %s", e)
            return []

        if "errors" in data:
            for err in data["errors"]:
                log.warning("X API error: %s", err.get("message", "unknown"))

        return data.get("data", [])

    def _build_search_query(self, task: ResearchTask) -> str:
        """Build X search query from weighted topics."""
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:2]

        # Use pre-built search templates if available
        for topic in top_topics:
            if topic in _TOPIC_SEARCH_TERMS:
                return _TOPIC_SEARCH_TERMS[topic]

        # Fallback: keyword-based query
        keywords = []
        for topic in top_topics:
            keywords.extend(topic.replace("_", " ").split())
        query_terms = " OR ".join(keywords[:4])
        return f"({query_terms}) -is:retweet lang:en"

    def _extract_title(self, text: str) -> str:
        """Extract a clean title from tweet text."""
        # Remove URLs
        import re
        clean = re.sub(r"https?://\S+", "", text).strip()
        # First sentence or first 120 chars
        for delim in [".", "!", "?"]:
            pos = clean.find(delim)
            if 20 < pos < 150:
                return clean[:pos + 1]
        return clean[:120] + ("..." if len(clean) > 120 else "")

    def _infer_topic(self, text: str, task: ResearchTask) -> str:
        text_lower = text.lower()
        topic_keywords = {
            "geopolitics": ["war", "sanctions", "diplomacy", "conflict", "nato", "military"],
            "ai_policy": ["ai ", "llm", "gpt", "artificial intelligence", "machine learning"],
            "technology": ["tech", "software", "cyber", "open source", "startup"],
            "markets": ["market", "economy", "fed", "gdp", "inflation", "stocks"],
            "crypto": ["bitcoin", "ethereum", "crypto", "blockchain"],
            "climate": ["climate", "carbon", "renewable", "emissions"],
        }
        for topic, keywords in topic_keywords.items():
            if any(kw in text_lower for kw in keywords):
                return topic
        return max(task.weighted_topics, key=task.weighted_topics.get, default="general")
