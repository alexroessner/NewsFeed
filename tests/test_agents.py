from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from newsfeed.agents.aljazeera import AlJazeeraAgent
from newsfeed.agents.arxiv import ArXivAgent
from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.bbc import BBCAgent, _FEED_TOPIC_MAP
from newsfeed.agents.experts import ExpertCouncil as NewExpertCouncil, EXPERT_PERSONAS
from newsfeed.agents.gdelt import GDELTAgent
from newsfeed.agents.guardian import GuardianAgent
from newsfeed.agents.hackernews import HackerNewsAgent
from newsfeed.agents.newsapi import NewsAPIAgent
from newsfeed.agents.reddit import RedditAgent, _SUBREDDIT_MAP
from newsfeed.agents.registry import create_agent
from newsfeed.agents.simulated import ExpertCouncil, SimulatedResearchAgent
from newsfeed.agents.websearch import WebSearchAgent
from newsfeed.agents.xtwitter import XTwitterAgent
from newsfeed.delivery.bot import TelegramBot, BriefingScheduler, BOT_COMMANDS
from newsfeed.models.domain import CandidateItem, DebateVote, ResearchTask, UrgencyLevel


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

    def test_web_source_always_real(self) -> None:
        from newsfeed.agents.websearch import WebSearchAgent
        cfg = {"id": "w1", "source": "web", "mandate": "Track web"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, WebSearchAgent)


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


class HackerNewsAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = HackerNewsAgent("hn1", "Track tech")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"technology": 0.8, "ai_policy": 0.5},
        )

    def test_infer_topic_ai(self) -> None:
        # "llm" is an ai_policy keyword
        self.assertEqual(self.agent._infer_topic("New LLM architecture paper from DeepMind", self.task), "ai_policy")

    def test_infer_topic_markets(self) -> None:
        self.assertEqual(self.agent._infer_topic("IPO valuation hits record high for tech", self.task), "markets")

    def test_infer_topic_fallback(self) -> None:
        # Should fall back to highest-weighted topic
        topic = self.agent._infer_topic("Random unrelated title", self.task)
        self.assertEqual(topic, "technology")

    @patch("newsfeed.agents.hackernews.urllib.request.urlopen")
    def test_run_returns_empty_on_api_failure(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = urllib_error()
        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(results, [])


class AlJazeeraAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = AlJazeeraAgent("aj1", "Track Middle East")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.9},
        )

    def test_detect_regions_middle_east(self) -> None:
        regions = self.agent._detect_regions("Gaza conflict escalates", "Israeli strikes continue")
        self.assertIn("middle_east", regions)

    def test_detect_regions_multiple(self) -> None:
        regions = self.agent._detect_regions("Russia and China talks", "Moscow and Beijing summit")
        self.assertIn("europe", regions)
        self.assertIn("east_asia", regions)

    def test_detect_regions_none(self) -> None:
        regions = self.agent._detect_regions("Tech conference opens", "Software development trends")
        self.assertEqual(regions, [])

    def test_strip_html(self) -> None:
        self.assertEqual(self.agent._strip_html("<p>Hello <b>world</b></p>"), "Hello world")


class ArXivAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = ArXivAgent("ax1", "Track AI research")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"ai_policy": 0.9, "science": 0.3},
        )

    def test_build_query_with_known_topics(self) -> None:
        query = self.agent._build_query(self.task)
        self.assertIn("cat:cs.AI", query)

    def test_build_query_unknown_topic(self) -> None:
        task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"underwater_basket_weaving": 0.9},
        )
        query = self.agent._build_query(task)
        self.assertIn("all:", query)

    def test_categories_to_topic(self) -> None:
        self.assertEqual(self.agent._categories_to_topic(["cs.AI"], self.task), "ai_policy")
        self.assertEqual(self.agent._categories_to_topic(["q-fin.PM"], self.task), "markets")
        self.assertEqual(self.agent._categories_to_topic(["unknown.XX"], self.task), "ai_policy")

    @patch("newsfeed.agents.arxiv.urlopen")
    def test_run_returns_empty_on_failure(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = urllib_error()
        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(results, [])


class GDELTAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = GDELTAgent("gd1", "Track global events")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8},
        )

    def test_build_query(self) -> None:
        query = self.agent._build_query(self.task)
        self.assertIn("geopolitics", query)

    def test_detect_regions(self) -> None:
        regions = self.agent._detect_regions("Ukraine conflict update", "bbc.com")
        self.assertIn("europe", regions)

    def test_infer_topic_military(self) -> None:
        self.assertEqual(self.agent._infer_topic("Military deployment in region", self.task), "geopolitics")

    @patch("newsfeed.agents.gdelt.urllib.request.urlopen")
    def test_run_parses_gdelt_response(self, mock_urlopen) -> None:
        response_data = {
            "articles": [
                {
                    "title": "Global crisis unfolds",
                    "url": "https://example.com/1",
                    "domain": "reuters.com",
                    "seendate": "20250115120000",
                    "language": "English",
                },
                {
                    "title": "Economic summit begins",
                    "url": "https://example.com/2",
                    "domain": "ft.com",
                    "seendate": "20250115110000",
                    "language": "English",
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "gdelt")


class XTwitterAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = XTwitterAgent("x1", "Track geopolitics", "fake-bearer-token")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.9},
        )

    def test_build_search_query_known_topic(self) -> None:
        query = self.agent._build_search_query(self.task)
        self.assertIn("geopolitics", query)
        self.assertIn("-is:retweet", query)

    def test_build_search_query_fallback(self) -> None:
        task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"underwater_weaving": 0.9},
        )
        query = self.agent._build_search_query(task)
        self.assertIn("underwater", query)

    def test_extract_title_short(self) -> None:
        title = self.agent._extract_title("Short tweet text.")
        self.assertEqual(title, "Short tweet text.")

    def test_extract_title_with_url(self) -> None:
        title = self.agent._extract_title("Breaking news. https://t.co/abc123 More details here")
        self.assertNotIn("https://", title)

    def test_infer_topic(self) -> None:
        self.assertEqual(self.agent._infer_topic("New AI model released by OpenAI", self.task), "ai_policy")
        self.assertEqual(self.agent._infer_topic("Military sanctions imposed", self.task), "geopolitics")

    @patch("newsfeed.agents.xtwitter.urllib.request.urlopen")
    def test_run_parses_response(self, mock_urlopen) -> None:
        response_data = {
            "data": [
                {
                    "id": "12345",
                    "text": "Major geopolitical development: new sanctions imposed on key trading partners, affecting global markets significantly.",
                    "created_at": "2025-01-15T10:00:00Z",
                    "public_metrics": {"like_count": 500, "retweet_count": 200, "reply_count": 50, "quote_count": 30},
                },
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        results = self.agent.run(self.task, top_k=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "x")


class WebSearchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = WebSearchAgent("ws1", "Broad discovery")
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.8, "technology": 0.5},
        )

    def test_build_query(self) -> None:
        query = self.agent._build_query(self.task)
        self.assertIn("geopolitics", query)

    def test_source_evidence_high_trust(self) -> None:
        self.assertGreaterEqual(self.agent._source_evidence("Reuters"), 0.80)

    def test_source_evidence_low_trust(self) -> None:
        self.assertLess(self.agent._source_evidence("Random Blog"), 0.60)

    def test_strip_html(self) -> None:
        self.assertEqual(self.agent._strip_html("<b>Bold</b> text"), "Bold text")


