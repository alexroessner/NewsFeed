"""Agent registry — creates the right agent type based on source and available API keys."""
from __future__ import annotations

import logging

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.simulated import SimulatedResearchAgent

log = logging.getLogger(__name__)


def create_agent(agent_cfg: dict, api_keys: dict) -> ResearchAgent:
    """Create a research agent based on its source and available API keys.

    Falls back to SimulatedResearchAgent when no API key is available.
    """
    agent_id = agent_cfg["id"]
    source = agent_cfg["source"]
    mandate = agent_cfg["mandate"]

    if source == "guardian" and api_keys.get("guardian"):
        from newsfeed.agents.guardian import GuardianAgent
        return GuardianAgent(
            agent_id=agent_id,
            mandate=mandate,
            api_key=api_keys["guardian"],
        )

    if source == "bbc":
        # BBC RSS feeds are free — no API key needed
        from newsfeed.agents.bbc import BBCAgent
        return BBCAgent(agent_id=agent_id, mandate=mandate)

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

    if source == "x" and api_keys.get("newsapi"):
        # X/Twitter content via NewsAPI as aggregator fallback
        from newsfeed.agents.newsapi import NewsAPIAgent
        return NewsAPIAgent(
            agent_id=agent_id,
            source="x",
            mandate=mandate,
            api_key=api_keys["newsapi"],
        )

    if source == "web" and api_keys.get("newsapi"):
        from newsfeed.agents.newsapi import NewsAPIAgent
        return NewsAPIAgent(
            agent_id=agent_id,
            source="web",
            mandate=mandate,
            api_key=api_keys["newsapi"],
        )

    # Fallback: simulated agent
    log.info("No API key for source=%s, using simulated agent for %s", source, agent_id)
    return SimulatedResearchAgent(agent_id=agent_id, source=source, mandate=mandate)
