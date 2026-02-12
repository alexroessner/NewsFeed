from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.experts import ExpertCouncil
from newsfeed.agents.registry import create_agent
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
from newsfeed.memory.store import CandidateCache, PreferenceStore, StatePersistence
from newsfeed.models.domain import (
    BriefingType,
    CandidateItem,
    ConfidenceBand,
    DeliveryPayload,
    ReportItem,
    ResearchTask,
    UrgencyLevel,
    UserProfile,
    configure_scoring,
    validate_candidate,
)
from newsfeed.orchestration.communication import CommunicationAgent
from newsfeed.orchestration.orchestrator import OrchestratorAgent, RequestStage
from newsfeed.orchestration.optimizer import SystemOptimizationAgent
from newsfeed.review.agents import ClarityReviewAgent, StyleReviewAgent
from newsfeed.review.personas import PersonaReviewStack

log = logging.getLogger(__name__)


def _run_sync(coro: object) -> object:
    """Run a coroutine safely, whether or not an event loop is already running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # Already inside an async context — run agents synchronously to avoid
        # nested event-loop errors.
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class NewsFeedEngine:
    def __init__(self, config: dict, pipeline: dict, personas: dict, personas_dir: Path) -> None:
        self.config = config
        self.pipeline = pipeline
        self.personas = personas

        log.info("Initializing NewsFeedEngine (config v%s)", pipeline.get("version", "?"))

        # Inject scoring config into domain models
        scoring_cfg = pipeline.get("scoring", {})
        configure_scoring(scoring_cfg)

        stale_after = pipeline.get("cache_policy", {}).get("stale_after_minutes", 180)
        expert_ids = [e.get("id") for e in config.get("expert_agents", []) if e.get("id")]

        self.preferences = PreferenceStore()
        self.cache = CandidateCache(stale_after_minutes=stale_after)

        # Expert council with configurable thresholds and optional LLM backing
        ec_cfg = pipeline.get("expert_council", {})
        api_keys = pipeline.get("api_keys", {})
        self.experts = ExpertCouncil(
            expert_ids=expert_ids,
            keep_threshold=ec_cfg.get("keep_threshold", 0.62),
            confidence_min=ec_cfg.get("confidence_min", 0.51),
            confidence_max=ec_cfg.get("confidence_max", 0.99),
            min_votes_to_accept=ec_cfg.get("min_votes_to_accept", "majority"),
            llm_api_key=api_keys.get("anthropic_api_key", ""),
            llm_model=ec_cfg.get("llm_model", "claude-sonnet-4-5-20250929"),
            llm_base_url=ec_cfg.get("llm_base_url", "https://api.anthropic.com/v1"),
        )

        # Telegram bot (initialized only when token is available)
        self._bot = None
        self._comm_agent = None
        telegram_token = api_keys.get("telegram_bot_token", "")
        if telegram_token:
            from newsfeed.delivery.bot import BriefingScheduler, TelegramBot
            self._bot = TelegramBot(bot_token=telegram_token)
            self._scheduler = BriefingScheduler()
            self._comm_agent = CommunicationAgent(
                engine=self, bot=self._bot, scheduler=self._scheduler,
            )
            log.info("Telegram bot + communication agent initialized")

        self.formatter = TelegramFormatter()
        self.review_stack = PersonaReviewStack(
            personas_dir=personas_dir,
            active_personas=personas.get("default_personas", []),
            persona_notes=personas.get("persona_notes", {}),
        )

        # Review agents (editorial layer)
        llm_key = api_keys.get("anthropic_api_key", "")
        llm_model = ec_cfg.get("llm_model", "claude-sonnet-4-5-20250929")
        llm_base = ec_cfg.get("llm_base_url", "https://api.anthropic.com/v1")
        self._style_reviewer = StyleReviewAgent(
            persona_context=self.review_stack.active_context(),
            llm_api_key=llm_key, llm_model=llm_model, llm_base_url=llm_base,
        )
        self._clarity_reviewer = ClarityReviewAgent(
            llm_api_key=llm_key, llm_model=llm_model, llm_base_url=llm_base,
        )

        # Orchestrator agent (lifecycle management)
        self.orchestrator = OrchestratorAgent(
            agent_configs=config.get("research_agents", []),
            pipeline_cfg=pipeline,
        )

        # System optimization agent (health monitoring)
        self.optimizer = SystemOptimizationAgent()

        # Intelligence modules with full config propagation
        intel_cfg = pipeline.get("intelligence", {})
        self._enabled_stages = set(intel_cfg.get("enabled_stages", [
            "credibility", "corroboration", "urgency",
            "diversity", "clustering", "georisk", "trends",
        ]))

        self.credibility = CredibilityTracker(intel_cfg={
            **intel_cfg,
            "_scoring": scoring_cfg,
        })
        self.breaking_detector = BreakingDetector(
            velocity_window_minutes=intel_cfg.get("velocity_window_minutes", 30),
            breaking_source_threshold=intel_cfg.get("breaking_source_threshold", 3),
            urgency_keywords_cfg=pipeline.get("urgency_keywords"),
            velocity_thresholds=intel_cfg.get("urgency_velocity_thresholds"),
            recency_elevated_minutes=intel_cfg.get("recency_elevated_minutes", 5),
            waning_novelty_threshold=intel_cfg.get("waning_novelty_threshold", 0.3),
        )
        self.clustering = StoryClustering(
            similarity_threshold=intel_cfg.get("clustering_similarity", 0.6),
            cross_source_factor=intel_cfg.get("cross_source_similarity_factor", 0.7),
        )
        self.georisk = GeoRiskIndex(georisk_cfg=pipeline.get("georisk"))
        self.trends = TrendDetector(
            window_minutes=intel_cfg.get("trend_window_minutes", 60),
            anomaly_threshold=intel_cfg.get("anomaly_threshold", 2.0),
            baseline_decay=intel_cfg.get("baseline_decay", 0.8),
        )

        # Configurable thresholds
        self._confidence_offset = scoring_cfg.get("confidence_band_offset", 0.15)
        self._contrarian_novelty = intel_cfg.get("contrarian_novelty_threshold", 0.8)
        self._contrarian_evidence = intel_cfg.get("contrarian_evidence_threshold", 0.6)
        self._preference_deltas = pipeline.get("preference_deltas", {"more": 0.2, "less": -0.2})

        bt = pipeline.get("briefing_type_thresholds", {})
        self._bt_critical_min = bt.get("breaking_alert_critical_min", 1)
        self._bt_breaking_min = bt.get("breaking_alert_breaking_min", 2)

        # State persistence
        persist_cfg = pipeline.get("persistence", {})
        self._persistence: StatePersistence | None = None
        if persist_cfg.get("enabled", False):
            state_dir = Path(persist_cfg.get("state_dir", "state"))
            self._persistence = StatePersistence(state_dir)

        log.info(
            "Engine ready: %d agents, %d experts, stages=%s",
            len(config.get("research_agents", [])),
            len(expert_ids),
            ",".join(sorted(self._enabled_stages)),
        )

    def _research_agents(self) -> list[ResearchAgent]:
        api_keys = self.pipeline.get("api_keys", {})
        agents = []
        for a in self.config.get("research_agents", []):
            agent = create_agent(a, api_keys)
            agents.append(agent)
        return agents

    async def _run_research_async(self, task: ResearchTask, top_k: int) -> list:
        coros = [agent.run_async(task, top_k=top_k) for agent in self._research_agents()]
        batch = await asyncio.gather(*coros)
        flattened = []
        for chunk in batch:
            flattened.extend(chunk)
        return flattened

    def _run_research(self, task: ResearchTask, top_k: int) -> list[CandidateItem]:
        """Run research fan-out, safely handling sync/async contexts."""
        return _run_sync(self._run_research_async(task, top_k))

    def handle_request(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> str:
        log.info("handle_request user=%s prompt=%r", user_id, prompt[:80])
        profile = self.preferences.get_or_create(user_id)
        limit = min(max_items or profile.max_items, self.pipeline.get("limits", {}).get("default_max_items", 10))

        # Orchestrator compiles brief and tracks lifecycle
        task, lifecycle = self.orchestrator.compile_brief(user_id, prompt, profile, limit)
        # Override with caller-provided topics if specified
        if weighted_topics:
            task.weighted_topics = weighted_topics

        # Stage 1: Research fan-out
        lifecycle.advance(RequestStage.RESEARCHING)
        top_k = self.pipeline.get("limits", {}).get("top_discoveries_per_research_agent", 5)
        all_candidates = self._run_research(task, top_k)
        self.orchestrator.record_research_results(lifecycle, len(all_candidates))
        log.info("Research produced %d candidates", len(all_candidates))

        # Validate candidates from agents
        valid_candidates: list[CandidateItem] = []
        for c in all_candidates:
            issues = validate_candidate(c)
            if issues:
                log.warning("Candidate %s has issues: %s — skipping", c.candidate_id, "; ".join(issues))
            else:
                valid_candidates.append(c)
        all_candidates = valid_candidates

        # Stage 2: Intelligence enrichment (conditionally enabled, with error isolation)
        all_candidates = self._run_intelligence(all_candidates)

        # Stage 3: Expert council selection (with arbitration)
        lifecycle.advance(RequestStage.EXPERT_REVIEW)
        selected, reserve, debate = self.experts.select(all_candidates, limit)
        self.orchestrator.record_selection(lifecycle, len(selected))
        log.info("Expert council: %d selected, %d reserve, %d votes", len(selected), len(reserve), len(debate.votes))

        dominant_topic = max(task.weighted_topics, key=task.weighted_topics.get, default="general")
        self.cache.put(user_id, dominant_topic, reserve)

        # Stage 4: Narrative threading
        threads = []
        if "clustering" in self._enabled_stages:
            try:
                threads = self.clustering.cluster(selected)
            except Exception:
                log.exception("Clustering stage failed, continuing without threads")

        # Stage 5: Geo-risk assessment
        geo_risks = []
        if "georisk" in self._enabled_stages:
            try:
                geo_risks = self.georisk.assess(all_candidates)
            except Exception:
                log.exception("Georisk stage failed, continuing without geo risks")

        # Stage 6: Trend analysis
        trend_snapshots = []
        if "trends" in self._enabled_stages:
            try:
                trend_snapshots = self.trends.analyze(all_candidates)
            except Exception:
                log.exception("Trend stage failed, continuing without trends")

        # Stage 7: Report assembly with editorial review
        lifecycle.advance(RequestStage.EDITORIAL_REVIEW)
        report_items = self._assemble_report(selected, threads, profile)

        # Stage 8: Determine briefing type
        briefing_type = self._determine_briefing_type(selected)

        # Collect active intelligence stages for metadata
        active_stages = [s for s in [
            "credibility", "corroboration", "urgency",
            "diversity", "clustering", "georisk", "trends",
        ] if s in self._enabled_stages]

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
                "intelligence_stages": active_stages,
            },
            briefing_type=briefing_type,
            threads=threads,
            geo_risks=geo_risks,
            trends=trend_snapshots,
        )

        # Persist state if enabled
        if self._persistence:
            try:
                self._save_state()
            except Exception:
                log.exception("State persistence failed")

        # Record completion in orchestrator
        lifecycle.advance(RequestStage.FORMATTING)
        self.orchestrator.record_completion(lifecycle)

        log.info("Report generated: %d items, briefing=%s", len(report_items), briefing_type.value)
        return self.formatter.format(payload)

    def _run_intelligence(self, candidates: list[CandidateItem]) -> list[CandidateItem]:
        """Run intelligence enrichment stages with error isolation."""
        if "credibility" in self._enabled_stages:
            try:
                for c in candidates:
                    self.credibility.record_item(c)
            except Exception:
                log.exception("Credibility stage failed")

        if "corroboration" in self._enabled_stages:
            try:
                candidates = detect_cross_corroboration(candidates)
            except Exception:
                log.exception("Corroboration stage failed")

        if "urgency" in self._enabled_stages:
            try:
                candidates = self.breaking_detector.assess(candidates)
            except Exception:
                log.exception("Urgency stage failed")

        if "diversity" in self._enabled_stages:
            try:
                max_per_source = self.pipeline.get("intelligence", {}).get("max_items_per_source", 3)
                candidates = enforce_source_diversity(candidates, max_per_source=max_per_source)
            except Exception:
                log.exception("Diversity stage failed")

        return candidates

    def _assemble_report(self, selected: list[CandidateItem], threads: list,
                         profile: UserProfile | None = None) -> list[ReportItem]:
        """Assemble report items from selected candidates, then apply editorial review."""
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
            offset = self._confidence_offset
            confidence = ConfidenceBand(
                low=round(max(0.0, cred_score - offset), 3),
                mid=round(min(1.0, cred_score), 3),
                high=round(min(1.0, cred_score + offset), 3),
                key_assumptions=self._build_assumptions(c),
            )

            contrarian = ""
            if c.contrarian_signal:
                contrarian = c.contrarian_signal
            elif c.novelty_score > self._contrarian_novelty and c.evidence_score < self._contrarian_evidence:
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

        # Editorial review: style agent rewrites for tone/voice, clarity agent tightens
        if profile is None:
            profile = UserProfile(user_id="default")
        for item in report_items:
            try:
                self._style_reviewer.review(item, profile)
            except Exception:
                log.exception("Style review failed for %s", item.candidate.candidate_id)
            try:
                self._clarity_reviewer.review(item, profile)
            except Exception:
                log.exception("Clarity review failed for %s", item.candidate.candidate_id)

        return report_items

    def show_more(self, user_id: str, topic: str, already_seen_ids: set[str], limit: int = 5) -> list[str]:
        more = self.cache.get_more(user_id=user_id, topic=topic, already_seen_ids=already_seen_ids, limit=limit)
        return [f"{c.title} ({c.source})" for c in more]

    def apply_user_feedback(self, user_id: str, feedback_text: str) -> dict[str, str]:
        log.info("Feedback from user=%s: %r", user_id, feedback_text[:80])
        results: dict[str, str] = {}
        commands = parse_preference_commands(feedback_text, deltas=self._preference_deltas)

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
            elif cmd.action == "region" and cmd.value:
                self.preferences.apply_region(user_id, cmd.value)
                results["region"] = cmd.value
            elif cmd.action == "cadence" and cmd.value:
                self.preferences.apply_cadence(user_id, cmd.value)
                results["cadence"] = cmd.value
            elif cmd.action == "max_items" and cmd.value:
                self.preferences.apply_max_items(user_id, int(cmd.value))
                results["max_items"] = cmd.value

        log.info("Applied %d preference updates for user=%s", len(results), user_id)
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

        if critical_count >= self._bt_critical_min:
            return BriefingType.BREAKING_ALERT
        if breaking_count >= self._bt_breaking_min:
            return BriefingType.BREAKING_ALERT
        return BriefingType.MORNING_DIGEST

    def engine_status(self) -> dict:
        """Return engine status info for the communication agent."""
        return {
            "agent_count": len(self.config.get("research_agents", [])),
            "expert_count": len(self.experts.expert_ids),
            "stage_count": len(self._enabled_stages),
            "llm_backed": bool(self.experts._use_llm),
            "telegram_connected": self._bot is not None,
            "cache_entries": sum(len(v) for v in self.cache._entries.values()),
            "orchestrator_metrics": self.orchestrator.metrics(),
            "optimizer_health": self.optimizer.health_report(),
        }

    def _save_state(self) -> None:
        if not self._persistence:
            return
        self._persistence.save("preferences", self.preferences.snapshot())
        self._persistence.save("credibility", self.credibility.snapshot())
        self._persistence.save("georisk", self.georisk.snapshot())
        self._persistence.save("trends", self.trends.snapshot())
        self._persistence.save("optimizer", self.optimizer.snapshot())