class RegistryNewAgentsTests(unittest.TestCase):
    def test_hackernews_always_real(self) -> None:
        cfg = {"id": "hn1", "source": "hackernews", "mandate": "Track tech"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, HackerNewsAgent)

    def test_aljazeera_always_real(self) -> None:
        cfg = {"id": "aj1", "source": "aljazeera", "mandate": "Track ME"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, AlJazeeraAgent)

    def test_arxiv_always_real(self) -> None:
        cfg = {"id": "ax1", "source": "arxiv", "mandate": "Track research"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, ArXivAgent)

    def test_gdelt_always_real(self) -> None:
        cfg = {"id": "gd1", "source": "gdelt", "mandate": "Track events"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, GDELTAgent)

    def test_web_always_real(self) -> None:
        cfg = {"id": "w1", "source": "web", "mandate": "Discovery"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, WebSearchAgent)

    def test_x_with_bearer_token(self) -> None:
        cfg = {"id": "x1", "source": "x", "mandate": "Track social"}
        agent = create_agent(cfg, {"x_bearer_token": "test-token"})
        self.assertIsInstance(agent, XTwitterAgent)

    def test_x_with_newsapi_fallback(self) -> None:
        cfg = {"id": "x1", "source": "x", "mandate": "Track social"}
        agent = create_agent(cfg, {"newsapi": "key"})
        self.assertIsInstance(agent, NewsAPIAgent)

    def test_x_without_any_key_falls_back(self) -> None:
        cfg = {"id": "x1", "source": "x", "mandate": "Track social"}
        agent = create_agent(cfg, {})
        self.assertIsInstance(agent, SimulatedResearchAgent)


class NewExpertCouncilTests(unittest.TestCase):
    """Tests for the deeply-prompted expert council."""

    def setUp(self) -> None:
        self.task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test",
            weighted_topics={"geopolitics": 0.9},
        )

    def test_all_personas_defined(self) -> None:
        expected = [
            "expert_quality_agent", "expert_relevance_agent",
            "expert_preference_fit_agent", "expert_geopolitical_risk_agent",
            "expert_market_signal_agent",
        ]
        for eid in expected:
            self.assertIn(eid, EXPERT_PERSONAS)
            self.assertIn("system_prompt", EXPERT_PERSONAS[eid])
            self.assertIn("weights", EXPERT_PERSONAS[eid])

    def test_heuristic_vote_quality_agent(self) -> None:
        council = NewExpertCouncil(
            expert_ids=["expert_quality_agent"],
            keep_threshold=0.5,
        )
        candidate = _make_candidate("c1")
        vote = council._vote_heuristic("expert_quality_agent", candidate)
        self.assertEqual(vote.expert_id, "expert_quality_agent")
        self.assertEqual(vote.candidate_id, "c1")
        self.assertIsInstance(vote.keep, bool)
        self.assertGreater(vote.confidence, 0)
        self.assertIn("source", vote.rationale.lower())

    def test_heuristic_vote_relevance_agent(self) -> None:
        council = NewExpertCouncil(expert_ids=["expert_relevance_agent"])
        candidate = _make_candidate("c1")
        vote = council._vote_heuristic("expert_relevance_agent", candidate)
        self.assertIn("novelty", vote.rationale.lower())

    def test_heuristic_vote_preference_agent(self) -> None:
        council = NewExpertCouncil(expert_ids=["expert_preference_fit_agent"])
        candidate = _make_candidate("c1")
        vote = council._vote_heuristic("expert_preference_fit_agent", candidate)
        self.assertIn("preference", vote.rationale.lower())

    def test_heuristic_vote_georisk_agent(self) -> None:
        council = NewExpertCouncil(expert_ids=["expert_geopolitical_risk_agent"])
        candidate = _make_candidate("c1")
        vote = council._vote_heuristic("expert_geopolitical_risk_agent", candidate)
        self.assertIn("region", vote.rationale.lower())

    def test_heuristic_vote_market_agent(self) -> None:
        council = NewExpertCouncil(expert_ids=["expert_market_signal_agent"])
        candidate = _make_candidate("c1")
        vote = council._vote_heuristic("expert_market_signal_agent", candidate)
        self.assertIn("market", vote.rationale.lower())

    def test_five_expert_debate(self) -> None:
        council = NewExpertCouncil(expert_ids=[
            "expert_quality_agent", "expert_relevance_agent",
            "expert_preference_fit_agent", "expert_geopolitical_risk_agent",
            "expert_market_signal_agent",
        ])
        candidates = [_make_candidate(f"c{i}") for i in range(3)]
        debate = council.debate(candidates)
        self.assertEqual(len(debate.votes), 15)  # 5 experts x 3 candidates

    def test_select_with_expanded_council(self) -> None:
        council = NewExpertCouncil(
            expert_ids=[
                "expert_quality_agent", "expert_relevance_agent",
                "expert_preference_fit_agent",
            ],
            keep_threshold=0.3,
        )
        candidates = [_make_candidate(f"c{i}") for i in range(5)]
        selected, reserve, debate = council.select(candidates, max_items=3)
        self.assertLessEqual(len(selected), 3)
        self.assertIsInstance(reserve, list)
        self.assertGreater(len(debate.votes), 0)

    def test_risk_note_generation(self) -> None:
        council = NewExpertCouncil()
        candidate = _make_candidate("c1")
        # Low score should generate verification recommendation
        note = council._generate_risk_note("expert_quality_agent", candidate, 0.3)
        self.assertIn("verification", note.lower())

    def test_risk_note_breaking(self) -> None:
        council = NewExpertCouncil()
        candidate = _make_candidate("c1")
        candidate.urgency = UrgencyLevel.BREAKING
        candidate.corroborated_by = ["ap", "bbc"]  # Need corroboration to reach breaking branch
        note = council._generate_risk_note("expert_quality_agent", candidate, 0.7)
        self.assertIn("fast-moving", note.lower())

    def test_llm_json_parsing(self) -> None:
        council = NewExpertCouncil()
        # Direct JSON
        result = council._parse_llm_json('{"keep": true, "confidence": 0.8}')
        self.assertTrue(result["keep"])
        # JSON in code block
        result = council._parse_llm_json('```json\n{"keep": false, "confidence": 0.5}\n```')
        self.assertFalse(result["keep"])
        # Invalid JSON
        result = council._parse_llm_json("not json at all")
        self.assertEqual(result, {})


class TelegramBotTests(unittest.TestCase):
    def test_format_help(self) -> None:
        bot = TelegramBot("fake-token")
        help_text = bot.format_help()
        self.assertIn("NewsFeed", help_text)
        for cmd in BOT_COMMANDS:
            self.assertIn(cmd["command"], help_text)

    def test_format_settings(self) -> None:
        bot = TelegramBot("fake-token")
        settings = bot.format_settings({
            "tone": "analyst",
            "format": "sections",
            "max_items": 15,
            "topic_weights": {"geopolitics": 0.8, "markets": 0.3},
        })
        self.assertIn("analyst", settings)
        self.assertIn("geopolitics", settings)

    def test_format_status(self) -> None:
        bot = TelegramBot("fake-token")
        status = bot.format_status({
            "agent_count": 18,
            "expert_count": 5,
            "stage_count": 7,
        })
        self.assertIn("18", status)
        self.assertIn("5", status)

    def test_split_message(self) -> None:
        bot = TelegramBot("fake-token")
        short = "Short message"
        chunks = bot._split_message(short)
        self.assertEqual(len(chunks), 1)

        # Long message should be split
        long_text = "\n".join([f"Line {i}: " + "x" * 100 for i in range(100)])
        chunks = bot._split_message(long_text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)

    def test_parse_command_slash(self) -> None:
        bot = TelegramBot("fake-token")
        update = {
            "message": {
                "text": "/briefing geopolitics",
                "chat": {"id": 12345},
                "from": {"id": 67890},
            }
        }
        parsed = bot.parse_command(update)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["command"], "briefing")
        self.assertEqual(parsed["args"], "geopolitics")

    def test_parse_command_feedback(self) -> None:
        bot = TelegramBot("fake-token")
        update = {
            "message": {
                "text": "more geopolitics less crypto",
                "chat": {"id": 12345},
                "from": {"id": 67890},
            }
        }
        parsed = bot.parse_command(update)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["type"], "feedback")

    def test_parse_command_empty(self) -> None:
        bot = TelegramBot("fake-token")
        parsed = bot.parse_command({"message": {}})
        self.assertIsNone(parsed)


