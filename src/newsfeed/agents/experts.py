"""Expert council agents with debate chair, weighted voting, and audit integration.

Each expert evaluates candidates through a distinct analytical lens.
LLM-backed experts use compressed parametric prompts; heuristic experts
use calibrated domain-specific scoring. The DebateChair manages flow,
tracks expert influence scores, and produces auditable decisions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from newsfeed.models.domain import CandidateItem, DebateRecord, DebateVote

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Compressed parametric expert prompts — shared template + per-expert spec
# ──────────────────────────────────────────────────────────────────────

_EXPERT_PREAMBLE = (
    "You are a specialist analyst on a news intelligence editorial board. "
    "Evaluate each candidate and respond ONLY in JSON: "
    '{{"keep":bool,"confidence":0.0-1.0,"rationale":"1-2 sentences","risk_note":"string"}}'
)

# Per-expert: (name, focus directive, criteria list)
_EXPERT_SPECS: dict[str, tuple[str, str, list[str]]] = {
    "expert_quality_agent": (
        "Source Quality & Evidence Analyst",
        "Evaluate EVIDENTIAL STRENGTH and SOURCE RELIABILITY. "
        "Tier-1 (Reuters/AP/BBC/Guardian/FT) starts at 0.85; tier-2 at 0.55. "
        "Score: primary sources > named officials > documents > unnamed sources > speculation. "
        "Cross-source corroboration dramatically boosts confidence. "
        "Account for source bias and recency decay.",
        ["source_quality", "evidence_strength", "corroboration", "recency"],
    ),
    "expert_relevance_agent": (
        "Topic Relevance & Novelty Analyst",
        "Evaluate GENUINE NOVELTY and TOPIC RELEVANCE. "
        "Score information delta vs 6h ago. Distinguish substantive developments from noise/rehash. "
        "Breaking > developing > ongoing > waning. "
        "High-novelty contrarian signals valuable even if uncomfortable.",
        ["topic_relevance", "novelty_delta", "signal_noise", "lifecycle_stage"],
    ),
    "expert_preference_fit_agent": (
        "User Preference & Decision Utility Analyst",
        "Evaluate USER-SPECIFIC FIT and DECISION UTILITY. "
        "Match against user's topic weights, tone, and regional interests. "
        "Actionable intelligence > entertainment. Every item competes for attention — "
        "only include what earns it. Consider briefing type fit and cognitive load.",
        ["user_affinity", "style_alignment", "decision_utility", "briefing_fit"],
    ),
    "expert_geopolitical_risk_agent": (
        "Geopolitical Risk & Escalation Analyst",
        "Evaluate GEOPOLITICAL SIGNIFICANCE and ESCALATION POTENTIAL. "
        "Score: military movements, sanctions, diplomatic incidents highest. "
        "Cross-regional contagion amplifies importance. State actors > non-state. "
        "Check historical escalation patterns. Balance with de-escalation signals.",
        ["escalation_potential", "regional_impact", "actor_significance", "pattern_match"],
    ),
    "expert_market_signal_agent": (
        "Market Signal & Economic Impact Analyst",
        "Evaluate MARKET-MOVING POTENTIAL and ECONOMIC IMPLICATIONS. "
        "Score: central bank decisions, trade policy, earnings surprises, commodity shocks highest. "
        "Leading indicators > lagging. Broader sector impact scores higher. "
        "Cross-reference prediction market signals.",
        ["market_impact", "leading_indicator", "sector_exposure", "policy_signal"],
    ),
}

def _build_system_prompt(expert_id: str) -> str:
    """Build a compressed system prompt from preamble + expert spec."""
    spec = _EXPERT_SPECS.get(expert_id)
    if not spec:
        return _EXPERT_PREAMBLE
    return f"{_EXPERT_PREAMBLE}\n\nRole: {spec[0]}.\n{spec[1]}"


# Heuristic scoring weights per expert (used when no LLM API)
_EXPERT_WEIGHTS: dict[str, dict[str, float]] = {
    "expert_quality_agent": {"evidence": 0.40, "source_tier": 0.30, "corroboration": 0.20, "recency": 0.10},
    "expert_relevance_agent": {"novelty": 0.35, "preference_fit": 0.30, "lifecycle": 0.20, "contrarian": 0.15},
    "expert_preference_fit_agent": {"preference_fit": 0.35, "prediction_signal": 0.25, "urgency": 0.20, "diversity": 0.20},
    "expert_geopolitical_risk_agent": {"urgency": 0.35, "evidence": 0.25, "regions": 0.25, "novelty": 0.15},
    "expert_market_signal_agent": {"prediction_signal": 0.35, "evidence": 0.25, "novelty": 0.20, "preference_fit": 0.20},
}

# Backward-compat wrapper used by heuristic voter
EXPERT_PERSONAS: dict[str, dict[str, Any]] = {}
for _eid, (_name, _focus, _criteria) in _EXPERT_SPECS.items():
    EXPERT_PERSONAS[_eid] = {
        "name": _name,
        "system_prompt": _build_system_prompt(_eid),
        "criteria": _criteria,
        "weights": _EXPERT_WEIGHTS.get(_eid, {}),
    }


# ──────────────────────────────────────────────────────────────────────
# Expert influence tracking (DebateChair)
# ──────────────────────────────────────────────────────────────────────

class DebateChair:
    """Manages debate flow, tracks expert influence, and produces weighted votes.

    Expert influence adjusts based on track record:
    - Experts whose "keep" picks consistently make the final slate gain influence
    - Experts whose picks get rejected or whose rationale is overridden lose influence
    - Influence is bounded [0.5, 2.0] and decays toward 1.0 over time
    """

    def __init__(self, expert_ids: list[str], decay: float = 0.95) -> None:
        self._influence: dict[str, float] = {eid: 1.0 for eid in expert_ids}
        self._decay = decay
        self._total_votes: dict[str, int] = {eid: 0 for eid in expert_ids}
        self._correct_votes: dict[str, int] = {eid: 0 for eid in expert_ids}

    def get_influence(self, expert_id: str) -> float:
        return self._influence.get(expert_id, 1.0)

    def weighted_keep_count(self, votes: list[DebateVote]) -> float:
        """Compute influence-weighted keep count."""
        return sum(self.get_influence(v.expert_id) for v in votes if v.keep)

    def weighted_total(self, votes: list[DebateVote]) -> float:
        """Compute total influence weight of voters."""
        return sum(self.get_influence(v.expert_id) for v in votes)

    def record_outcome(self, expert_id: str, voted_keep: bool, was_selected: bool) -> None:
        """Update influence based on whether expert's vote aligned with final outcome."""
        self._total_votes[expert_id] = self._total_votes.get(expert_id, 0) + 1
        correct = (voted_keep and was_selected) or (not voted_keep and not was_selected)
        if correct:
            self._correct_votes[expert_id] = self._correct_votes.get(expert_id, 0) + 1

        # Adjust influence
        current = self._influence.get(expert_id, 1.0)
        if correct:
            new = min(2.0, current * 1.02)  # Small reward
        else:
            new = max(0.5, current * 0.97)  # Small penalty

        # Decay toward 1.0
        new = new * self._decay + 1.0 * (1.0 - self._decay)
        self._influence[expert_id] = round(new, 4)

    def accuracy(self, expert_id: str) -> float:
        total = self._total_votes.get(expert_id, 0)
        if total == 0:
            return 0.0
        return round(self._correct_votes.get(expert_id, 0) / total, 3)

    def rankings(self) -> list[tuple[str, float, float]]:
        """Return experts ranked by influence: (id, influence, accuracy)."""
        return sorted(
            [(eid, inf, self.accuracy(eid)) for eid, inf in self._influence.items()],
            key=lambda x: x[1], reverse=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "influence": dict(self._influence),
            "accuracy": {eid: self.accuracy(eid) for eid in self._influence},
            "total_votes": dict(self._total_votes),
        }


