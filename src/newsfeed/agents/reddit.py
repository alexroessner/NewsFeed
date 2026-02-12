"""Reddit API agent using OAuth2 app-only flow.

Requires client_id and client_secret from https://www.reddit.com/prefs/apps
Create a "script" type app. Set via config keys: api_keys.reddit_client_id, api_keys.reddit_client_secret
"""
from __future__ import annotations

import base64
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

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API_BASE = "https://oauth.reddit.com"

_SUBREDDIT_MAP: dict[str, list[str]] = {
    "geopolitics": ["geopolitics", "worldnews", "internationalaffairs"],
    "ai_policy": ["artificial", "MachineLearning", "AIPolicy"],
    "technology": ["technology", "Futurology", "tech"],
    "markets": ["economics", "finance", "wallstreetbets"],
    "crypto": ["CryptoCurrency", "bitcoin", "ethereum"],
    "science": ["science", "EverythingScience"],
    "climate": ["climate", "environment", "energy"],
    "health": ["health", "medicine"],
}


class RedditAgent(ResearchAgent):
    def __init__(
        self, agent_id: str, mandate: str,
        client_id: str, client_secret: str,
        timeout: int = 10,
    ) -> None:
        super().__init__(agent_id=agent_id, source="reddit", mandate=mandate)
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token: str | None = None

    def _authenticate(self) -> bool:
        """Get an app-only OAuth2 token."""
        if self._token:
            return True
        try:
            creds = base64.b64encode(
                f"{self._client_id}:{self._client_secret}".encode()
            ).decode()
            data = urlencode({"grant_type": "client_credentials"}).encode()
            req = urllib.request.Request(
                _TOKEN_URL,
                data=data,
                headers={
                    "Authorization": f"Basic {creds}",
                    "User-Agent": "NewsFeed/1.0 (research agent)",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self._token = body.get("access_token")
            if not self._token:
                log.error("Reddit OAuth returned no access_token: %s", body)
                return False
            log.info("Reddit OAuth token acquired")
            return True
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.error("Reddit OAuth failed: %s", e)
            return False

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        if not self._authenticate():
            log.warning("Reddit agent %s: auth failed, returning empty", self.agent_id)
            return []

        subreddits = self._pick_subreddits(task.weighted_topics)
        all_items: list[CandidateItem] = []

        for sub in subreddits:
            items = self._fetch_subreddit(sub, task, limit=top_k)
            all_items.extend(items)

        # Dedupe
        seen: set[str] = set()
        deduped: list[CandidateItem] = []
        for item in all_items:
            key = item.title.lower().strip()
            if key not in seen:
                seen.add(key)
                deduped.append(item)

        deduped.sort(key=lambda c: c.composite_score(), reverse=True)
        result = deduped[:top_k]
        log.info("Reddit agent %s returned %d candidates from %d subreddits", self.agent_id, len(result), len(subreddits))
        return result

    def _pick_subreddits(self, weighted_topics: dict[str, float]) -> list[str]:
        """Pick subreddits relevant to the user's topics."""
        subs: list[str] = []
        for topic in sorted(weighted_topics, key=weighted_topics.get, reverse=True)[:3]:
            candidates = _SUBREDDIT_MAP.get(topic, [])
            for sub in candidates:
                if sub not in subs:
                    subs.append(sub)
                if len(subs) >= 5:
                    break
            if len(subs) >= 5:
                break
        return subs or ["worldnews", "technology"]

    def _fetch_subreddit(self, subreddit: str, task: ResearchTask, limit: int = 5) -> list[CandidateItem]:
        url = f"{_API_BASE}/r/{subreddit}/hot?limit={limit}&raw_json=1"
        try:
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "NewsFeed/1.0 (research agent)",
            })
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.error("Reddit fetch failed for r/%s: %s", subreddit, e)
            return []

        posts = data.get("data", {}).get("children", [])
        candidates: list[CandidateItem] = []

        for idx, post_wrapper in enumerate(posts):
            post = post_wrapper.get("data", {})
            title = post.get("title", "")
            selftext = post.get("selftext", "")[:300]
            permalink = post.get("permalink", "")
            score = post.get("score", 0)
            num_comments = post.get("num_comments", 0)
            created_utc = post.get("created_utc", 0)

            if not title or post.get("stickied"):
                continue

            url_full = f"https://reddit.com{permalink}" if permalink else ""
            # Use Reddit score as evidence proxy (normalized)
            evidence = round(min(1.0, score / 5000 + 0.3), 3)
            novelty = round(max(0.3, 1.0 - idx * 0.1), 3)
            preference_fit = self._score_relevance(title, selftext, task.weighted_topics)

            created_at = datetime.now(timezone.utc)
            if created_utc:
                try:
                    created_at = datetime.fromtimestamp(created_utc, tz=timezone.utc)
                except (ValueError, OSError):
                    pass

            cid = hashlib.sha256(f"{self.agent_id}:{permalink}".encode()).hexdigest()[:16]

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title,
                source="reddit",
                summary=selftext[:300] or f"r/{subreddit}: {num_comments} comments, score {score}",
                url=url_full,
                topic=self._subreddit_to_topic(subreddit),
                evidence_score=evidence,
                novelty_score=novelty,
                preference_fit=preference_fit,
                prediction_signal=round(min(1.0, num_comments / 1000 + 0.2), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
            ))

        return candidates

    def _subreddit_to_topic(self, subreddit: str) -> str:
        for topic, subs in _SUBREDDIT_MAP.items():
            if subreddit.lower() in [s.lower() for s in subs]:
                return topic
        return "general"