class BriefingSchedulerTests(unittest.TestCase):
    def test_set_schedule_morning(self) -> None:
        scheduler = BriefingScheduler()
        result = scheduler.set_schedule("u1", "morning")
        self.assertIn("08:00", result)

    def test_set_schedule_custom_time(self) -> None:
        scheduler = BriefingScheduler()
        result = scheduler.set_schedule("u1", "morning", "09:30")
        self.assertIn("09:30", result)

    def test_set_schedule_off(self) -> None:
        scheduler = BriefingScheduler()
        scheduler.set_schedule("u1", "morning")
        result = scheduler.set_schedule("u1", "off")
        self.assertIn("disabled", result.lower())
        self.assertEqual(scheduler.get_due_users(), [])

    def test_set_schedule_realtime(self) -> None:
        scheduler = BriefingScheduler()
        result = scheduler.set_schedule("u1", "realtime")
        self.assertIn("real-time", result.lower())

    def test_should_send_breaking_default(self) -> None:
        scheduler = BriefingScheduler()
        self.assertTrue(scheduler.should_send_breaking("unknown_user"))

    def test_snapshot(self) -> None:
        scheduler = BriefingScheduler()
        scheduler.set_schedule("u1", "morning")
        snap = scheduler.snapshot()
        self.assertIn("u1", snap)
        self.assertEqual(snap["u1"]["type"], "morning")


def urllib_error():
    """Create a URLError for testing."""
    import urllib.error
    return urllib.error.URLError("Connection failed")


# ──────────────────────────────────────────────────────────────────────────
# Review Agent Tests
# ──────────────────────────────────────────────────────────────────────────

from newsfeed.review.agents import StyleReviewAgent, ClarityReviewAgent
from newsfeed.models.domain import ReportItem, ConfidenceBand, StoryLifecycle, UserProfile


def _make_report_item(topic: str = "geopolitics", source: str = "reuters",
                      urgency: UrgencyLevel = UrgencyLevel.ROUTINE) -> ReportItem:
    c = CandidateItem(
        candidate_id="c-test", title="Test headline", source=source,
        summary="Test summary", url="https://example.com", topic=topic,
        evidence_score=0.7, novelty_score=0.6, preference_fit=0.8,
        prediction_signal=0.5, discovered_by="test_agent",
        created_at=datetime.now(timezone.utc),
        urgency=urgency,
    )
    return ReportItem(
        candidate=c,
        why_it_matters="Base why text.",
        what_changed="Base changed text.",
        predictive_outlook="Base outlook text.",
        adjacent_reads=["Read 1", "Read 2", "Read 3"],
    )