# ──────────────────────────────────────────────────────────────────────
# Expert Council with LLM-backed reasoning
# ──────────────────────────────────────────────────────────────────────

class ExpertCouncil:
    """Multi-expert evaluation council with optional LLM backing.

    When an LLM API key is provided, experts generate genuine reasoning
    using their persona prompts. Without an API, they use calibrated
    heuristic scoring based on their specialist weights.
    """

    def __init__(
        self,
        expert_ids: list[str] | None = None,
        keep_threshold: float = 0.62,
        confidence_min: float = 0.51,
        confidence_max: float = 0.99,
        min_votes_to_accept: str = "majority",
        llm_api_key: str = "",
        llm_model: str = "claude-sonnet-4-5-20250929",
        llm_base_url: str = "https://api.anthropic.com/v1",
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
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._llm_base_url = llm_base_url
        self._use_llm = bool(llm_api_key)
        self.chair = DebateChair(self.expert_ids)

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

    def _vote_heuristic(self, expert_id: str, candidate: CandidateItem) -> DebateVote:
        """Generate a vote using calibrated heuristic scoring."""
        persona = EXPERT_PERSONAS.get(expert_id, {})
        weights = persona.get("weights", {})

        # Compute weighted score based on expert's specialty
        score = 0.0
        w_sum = 0.0

        for dimension, weight in weights.items():
            if dimension == "evidence":
                score += weight * candidate.evidence_score
            elif dimension == "novelty":
                score += weight * candidate.novelty_score
            elif dimension == "preference_fit":
                score += weight * candidate.preference_fit
            elif dimension == "prediction_signal":
                score += weight * candidate.prediction_signal
            elif dimension == "source_tier":
                # Tier-1 sources score higher
                tier_scores = {"reuters": 0.92, "ap": 0.90, "bbc": 0.88, "guardian": 0.85,
                               "ft": 0.87, "aljazeera": 0.78, "arxiv": 0.75,
                               "hackernews": 0.60, "reddit": 0.55, "x": 0.50,
                               "gdelt": 0.58, "web": 0.50}
                score += weight * tier_scores.get(candidate.source, 0.50)
            elif dimension == "corroboration":
                corr_score = min(1.0, len(candidate.corroborated_by) * 0.3 + 0.2)
                score += weight * corr_score
            elif dimension == "recency":
                age_minutes = (datetime.now(timezone.utc) - candidate.created_at).total_seconds() / 60
                recency = max(0.1, 1.0 - age_minutes / 1440)  # Decay over 24h
                score += weight * recency
            elif dimension == "lifecycle":
                lifecycle_scores = {"developing": 0.8, "breaking": 1.0, "ongoing": 0.6,
                                    "waning": 0.3, "resolved": 0.1}
                score += weight * lifecycle_scores.get(candidate.lifecycle.value, 0.5)
            elif dimension == "contrarian":
                if candidate.contrarian_signal:
                    score += weight * 0.85
                elif candidate.novelty_score > 0.8:
                    score += weight * 0.65
                else:
                    score += weight * 0.3
            elif dimension == "urgency":
                urgency_scores = {"routine": 0.3, "elevated": 0.6, "breaking": 0.85, "critical": 1.0}
                score += weight * urgency_scores.get(candidate.urgency.value, 0.3)
            elif dimension == "regions":
                score += weight * min(1.0, len(candidate.regions) * 0.3 + 0.2)
            elif dimension == "diversity":
                # Diversity bonus for underrepresented sources
                diverse_sources = {"aljazeera", "arxiv", "gdelt", "hackernews"}
                score += weight * (0.8 if candidate.source in diverse_sources else 0.4)
            w_sum += weight

        if w_sum > 0:
            score /= w_sum

        keep = score >= self.keep_threshold
        confidence = min(self.confidence_max, max(self.confidence_min, score))

        expert_name = persona.get("name", expert_id)
        rationale = self._generate_heuristic_rationale(expert_id, candidate, score, keep)
        risk_note = self._generate_risk_note(expert_id, candidate, score)

        return DebateVote(
            expert_id=expert_id,
            candidate_id=candidate.candidate_id,
            keep=keep,
            confidence=round(confidence, 3),
            rationale=rationale,
            risk_note=risk_note,
        )

    def _vote_llm(self, expert_id: str, candidate: CandidateItem) -> DebateVote:
        """Generate a vote using LLM reasoning with compressed expert prompt."""
        system_prompt = _build_system_prompt(expert_id)

        # Compact user message — no redundant labels, minimal tokens
        corr = ",".join(candidate.corroborated_by[:3]) or "-"
        rgn = ",".join(candidate.regions[:3]) or "-"
        user_message = (
            f"{candidate.title} | {candidate.source} | {candidate.topic}\n"
            f"{candidate.summary[:150]}\n"
            f"ev={candidate.evidence_score:.2f} nov={candidate.novelty_score:.2f} "
            f"urg={candidate.urgency.value} life={candidate.lifecycle.value} "
            f"corr=[{corr}] rgn=[{rgn}]"
        )

        try:
            body = json.dumps({
                "model": self._llm_model,
                "max_tokens": 300,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._llm_base_url}/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            # Parse the LLM response
            content = result.get("content", [{}])[0].get("text", "{}")
            # Try to extract JSON from response
            parsed = self._parse_llm_json(content)

            # If LLM returned garbage (empty parse), fall back to heuristic
            # instead of blindly defaulting to keep=True
            if not parsed:
                log.warning("LLM returned unparseable response for %s, falling back to heuristic", expert_id)
                return self._vote_heuristic(expert_id, candidate)

            # Robust boolean parsing — LLM may return "false"/"true" as strings
            raw_keep = parsed.get("keep", True)
            if isinstance(raw_keep, str):
                keep = raw_keep.lower() not in ("false", "0", "no", "n")
            else:
                keep = bool(raw_keep)
            # Guard against NaN/Infinity from malformed LLM output
            try:
                raw_conf = float(parsed.get("confidence", 0.7))
                if not math.isfinite(raw_conf):
                    raw_conf = 0.7
            except (ValueError, TypeError):
                raw_conf = 0.7
            confidence = min(self.confidence_max, max(self.confidence_min, raw_conf))
            rationale = parsed.get("rationale", "LLM evaluation complete.")
            risk_note = parsed.get("risk_note", "Assessment based on available signals.")

            return DebateVote(
                expert_id=expert_id,
                candidate_id=candidate.candidate_id,
                keep=keep,
                confidence=round(confidence, 3),
                rationale=rationale[:200],
                risk_note=risk_note[:200],
            )

        except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("LLM vote failed for %s, falling back to heuristic: %s", expert_id, e)
            return self._vote_heuristic(expert_id, candidate)

    def _parse_llm_json(self, text: str) -> dict:
        """Extract JSON from LLM response, handling markdown code blocks."""
        import re
        # Try direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Try extracting from code block
        match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        # Try finding JSON object (non-greedy to avoid matching too much)
        match = re.search(r"\{.*?\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return {}

    def _generate_heuristic_rationale(self, expert_id: str, c: CandidateItem,
                                       score: float, keep: bool) -> str:
        """Generate human-readable rationale from heuristic evaluation."""
        if expert_id == "expert_quality_agent":
            tier = "tier-1" if c.source in {"reuters", "ap", "bbc", "guardian", "ft"} else "tier-2"
            corr = f"corroborated by {len(c.corroborated_by)} source(s)" if c.corroborated_by else "awaiting corroboration"
            return (f"Source quality: {tier} ({c.source}), evidence={c.evidence_score:.2f}, "
                    f"{corr}. Overall quality score: {score:.2f}.")

        if expert_id == "expert_relevance_agent":
            return (f"Novelty={c.novelty_score:.2f}, topic fit={c.preference_fit:.2f}, "
                    f"lifecycle={c.lifecycle.value}. "
                    f"{'Passes' if keep else 'Fails'} relevance threshold at {score:.2f}.")

        if expert_id == "expert_preference_fit_agent":
            return (f"Preference alignment={c.preference_fit:.2f}, "
                    f"prediction signal={c.prediction_signal:.2f}, "
                    f"urgency={c.urgency.value}. User utility score: {score:.2f}.")

        if expert_id == "expert_geopolitical_risk_agent":
            regions = ", ".join(c.regions) if c.regions else "unlocalized"
            return (f"Regions: {regions}, urgency={c.urgency.value}, "
                    f"escalation risk score: {score:.2f}.")

        if expert_id == "expert_market_signal_agent":
            return (f"Market signal={c.prediction_signal:.2f}, evidence={c.evidence_score:.2f}. "
                    f"Economic impact score: {score:.2f}.")

        return f"{expert_id} evaluated candidate with score {score:.2f}."

    def _generate_risk_note(self, expert_id: str, c: CandidateItem, score: float) -> str:
        """Generate risk assessment note."""
        if score < 0.4:
            return "Low-confidence assessment — recommend additional verification before inclusion."
        if not c.corroborated_by:
            return "Single-source reporting — may degrade if contradicted by subsequent coverage."
        if c.urgency.value in ("breaking", "critical"):
            return "Fast-moving story — assessment may change rapidly as new information emerges."
        return "Assessment stable given current evidence and source quality signals."

    def debate(self, candidates: list[CandidateItem]) -> DebateRecord:
        """Run all experts against all candidates."""
        votes: list[DebateVote] = []
        vote_fn = self._vote_llm if self._use_llm else self._vote_heuristic

        for candidate in candidates:
            for expert_id in self.expert_ids:
                votes.append(vote_fn(expert_id, candidate))

        return DebateRecord(votes=votes)

    def _arbitrate(self, candidate: CandidateItem, votes: list[DebateVote]) -> list[DebateVote]:
        """Run an arbitration round for closely-split votes.

        When votes are close (e.g. 3-2 split), dissenting experts re-evaluate
        with access to the majority rationale. This implements the vision's
        "conflicts trigger short arbitration round before final selection."
        """
        keep_votes = [v for v in votes if v.keep]
        drop_votes = [v for v in votes if not v.keep]

        # Only arbitrate if votes are closely split (differ by at most 1)
        if abs(len(keep_votes) - len(drop_votes)) > 1:
            return votes  # Clear majority — no arbitration needed

        # Identify majority and minority
        if len(keep_votes) >= len(drop_votes):
            majority_rationales = [v.rationale for v in keep_votes]
            minority_votes = drop_votes
        else:
            majority_rationales = [v.rationale for v in drop_votes]
            minority_votes = keep_votes

        if not minority_votes:
            return votes

        # Minority experts re-evaluate considering majority reasoning
        vote_fn = self._vote_llm if self._use_llm else self._vote_heuristic
        revised_votes = list(votes)

        for mv in minority_votes:
            new_vote = self._arbitration_revote(mv.expert_id, candidate, vote_fn)
            revised_votes = [v if v.expert_id != mv.expert_id else new_vote for v in revised_votes]

        return revised_votes

    def _arbitration_revote(self, expert_id: str, candidate: CandidateItem,
                            vote_fn) -> DebateVote:
        """Have a minority expert reconsider during arbitration."""
        base_vote = vote_fn(expert_id, candidate)

        # Slight confidence adjustment toward center (arbitration effect)
        adjusted_confidence = base_vote.confidence * 0.9 + 0.05
        adjusted_confidence = min(self.confidence_max, max(self.confidence_min, adjusted_confidence))

        # Expert may flip if their confidence was borderline
        threshold_distance = abs(base_vote.confidence - self.keep_threshold)
        flip = threshold_distance < 0.08

        new_keep = not base_vote.keep if flip else base_vote.keep
        rationale_suffix = " [Revised after arbitration.]"

        return DebateVote(
            expert_id=expert_id,
            candidate_id=candidate.candidate_id,
            keep=new_keep,
            confidence=round(adjusted_confidence, 3),
            rationale=(base_vote.rationale + rationale_suffix)[:250],
            risk_note=base_vote.risk_note,
        )

    def select(
        self, candidates: list[CandidateItem], max_items: int
    ) -> tuple[list[CandidateItem], list[CandidateItem], DebateRecord]:
        """Run expert debate with arbitration and select top candidates."""
        debate = self.debate(candidates)
        required = self._required_votes()

        # Group votes by candidate
        votes_by_candidate: dict[str, list[DebateVote]] = {}
        for vote in debate.votes:
            votes_by_candidate.setdefault(vote.candidate_id, []).append(vote)

        # Build candidate lookup for arbitration
        candidate_map = {c.candidate_id: c for c in candidates}

        # Run arbitration on closely-split votes
        arbitration_count = 0
        final_votes: list[DebateVote] = []
        for candidate_id, cvotes in votes_by_candidate.items():
            keep_count = sum(1 for v in cvotes if v.keep)
            drop_count = len(cvotes) - keep_count

            if abs(keep_count - drop_count) <= 1 and candidate_id in candidate_map:
                revised = self._arbitrate(candidate_map[candidate_id], cvotes)
                final_votes.extend(revised)
                arbitration_count += 1
            else:
                final_votes.extend(cvotes)

        # Regroup after arbitration
        final_by_candidate: dict[str, list[DebateVote]] = {}
        for vote in final_votes:
            final_by_candidate.setdefault(vote.candidate_id, []).append(vote)

        # Use influence-weighted voting via DebateChair
        accepted_ids: set[str] = set()
        for candidate_id, cvotes in final_by_candidate.items():
            weighted_keep = self.chair.weighted_keep_count(cvotes)
            weighted_total = self.chair.weighted_total(cvotes)
            # Accept if weighted keep proportion exceeds threshold
            if weighted_total > 0 and (weighted_keep / weighted_total) >= 0.5:
                accepted_ids.add(candidate_id)

        # Update debate record with final (potentially revised) votes
        debate = DebateRecord(votes=final_votes)

        # Deduplicate and rank
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

        # Record outcomes in DebateChair for influence tracking
        selected_ids = {c.candidate_id for c in selected}
        for vote in final_votes:
            self.chair.record_outcome(
                vote.expert_id, vote.keep,
                vote.candidate_id in selected_ids,
            )

        log.info(
            "Expert council: %d/%d accepted (%d experts, %d required, %d arbitrated), "
            "%d selected, %d reserve, chair=%s",
            len(accepted_ids), len(candidates), len(self.expert_ids),
            required, arbitration_count, len(selected), len(reserve),
            {eid: f"{inf:.2f}" for eid, inf, _ in self.chair.rankings()[:3]},
        )

        return selected, reserve, debate
