"""GDELT Project agent — global event monitoring via free API.

No API key required. Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
GDELT monitors broadcast, print, and web news from nearly every country,
providing real-time event detection and geographic intelligence.
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

_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
_GEO_API = "https://api.gdeltproject.org/api/v2/geo/geo"

# GDELT themes to internal topic mapping
_THEME_TOPIC_MAP: dict[str, str] = {
    "TAX_": "markets",
    "ECON_": "markets",
    "WB_": "markets",
    "ENV_": "climate",
    "TERROR": "geopolitics",
    "PROTEST": "geopolitics",
    "MILITARY": "geopolitics",
    "REBELLION": "geopolitics",
    "CRISISLEX": "geopolitics",
    "HEALTH_": "health",
    "SCIENCE": "science",
    "CYBER": "technology",
    "AI": "ai_policy",
}


class GDELTAgent(ResearchAgent):
    """Monitors global events via the GDELT 2.0 Doc API.

    GDELT processes news from 100+ languages across virtually every country.
    Provides unmatched breadth for geo-risk signals, conflict tracking,
    and emerging crises detection.
    """

    def __init__(self, agent_id: str, mandate: str, timeout: int = 12) -> None:
        super().__init__(agent_id=agent_id, source="gdelt", mandate=mandate)
        self._timeout = timeout

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        query = self._build_query(task)
        params = urlencode({
            "query": query,
            "mode": "ArtList",
            "maxrecords": str(min(top_k * 3, 75)),
            "format": "json",
            "sort": "DateDesc",
            "timespan": "24h",
        })
        url = f"{_DOC_API}?{params}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NewsFeed/1.0"})
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            log.error("GDELT API request failed: %s", e)
            return []

        articles = data.get("articles", [])
        if not articles:
            log.info("GDELT returned 0 articles for query=%r", query)
            return []

        candidates: list[CandidateItem] = []
        seen_titles: set[str] = set()

        for idx, article in enumerate(articles):
            title = article.get("title", "")
            article_url = article.get("url", "")
            source_name = article.get("domain", "unknown")
            seendate = article.get("seendate", "")
            language = article.get("language", "English")
            socialimage = article.get("socialimage", "")

            if not title:
                continue

            # Dedupe
            title_key = title.lower().strip()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            created_at = datetime.now(timezone.utc)
            if seendate:
                try:
                    created_at = datetime.strptime(seendate[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                except (ValueError, IndexError):
                    pass

            preference_fit = self._score_relevance(title, "", task.weighted_topics)
            topic = self._infer_topic(title, task)
            regions = self._detect_regions(title, source_name)
            cid = hashlib.sha256(f"{self.agent_id}:{article_url}".encode()).hexdigest()[:16]

            # GDELT articles from diverse sources — moderate base evidence
            evidence = 0.65 if language == "English" else 0.58

            # Content-aware scoring
            age_hours = max(0.1, (datetime.now(timezone.utc) - created_at).total_seconds() / 3600)
            recency_novelty = round(max(0.3, min(1.0, 1.0 - age_hours / 48)), 3)
            mandate_fit = self._mandate_boost(title, "")
            pred_boost = self._prediction_boost(title, "")

            candidates.append(CandidateItem(
                candidate_id=f"{self.agent_id}-{cid}",
                title=title[:200],
                source="gdelt",
                summary=f"via {source_name} ({language})",
                url=article_url,
                topic=topic,
                evidence_score=evidence,
                novelty_score=recency_novelty,
                preference_fit=round(min(1.0, preference_fit + mandate_fit), 3),
                prediction_signal=round(min(1.0, 0.45 + pred_boost), 3),
                discovered_by=self.agent_id,
                created_at=created_at,
                regions=regions,
            ))

        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        result = candidates[:top_k]
        log.info("GDELT agent %s returned %d candidates from %d articles", self.agent_id, len(result), len(articles))
        return result

    def _build_query(self, task: ResearchTask) -> str:
        top_topics = sorted(task.weighted_topics, key=task.weighted_topics.get, reverse=True)[:3]
        keywords = []
        for topic in top_topics:
            keywords.extend(topic.replace("_", " ").split())
        return " ".join(keywords[:6]) if keywords else task.prompt[:100]

    def _infer_topic(self, title: str, task: ResearchTask) -> str:
        text = title.lower()
        for prefix, topic in _THEME_TOPIC_MAP.items():
            if prefix.lower().rstrip("_") in text:
                return topic
        # Content-based fallback: scan title for topic keywords instead of
        # blindly using the user's highest-weighted topic (which caused
        # mismatches like "health" label on a banking/markets story).
        _TITLE_KEYWORDS: dict[str, list[str]] = {
            "markets": ["stock", "share", "bank", "rally", "investor", "trading",
                        "bond", "equity", "fund", "dow", "nasdaq", "ftse", "index"],
            "technology": ["tech", "software", "chip", "semiconductor", "app", "startup"],
            "geopolitics": ["war", "sanction", "diplomat", "treaty", "troop", "nato",
                            "summit", "election", "conflict", "border"],
            "health": ["vaccine", "disease", "hospital", "drug", "pandemic", "who"],
            "climate": ["climate", "emission", "carbon", "renewable", "wildfire"],
            "science": ["study", "research", "discovery", "nasa", "space", "genome"],
            "ai_policy": ["artificial intelligence", " ai ", "chatbot", "llm", "openai"],
        }
        for topic, keywords in _TITLE_KEYWORDS.items():
            if any(kw in text for kw in keywords):
                return topic
        return "geopolitics"

    def _detect_regions(self, title: str, source_domain: str) -> list[str]:
        text = f"{title} {source_domain}".lower()
        regions: list[str] = []
        region_keywords = {
            "middle_east": ["iran", "israel", "gaza", "syria", "iraq", "saudi", "yemen", "lebanon"],
            "europe": ["ukraine", "russia", "eu", "nato", "germany", "france", "uk"],
            "east_asia": ["china", "taiwan", "japan", "korea", "beijing"],
            "south_asia": ["india", "pakistan", "bangladesh"],
            "africa": ["nigeria", "ethiopia", "sudan", "kenya", "sahel", "congo"],
            "americas": ["us", "congress", "brazil", "mexico", "fed"],
        }
        for region, kws in region_keywords.items():
            if any(kw in text for kw in kws):
                regions.append(region)
        return regions