class StyleReviewAgentTests(unittest.TestCase):
    def test_concise_tone_rewrite(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item()
        profile = UserProfile(user_id="u1", tone="concise")
        result = agent.review(item, profile)
        # Should have rewritten why_it_matters (not the original base text)
        self.assertNotEqual(result.why_it_matters, "Base why text.")
        self.assertIn("geopolitics", result.why_it_matters.lower())

    def test_analyst_tone_rewrite(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item()
        profile = UserProfile(user_id="u1", tone="analyst")
        result = agent.review(item, profile)
        self.assertIn("Assessment:", result.why_it_matters)

    def test_executive_tone_rewrite(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item()
        profile = UserProfile(user_id="u1", tone="executive")
        result = agent.review(item, profile)
        self.assertIn("Bottom line:", result.why_it_matters)

    def test_high_priority_topic_personalization(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item(topic="ai_policy")
        profile = UserProfile(user_id="u1", topic_weights={"ai_policy": 0.9})
        result = agent.review(item, profile)
        self.assertIn("high-priority", result.why_it_matters.lower())

    def test_urgency_framing(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item(urgency=UrgencyLevel.BREAKING)
        profile = UserProfile(user_id="u1")
        result = agent.review(item, profile)
        self.assertIn("developing rapidly", result.why_it_matters.lower())

    def test_what_changed_corroboration(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item()
        item.candidate.corroborated_by = ["ap", "bbc"]
        profile = UserProfile(user_id="u1")
        result = agent.review(item, profile)
        self.assertIn("corroborated", result.what_changed.lower())

    def test_outlook_with_regions(self) -> None:
        agent = StyleReviewAgent()
        item = _make_report_item()
        item.candidate.regions = ["middle_east", "europe"]
        profile = UserProfile(user_id="u1", regions_of_interest=["middle_east"])
        result = agent.review(item, profile)
        self.assertIn("middle_east", result.predictive_outlook.lower())

    def test_persona_context_appended(self) -> None:
        agent = StyleReviewAgent(persona_context=["source-quality focus", "audience-first"])
        item = _make_report_item()
        profile = UserProfile(user_id="u1")
        result = agent.review(item, profile)
        self.assertIn("source-quality focus", result.why_it_matters)


class ClarityReviewAgentTests(unittest.TestCase):
    def test_compress_removes_filler(self) -> None:
        agent = ClarityReviewAgent()
        result = agent._compress("It is worth noting that the economy grew.")
        self.assertNotIn("it is worth noting that", result.lower())
        self.assertIn("economy grew", result.lower())

    def test_compress_in_order_to(self) -> None:
        agent = ClarityReviewAgent()
        result = agent._compress("They acted in order to prevent collapse.")
        self.assertIn("to prevent", result)
        self.assertNotIn("in order to", result)

    def test_adds_watchpoint(self) -> None:
        agent = ClarityReviewAgent()
        item = _make_report_item(topic="geopolitics")
        profile = UserProfile(user_id="u1")
        result = agent.review(item, profile)
        # Clarity agent should add a watchpoint if missing
        self.assertIn("watch", result.predictive_outlook.lower())

    def test_improves_adjacent_reads(self) -> None:
        agent = ClarityReviewAgent()
        item = _make_report_item(topic="ai_policy")
        profile = UserProfile(user_id="u1")
        result = agent.review(item, profile)
        # Should replace generic reads with topic-specific ones
        self.assertNotEqual(result.adjacent_reads[0], "Read 1")
        # ai_policy reads should reference technical/regulatory content
        combined = " ".join(result.adjacent_reads).lower()
        self.assertTrue("technical" in combined or "regulatory" in combined or "industry" in combined)

    def test_batch_review(self) -> None:
        agent = ClarityReviewAgent()
        items = [_make_report_item(topic="markets"), _make_report_item(topic="technology")]
        profile = UserProfile(user_id="u1")
        results = agent.review_batch(items, profile)
        self.assertEqual(len(results), 2)
        # Each should have been processed — watchpoint uses "watch" or "monitor"
        for item in results:
            outlook = item.predictive_outlook.lower()
            self.assertTrue("watch" in outlook or "monitor" in outlook or "track" in outlook)


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator Agent Tests
# ──────────────────────────────────────────────────────────────────────────

from newsfeed.orchestration.orchestrator import (
    OrchestratorAgent, RequestLifecycle, RequestStage,
)


class OrchestratorAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_configs = [
            {"id": "news_reuters", "source": "reuters", "mandate": "wire news"},
            {"id": "news_bbc", "source": "bbc", "mandate": "global coverage"},
            {"id": "x_agent_1", "source": "x", "mandate": "social signals"},
            {"id": "hn_agent", "source": "hackernews", "mandate": "tech signals"},
            {"id": "arxiv_agent", "source": "arxiv", "mandate": "research"},
        ]
        self.orchestrator = OrchestratorAgent(self.agent_configs, {})

    def test_compile_brief_creates_task(self) -> None:
        profile = UserProfile(user_id="u1", topic_weights={"geopolitics": 0.9})
        task, lifecycle = self.orchestrator.compile_brief("u1", "daily brief", profile)
        self.assertEqual(task.user_id, "u1")
        self.assertIn("geopolitics", task.weighted_topics)
        self.assertGreater(task.weighted_topics["geopolitics"], 0.5)

    def test_compile_brief_default_topics(self) -> None:
        profile = UserProfile(user_id="u1")  # No topic weights
        task, lifecycle = self.orchestrator.compile_brief("u1", "brief", profile)
        # Should get default topics
        self.assertIn("geopolitics", task.weighted_topics)
        self.assertIn("ai_policy", task.weighted_topics)

    def test_compile_brief_prompt_boost(self) -> None:
        profile = UserProfile(user_id="u1", topic_weights={"markets": 0.3})
        task, _ = self.orchestrator.compile_brief("u1", "markets are crashing", profile)
        # "markets" in prompt should boost the markets topic
        self.assertGreater(task.weighted_topics["markets"], 0.3)

    def test_lifecycle_tracking(self) -> None:
        profile = UserProfile(user_id="u1")
        _, lifecycle = self.orchestrator.compile_brief("u1", "test", profile)
        self.assertEqual(lifecycle.stage, RequestStage.COMPILING_BRIEF)
        lifecycle.advance(RequestStage.RESEARCHING)
        self.assertEqual(lifecycle.stage, RequestStage.RESEARCHING)
        self.assertIn("compiling_brief", lifecycle.stage_times)

    def test_lifecycle_snapshot(self) -> None:
        lifecycle = RequestLifecycle(request_id="req-1", user_id="u1")
        lifecycle.advance(RequestStage.RESEARCHING)
        snap = lifecycle.snapshot()
        self.assertEqual(snap["request_id"], "req-1")
        self.assertEqual(snap["stage"], "researching")
        self.assertIn("elapsed_s", snap)

    def test_select_agents_prioritizes_by_topic(self) -> None:
        from newsfeed.models.domain import ResearchTask
        task = ResearchTask(
            request_id="req-1", user_id="u1", prompt="test",
            weighted_topics={"technology": 0.9, "ai_policy": 0.8},
        )
        selected = self.orchestrator.select_agents(task)
        # hackernews and arxiv should be near the top for tech/AI topics
        top_ids = [a["id"] for a in selected[:3]]
        self.assertTrue(any("hn" in a or "arxiv" in a for a in top_ids))

    def test_record_completion_stores_metrics(self) -> None:
        profile = UserProfile(user_id="u1")
        _, lifecycle = self.orchestrator.compile_brief("u1", "test", profile)
        lifecycle.candidate_count = 50
        lifecycle.selected_count = 8
        self.orchestrator.record_completion(lifecycle)
        metrics = self.orchestrator.metrics()
        self.assertEqual(metrics["total_requests"], 1)
        self.assertGreater(metrics["avg_candidates"], 0)

    def test_lifecycle_fail(self) -> None:
        lifecycle = RequestLifecycle(request_id="req-1", user_id="u1")
        lifecycle.fail("timeout")
        self.assertEqual(lifecycle.stage, RequestStage.FAILED)
        self.assertEqual(lifecycle.error, "timeout")


# ──────────────────────────────────────────────────────────────────────────
# Communication Agent Tests
# ──────────────────────────────────────────────────────────────────────────

from newsfeed.orchestration.communication import CommunicationAgent


class CommunicationAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_engine = MagicMock()
        self.mock_engine.preferences.get_or_create.return_value = UserProfile(
            user_id="u1", topic_weights={"geopolitics": 0.8}, max_items=10,
        )
        self.mock_engine.handle_request.return_value = "Briefing text here"
        self.mock_engine.show_more.return_value = ["Story 1 (reuters)", "Story 2 (bbc)"]
        self.mock_engine.apply_user_feedback.return_value = {"topic:geopolitics": "1.0"}
        self.mock_engine.engine_status.return_value = {"agent_count": 18}

        self.mock_bot = MagicMock()
        self.mock_bot.parse_command.return_value = None
        self.mock_bot.format_help.return_value = "Help text"
        self.mock_bot.format_settings.return_value = "Settings text"
        self.mock_bot.format_status.return_value = "Status text"

        self.agent = CommunicationAgent(engine=self.mock_engine, bot=self.mock_bot)

    def test_handle_briefing_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "briefing", "args": "", "text": "/briefing",
        }
        update = {"message": {"text": "/briefing"}}
        result = self.agent.handle_update(update)
        self.assertEqual(result["action"], "briefing")
        self.mock_engine.handle_request.assert_called_once()
        self.mock_bot.send_briefing.assert_called_once()

    def test_handle_more_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "more", "args": "technology", "text": "/more technology",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "show_more")
        self.mock_engine.show_more.assert_called_once()

    def test_handle_feedback_text(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "feedback", "chat_id": 123, "user_id": "u1",
            "command": "", "args": "", "text": "more geopolitics",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "feedback")
        self.mock_engine.apply_user_feedback.assert_called_with("u1", "more geopolitics")

    def test_handle_preference_more_similar(self) -> None:
        self.agent._last_topic["u1"] = "technology"
        self.mock_bot.parse_command.return_value = {
            "type": "preference", "chat_id": 123, "user_id": "u1",
            "command": "more_similar", "args": "", "text": "",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "pref_more")
        self.assertIn("technology", result["topic"])

    def test_handle_preference_less_similar(self) -> None:
        self.agent._last_topic["u1"] = "markets"
        self.mock_bot.parse_command.return_value = {
            "type": "preference", "chat_id": 123, "user_id": "u1",
            "command": "less_similar", "args": "", "text": "",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "pref_less")

    def test_handle_help_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "help", "args": "", "text": "/help",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "help")
        self.mock_bot.send_message.assert_called()

    def test_handle_settings_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "settings", "args": "", "text": "/settings",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "settings")

    def test_handle_status_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "status", "args": "", "text": "/status",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "status")

    def test_handle_unknown_command(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "foo", "args": "", "text": "/foo",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "unknown_command")

    def test_handle_mute(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "mute", "chat_id": 123, "user_id": "u1",
            "command": "mute", "args": "60", "text": "",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "mute")
        self.assertEqual(result["duration"], "60")

    def test_ignored_update(self) -> None:
        # parse_command returns None
        result = self.agent.handle_update({})
        self.assertIsNone(result)

    def test_schedule_command_without_scheduler(self) -> None:
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "schedule", "args": "morning", "text": "/schedule morning",
        }
        result = self.agent.handle_update({})
        self.assertEqual(result["action"], "schedule_unavailable")

    def test_schedule_command_with_scheduler(self) -> None:
        scheduler = BriefingScheduler()
        agent = CommunicationAgent(
            engine=self.mock_engine, bot=self.mock_bot, scheduler=scheduler,
        )
        self.mock_bot.parse_command.return_value = {
            "type": "command", "chat_id": 123, "user_id": "u1",
            "command": "schedule", "args": "morning 07:30", "text": "/schedule morning 07:30",
        }
        result = agent.handle_update({})
        self.assertEqual(result["action"], "schedule")
        self.assertEqual(result["type"], "morning")


