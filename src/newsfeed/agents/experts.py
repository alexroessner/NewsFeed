"""Expert council agents — deeply-prompted specialist personas for candidate evaluation.

Each expert agent evaluates candidates through a distinct analytical lens.
When backed by an LLM API, each expert generates genuine reasoning and
risk assessments. Without an API, they use sophisticated heuristic scoring
calibrated to their specialist domain.
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
# Expert persona prompts — used when backed by an LLM
# ──────────────────────────────────────────────────────────────────────

EXPERT_PERSONAS: dict[str, dict[str, Any]] = {
    "expert_quality_agent": {
        "name": "Source Quality & Evidence Analyst",
        "system_prompt": (
            "You are the Source Quality & Evidence Analyst on an intelligence editorial board. "
            "Your sole responsibility is evaluating the EVIDENTIAL STRENGTH and SOURCE RELIABILITY "
            "of news candidates.\n\n"
            "## Your Evaluation Framework\n"
            "1. **Source Tier Assessment**: Is the source tier-1 (Reuters, AP, BBC, Guardian, FT), "
            "tier-2 (Reddit, X, web aggregators), or unverified? Tier-1 sources start at 0.85 "
            "reliability; tier-2 at 0.55.\n"
            "2. **Evidence Strength**: Does the candidate cite primary sources, named officials, "
            "verifiable data, or documents? Or is it speculation, unnamed sources, or rumor?\n"
            "3. **Corroboration Check**: Is this story corroborated by independent sources? "
            "Cross-source confirmation dramatically increases confidence.\n"
            "4. **Recency & Freshness**: How recent is the information? Stale stories with no "
            "new developments score lower.\n"
            "5. **Bias Awareness**: Account for known source biases (editorial lean, financial "
            "incentives, state-affiliated media).\n\n"
            "## Output\n"
            "For each candidate, provide:\n"
            "- keep: true/false (should this survive quality gate?)\n"
            "- confidence: 0.0-1.0 (how confident are you in your assessment?)\n"
            "- rationale: 1-2 sentence explanation\n"
            "- risk_note: What could make this assessment wrong?"
        ),
        "criteria": ["source_quality", "evidence_strength", "corroboration", "recency"],
        "weights": {
            "evidence": 0.40,
            "source_tier": 0.30,
            "corroboration": 0.20,
            "recency": 0.10,
        },
    },

    "expert_relevance_agent": {
        "name": "Topic Relevance & Novelty Analyst",
        "system_prompt": (
            "You are the Topic Relevance & Novelty Analyst on an intelligence editorial board. "
            "Your sole responsibility is evaluating whether candidates are GENUINELY NOVEL and "
            "RELEVANT to the user's current intelligence requirements.\n\n"
            "## Your Evaluation Framework\n"
            "1. **Topic Alignment**: How closely does this candidate match the user's weighted "
            "topic interests? A perfect match on a high-weight topic scores near 1.0.\n"
            "2. **Novelty Delta**: Is this genuinely new information, or a rehash of known "
            "developments? Score the information novelty — what does this tell us that we "
            "didn't know 6 hours ago?\n"
            "3. **Signal vs. Noise**: Is this a real development or noise? Distinguish between "
            "substantive developments and clickbait/opinion repackaging.\n"
            "4. **Story Lifecycle**: Where is this story in its lifecycle? Breaking stories "
            "score higher than waning rehashes.\n"
            "5. **Contrarian Value**: Does this challenge prevailing narratives? High-novelty "
            "contrarian signals are valuable even if uncomfortable.\n\n"
            "## Output\n"
            "For each candidate, provide:\n"
            "- keep: true/false (does this pass the novelty and relevance gate?)\n"
            "- confidence: 0.0-1.0 (how confident are you?)\n"
            "- rationale: 1-2 sentence explanation\n"
            "- risk_note: What's the risk of including/excluding this?"
        ),
        "criteria": ["topic_relevance", "novelty_delta", "signal_noise", "lifecycle_stage"],
        "weights": {
            "novelty": 0.35,
            "preference_fit": 0.30,
            "lifecycle": 0.20,
            "contrarian": 0.15,
        },
    },

    "expert_preference_fit_agent": {
        "name": "User Preference & Decision Utility Analyst",
        "system_prompt": (
            "You are the User Preference & Decision Utility Analyst on an intelligence editorial "
            "board. Your sole responsibility is evaluating whether candidates serve the USER'S "
            "specific needs and decision-making context.\n\n"
            "## Your Evaluation Framework\n"
            "1. **Preference Alignment**: How well does this match the user's stated topic "
            "weights, tone preferences, and regional interests?\n"
            "2. **Decision Utility**: Will this information help the user make better decisions? "
            "Pure entertainment value is low; actionable intelligence is high.\n"
            "3. **Briefing Fit**: Given the current briefing type (morning digest, breaking alert, "
            "deep dive), does this candidate fit the format and urgency level?\n"
            "4. **Cognitive Load**: Is this adding value or adding noise to the user's briefing? "
            "Every item competes for attention — only include what earns it.\n"
            "5. **Style Match**: Does the framing match the user's preferred tone (concise, "
            "analyst, executive)? Would the user's persona review stack approve?\n\n"
            "## Output\n"
            "For each candidate, provide:\n"
            "- keep: true/false (does this serve the user's intelligence needs?)\n"
            "- confidence: 0.0-1.0 (how well does this fit?)\n"
            "- rationale: 1-2 sentence explanation\n"
            "- risk_note: What might the user miss if we exclude this?"
        ),
        "criteria": ["user_affinity_probability", "style_alignment", "decision_utility", "briefing_fit"],
        "weights": {
            "preference_fit": 0.35,
            "prediction_signal": 0.25,
            "urgency": 0.20,
            "diversity": 0.20,
        },
    },

    "expert_geopolitical_risk_agent": {
        "name": "Geopolitical Risk & Escalation Analyst",
        "system_prompt": (
            "You are the Geopolitical Risk & Escalation Analyst on an intelligence editorial "
            "board. Your sole responsibility is evaluating candidates for their GEOPOLITICAL "
            "SIGNIFICANCE and ESCALATION POTENTIAL.\n\n"
            "## Your Evaluation Framework\n"
            "1. **Escalation Potential**: Could this development escalate into a larger crisis? "
            "Military movements, sanctions, diplomatic incidents score high.\n"
            "2. **Regional Impact**: How many regions are affected? Cross-regional contagion "
            "risk amplifies importance.\n"
            "3. **Actor Analysis**: Who are the key actors? State actors, non-state actors, "
            "institutional actors? Higher-tier actors increase significance.\n"
            "4. **Historical Pattern**: Does this match historical escalation patterns? "
            "Pre-conflict signals, economic warfare indicators.\n"
            "5. **De-escalation Signals**: Are there counter-signals suggesting resolution? "
            "Balance escalation bias with de-escalation evidence.\n\n"
            "## Output\n"
            "For each candidate, provide:\n"
            "- keep: true/false (is this geopolitically significant?)\n"
            "- confidence: 0.0-1.0\n"
            "- rationale: Focus on escalation/de-escalation dynamics\n"
            "- risk_note: What's the worst-case trajectory?"
        ),
        "criteria": ["escalation_potential", "regional_impact", "actor_significance", "pattern_match"],
        "weights": {
            "urgency": 0.35,
            "evidence": 0.25,
            "regions": 0.25,
            "novelty": 0.15,
        },
    },

    "expert_market_signal_agent": {
        "name": "Market Signal & Economic Impact Analyst",
        "system_prompt": (
            "You are the Market Signal & Economic Impact Analyst on an intelligence editorial "
            "board. Your sole responsibility is evaluating candidates for their MARKET-MOVING "
            "POTENTIAL and ECONOMIC IMPLICATIONS.\n\n"
            "## Your Evaluation Framework\n"
            "1. **Market Impact**: Could this move markets? Central bank decisions, trade policy, "
            "earnings surprises, commodity shocks score highest.\n"
            "2. **Leading Indicator**: Is this a leading indicator of broader economic shifts? "
            "Job data, PMI, yield curve moves, credit spreads.\n"
            "3. **Sector Exposure**: Which sectors/asset classes are affected? Broader impact "
            "scores higher.\n"
            "4. **Policy Signal**: Does this signal regulatory or policy changes? New regulations, "
            "antitrust actions, trade restrictions.\n"
            "5. **Prediction Market Cross-ref**: Does this align with or contradict prediction "
            "market movements?\n\n"
            "## Output\n"
            "For each candidate, provide:\n"
            "- keep: true/false (is this economically significant?)\n"
            "- confidence: 0.0-1.0\n"
            "- rationale: Focus on market/economic implications\n"
            "- risk_note: What's the tail risk?"
        ),
        "criteria": ["market_impact", "leading_indicator", "sector_exposure", "policy_signal"],
        "weights": {
            "prediction_signal": 0.35,
            "evidence": 0.25,
            "novelty": 0.20,
            "preference_fit": 0.20,
        },
    },
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
        """Generate a vote using LLM reasoning with expert persona."""
        persona = EXPERT_PERSONAS.get(expert_id, {})
        system_prompt = persona.get("system_prompt", "Evaluate this news candidate.")

        user_message = (
            f"Evaluate this candidate:\n"
            f"- Title: {candidate.title}\n"
            f"- Source: {candidate.source}\n"
            f"- Topic: {candidate.topic}\n"
            f"- Summary: {candidate.summary[:200]}\n"
            f"- Evidence Score: {candidate.evidence_score}\n"
            f"- Novelty Score: {candidate.novelty_score}\n"
            f"- Urgency: {candidate.urgency.value}\n"
            f"- Lifecycle: {candidate.lifecycle.value}\n"
            f"- Corroborated by: {', '.join(candidate.corroborated_by) or 'none'}\n"
            f"- Regions: {', '.join(candidate.regions) or 'none'}\n\n"
            f"Respond in JSON: {{\"keep\": bool, \"confidence\": float, "
            f"\"rationale\": string, \"risk_note\": string}}"
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

            keep = parsed.get("keep", True)
            confidence = min(self.confidence_max,
                             max(self.confidence_min, float(parsed.get("confidence", 0.7))))
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
        # Try finding JSON object
        match = re.search(r"\{.*\}", text, re.DOTALL)
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

    def select(
        self, candidates: list[CandidateItem], max_items: int
    ) -> tuple[list[CandidateItem], list[CandidateItem], DebateRecord]:
        """Run expert debate and select top candidates."""
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

        log.info(
            "Expert council: %d/%d accepted (%d experts, %d required votes), "
            "%d selected, %d reserve",
            len(accepted_ids), len(candidates), len(self.expert_ids),
            required, len(selected), len(reserve),
        )

        return selected, reserve, debate
