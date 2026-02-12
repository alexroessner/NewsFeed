from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.bbc import BBCAgent, _FEED_TOPIC_MAP
from newsfeed.agents.guardian import GuardianAgent
from newsfeed.agents.newsapi import NewsAPIAgent
from newsfeed.agents.reddit import RedditAgent, _SUBREDDIT_MAP
from newsfeed.agents.registry import create_agent
from newsfeed.agents.simulated import ExpertCouncil, SimulatedResearchAgent
from newsfeed.models.domain import CandidateItem, ResearchTask


def _make_candidate(cid: str = "c1", score_offset: float = 0.0) -> CandidateItem:
    return CandidateItem(
        candidate_id=cid, title=f"Signal {cid}", source="reuters",
        summary="Summary", url="https://example.com", topic="geopolitics",
        evidence_score=0.7 + score_offset, novelty_score=0.6 + score_offset,
        preference_fit=0.8, prediction_signal=0.5 + score_offset,
        discovered_by="agent",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )


class ExpertCouncilTests(unittest.TestCase):
    def test_majority_voting(self) -> None:
        council = ExpertCouncil(
            expert_ids=["e1", "e2", "e3"],
            keep_threshold=0.5,
            min_votes_to_accept="majority",
        )
        self.assertEqual(council._required_votes(), 2)

    def test_unanimous_voting(self) -> None:
        council = ExpertCouncil(
            expert_ids=["e1", "e2", "e3", "e4"],
            min_votes_to_accept="unanimous",
        )
        self.assertEqual(council._required_votes(), 4)

    def test_fixed_n_voting(self) -> None:
        council = ExpertCouncil(
            expert_ids=["e1", "e2", "e3", "e4", "e5"],
            min_votes_to_accept="3",
        )
        self.assertEqual(council._required_votes(), 3)

    def test_invalid_voting_falls_back_to_majority(self) -> None:
        council = ExpertCouncil(
            expert_ids=["e1", "e2", "e3"],
            min_votes_to_accept="invalid",
        )
        self.assertEqual(council._required_votes(), 2)

    def test_fixed_n_clamped_to_expert_count(self) -> None:
        council = ExpertCouncil(
            expert_ids=["e1", "e2"],
            min_votes_to_accept="10",
        )
        # Should clamp to 2 (expert count), not 10
        self.assertEqual(council._required_votes(), 2)

    def test_configurable_keep_threshold(self) -> None:
        high_bar = ExpertCouncil(keep_threshold=0.95)
        low_bar = ExpertCouncil(keep_threshold=0.3)
        candidates = [_make_candidate(f"c{i}") for i in range(5)]

        _, _, debate_high = high_bar.select(candidates, max_items=5)
        _, _, debate_low = low_bar.select(candidates, max_items=5)

        keep_high = sum(1 for v in debate_high.votes if v.keep)
        keep_low = sum(1 for v in debate_low.votes if v.keep)
        self.assertGreaterEqual(keep_low, keep_high)

    def test_confidence_bounds(self) -> None:
        council = ExpertCouncil(confidence_min=0.6, confidence_max=0.9)
        candidates = [_make_candidate()]
        _, _, debate = council.select(candidates, max_items=1)
        for vote in debate.votes:
            self.assertGreaterEqual(vote.confidence, 0.6)
            self.assertLessEqual(vote.confidence, 0.9)

    def test_select_respects_max_items(self) -> None:
        council = ExpertCouncil(keep_threshold=0.3)
        candidates = [_make_candidate(f"c{i}") for i in range(10)]
        selected, reserve, _ = council.select(candidates, max_items=3)
        self.assertLessEqual(len(selected), 3)
        self.assertIsInstance(reserve, list)

    def test_debate_produces_votes_for_all_experts(self) -> None:
        council = ExpertCouncil(expert_ids=["e1", "e2", "e3", "e4"])
        candidates = [_make_candidate()]
        debate = council.debate(candidates)
        self.assertEqual(len(debate.votes), 4)


