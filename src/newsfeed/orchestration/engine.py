from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.experts import ExpertCouncil
from newsfeed.agents.registry import create_agent
from newsfeed.db.analytics import AnalyticsDB, create_analytics_db
from newsfeed.delivery.telegram import TelegramFormatter
from newsfeed.intelligence.clustering import StoryClustering
from newsfeed.intelligence.enrichment import ArticleEnricher
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
from newsfeed.orchestration.audit import AuditTrail
from newsfeed.orchestration.communication import CommunicationAgent
from newsfeed.orchestration.configurator import SystemConfigurator
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
                default_topics=pipeline.get("default_topics"),
            )
            log.info("Telegram bot + communication agent initialized")

        self.formatter = TelegramFormatter()
        self.review_stack = PersonaReviewStack(
            personas_dir=personas_dir,
            active_personas=personas.get("default_personas", []),
            persona_notes=personas.get("persona_notes", {}),
        )

        # Review agents (editorial layer) with config-driven templates
        llm_key = api_keys.get("anthropic_api_key", "")
        llm_model = ec_cfg.get("llm_model", "claude-sonnet-4-5-20250929")
        llm_base = ec_cfg.get("llm_base_url", "https://api.anthropic.com/v1")
        editorial_cfg = pipeline.get("editorial_review", {})
        self._style_reviewer = StyleReviewAgent(
            persona_context=self.review_stack.active_context(),
            llm_api_key=llm_key, llm_model=llm_model, llm_base_url=llm_base,
            editorial_cfg=editorial_cfg,
        )
        self._clarity_reviewer = ClarityReviewAgent(
            llm_api_key=llm_key, llm_model=llm_model, llm_base_url=llm_base,
            editorial_cfg=editorial_cfg,
        )

        # Orchestrator agent (lifecycle management + config-driven routing)
        self.orchestrator = OrchestratorAgent(
            agent_configs=config.get("research_agents", []),
            pipeline_cfg=pipeline,
            agents_cfg=config,
        )

        # System optimization agent (health monitoring + self-tuning)
        self.optimizer = SystemOptimizationAgent()

        # Audit trail (full decision tracking)
        self.audit = AuditTrail()

        # Universal configurator (plain-text config changes)
        self.configurator = SystemConfigurator(pipeline, config, personas)

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

        # Article enrichment — fetch and summarize full articles post-selection
        enrich_cfg = pipeline.get("enrichment", {})
        self.enricher = ArticleEnricher(
            llm_api_key=api_keys.get("anthropic_api_key", ""),
            llm_model=ec_cfg.get("llm_model", "claude-sonnet-4-5-20250929"),
            llm_base_url=ec_cfg.get("llm_base_url", "https://api.anthropic.com/v1"),
            gemini_api_key=api_keys.get("gemini_api_key", ""),
            gemini_model=enrich_cfg.get("gemini_model", "gemini-2.0-flash"),
            fetch_timeout=enrich_cfg.get("fetch_timeout", 8),
            max_workers=enrich_cfg.get("max_workers", 5),
            target_summary_chars=enrich_cfg.get("target_summary_chars", 500),
        )

        # Configurable thresholds
        self._confidence_offset = scoring_cfg.get("confidence_band_offset", 0.15)
        self._contrarian_novelty = intel_cfg.get("contrarian_novelty_threshold", 0.8)
        self._contrarian_evidence = intel_cfg.get("contrarian_evidence_threshold", 0.6)
        self._preference_deltas = pipeline.get("preference_deltas", {"more": 0.2, "less": -0.2})

        bt = pipeline.get("briefing_type_thresholds", {})
        self._bt_critical_min = bt.get("breaking_alert_critical_min", 1)
        self._bt_breaking_min = bt.get("breaking_alert_breaking_min", 2)

        # Disabled agents (populated by optimizer or configurator)
        self._disabled_agents: set[str] = set()

        # Track last briefing item topics per user (for "More/Less like this")
        self._last_briefing_topics: dict[str, list[str]] = {}
        # Track per-item info per user (for per-item thumbs up/down)
        self._last_briefing_items: dict[str, list[dict]] = {}  # [{topic, source}, ...]
        # Track full ReportItem objects for per-story deep dive
        self._last_report_items: dict[str, list[ReportItem]] = {}  # user_id -> [ReportItem, ...]

        # State persistence — save and restore preferences, credibility, etc.
        persist_cfg = pipeline.get("persistence", {})
        self._persistence: StatePersistence | None = None
        if persist_cfg.get("enabled", False):
            state_dir = Path(persist_cfg.get("state_dir", "state"))
            self._persistence = StatePersistence(state_dir)
            self._load_state()

        # Analytics database — auto-selects Cloudflare D1 (persistent) or local SQLite
        analytics_dir = Path(persist_cfg.get("state_dir", "state"))
        analytics_dir.mkdir(parents=True, exist_ok=True)
        self.analytics = create_analytics_db(local_path=analytics_dir / "analytics.db")

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
            agent_id = a.get("id", "")
            # Skip agents disabled by optimizer or configurator
            if agent_id in self._disabled_agents or self.optimizer.is_agent_disabled(agent_id):
                continue
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

    def handle_request_payload(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> DeliveryPayload:
        log.info("handle_request user=%s prompt=%r", user_id, prompt[:80])
        profile = self.preferences.get_or_create(user_id)
        limit = min(max_items or profile.max_items, self.pipeline.get("limits", {}).get("default_max_items", 10))

        # Orchestrator compiles brief and tracks lifecycle
        task, lifecycle = self.orchestrator.compile_brief(user_id, prompt, profile, limit)
        request_id = task.request_id
        # Override with caller-provided topics if specified
        if weighted_topics:
            task.weighted_topics = weighted_topics

        # Analytics: record request start
        self.analytics.record_request_start(request_id, user_id, prompt, task.weighted_topics, limit)

        # Self-optimization: apply any pending recommendations before research
        opt_actions = self.optimizer.apply_recommendations()
        for action in opt_actions:
            self.audit.record_config_change(request_id, "optimizer", None, action, "system_optimization_agent")

        # Stage 1: Research fan-out
        lifecycle.advance(RequestStage.RESEARCHING)
        top_k = self.pipeline.get("limits", {}).get("top_discoveries_per_research_agent", 5)
        t0 = time.monotonic()
        all_candidates = self._run_research(task, top_k)
        research_ms = (time.monotonic() - t0) * 1000
        self.orchestrator.record_research_results(lifecycle, len(all_candidates))
        self.optimizer.record_stage_run("research", research_ms)
        log.info("Research produced %d candidates in %.0fms", len(all_candidates), research_ms)

        # Audit: record per-agent contributions
        by_agent: dict[str, int] = {}
        for c in all_candidates:
            by_agent[c.discovered_by] = by_agent.get(c.discovered_by, 0) + 1
        for agent_id, count in by_agent.items():
            self.audit.record_research(request_id, agent_id, "", count, research_ms / max(len(by_agent), 1))
            self.optimizer.record_agent_run(agent_id, "", count, research_ms / max(len(by_agent), 1))

        # Validate candidates from agents
        valid_candidates: list[CandidateItem] = []
        for c in all_candidates:
            issues = validate_candidate(c)
            if issues:
                log.warning("Candidate %s has issues: %s — skipping", c.candidate_id, "; ".join(issues))
            else:
                valid_candidates.append(c)
        all_candidates = valid_candidates

        # Apply user source weights — boost/penalize preference_fit for preferred/demoted sources
        if profile.source_weights:
            for c in all_candidates:
                sw = profile.source_weights.get(c.source, 0.0)
                if sw != 0.0:
                    # Apply as additive adjustment to preference_fit, clamped to [0, 1]
                    c.preference_fit = round(max(0.0, min(1.0, c.preference_fit + sw * 0.15)), 3)

        # Filter out muted topics
        if profile.muted_topics:
            muted_set = set(profile.muted_topics)
            all_candidates = [c for c in all_candidates if c.topic not in muted_set]

        # Boost stories matching user's regions of interest
        if profile.regions_of_interest:
            roi_set = {r.lower().replace(" ", "_") for r in profile.regions_of_interest}
            for c in all_candidates:
                candidate_regions = {r.lower().replace(" ", "_") for r in c.regions}
                if candidate_regions & roi_set:
                    c.preference_fit = round(min(1.0, c.preference_fit + 0.15), 3)

        # Stage 2: Intelligence enrichment (conditionally enabled, with error isolation)
        t0 = time.monotonic()
        all_candidates = self._run_intelligence(all_candidates)
        intel_ms = (time.monotonic() - t0) * 1000
        self.optimizer.record_stage_run("intelligence", intel_ms)

        # Stage 3: Expert council selection (with arbitration + weighted voting)
        lifecycle.advance(RequestStage.EXPERT_REVIEW)
        t0 = time.monotonic()
        selected, reserve, debate = self.experts.select(all_candidates, limit)
        expert_ms = (time.monotonic() - t0) * 1000
        self.orchestrator.record_selection(lifecycle, len(selected))
        self.optimizer.record_stage_run("expert_council", expert_ms)
        log.info("Expert council: %d selected, %d reserve, %d votes", len(selected), len(reserve), len(debate.votes))

        # Audit: record all votes
        selected_ids = {c.candidate_id for c in selected}
        for vote in debate.votes:
            self.audit.record_vote(
                request_id, vote.expert_id, vote.candidate_id,
                vote.keep, vote.confidence, vote.rationale, vote.risk_note,
                arbitrated="arbitration" in vote.rationale.lower(),
            )
        # Audit: record selection decisions
        for c in all_candidates:
            is_selected = c.candidate_id in selected_ids
            reason = "Accepted by expert council" if is_selected else "Below vote threshold or deduplicated"
            self.audit.record_selection(request_id, c.candidate_id, c.title, is_selected, reason, c.composite_score())
        # Update optimizer with per-agent selection data
        for c in selected:
            self.optimizer.record_agent_selection(c.discovered_by, 1)

        # Analytics: record all candidates, votes, and agent performance
        self.analytics.record_candidates(request_id, all_candidates, selected_ids)
        self.analytics.record_expert_votes(request_id, debate.votes)
        for agent_id, count in by_agent.items():
            agent_selected = sum(1 for c in selected if c.discovered_by == agent_id)
            self.analytics.record_agent_performance(
                request_id, agent_id, count, agent_selected,
                research_ms / max(len(by_agent), 1),
            )

        dominant_topic = max(task.weighted_topics, key=task.weighted_topics.get, default="general")
        self.cache.put(user_id, dominant_topic, reserve)

        # Stage 3.5: Article enrichment — fetch full articles and generate real summaries
        # Only the selected stories get enriched (not all candidates)
        t0 = time.monotonic()
        try:
            selected = self.enricher.enrich(selected)
        except Exception:
            log.exception("Article enrichment failed, continuing with RSS summaries")
        enrich_ms = (time.monotonic() - t0) * 1000
        self.optimizer.record_stage_run("article_enrichment", enrich_ms)
        log.info("Article enrichment completed in %.0fms", enrich_ms)

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
        t0 = time.monotonic()
        report_items = self._assemble_report(selected, threads, profile, request_id)
        review_ms = (time.monotonic() - t0) * 1000
        self.optimizer.record_stage_run("editorial_review", review_ms)

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
                "expert_influence": {eid: f"{inf:.2f}" for eid, inf, _ in self.experts.chair.rankings()},
            },
            briefing_type=briefing_type,
            threads=threads,
            geo_risks=geo_risks,
            trends=trend_snapshots,
        )

        # Track which topics were actually shown (for "More/Less like this" feedback)
        self._last_briefing_topics[user_id] = list(
            dict.fromkeys(item.candidate.topic for item in report_items)
        )
        # Track per-item info for per-item rating buttons
        self._last_briefing_items[user_id] = [
            {"topic": item.candidate.topic, "source": item.candidate.source, "title": item.candidate.title}
            for item in report_items
        ]
        # Track full ReportItems for per-story deep dive
        self._last_report_items[user_id] = list(report_items)

        # Persist state if enabled
        if self._persistence:
            try:
                self._save_state()
            except Exception:
                log.exception("State persistence failed")

        # Record completion in orchestrator
        lifecycle.advance(RequestStage.FORMATTING)
        self.orchestrator.record_completion(lifecycle)

        # Audit: delivery record
        self.audit.record_delivery(
            request_id, user_id, len(report_items),
            briefing_type.value, lifecycle.total_elapsed(),
        )

        # Analytics: record full briefing + intelligence snapshots
        self.analytics.record_briefing(request_id, user_id, payload)
        self.analytics.record_request_complete(
            request_id, len(all_candidates), len(selected),
            briefing_type.value, lifecycle.total_elapsed(),
        )
        if geo_risks:
            self.analytics.record_georisk_snapshot(request_id, geo_risks)
        if trend_snapshots:
            self.analytics.record_trend_snapshot(request_id, trend_snapshots)
        self.analytics.record_credibility_snapshot(request_id, self.credibility.snapshot())
        self.analytics.record_expert_snapshot(request_id, self.experts.chair.snapshot())
        # Snapshot user profile after briefing
        self.analytics.record_profile_snapshot(user_id, self.preferences.snapshot().get(user_id, {}))

        log.info("Report generated: %d items, briefing=%s", len(report_items), briefing_type.value)
        return payload

    def handle_request(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> str:
        """Run the full pipeline and return formatted string (backward compat)."""
        payload = self.handle_request_payload(user_id, prompt, weighted_topics, max_items)
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
                         profile: UserProfile | None = None,
                         request_id: str = "") -> list[ReportItem]:
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
            cid = item.candidate.candidate_id
            # Style review with audit
            before_why = item.why_it_matters
            try:
                self._style_reviewer.review(item, profile)
            except Exception:
                log.exception("Style review failed for %s", cid)
            if request_id:
                self.audit.record_review(request_id, "review_agent_style", cid, "why_it_matters", before_why, item.why_it_matters)

            # Clarity review with audit
            before_outlook = item.predictive_outlook
            try:
                self._clarity_reviewer.review(item, profile)
            except Exception:
                log.exception("Clarity review failed for %s", cid)
            if request_id:
                self.audit.record_review(request_id, "review_agent_clarity", cid, "predictive_outlook", before_outlook, item.predictive_outlook)

        return report_items

    def last_briefing_topics(self, user_id: str) -> list[str]:
        """Return the topics from the user's last briefing (in item order)."""
        return self._last_briefing_topics.get(user_id, [])

    def last_briefing_items(self, user_id: str) -> list[dict]:
        """Return per-item info [{topic, source}, ...] from the user's last briefing."""
        return self._last_briefing_items.get(user_id, [])

    def get_report_item(self, user_id: str, index: int) -> ReportItem | None:
        """Return a specific ReportItem from the user's last briefing (1-indexed)."""
        items = self._last_report_items.get(user_id, [])
        if 1 <= index <= len(items):
            return items[index - 1]
        return None

    def show_more(self, user_id: str, topic: str, already_seen_ids: set[str], limit: int = 5) -> list[CandidateItem]:
        """Return cached candidates the user hasn't seen yet."""
        return self.cache.get_more(user_id=user_id, topic=topic, already_seen_ids=already_seen_ids, limit=limit)

    def apply_user_feedback(self, user_id: str, feedback_text: str) -> dict[str, str]:
        log.info("Feedback from user=%s: %r", user_id, feedback_text[:80])
        results: dict[str, str] = {}

        # First: try universal configurator for system-level changes
        config_changes = self.configurator.parse_and_apply(feedback_text)
        for change in config_changes:
            results[change.path] = str(change.new_value)
            self.audit.record_config_change(
                f"feedback-{user_id}", change.path,
                change.old_value, change.new_value, "user_command",
            )

        # Then: preference commands for user-level changes
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
            elif cmd.action == "source_boost" and cmd.topic:
                self.preferences.apply_source_weight(user_id, cmd.topic, 1.0)
                results[f"source:{cmd.topic}"] = "boosted"
            elif cmd.action == "source_demote" and cmd.topic:
                self.preferences.apply_source_weight(user_id, cmd.topic, -1.0)
                results[f"source:{cmd.topic}"] = "demoted"
            elif cmd.action == "remove_region" and cmd.value:
                self.preferences.remove_region(user_id, cmd.value)
                results["remove_region"] = cmd.value
            elif cmd.action == "reset":
                self.preferences.reset(user_id)
                results["reset"] = "all preferences reset to defaults"

        if results:
            self.audit.record_preference(
                f"feedback-{user_id}", user_id,
                "multi_update", "; ".join(f"{k}={v}" for k, v in results.items()),
            )
            # Persist immediately so feedback survives restarts
            if self._persistence:
                try:
                    self._persistence.save("preferences", self.preferences.snapshot())
                except Exception:
                    log.exception("Failed to persist preferences after feedback")

        # Analytics: record feedback and each preference change
        self.analytics.record_feedback(user_id, feedback_text, results)
        for key, val in results.items():
            self.analytics.record_preference_change(
                user_id, "feedback", key, None, val, source="user_feedback",
            )

        log.info("Applied %d updates for user=%s", len(results), user_id)
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
            "audit_stats": self.audit.stats(),
            "expert_influence": self.experts.chair.snapshot(),
            "config_changes": len(self.configurator.history()),
        }

    def _save_state(self) -> None:
        if not self._persistence:
            return
        self._persistence.save("preferences", self.preferences.snapshot())
        self._persistence.save("credibility", self.credibility.snapshot())
        self._persistence.save("georisk", self.georisk.snapshot())
        self._persistence.save("trends", self.trends.snapshot())
        self._persistence.save("optimizer", self.optimizer.snapshot())
        self._persistence.save("debate_chair", self.experts.chair.snapshot())

    def _load_state(self) -> None:
        """Restore persisted state from disk on startup."""
        if not self._persistence:
            return

        # Restore user preferences
        prefs_data = self._persistence.load("preferences")
        if prefs_data and isinstance(prefs_data, dict):
            for uid, pdata in prefs_data.items():
                profile = self.preferences.get_or_create(uid)
                if isinstance(pdata.get("topic_weights"), dict):
                    profile.topic_weights.update(pdata["topic_weights"])
                if isinstance(pdata.get("source_weights"), dict):
                    profile.source_weights.update(pdata["source_weights"])
                if pdata.get("tone"):
                    profile.tone = pdata["tone"]
                if pdata.get("format"):
                    profile.format = pdata["format"]
                if pdata.get("max_items"):
                    profile.max_items = int(pdata["max_items"])
                if pdata.get("cadence"):
                    profile.briefing_cadence = pdata["cadence"]
                if isinstance(pdata.get("regions"), list):
                    profile.regions_of_interest = list(pdata["regions"])
                if isinstance(pdata.get("watchlist_crypto"), list):
                    profile.watchlist_crypto = list(pdata["watchlist_crypto"])
                if isinstance(pdata.get("watchlist_stocks"), list):
                    profile.watchlist_stocks = list(pdata["watchlist_stocks"])
                if pdata.get("timezone"):
                    profile.timezone = pdata["timezone"]
                if isinstance(pdata.get("muted_topics"), list):
                    profile.muted_topics = list(pdata["muted_topics"])
            log.info("Restored preferences for %d users from disk", len(prefs_data))

        log.info("State loaded from %s", self._persistence.state_dir)