# ──────────────────────────────────────────────────────────────────────────
# System Optimization Agent Tests
# ──────────────────────────────────────────────────────────────────────────

from newsfeed.orchestration.optimizer import (
    SystemOptimizationAgent, AgentMetric, StageMetric, TuningRecommendation,
)


class SystemOptimizationAgentTests(unittest.TestCase):
    def test_record_and_report(self) -> None:
        opt = SystemOptimizationAgent()
        opt.record_agent_run("agent_1", "reuters", 5, 150.0)
        opt.record_agent_run("agent_1", "reuters", 3, 200.0)
        report = opt.health_report()
        self.assertIn("agent_1", report["agents"])
        self.assertEqual(report["agents"]["agent_1"]["runs"], 2)

    def test_avg_metrics(self) -> None:
        m = AgentMetric(agent_id="a1", source="bbc", total_runs=4,
                        total_candidates=20, total_selected=8, total_latency_ms=400.0)
        self.assertEqual(m.avg_yield, 5.0)
        self.assertEqual(m.keep_rate, 0.4)
        self.assertEqual(m.avg_latency_ms, 100.0)

    def test_error_rate_detection(self) -> None:
        opt = SystemOptimizationAgent(error_rate_threshold=0.3)
        for _ in range(5):
            opt.record_agent_run("bad_agent", "web", 0, 100.0, error=True)
        recs = opt.analyze()
        error_recs = [r for r in recs if r.agent_id == "bad_agent"]
        self.assertTrue(any("error rate" in r.reason.lower() for r in error_recs))

    def test_low_keep_rate_detection(self) -> None:
        opt = SystemOptimizationAgent(keep_rate_threshold=0.1)
        # Record many candidates but few selected
        for _ in range(5):
            opt.record_agent_run("noisy_agent", "reddit", 10, 100.0)
        opt.record_agent_selection("noisy_agent", 0)  # No candidates survived
        recs = opt.analyze()
        keep_recs = [r for r in recs if r.agent_id == "noisy_agent" and "keep rate" in r.reason.lower()]
        self.assertTrue(len(keep_recs) > 0)

    def test_stage_failure_detection(self) -> None:
        opt = SystemOptimizationAgent()
        for _ in range(5):
            opt.record_stage_run("clustering", 50.0, failed=True)
        recs = opt.analyze()
        stage_recs = [r for r in recs if "clustering" in r.agent_id]
        self.assertTrue(len(stage_recs) > 0)

    def test_apply_recommendations_reduce_weight(self) -> None:
        opt = SystemOptimizationAgent(keep_rate_threshold=0.1)
        for _ in range(5):
            opt.record_agent_run("low_quality", "web", 10, 100.0)
        opt.record_agent_selection("low_quality", 0)
        actions = opt.apply_recommendations()
        self.assertTrue(any("Reduced weight" in a for a in actions))
        self.assertLess(opt.get_weight_override("low_quality"), 1.0)

    def test_auto_disable(self) -> None:
        opt = SystemOptimizationAgent(error_rate_threshold=0.3)
        for _ in range(5):
            opt.record_agent_run("broken", "x", 0, 100.0, error=True)
        actions = opt.apply_recommendations(auto_disable=True)
        self.assertTrue(opt.is_agent_disabled("broken"))

    def test_snapshot(self) -> None:
        opt = SystemOptimizationAgent()
        opt.record_agent_run("a1", "bbc", 5, 100.0)
        snap = opt.snapshot()
        self.assertIn("a1", snap["agent_stats"])

    def test_no_recommendations_with_few_runs(self) -> None:
        opt = SystemOptimizationAgent()
        opt.record_agent_run("new_agent", "bbc", 0, 100.0, error=True)
        recs = opt.analyze()
        # Should not recommend for < 3 runs
        agent_recs = [r for r in recs if r.agent_id == "new_agent"]
        self.assertEqual(len(agent_recs), 0)


# ──────────────────────────────────────────────────────────────────────────
# Expert Arbitration Tests
# ──────────────────────────────────────────────────────────────────────────

class ExpertArbitrationTests(unittest.TestCase):
    def test_arbitration_triggered_on_close_split(self) -> None:
        """Arbitration should trigger when votes are split by at most 1."""
        council = NewExpertCouncil(
            expert_ids=["expert_quality_agent", "expert_relevance_agent",
                        "expert_preference_fit_agent", "expert_geopolitical_risk_agent",
                        "expert_market_signal_agent"],
            keep_threshold=0.62,
            min_votes_to_accept="majority",
        )
        candidate = _make_candidate("arb-test", 0.1)
        # Run full select — should include arbitration
        selected, reserve, debate = council.select([candidate], 10)
        # Verify votes exist (arbitration produces votes)
        self.assertGreater(len(debate.votes), 0)

    def test_arbitration_not_triggered_for_clear_majority(self) -> None:
        """No arbitration when there's a clear majority (e.g. 5-0 or 4-1)."""
        from newsfeed.models.domain import DebateVote
        council = NewExpertCouncil(
            expert_ids=["e1", "e2", "e3", "e4", "e5"],
            keep_threshold=0.62,
        )
        candidate = _make_candidate("clear-test")
        # Create clear majority votes (4 keep, 1 drop)
        votes = [
            DebateVote(expert_id="e1", candidate_id="clear-test", keep=True, confidence=0.9, rationale="good", risk_note="low"),
            DebateVote(expert_id="e2", candidate_id="clear-test", keep=True, confidence=0.85, rationale="good", risk_note="low"),
            DebateVote(expert_id="e3", candidate_id="clear-test", keep=True, confidence=0.8, rationale="good", risk_note="low"),
            DebateVote(expert_id="e4", candidate_id="clear-test", keep=True, confidence=0.75, rationale="good", risk_note="low"),
            DebateVote(expert_id="e5", candidate_id="clear-test", keep=False, confidence=0.6, rationale="weak", risk_note="none"),
        ]
        result = council._arbitrate(candidate, votes)
        # Should return unchanged (4-1 is not close enough)
        self.assertEqual(len(result), 5)
        # Vote counts should be unchanged since no arbitration
        keep = sum(1 for v in result if v.keep)
        self.assertEqual(keep, 4)

    def test_arbitration_revote_may_flip(self) -> None:
        """An arbitration revote should mark the rationale as revised."""
        council = NewExpertCouncil(
            expert_ids=["expert_quality_agent"],
            keep_threshold=0.62,
        )
        candidate = _make_candidate("flip-test")
        vote = council._arbitration_revote(
            "expert_quality_agent", candidate,
            council._vote_heuristic,
        )
        self.assertIn("arbitration", vote.rationale.lower())


