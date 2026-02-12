from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from newsfeed.agents.simulated import ExpertCouncil, SimulatedResearchAgent
from newsfeed.delivery.telegram import TelegramFormatter
from newsfeed.intelligence.clustering import StoryClustering
from newsfeed.intelligence.credibility import (
    CredibilityTracker,
    detect_cross_corroboration,
    enforce_source_diversity,
)
from newsfeed.intelligence.georisk import GeoRiskIndex
from newsfeed.intelligence.trends import TrendDetector
from newsfeed.intelligence.urgency import BreakingDetector
from newsfeed.memory.commands import parse_preference_commands
from newsfeed.memory.store import CandidateCache, PreferenceStore
from newsfeed.models.domain import (
    BriefingType,
    ConfidenceBand,
    DeliveryPayload,
    ReportItem,
    ResearchTask,
    UrgencyLevel,
)
from newsfeed.review.personas import PersonaReviewStack


class NewsFeedEngine:
    def __init__(self, config: dict, pipeline: dict, personas: dict, personas_dir: Path) -> None:
        self.config = config
        self.pipeline = pipeline
        self.personas = personas
        stale_after = pipeline.get("cache_policy", {}).get("stale_after_minutes", 180)
        expert_ids = [e.get("id") for e in config.get("expert_agents", []) if e.get("id")]
        self.preferences = PreferenceStore()
        self.cache = CandidateCache(stale_after_minutes=stale_after)
        self.experts = ExpertCouncil(expert_ids=expert_ids)
        self.formatter = TelegramFormatter()
        self.review_stack = PersonaReviewStack(
            personas_dir=personas_dir,
            active_personas=personas.get("default_personas", []),
            persona_notes=personas.get("persona_notes", {}),
        )

        intel_cfg = pipeline.get("intelligence", {})
        self.credibility = CredibilityTracker()
        self.breaking_detector = BreakingDetector(
            velocity_window_minutes=intel_cfg.get("velocity_window_minutes", 30),
            breaking_source_threshold=intel_cfg.get("breaking_source_threshold", 3),
        )
        self.clustering = StoryClustering(
            similarity_threshold=intel_cfg.get("clustering_similarity", 0.6),
        )
        self.georisk = GeoRiskIndex()
        self.trends = TrendDetector(
            window_minutes=intel_cfg.get("trend_window_minutes", 60),
            anomaly_threshold=intel_cfg.get("anomaly_threshold", 2.0),
        )

    def _research_agents(self) -> list[SimulatedResearchAgent]:
        agents = []
        for a in self.config.get("research_agents", []):
            agents.append(SimulatedResearchAgent(a["id"], a["source"], a["mandate"]))
        return agents

    async def _run_research_async(self, task: ResearchTask, top_k: int) -> list:
        coros = [agent.run_async(task, top_k=top_k) for agent in self._research_agents()]
        batch = await asyncio.gather(*coros)
        flattened = []
        for chunk in batch:
            flattened.extend(chunk)
        return flattened

    def handle_request(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> str:
        profile = self.preferences.get_or_create(user_id)
        limit = min(max_items or profile.max_items, self.pipeline.get("limits", {}).get("default_max_items", 10))
        task = ResearchTask(
            request_id=f"req-{int(datetime.now(timezone.utc).timestamp())}",
            user_id=user_id,
            prompt=prompt,
            weighted_topics=weighted_topics,
        )

        # Stage 1: Research fan-out
        top_k = self.pipeline.get("limits", {}).get("top_discoveries_per_research_agent", 5)
        all_candidates = asyncio.run(self._run_research_async(task, top_k))

        # Stage 2: Intelligence enrichment
        for c in all_candidates:
            self.credibility.record_item(c)

        all_candidates = detect_cross_corroboration(all_candidates)
        all_candidates = self.breaking_detector.assess(all_candidates)
        max_per_source = self.pipeline.get("intelligence", {}).get("max_items_per_source", 3)
        all_candidates = enforce_source_diversity(all_candidates, max_per_source=max_per_source)

        # Stage 3: Expert council selection
        selected, reserve, debate = self.experts.select(all_candidates, limit)

        dominant_topic = max(weighted_topics, key=weighted_topics.get, default="general")
        self.cache.put(user_id, dominant_topic, reserve)

        # Stage 4: Narrative threading
        threads = self.clustering.cluster(selected)

        # Stage 5: Geo-risk assessment
        geo_risks = self.georisk.assess(all_candidates)

        # Stage 6: Trend analysis
        trend_snapshots = self.trends.analyze(all_candidates)

        # Stage 7: Report assembly with intelligence enrichment
        report_items = []
        adjacent_bounds = self.pipeline.get("limits", {}).get("adjacent_reads_per_item", {"min": 2, "max": 3})
        adjacent_count = adjacent_bounds.get("max", 3)

        thread_map: dict[str, str] = {}
        for thread in threads:
            for c in thread.candidates:
                thread_map[c.candidate_id] = thread.thread_id

        for c in selected:
            reads = [f"Context read {i + 1} for {c.topic}" for i in range(adjacent_count)]
            why = self.review_stack.refine_why(
                f"Aligned with your weighted interest in {c.topic} and strong source quality."
            )
            outlook = self.review_stack.refine_outlook(
                "Market and narrative signals suggest elevated watch priority."
            )

            cred_score = self.credibility.score_candidate(c)
            confidence = ConfidenceBand(
                low=round(max(0.0, cred_score - 0.15), 3),
                mid=round(cred_score, 3),
                high=round(min(1.0, cred_score + 0.15), 3),
                key_assumptions=self._build_assumptions(c),
            )

            contrarian = ""
            if c.contrarian_signal:
                contrarian = c.contrarian_signal
            elif c.novelty_score > 0.8 and c.evidence_score < 0.6:
                contrarian = "High novelty but limited evidence — monitor for confirmation."

            report_items.append(
                ReportItem(
                    candidate=c,
                    why_it_matters=why,
                    what_changed="New cross-source confirmation and discussion momentum since last cycle.",
                    predictive_outlook=outlook,
                    adjacent_reads=reads,
                    confidence=confidence,
                    thread_id=thread_map.get(c.candidate_id),
                    contrarian_note=contrarian,
                )
            )

        # Stage 8: Determine briefing type
        briefing_type = self._determine_briefing_type(selected)

        payload = DeliveryPayload(
            user_id=user_id,
            generated_at=datetime.now(timezone.utc),
            items=report_items,
            metadata={
                "tone": profile.tone,
                "format": profile.format,
                "debate_vote_count": len(debate.votes),
                "selected_count": len(selected),
                "review_personas": self.personas.get("default_personas", []),
                "thread_count": len(threads),
                "geo_risk_regions": len(geo_risks),
                "emerging_trends": sum(1 for t in trend_snapshots if t.is_emerging),
                "intelligence_stages": [
                    "credibility", "corroboration", "urgency",
                    "diversity", "clustering", "georisk", "trends",
                ],
            },
            briefing_type=briefing_type,
            threads=threads,
            geo_risks=geo_risks,
            trends=trend_snapshots,
        )
        return self.formatter.format(payload)

    def show_more(self, user_id: str, topic: str, already_seen_ids: set[str], limit: int = 5) -> list[str]:
        more = self.cache.get_more(user_id=user_id, topic=topic, already_seen_ids=already_seen_ids, limit=limit)
        return [f"{c.title} ({c.source})" for c in more]

    def apply_user_feedback(self, user_id: str, feedback_text: str) -> dict[str, str]:
        profile = self.preferences.get_or_create(user_id)
        results: dict[str, str] = {}
        commands = parse_preference_commands(feedback_text)

        for cmd in commands:
            if cmd.action == "topic_delta" and cmd.topic and cmd.value:
                delta = float(cmd.value)
                updated = self.preferences.apply_weight_adjustment(user_id, cmd.topic, delta)
                results[f"topic:{cmd.topic}"] = str(updated.topic_weights.get(cmd.topic, 0.0))
            elif cmd.action == "tone" and cmd.value:
                self.preferences.apply_style_update(user_id, tone=cmd.value)
                results["tone"] = cmd.value
            elif cmd.action == "format" and cmd.value:
                self.preferences.apply_style_update(user_id, fmt=cmd.value)
                results["format"] = cmd.value

        _ = profile
        return results

    def _build_assumptions(self, c) -> list[str]:
        assumptions = []
        if c.corroborated_by:
            assumptions.append(f"Corroborated by {len(c.corroborated_by)} independent source(s)")
        else:
            assumptions.append("Awaiting independent corroboration")

        sr = self.credibility.get_source(c.source)
        if sr.reliability_score >= 0.8:
            assumptions.append(f"Source ({c.source}) rated high reliability")
        elif sr.reliability_score < 0.6:
            assumptions.append(f"Source ({c.source}) rated lower reliability — verify independently")

        return assumptions

    def _determine_briefing_type(self, selected) -> BriefingType:
        critical_count = sum(1 for c in selected if c.urgency == UrgencyLevel.CRITICAL)
        breaking_count = sum(1 for c in selected if c.urgency == UrgencyLevel.BREAKING)

        if critical_count >= 1:
            return BriefingType.BREAKING_ALERT
        if breaking_count >= 2:
            return BriefingType.BREAKING_ALERT
        return BriefingType.MORNING_DIGEST
