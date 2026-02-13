from __future__ import annotations

import asyncio
import hashlib
import logging
import math

from newsfeed.models.domain import CandidateItem, DebateRecord, DebateVote, ResearchTask

log = logging.getLogger(__name__)


class SimulatedResearchAgent:
    def __init__(self, agent_id: str, source: str, mandate: str) -> None:
        self.agent_id = agent_id
        self.source = source
        self.mandate = mandate

    def run(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        base_topic = max(task.weighted_topics, key=task.weighted_topics.get, default="general")
        candidates: list[CandidateItem] = []
        for rank in range(top_k):
            seed = f"{self.agent_id}:{task.request_id}:{base_topic}:{rank}".encode("utf-8")
            digest = hashlib.sha256(seed).hexdigest()
            scale = int(digest[:8], 16) / 0xFFFFFFFF
            # Simulated scores are intentionally LOW — this is synthetic fallback
            # data that should never outrank real agent output (real agents: 0.55-0.73).
            evidence = 0.30 + (scale * 0.20)       # 0.30-0.50 (was 0.55-1.0)
            novelty = 0.30 + ((1 - scale) * 0.25)  # 0.30-0.55 (was 0.45-0.95)
            preference = min(0.45, task.weighted_topics.get(base_topic, 0.2) * 0.35 + 0.15)
            pred = 0.20 + (scale * 0.20)            # 0.20-0.40 (was 0.40-0.85)
            candidates.append(
                CandidateItem(
                    candidate_id=f"{self.agent_id}-{rank}",
                    title=f"[Simulated] {base_topic.replace('_', ' ').title()} #{rank + 1} ({self.source})",
                    source=self.source,
                    summary=f"Simulated placeholder — {self.source} agent requires API credentials for real data.",
                    url=f"https://example.com/{self.source}/{base_topic}/{rank}",
                    topic=base_topic,
                    evidence_score=round(evidence, 3),
                    novelty_score=round(novelty, 3),
                    preference_fit=round(preference, 3),
                    prediction_signal=round(pred, 3),
                    discovered_by=self.agent_id,
                )
            )
        candidates.sort(key=lambda c: c.composite_score(), reverse=True)
        return candidates

    async def run_async(self, task: ResearchTask, top_k: int = 5) -> list[CandidateItem]:
        await asyncio.sleep(0)
        return self.run(task, top_k=top_k)


class ExpertCouncil:
    def __init__(
        self,
        expert_ids: list[str] | None = None,
        keep_threshold: float = 0.62,
        confidence_min: float = 0.51,
        confidence_max: float = 0.99,
        min_votes_to_accept: str = "majority",
    ) -> None:
        self.expert_ids = expert_ids or [
            "expert_quality_agent",
            "expert_relevance_agent",
            "expert_preference_fit_agent",
        ]
        self.keep_threshold = keep_threshold
        self.confidence_min = confidence_min
        self.confidence_max = confidence_max
        self.min_votes_to_accept = min_votes_to_accept

    def _required_votes(self) -> int:
        n = len(self.expert_ids)
        if self.min_votes_to_accept == "majority":
            return math.ceil(n / 2)
        if self.min_votes_to_accept == "unanimous":
            return n
        try:
            requested = int(self.min_votes_to_accept)
        except (ValueError, TypeError):
            return math.ceil(n / 2)
        if requested > n:
            log.warning(
                "min_votes_to_accept=%d exceeds expert count=%d, clamping",
                requested, n,
            )
            return n
        return max(1, requested)

    def _vote(self, expert_id: str, candidate: CandidateItem) -> DebateVote:
        score = candidate.composite_score()
        keep = score >= self.keep_threshold
        confidence = min(self.confidence_max, max(self.confidence_min, score))
        return DebateVote(
            expert_id=expert_id,
            candidate_id=candidate.candidate_id,
            keep=keep,
            confidence=round(confidence, 3),
            rationale=f"{expert_id} evaluated source quality, novelty, and preference fit.",
            risk_note="May degrade if the story is stale or weakly corroborated.",
        )

    def debate(self, candidates: list[CandidateItem]) -> DebateRecord:
        votes: list[DebateVote] = []
        for candidate in candidates:
            for expert_id in self.expert_ids:
                votes.append(self._vote(expert_id, candidate))
        return DebateRecord(votes=votes)

    def select(self, candidates: list[CandidateItem], max_items: int) -> tuple[list[CandidateItem], list[CandidateItem], DebateRecord]:
        debate = self.debate(candidates)
        required = self._required_votes()

        votes_by_candidate: dict[str, list[DebateVote]] = {}
        for vote in debate.votes:
            votes_by_candidate.setdefault(vote.candidate_id, []).append(vote)

        accepted_ids: set[str] = set()
        for candidate_id, cvotes in votes_by_candidate.items():
            keep_votes = sum(1 for v in cvotes if v.keep)
            if keep_votes >= required:
                accepted_ids.add(candidate_id)

        deduped: dict[str, CandidateItem] = {}
        for c in sorted(candidates, key=lambda x: x.composite_score(), reverse=True):
            if c.candidate_id not in accepted_ids:
                continue
            dedupe_key = c.title.lower().strip()
            if dedupe_key not in deduped:
                deduped[dedupe_key] = c

        ranked = list(deduped.values())
        selected = ranked[:max_items]
        reserve = ranked[max_items:]
        return selected, reserve, debate
