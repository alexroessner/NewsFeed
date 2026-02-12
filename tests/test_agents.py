from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    unittest.main()
