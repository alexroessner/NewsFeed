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
from newsfeed.models.domain import CandidateItem, ResearchTask, UrgencyLevel


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


if __name__ == "__main__":
    unittest.main()
