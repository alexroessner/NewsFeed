"""Agent registry — creates the right agent type based on source and available API keys.

Routes each configured agent to its real implementation when API keys are available,
and falls back to SimulatedResearchAgent when they are not.

Free agents (no API key needed): BBC RSS, HackerNews, Al Jazeera RSS, arXiv, GDELT,
                                  Google News RSS (web search), NPR RSS, CNBC RSS,
                                  France 24 RSS, TechCrunch RSS, Nature RSS.
Keyed agents: Guardian, Reddit, NewsAPI (Reuters/AP/FT), X/Twitter.
"""
from __future__ import annotations

import logging

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.simulated import SimulatedResearchAgent

log = logging.getLogger(__name__)


def create_agent(agent_cfg: dict, api_keys: dict) -> ResearchAgent:
    """Create a research agent based on its source and available API keys.

    Falls back to SimulatedResearchAgent when no API key is available
    for sources that require one.
    """
    agent_id = agent_cfg["id"]
    source = agent_cfg["source"]
    mandate = agent_cfg["mandate"]

    # ── Free agents (no API key needed) ──────────────────────────

    if source == "bbc":
        from newsfeed.agents.bbc import BBCAgent
        return BBCAgent(agent_id=agent_id, mandate=mandate)

    if source == "hackernews":
        from newsfeed.agents.hackernews import HackerNewsAgent
        return HackerNewsAgent(agent_id=agent_id, mandate=mandate)

    if source == "aljazeera":
        from newsfeed.agents.aljazeera import AlJazeeraAgent
        return AlJazeeraAgent(agent_id=agent_id, mandate=mandate)

    if source == "arxiv":
        from newsfeed.agents.arxiv import ArXivAgent
        return ArXivAgent(agent_id=agent_id, mandate=mandate)

    if source == "gdelt":
        from newsfeed.agents.gdelt import GDELTAgent
        return GDELTAgent(agent_id=agent_id, mandate=mandate)

    if source == "npr":
        from newsfeed.agents.npr import NPRAgent
        return NPRAgent(agent_id=agent_id, mandate=mandate)

    if source == "cnbc":
        from newsfeed.agents.cnbc import CNBCAgent
        return CNBCAgent(agent_id=agent_id, mandate=mandate)

    if source == "france24":
        from newsfeed.agents.france24 import France24Agent
        return France24Agent(agent_id=agent_id, mandate=mandate)

    if source == "techcrunch":
        from newsfeed.agents.techcrunch import TechCrunchAgent
        return TechCrunchAgent(agent_id=agent_id, mandate=mandate)

    if source == "nature":
        from newsfeed.agents.nature_rss import NatureAgent
        return NatureAgent(agent_id=agent_id, mandate=mandate)

    # ── Keyed agents ─────────────────────────────────────────────

    if source == "guardian" and api_keys.get("guardian"):
        from newsfeed.agents.guardian import GuardianAgent
        return GuardianAgent(
            agent_id=agent_id,
            mandate=mandate,
            api_key=api_keys["guardian"],
        )

    if source == "reddit" and api_keys.get("reddit_client_id") and api_keys.get("reddit_client_secret"):
        from newsfeed.agents.reddit import RedditAgent
        return RedditAgent(
            agent_id=agent_id,
            mandate=mandate,
            client_id=api_keys["reddit_client_id"],
            client_secret=api_keys["reddit_client_secret"],
        )

    if source in ("reuters", "ap", "ft") and api_keys.get("newsapi"):
        from newsfeed.agents.newsapi import NewsAPIAgent
        return NewsAPIAgent(
            agent_id=agent_id,
            source=source,
            mandate=mandate,
            api_key=api_keys["newsapi"],
        )

    if source == "x" and api_keys.get("x_bearer_token"):
        from newsfeed.agents.xtwitter import XTwitterAgent
        return XTwitterAgent(
            agent_id=agent_id,
            mandate=mandate,
            bearer_token=api_keys["x_bearer_token"],
        )

    # X fallback: use NewsAPI if no direct X token
    if source == "x" and api_keys.get("newsapi"):
        from newsfeed.agents.newsapi import NewsAPIAgent
        return NewsAPIAgent(
            agent_id=agent_id,
            source="x",
            mandate=mandate,
            api_key=api_keys["newsapi"],
        )

    if source == "web":
        # Google News RSS is free — always use real agent
        from newsfeed.agents.websearch import WebSearchAgent
        return WebSearchAgent(agent_id=agent_id, mandate=mandate)

    # ── Fallback to simulated ────────────────────────────────────

    if source == "guardian" and not api_keys.get("guardian"):
        log.info("No Guardian API key, using simulated agent for %s", agent_id)
    elif source == "reddit":
        log.info("No Reddit credentials, using simulated agent for %s", agent_id)
    elif source in ("reuters", "ap", "ft"):
        log.info("No NewsAPI key, using simulated agent for %s (source=%s)", agent_id, source)
    elif source == "x":
        log.info("No X bearer token or NewsAPI key, using simulated agent for %s", agent_id)
    else:
        log.info("Unknown source=%s, using simulated agent for %s", source, agent_id)

    return SimulatedResearchAgent(agent_id=agent_id, source=source, mandate=mandate)
