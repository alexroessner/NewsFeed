from __future__ import annotations

import asyncio
import hashlib

from newsfeed.models.domain import CandidateItem, DebateRecord, DebateVote, ResearchTask


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
            evidence = 0.55 + (scale * 0.45)
            novelty = 0.45 + ((1 - scale) * 0.50)
            preference = min(1.0, task.weighted_topics.get(base_topic, 0.2) + 0.25 + rank * 0.03)
            pred = 0.40 + (scale * 0.45)
            candidates.append(
                CandidateItem(
                    candidate_id=f"{self.agent_id}-{rank}",
                    title=f"{base_topic.title()} signal #{rank + 1} from {self.source}",
                    source=self.source,
                    summary=f"{self.mandate}: candidate insight generated for {base_topic}.",
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
    def __init__(self, expert_ids: list[str] | None = None) -> None:
        self.expert_ids = expert_ids or [
            "expert_quality_agent",
            "expert_relevance_agent",
            "expert_preference_fit_agent",
        ]

    def _vote(self, expert_id: str, candidate: CandidateItem) -> DebateVote:
        score = candidate.composite_score()
        keep = score >= 0.62
        confidence = min(0.99, max(0.51, score))
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

        votes_by_candidate: dict[str, list[DebateVote]] = {}
        for vote in debate.votes:
            votes_by_candidate.setdefault(vote.candidate_id, []).append(vote)

        accepted_ids: set[str] = set()
        for candidate_id, votes in votes_by_candidate.items():
            keep_votes = sum(1 for v in votes if v.keep)
            if keep_votes >= 2:
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