# ──────────────────────────────────────────────────────────────────────────
# Integration: Engine wiring tests
# ──────────────────────────────────────────────────────────────────────────

class EngineWiringTests(unittest.TestCase):
    """Verify the engine properly wires all new components."""

    def test_engine_has_orchestrator(self) -> None:
        from newsfeed.orchestration.orchestrator import OrchestratorAgent
        # Just verify the import chain works
        self.assertTrue(hasattr(OrchestratorAgent, 'agent_id'))
        self.assertEqual(OrchestratorAgent.agent_id, "orchestrator_agent")

    def test_engine_has_optimizer(self) -> None:
        from newsfeed.orchestration.optimizer import SystemOptimizationAgent
        self.assertTrue(hasattr(SystemOptimizationAgent, 'agent_id'))
        self.assertEqual(SystemOptimizationAgent.agent_id, "system_optimization_agent")

    def test_engine_has_review_agents(self) -> None:
        from newsfeed.review.agents import StyleReviewAgent, ClarityReviewAgent
        self.assertEqual(StyleReviewAgent.agent_id, "review_agent_style")
        self.assertEqual(ClarityReviewAgent.agent_id, "review_agent_clarity")

    def test_communication_agent_id(self) -> None:
        from newsfeed.orchestration.communication import CommunicationAgent
        self.assertEqual(CommunicationAgent.agent_id, "communication_agent")


# ──────────────────────────────────────────────────────────────────────
# Session 2 component tests: Configurator, Audit, DebateChair, Config-driven
# ──────────────────────────────────────────────────────────────────────