class SimulatedResearchAgentTests(unittest.TestCase):
    def test_deterministic_output(self) -> None:
        agent = SimulatedResearchAgent("a1", "reuters", "Track geopolitics")
        task = ResearchTask(request_id="r1", user_id="u1", prompt="geo", weighted_topics={"geopolitics": 0.9})
        run1 = agent.run(task, top_k=3)
        run2 = agent.run(task, top_k=3)
        self.assertEqual(
            [c.candidate_id for c in run1],
            [c.candidate_id for c in run2],
        )

    def test_top_k_respected(self) -> None:
        agent = SimulatedResearchAgent("a1", "reuters", "mandate")
        task = ResearchTask(request_id="r1", user_id="u1", prompt="test", weighted_topics={"tech": 0.5})
        results = agent.run(task, top_k=7)
        self.assertEqual(len(results), 7)

    def test_candidates_sorted_by_score(self) -> None:
        agent = SimulatedResearchAgent("a1", "reuters", "mandate")
        task = ResearchTask(request_id="r1", user_id="u1", prompt="test", weighted_topics={"tech": 0.5})
        results = agent.run(task, top_k=5)
        scores = [c.composite_score() for c in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


class BaseAgentTests(unittest.TestCase):
    def test_cannot_instantiate_abstract(self) -> None:
        with self.assertRaises(TypeError):
            ResearchAgent("a1", "test", "mandate")

    def test_score_relevance_with_matching_topic(self) -> None:
        # Use BBCAgent which inherits _score_relevance from base
        agent = BBCAgent("a1", "mandate")
        score = agent._score_relevance(
            "China tensions escalate in geopolitics crisis",
            "Geopolitical tensions in Asia",
            {"geopolitics": 0.9, "markets": 0.3},
        )
        self.assertGreater(score, 0.2)  # Should be above baseline
        self.assertLessEqual(score, 1.0)

    def test_score_relevance_no_match(self) -> None:
        agent = BBCAgent("a1", "mandate")
        score = agent._score_relevance(
            "Local weather report sunny skies",
            "Temperature forecast for tomorrow",
            {"geopolitics": 0.9},
        )
        # Should be baseline 0.2 with no keyword matches
        self.assertEqual(score, 0.2)

    def test_score_relevance_empty_topics(self) -> None:
        agent = BBCAgent("a1", "mandate")
        score = agent._score_relevance("Some title", "Some text", {})
        self.assertEqual(score, 0.2)


class RegistryTests(unittest.TestCase):
    def test_bbc_always_real(self) -> None:
        cfg = {"id": "bbc1", "source": "bbc", "mandate": "Track news"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, BBCAgent)

    def test_guardian_with_key(self) -> None:
        cfg = {"id": "g1", "source": "guardian", "mandate": "Track news"}
        agent = create_agent(cfg, {"guardian": "test-key"})
        self.assertIsInstance(agent, GuardianAgent)

    def test_guardian_without_key_falls_back(self) -> None:
        cfg = {"id": "g1", "source": "guardian", "mandate": "Track news"}
        agent = create_agent(cfg, {"guardian": ""})
        self.assertIsInstance(agent, SimulatedResearchAgent)

    def test_reddit_with_credentials(self) -> None:
        cfg = {"id": "r1", "source": "reddit", "mandate": "Track discussions"}
        agent = create_agent(cfg, {"reddit_client_id": "cid", "reddit_client_secret": "csecret"})
        self.assertIsInstance(agent, RedditAgent)

    def test_reddit_without_credentials_falls_back(self) -> None:
        cfg = {"id": "r1", "source": "reddit", "mandate": "Track discussions"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, SimulatedResearchAgent)

    def test_newsapi_sources_with_key(self) -> None:
        for source in ["reuters", "ap", "ft"]:
            cfg = {"id": f"{source}1", "source": source, "mandate": "Track news"}
            agent = create_agent(cfg, {"newsapi": "test-key"})
            self.assertIsInstance(agent, NewsAPIAgent, f"Failed for {source}")

    def test_newsapi_sources_without_key_falls_back(self) -> None:
        cfg = {"id": "r1", "source": "reuters", "mandate": "Track news"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, SimulatedResearchAgent)

    def test_unknown_source_falls_back(self) -> None:
        cfg = {"id": "u1", "source": "unknown_source", "mandate": "mandate"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, SimulatedResearchAgent)

    def test_x_source_with_newsapi_key(self) -> None:
        cfg = {"id": "x1", "source": "x", "mandate": "Track social"}
        agent = create_agent(cfg, {"newsapi": "key"})
        self.assertIsInstance(agent, NewsAPIAgent)

    def test_web_source_with_newsapi_key(self) -> None:
        cfg = {"id": "w1", "source": "web", "mandate": "Track web"}
        agent = create_agent(cfg, {"newsapi": "key"})
        self.assertIsInstance(agent, NewsAPIAgent)


class GuardianAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = GuardianAgent("g1", "Track world news", "fake-key")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8, "markets": 0.5},
        )

    def test_build_query(self) -> None:
        query = self.agent._build_query(self.task)
        self.assertIn("geopolitics", query)
        self.assertIn("OR", query)

    def test_map_section_to_topic(self) -> None:
        self.assertEqual(self.agent._map_section_to_topic("world"), "geopolitics")
        self.assertEqual(self.agent._map_section_to_topic("business"), "markets")
        self.assertEqual(self.agent._map_section_to_topic("technology"), "technology")
        self.assertEqual(self.agent._map_section_to_topic("unknown"), "unknown")

    @patch("newsfeed.agents.guardian.urllib.request.urlopen")
    def test_run_parses_guardian_response(self, mock_urlopen) -> None:
        response_data = {
            "response": {
                "status": "ok",
                "results": [
                    {
                        "webTitle": "Test Article",
                        "webUrl": "https://guardian.com/test",
                        "sectionId": "world",
                        "webPublicationDate": "2025-01-15T10:00:00Z",
                        "fields": {
                            "headline": "Test Headline",
                            "trailText": "Test summary text",
                        },
                    },
                    {
                        "webTitle": "Second Article",
                        "webUrl": "https://guardian.com/test2",
                        "sectionId": "business",
                        "webPublicationDate": "2025-01-15T09:00:00Z",
                        "fields": {
                            "headline": "Market Update",
                            "trailText": "Markets summary",
                        },
                    },
                ],
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "guardian")
        self.assertTrue(all(r.candidate_id.startswith("g1-") for r in results))

    @patch("newsfeed.agents.guardian.urllib.request.urlopen")
    def test_run_handles_api_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = urllib_error()
        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(results, [])


class BBCAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = BBCAgent("bbc1", "Track world news")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8, "technology": 0.5},
        )

    def test_select_feeds_includes_top(self) -> None:
        feeds = self.agent._select_feeds({"geopolitics": 0.9})
        self.assertIn("top", feeds)

    def test_select_feeds_relevance(self) -> None:
        feeds = self.agent._select_feeds({"geopolitics": 0.9, "technology": 0.1})
        self.assertLessEqual(len(feeds), 3)

    @patch("newsfeed.agents.bbc.urlopen")
    def test_fetch_feed_parses_rss(self, mock_urlopen) -> None:
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
            <channel>
                <title>BBC News</title>
                <item>
                    <title>Test Story</title>
                    <description>Test description</description>
                    <link>https://bbc.co.uk/news/test</link>
                    <pubDate>Wed, 15 Jan 2025 10:00:00 GMT</pubDate>
                </item>
                <item>
                    <title>Another Story</title>
                    <description>Another description</description>
                    <link>https://bbc.co.uk/news/test2</link>
                    <pubDate>Wed, 15 Jan 2025 09:00:00 GMT</pubDate>
                </item>
            </channel>
        </rss>"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = rss_xml.encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        items = self.agent._fetch_feed("world", "https://feeds.bbci.co.uk/news/world/rss.xml", self.task)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].source, "bbc")
        self.assertEqual(items[0].topic, "geopolitics")

    def test_feed_topic_map_completeness(self) -> None:
        # Every feed in the agent's default list should have a topic mapping
        for feed_name in ["top", "world", "business", "technology"]:
            self.assertIn(feed_name, _FEED_TOPIC_MAP)


class RedditAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = RedditAgent("r1", "Track discussions", "client_id", "client_secret")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8, "technology": 0.5},
        )

    def test_pick_subreddits_for_geopolitics(self) -> None:
        subs = self.agent._pick_subreddits({"geopolitics": 0.9})
        self.assertIn("geopolitics", subs)
        self.assertIn("worldnews", subs)

    def test_pick_subreddits_fallback(self) -> None:
        subs = self.agent._pick_subreddits({"unknown_topic_xyz": 0.5})
        # Should fall back to defaults
        self.assertEqual(subs, ["worldnews", "technology"])

    def test_pick_subreddits_max_5(self) -> None:
        topics = {t: 0.5 for t in _SUBREDDIT_MAP}
        subs = self.agent._pick_subreddits(topics)
        self.assertLessEqual(len(subs), 5)

    def test_subreddit_to_topic(self) -> None:
        self.assertEqual(self.agent._subreddit_to_topic("worldnews"), "geopolitics")
        self.assertEqual(self.agent._subreddit_to_topic("MachineLearning"), "ai_policy")
        self.assertEqual(self.agent._subreddit_to_topic("unknown_sub"), "general")

    def test_auth_failure_returns_empty(self) -> None:
        # Without a real API, auth will fail, so run should return empty
        results = self.agent.run(self.task, top_k=3)
        self.assertEqual(results, [])


class NewsAPIAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = NewsAPIAgent("n1", "reuters", "Track news", "fake-key")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8},
        )

    def test_build_query(self) -> None:
        query = self.agent._build_query(self.task)
        self.assertIn("geopolitics", query)

    def test_source_evidence_tier1(self) -> None:
        self.assertEqual(self.agent._source_evidence("Reuters"), 0.85)
        self.assertEqual(self.agent._source_evidence("Associated Press"), 0.85)

    def test_source_evidence_unknown(self) -> None:
        self.assertEqual(self.agent._source_evidence("Random Blog"), 0.65)

    def test_infer_topic(self) -> None:
        topic = self.agent._infer_topic(
            "China geopolitics tensions",
            "Geopolitical standoff in Asia",
            self.task,
        )
        self.assertEqual(topic, "geopolitics")

    @patch("newsfeed.agents.newsapi.urllib.request.urlopen")
    def test_run_parses_newsapi_response(self, mock_urlopen) -> None:
        response_data = {
            "status": "ok",
            "totalResults": 2,
            "articles": [
                {
                    "title": "Reuters Headline",
                    "description": "Article summary",
                    "url": "https://reuters.com/test",
                    "publishedAt": "2025-01-15T10:00:00Z",
                    "source": {"id": "reuters", "name": "Reuters"},
                },
                {
                    "title": "Another Article",
                    "description": "More news",
                    "url": "https://reuters.com/test2",
                    "publishedAt": "2025-01-15T09:00:00Z",
                    "source": {"id": "reuters", "name": "Reuters"},
                },
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "reuters")

    @patch("newsfeed.agents.newsapi.urllib.request.urlopen")
    def test_run_handles_api_error_response(self, mock_urlopen) -> None:
        response_data = {"status": "error", "message": "API key invalid"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(results, [])

    @patch("newsfeed.agents.newsapi.urllib.request.urlopen")
    def test_filters_removed_articles(self, mock_urlopen) -> None:
        response_data = {
            "status": "ok",
            "articles": [
                {"title": "[Removed]", "description": "", "url": "https://example.com", "source": {"name": "X"}},
                {"title": "Valid Headline", "description": "desc", "url": "https://example.com/2", "source": {"name": "Reuters"}},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Valid Headline")


def urllib_error():
    """Create a URLError for testing."""
    import urllib.error
    return urllib.error.URLError("Connection failed")


if __name__ == "__main__":
    unittest.main()