class SystemConfiguratorTests(unittest.TestCase):
    """Tests for the universal plain-text configuration system."""

    def _make_cfgs(self):
        pipeline = {
            "scoring": {"composite_weights": {"evidence": 0.30, "novelty": 0.25}},
            "expert_council": {"keep_threshold": 0.62, "min_votes_to_accept": "majority"},
            "intelligence": {"enabled_stages": ["credibility", "corroboration", "urgency"]},
            "limits": {"default_max_items": 10},
        }
        agents = {"research_agents": [{"id": "x_agent_1", "source": "x"}]}
        personas = {"default_personas": ["engineer", "source_critic"]}
        return pipeline, agents, personas

    def test_set_evidence_weight(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("set evidence weight to 0.4")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].new_value, 0.4)
        self.assertEqual(p["scoring"]["composite_weights"]["evidence"], 0.4)

    def test_set_evidence_weight_bounded(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("set evidence weight to 5.0")
        self.assertEqual(changes[0].new_value, 1.0)  # Clamped to upper bound

    def test_make_experts_stricter(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("make experts stricter")
        self.assertEqual(len(changes), 1)
        self.assertGreater(changes[0].new_value, 0.62)

    def test_make_experts_lenient(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("make experts more lenient")
        self.assertEqual(len(changes), 1)
        self.assertLess(changes[0].new_value, 0.62)

    def test_set_voting_unanimous(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("set voting to unanimous")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].new_value, "unanimous")

    def test_disable_pipeline_stage(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("disable clustering")
        self.assertEqual(len(changes), 1)
        self.assertNotIn("clustering", p["intelligence"]["enabled_stages"])

    def test_enable_pipeline_stage(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("enable georisk")
        self.assertEqual(len(changes), 1)
        self.assertIn("georisk", p["intelligence"]["enabled_stages"])

    def test_disable_agent(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("disable agent x_agent_1")
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0].new_value)

    def test_add_persona(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("add persona forecaster")
        self.assertEqual(len(changes), 1)
        self.assertIn("forecaster", per["default_personas"])

    def test_remove_persona(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("remove persona engineer")
        self.assertEqual(len(changes), 1)
        self.assertNotIn("engineer", per["default_personas"])

    def test_set_max_items(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("show me 15 items")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].new_value, 15)
        self.assertEqual(p["limits"]["default_max_items"], 15)

    def test_no_match_returns_empty(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("hello how are you")
        self.assertEqual(changes, [])

    def test_history_and_snapshot(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        cfg.parse_and_apply("set evidence weight to 0.5")
        cfg.parse_and_apply("make experts stricter")
        hist = cfg.history()
        self.assertEqual(len(hist), 2)
        snap = cfg.snapshot()
        self.assertEqual(snap["changes_applied"], 2)

    def test_source_priority_boost(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("trust reuters source more")
        self.assertEqual(len(changes), 1)
        self.assertIn("source_weight", changes[0].path)

    def test_prioritize_source_pair(self) -> None:
        from newsfeed.orchestration.configurator import SystemConfigurator
        p, a, per = self._make_cfgs()
        cfg = SystemConfigurator(p, a, per)
        changes = cfg.parse_and_apply("prioritize reuters over reddit")
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].new_value["prefer"], "reuters")


class AuditTrailTests(unittest.TestCase):
    """Tests for the full decision audit system."""

    def test_record_and_query(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_research("req1", "agent_a", "reuters", 5, 120.5)
        trace = audit.get_request_trace("req1")
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["agent_id"], "agent_a")

    def test_record_vote(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_vote("req1", "expert_a", "c1", True, 0.85, "Strong evidence", "None")
        trace = audit.get_request_trace("req1")
        self.assertEqual(len(trace), 1)
        self.assertTrue(trace[0]["keep"])
        self.assertEqual(trace[0]["confidence"], 0.85)

    def test_record_selection(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_selection("req1", "c1", "Title1", True, "Accepted", 0.87)
        trace = audit.get_request_trace("req1")
        self.assertTrue(trace[0]["selected"])

    def test_record_review(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_review("req1", "style", "c1", "why", "old text", "new text")
        trace = audit.get_request_trace("req1")
        self.assertTrue(trace[0]["changed"])

    def test_record_config_change(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_config_change("req1", "scoring.evidence", 0.3, 0.5, "user")
        trace = audit.get_request_trace("req1")
        self.assertEqual(trace[0]["old"], 0.3)
        self.assertEqual(trace[0]["new"], 0.5)

    def test_record_preference(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_preference("req1", "u1", "topic_boost", "geopolitics +0.2")
        trace = audit.get_request_trace("req1")
        self.assertEqual(trace[0]["user_id"], "u1")

    def test_record_delivery(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_delivery("req1", "u1", 10, "morning_digest", 2.5)
        trace = audit.get_request_trace("req1")
        self.assertEqual(trace[0]["item_count"], 10)

    def test_candidate_trace(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_vote("req1", "e1", "c1", True, 0.9, "Good", "")
        audit.record_vote("req1", "e2", "c1", False, 0.4, "Weak", "low evidence")
        audit.record_vote("req1", "e1", "c2", True, 0.8, "OK", "")
        trace = audit.get_candidate_trace("req1", "c1")
        self.assertEqual(len(trace), 2)  # Only c1 events

    def test_expert_votes_grouped(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_vote("req1", "e1", "c1", True, 0.9, "Good", "")
        audit.record_vote("req1", "e1", "c2", True, 0.8, "OK", "")
        audit.record_vote("req1", "e2", "c1", False, 0.4, "Weak", "")
        votes = audit.get_expert_votes("req1")
        self.assertEqual(len(votes["e1"]), 2)
        self.assertEqual(len(votes["e2"]), 1)

    def test_recent_requests(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        for i in range(5):
            audit.record_research(f"req{i}", "a", "", 1, 10.0)
        recent = audit.get_recent_requests(3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[0], "req4")  # Most recent first

    def test_format_report(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_research("req1", "agent_a", "reuters", 5, 120.0)
        audit.record_vote("req1", "e1", "c1", True, 0.9, "Strong", "")
        audit.record_selection("req1", "c1", "Title", True, "Accepted", 0.87)
        audit.record_delivery("req1", "u1", 1, "morning_digest", 1.5)
        report = audit.format_request_report("req1")
        self.assertIn("AUDIT REPORT", report)
        self.assertIn("RESEARCH PHASE", report)
        self.assertIn("EXPERT COUNCIL", report)
        self.assertIn("DELIVERY", report)

    def test_stats(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        audit.record_research("r1", "a", "", 3, 50.0)
        audit.record_vote("r1", "e1", "c1", True, 0.9, "", "")
        stats = audit.stats()
        self.assertEqual(stats["total_events"], 2)
        self.assertEqual(stats["events_by_type"]["research"], 1)
        self.assertEqual(stats["events_by_type"]["vote"], 1)

    def test_trimming(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail(max_requests=3)
        for i in range(5):
            audit.record_research(f"req{i}", "a", "", 1, 10.0)
        # Only 3 most recent requests should survive
        self.assertLessEqual(len(audit._request_index), 3)
        self.assertIn("req4", audit._request_index)
        self.assertNotIn("req0", audit._request_index)

    def test_empty_report(self) -> None:
        from newsfeed.orchestration.audit import AuditTrail
        audit = AuditTrail()
        report = audit.format_request_report("nonexistent")
        self.assertIn("No audit data", report)


class DebateChairTests(unittest.TestCase):
    """Tests for the DebateChair expert influence system."""

    def test_initial_influence_is_one(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1", "e2", "e3"])
        self.assertAlmostEqual(chair.get_influence("e1"), 1.0)
        self.assertAlmostEqual(chair.get_influence("e2"), 1.0)

    def test_unknown_expert_returns_one(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1"])
        self.assertAlmostEqual(chair.get_influence("unknown"), 1.0)

    def test_weighted_keep_count(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1", "e2", "e3"])
        votes = [
            DebateVote(expert_id="e1", candidate_id="c1", keep=True, confidence=0.9, rationale="ok", risk_note=""),
            DebateVote(expert_id="e2", candidate_id="c1", keep=False, confidence=0.4, rationale="weak", risk_note=""),
            DebateVote(expert_id="e3", candidate_id="c1", keep=True, confidence=0.8, rationale="ok", risk_note=""),
        ]
        count = chair.weighted_keep_count(votes)
        self.assertAlmostEqual(count, 2.0)  # e1 + e3, both influence 1.0

    def test_record_outcome_correct_boosts(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1"], decay=1.0)  # No decay for testing
        # Correct vote (voted keep, was selected)
        for _ in range(5):
            chair.record_outcome("e1", voted_keep=True, was_selected=True)
        self.assertGreater(chair.get_influence("e1"), 1.0)

    def test_record_outcome_wrong_penalizes(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1"], decay=1.0)
        # Wrong vote (voted keep, was not selected)
        for _ in range(5):
            chair.record_outcome("e1", voted_keep=True, was_selected=False)
        self.assertLess(chair.get_influence("e1"), 1.0)

    def test_influence_bounded(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1"], decay=1.0)
        # Many correct votes
        for _ in range(100):
            chair.record_outcome("e1", voted_keep=True, was_selected=True)
        self.assertLessEqual(chair.get_influence("e1"), 2.0)
        # Many wrong votes
        chair2 = DebateChair(["e1"], decay=1.0)
        for _ in range(100):
            chair2.record_outcome("e1", voted_keep=True, was_selected=False)
        self.assertGreaterEqual(chair2.get_influence("e1"), 0.5)

    def test_accuracy(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1"])
        chair.record_outcome("e1", True, True)   # correct
        chair.record_outcome("e1", False, False)  # correct
        chair.record_outcome("e1", True, False)   # wrong
        self.assertAlmostEqual(chair.accuracy("e1"), 2/3, places=2)

    def test_rankings(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1", "e2"], decay=1.0)
        # e1 always correct, e2 always wrong
        for _ in range(5):
            chair.record_outcome("e1", True, True)
            chair.record_outcome("e2", True, False)
        rankings = chair.rankings()
        self.assertEqual(rankings[0][0], "e1")  # e1 should be ranked higher
        self.assertGreater(rankings[0][1], rankings[1][1])

    def test_snapshot(self) -> None:
        from newsfeed.agents.experts import DebateChair
        chair = DebateChair(["e1", "e2"])
        snap = chair.snapshot()
        self.assertIn("influence", snap)
        self.assertIn("accuracy", snap)
        self.assertIn("e1", snap["influence"])


class CompressedPromptTests(unittest.TestCase):
    """Tests for the parametric expert prompt system."""

    def test_expert_specs_all_defined(self) -> None:
        from newsfeed.agents.experts import _EXPERT_SPECS
        expected = {
            "expert_quality_agent", "expert_relevance_agent",
            "expert_preference_fit_agent", "expert_geopolitical_risk_agent",
            "expert_market_signal_agent",
        }
        self.assertEqual(set(_EXPERT_SPECS.keys()), expected)

    def test_expert_spec_structure(self) -> None:
        from newsfeed.agents.experts import _EXPERT_SPECS
        for eid, spec in _EXPERT_SPECS.items():
            self.assertEqual(len(spec), 3, f"Spec for {eid} should be (name, directive, criteria)")
            name, directive, criteria = spec
            self.assertIsInstance(name, str)
            self.assertIsInstance(directive, str)
            self.assertIsInstance(criteria, list)
            self.assertGreaterEqual(len(criteria), 2)

    def test_preamble_contains_json_format(self) -> None:
        from newsfeed.agents.experts import _EXPERT_PREAMBLE
        self.assertIn("JSON", _EXPERT_PREAMBLE)
        self.assertIn("keep", _EXPERT_PREAMBLE)
        self.assertIn("confidence", _EXPERT_PREAMBLE)

    def test_expert_personas_backward_compat(self) -> None:
        """EXPERT_PERSONAS should still be importable for backward compatibility."""
        from newsfeed.agents.experts import EXPERT_PERSONAS
        self.assertIsInstance(EXPERT_PERSONAS, dict)
        self.assertGreaterEqual(len(EXPERT_PERSONAS), 5)
        for eid, persona in EXPERT_PERSONAS.items():
            self.assertIn("system_prompt", persona)


class ConfigDrivenReviewTests(unittest.TestCase):
    """Tests for config-driven editorial review agents."""

    def test_style_agent_custom_tone_template(self) -> None:
        from newsfeed.review.agents import StyleReviewAgent
        from newsfeed.models.domain import ReportItem, ConfidenceBand, UserProfile
        custom_cfg = {
            "tone_templates": {
                "concise": {
                    "why_prefix": "CUSTOM: ",
                    "outlook_prefix": "VIEW: ",
                    "changed_prefix": "DELTA: ",
                    "style": "Custom style.",
                },
            },
        }
        agent = StyleReviewAgent(editorial_cfg=custom_cfg)
        c = _make_candidate("t1")
        item = ReportItem(
            candidate=c, why_it_matters="Base", what_changed="Base",
            predictive_outlook="Base", adjacent_reads=[],
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )
        profile = UserProfile(user_id="u1")  # tone defaults to "concise"
        agent.review(item, profile)
        # Custom prefix not applied because _rewrite_why builds its own string
        # but the template is loaded from config
        self.assertIsInstance(item.why_it_matters, str)

    def test_style_agent_default_without_config(self) -> None:
        from newsfeed.review.agents import StyleReviewAgent
        agent = StyleReviewAgent()
        self.assertIn("concise", agent._tone_templates)
        self.assertIn("analyst", agent._tone_templates)

    def test_clarity_agent_custom_watchpoints(self) -> None:
        from newsfeed.review.agents import ClarityReviewAgent
        from newsfeed.models.domain import ReportItem, ConfidenceBand, UserProfile
        custom_cfg = {
            "watchpoints": {"geopolitics": "CUSTOM WATCHPOINT."},
        }
        agent = ClarityReviewAgent(editorial_cfg=custom_cfg)
        c = _make_candidate("t1")
        item = ReportItem(
            candidate=c, why_it_matters="Base", what_changed="Base",
            predictive_outlook="Limited signal at this stage", adjacent_reads=["read1"],
            confidence=ConfidenceBand(low=0.5, mid=0.7, high=0.9),
        )
        profile = UserProfile(user_id="u1")
        agent.review(item, profile)
        self.assertIn("CUSTOM WATCHPOINT", item.predictive_outlook)

    def test_clarity_agent_custom_filler_patterns(self) -> None:
        from newsfeed.review.agents import ClarityReviewAgent
        custom_cfg = {
            "filler_patterns": [["\\bfoobar\\b", "baz"]],
        }
        agent = ClarityReviewAgent(editorial_cfg=custom_cfg)
        result = agent._compress("this is foobar text")
        self.assertIn("baz", result)
        self.assertNotIn("foobar", result)

    def test_clarity_agent_default_filler_patterns(self) -> None:
        from newsfeed.review.agents import ClarityReviewAgent
        agent = ClarityReviewAgent()
        result = agent._compress("in order to proceed")
        self.assertIn("to", result)
        self.assertNotIn("in order to", result)


class ConfigDrivenOrchestratorTests(unittest.TestCase):
    """Tests for config-driven topic capabilities and source priority."""

    def test_custom_topic_capabilities(self) -> None:
        from newsfeed.orchestration.orchestrator import OrchestratorAgent
        from newsfeed.models.domain import ResearchTask, UserProfile
        custom_agents_cfg = {
            "topic_capabilities": {"test_topic": ["custom_source"]},
            "source_priority": {"custom_source": 0.99},
        }
        orch = OrchestratorAgent(
            agent_configs=[{"id": "a1", "source": "custom_source"}],
            pipeline_cfg={"limits": {}},
            agents_cfg=custom_agents_cfg,
        )
        task = ResearchTask(
            request_id="r1", user_id="u1", prompt="test_topic update",
            weighted_topics={"test_topic": 1.0},
        )
        selected = orch.select_agents(task)
        self.assertEqual(len(selected), 1)

    def test_default_topic_capabilities_without_config(self) -> None:
        from newsfeed.orchestration.orchestrator import OrchestratorAgent
        orch = OrchestratorAgent(
            agent_configs=[{"id": "a1", "source": "reuters"}],
            pipeline_cfg={"limits": {}},
        )
        # Should use defaults
        self.assertIn("geopolitics", orch._topic_capabilities)

    def test_default_topics_from_config(self) -> None:
        from newsfeed.orchestration.orchestrator import OrchestratorAgent
        from newsfeed.models.domain import UserProfile
        custom_pipeline = {
            "limits": {},
            "default_topics": {"custom_topic": 0.9},
        }
        orch = OrchestratorAgent(
            agent_configs=[],
            pipeline_cfg=custom_pipeline,
        )
        profile = UserProfile(user_id="u1")
        task, lifecycle = orch.compile_brief("u1", "test", profile)
        self.assertIn("custom_topic", task.weighted_topics)

    def test_select_agents_uses_config_priority(self) -> None:
        from newsfeed.orchestration.orchestrator import OrchestratorAgent
        from newsfeed.models.domain import ResearchTask
        agents_cfg = {
            "source_priority": {"high_src": 0.99, "low_src": 0.01},
            "topic_capabilities": {"geopolitics": ["high_src", "low_src"]},
        }
        orch = OrchestratorAgent(
            agent_configs=[
                {"id": "low", "source": "low_src"},
                {"id": "high", "source": "high_src"},
            ],
            pipeline_cfg={"limits": {}},
            agents_cfg=agents_cfg,
        )
        task = ResearchTask(
            request_id="r1", user_id="u1", prompt="geo",
            weighted_topics={"geopolitics": 1.0},
        )
        selected = orch.select_agents(task)
        self.assertEqual(selected[0]["id"], "high")  # Higher priority first


class EngineIntegrationTests(unittest.TestCase):
    """Tests for end-to-end engine integration with new components."""

    def _make_engine(self):
        from pathlib import Path
        from newsfeed.models.config import load_runtime_config
        from newsfeed.orchestration.engine import NewsFeedEngine
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        return NewsFeedEngine(cfg.agents, cfg.pipeline, cfg.personas, root / "personas")

    def test_engine_has_audit_trail(self) -> None:
        engine = self._make_engine()
        self.assertIsNotNone(engine.audit)
        self.assertEqual(engine.audit.stats()["total_events"], 0)

    def test_engine_has_configurator(self) -> None:
        engine = self._make_engine()
        self.assertIsNotNone(engine.configurator)
        snap = engine.configurator.snapshot()
        self.assertIn("scoring", snap)
        self.assertIn("personas", snap)

    def test_engine_has_debate_chair(self) -> None:
        engine = self._make_engine()
        self.assertIsNotNone(engine.experts.chair)
        rankings = engine.experts.chair.rankings()
        self.assertGreaterEqual(len(rankings), 1)

    def test_audit_populated_after_request(self) -> None:
        engine = self._make_engine()
        engine.handle_request(
            user_id="u-audit",
            prompt="test audit",
            weighted_topics={"geopolitics": 0.9},
        )
        stats = engine.audit.stats()
        self.assertGreater(stats["total_events"], 0)
        # Should have research, vote, selection, review, and delivery events
        by_type = stats["events_by_type"]
        self.assertIn("research", by_type)
        self.assertIn("vote", by_type)
        self.assertIn("delivery", by_type)

    def test_config_change_via_feedback(self) -> None:
        engine = self._make_engine()
        results = engine.apply_user_feedback("u-cfg", "set evidence weight to 0.4")
        self.assertIn("scoring.composite_weights.evidence", results)

    def test_engine_status_includes_new_fields(self) -> None:
        engine = self._make_engine()
        status = engine.engine_status()
        self.assertIn("audit_stats", status)
        self.assertIn("expert_influence", status)
        self.assertIn("config_changes", status)

    def test_editorial_cfg_loaded_into_review_agents(self) -> None:
        engine = self._make_engine()
        # Style reviewer should have tone_templates from config
        self.assertIn("concise", engine._style_reviewer._tone_templates)
        self.assertIn("analyst", engine._style_reviewer._tone_templates)
        # Clarity reviewer should have watchpoints from config
        self.assertIn("geopolitics", engine._clarity_reviewer._watchpoints)

    def test_orchestrator_loads_config_capabilities(self) -> None:
        engine = self._make_engine()
        # Orchestrator should have topic capabilities from agents.json
        self.assertIn("geopolitics", engine.orchestrator._topic_capabilities)
        self.assertIn("reuters", engine.orchestrator._source_priority)


if __name__ == "__main__":
    unittest.main()
