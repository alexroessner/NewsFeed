from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from newsfeed.agents.base import ResearchAgent
from newsfeed.agents.dynamic_sources import create_custom_agent
from newsfeed.agents.experts import ExpertCouncil
from newsfeed.agents.registry import create_agent
from newsfeed.db.analytics import AnalyticsDB, create_analytics_db
from newsfeed.db.state_store import D1StateStore
from newsfeed.delivery.telegram import TelegramFormatter
from newsfeed.intelligence.clustering import StoryClustering
from newsfeed.intelligence.enrichment import ArticleEnricher
from newsfeed.intelligence.credibility import (
    CredibilityTracker,
    detect_cross_corroboration,
    enforce_source_diversity,
)
from newsfeed.intelligence.narrative import (
    generate_adjacent_reads,
    generate_outlook,
    generate_what_changed,
    generate_why,
)
from newsfeed.intelligence.georisk import GeoRiskIndex
from newsfeed.intelligence.trends import TrendDetector
from newsfeed.intelligence.urgency import BreakingDetector
from newsfeed.memory.commands import parse_preference_commands
from newsfeed.memory.store import BoundedUserDict, CandidateCache, PreferenceStore, StatePersistence
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
from newsfeed.orchestration.access_control import AccessControl
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
    # Maximum concurrent pipeline runs.  Prevents resource exhaustion
    # when many Telegram users trigger briefings simultaneously.
    MAX_CONCURRENT_REQUESTS = 4
    # Hard deadline for a single pipeline run (seconds).  If the pipeline
    # exceeds this, handle_request_payload raises TimeoutError so the
    # caller can send an apologetic partial response instead of hanging.
    DEFAULT_PIPELINE_TIMEOUT_S = 120

    def __init__(self, config: dict, pipeline: dict, personas: dict, personas_dir: Path) -> None:
        self.config = config
        self.pipeline = pipeline
        self.personas = personas

        log.info("Initializing NewsFeedEngine (config v%s)", pipeline.get("version", "?"))

        # Concurrency limiter — excess requests block until a slot opens
        max_conc = pipeline.get("limits", {}).get(
            "max_concurrent_requests", self.MAX_CONCURRENT_REQUESTS,
        )
        self._request_semaphore = threading.Semaphore(max_conc)

        # Pipeline deadline — prevents runaway requests from hanging forever
        self._pipeline_timeout_s: float = pipeline.get("limits", {}).get(
            "pipeline_timeout_seconds", self.DEFAULT_PIPELINE_TIMEOUT_S,
        )

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

        # Access control (user allowlist + admin roles)
        ac_cfg = pipeline.get("access_control", {})
        self.access_control = AccessControl(ac_cfg)

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
        # BoundedUserDict caps per-user dicts at 500 entries with LRU eviction
        # to prevent unbounded memory growth in multi-user deployments.
        self._last_briefing_topics: BoundedUserDict[list[str]] = BoundedUserDict(maxlen=500)
        # Track per-item info per user (for per-item thumbs up/down)
        self._last_briefing_items: BoundedUserDict[list[dict]] = BoundedUserDict(maxlen=500)
        # Track full ReportItem objects for per-story deep dive
        self._last_report_items: BoundedUserDict[list[ReportItem]] = BoundedUserDict(maxlen=500)
        # Track threads from last briefing for source comparison
        from newsfeed.models.domain import NarrativeThread
        self._last_threads: BoundedUserDict[list[NarrativeThread]] = BoundedUserDict(maxlen=500)

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

        # D1-backed state store — persists preferences, trends, etc. across runs
        self._d1_state = D1StateStore(self.analytics)
        # Load state from D1 if file-based persistence didn't find anything
        if not self._persistence or not (Path(persist_cfg.get("state_dir", "state")) / "preferences.json").exists():
            self._load_d1_state()

        # Run pending database migrations
        from newsfeed.db.migrations import MigrationRunner
        try:
            migration_runner = MigrationRunner(self.analytics)
            applied = migration_runner.apply_all()
            if applied:
                log.info("Applied %d database migrations (now at v%d)", applied, migration_runner.current_version())
        except Exception:
            log.debug("Migration runner skipped (non-critical)", exc_info=True)

        # Operator dashboard
        from newsfeed.monitoring.dashboard import OperatorDashboard
        self.dashboard = OperatorDashboard(self)

        log.info(
            "Engine ready: %d agents, %d experts, stages=%s",
            len(config.get("research_agents", [])),
            len(expert_ids),
            ",".join(sorted(self._enabled_stages)),
        )

    def _research_agents(self, user_id: str | None = None) -> list[ResearchAgent]:
        api_keys = self.pipeline.get("api_keys", {})
        agents = []
        for a in self.config.get("research_agents", []):
            agent_id = a.get("id", "")
            # Skip agents disabled by optimizer or configurator
            if agent_id in self._disabled_agents or self.optimizer.is_agent_disabled(agent_id):
                continue
            agent = create_agent(a, api_keys)
            agents.append(agent)

        # Inject per-user custom source agents
        if user_id:
            for src in self.preferences.get_custom_sources(user_id):
                try:
                    agent = create_custom_agent(
                        name=src["name"],
                        feed_url=src["feed_url"],
                        user_id=user_id,
                        topics=src.get("topics"),
                    )
                    agents.append(agent)
                except Exception:
                    log.debug("Failed to create custom agent %s", src.get("name"), exc_info=True)

        return agents

    async def _run_agent_with_breaker(self, agent: ResearchAgent, task: ResearchTask, top_k: int) -> tuple[list, str | None]:
        """Run a single agent with circuit breaker protection.

        Returns (results, failure_reason) where failure_reason is None on
        success, a short string on failure or circuit-breaker skip.
        """
        cb = self.optimizer.circuit_breaker
        if not cb.allow_request(agent.agent_id):
            log.debug("Circuit breaker OPEN — skipping %s", agent.agent_id)
            return [], "circuit_breaker"
        try:
            result = await agent.run_async(task, top_k=top_k)
            cb.record_success(agent.agent_id)
            return result, None
        except Exception:
            cb.record_failure(agent.agent_id)
            log.warning("Agent %s failed (circuit breaker tracking)", agent.agent_id, exc_info=True)
            return [], "error"

    async def _run_research_async(self, task: ResearchTask, top_k: int) -> tuple[list, list[str]]:
        """Run all agents and return (candidates, failed_agent_ids)."""
        agents = self._research_agents(task.user_id)
        coros = [
            self._run_agent_with_breaker(agent, task, top_k)
            for agent in agents
        ]
        batch = await asyncio.gather(*coros)
        flattened: list = []
        failed_agents: list[str] = []
        for agent, (results, failure) in zip(agents, batch):
            flattened.extend(results)
            if failure is not None:
                failed_agents.append(agent.agent_id)
        return flattened, failed_agents

    def _run_research(self, task: ResearchTask, top_k: int) -> tuple[list[CandidateItem], list[str]]:
        """Run research fan-out, safely handling sync/async contexts.

        Returns (candidates, failed_agent_ids).
        """
        return _run_sync(self._run_research_async(task, top_k))

    def handle_request_payload(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> DeliveryPayload:
        # Backpressure: block if too many pipeline runs are active.
        # This prevents resource exhaustion (memory, CPU, API quotas)
        # when many users trigger briefings simultaneously.
        acquired = self._request_semaphore.acquire(timeout=30)
        if not acquired:
            raise RuntimeError("Server busy — too many concurrent briefing requests. Please retry shortly.")
        try:
            return self._run_with_deadline(user_id, prompt, weighted_topics, max_items)
        finally:
            self._request_semaphore.release()

    def _run_with_deadline(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None) -> DeliveryPayload:
        """Run the pipeline in a thread with a hard deadline.

        If the pipeline exceeds ``_pipeline_timeout_s``, raises
        ``TimeoutError`` so the caller can send a partial/error response
        instead of hanging indefinitely on a stuck external API.
        """
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._handle_request_inner, user_id, prompt, weighted_topics, max_items)
            try:
                return future.result(timeout=self._pipeline_timeout_s)
            except concurrent.futures.TimeoutError:
                log.error(
                    "Pipeline timeout after %.0fs for user=%s prompt=%r",
                    self._pipeline_timeout_s, user_id, prompt[:80],
                )
                raise TimeoutError(
                    f"Briefing timed out after {self._pipeline_timeout_s:.0f}s. "
                    "An external source may be unresponsive. Please retry."
                ) from None

    def _handle_request_inner(self, user_id: str, prompt: str, weighted_topics: dict[str, float], max_items: int | None = None) -> DeliveryPayload:
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
        all_candidates, failed_agents = self._run_research(task, top_k)
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

        # Boost stories matching keyword alerts — cross-topic priority boosting
        if profile.alert_keywords:
            for c in all_candidates:
                text = f"{c.title} {c.summary}".lower()
                if any(kw in text for kw in profile.alert_keywords):
                    c.preference_fit = round(min(1.0, c.preference_fit + 0.25), 3)
                    c.novelty_score = round(min(1.0, c.novelty_score + 0.10), 3)

        # Stage 2: Intelligence enrichment (conditionally enabled, with error isolation)
        t0 = time.monotonic()
        all_candidates, failed_stages = self._run_intelligence(all_candidates)
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
        enrichment_ok = True
        try:
            selected = self.enricher.enrich(selected)
        except Exception:
            log.exception("Article enrichment failed, continuing with RSS summaries")
            enrichment_ok = False
            failed_stages.append("enrichment")
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
                failed_stages.append("clustering")

        # Stage 5: Geo-risk assessment
        geo_risks = []
        if "georisk" in self._enabled_stages:
            try:
                geo_risks = self.georisk.assess(all_candidates)
            except Exception:
                log.exception("Georisk stage failed, continuing without geo risks")
                failed_stages.append("georisk")

        # Stage 6: Trend analysis
        trend_snapshots = []
        if "trends" in self._enabled_stages:
            try:
                trend_snapshots = self.trends.analyze(all_candidates)
            except Exception:
                log.exception("Trend stage failed, continuing without trends")
                failed_stages.append("trends")

        # Stage 7: Report assembly with editorial review
        lifecycle.advance(RequestStage.EDITORIAL_REVIEW)
        t0 = time.monotonic()
        report_items = self._assemble_report(selected, threads, profile, request_id, reserve=reserve)
        review_ms = (time.monotonic() - t0) * 1000
        self.optimizer.record_stage_run("editorial_review", review_ms)

        # Stage 8: Determine briefing type
        briefing_type = self._determine_briefing_type(selected)

        # Collect active intelligence stages for metadata
        active_stages = [s for s in [
            "credibility", "corroboration", "urgency",
            "diversity", "clustering", "georisk", "trends",
        ] if s in self._enabled_stages]

        # Pipeline trace metadata — powers /transparency command
        pipeline_trace = {
            "total_candidates_researched": len(all_candidates),
            "valid_candidates": len(valid_candidates),
            "research_time_ms": round(research_ms),
            "intelligence_time_ms": round(intel_ms),
            "expert_time_ms": round(expert_ms),
            "enrichment_time_ms": round(enrich_ms),
            "review_time_ms": round(review_ms),
            "agents_contributing": dict(by_agent),
            "expert_votes_total": len(debate.votes),
            "expert_agreements": sum(1 for v in debate.votes if v.keep),
            "expert_rejections": sum(1 for v in debate.votes if not v.keep),
            "arbitrated_votes": sum(1 for v in debate.votes if "arbitration" in v.rationale.lower()),
            "credibility_filtered": 0,
            "source_diversity_applied": "diversity" in self._enabled_stages,
        }

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
                "pipeline_trace": pipeline_trace,
                "pipeline_health": {
                    "agents_total": len(self.config.get("research_agents", [])),
                    "agents_contributing": len(by_agent),
                    "agents_silent": len(self.config.get("research_agents", [])) - len(by_agent),
                    "agents_failed": failed_agents,
                    "stages_enabled": list(self._enabled_stages),
                    "stages_failed": failed_stages,
                    "total_candidates": len(all_candidates),
                },
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
        # Track threads for source comparison
        self._last_threads[user_id] = list(threads)

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

    def _run_intelligence(self, candidates: list[CandidateItem]) -> tuple[list[CandidateItem], list[str]]:
        """Run intelligence enrichment stages with error isolation.

        Returns (candidates, failed_stages) where failed_stages lists
        the names of any stages that raised exceptions.
        """
        failed_stages: list[str] = []

        if "credibility" in self._enabled_stages:
            try:
                for c in candidates:
                    self.credibility.record_item(c)
            except Exception:
                log.exception("Credibility stage failed")
                failed_stages.append("credibility")

        if "corroboration" in self._enabled_stages:
            try:
                candidates = detect_cross_corroboration(candidates)
            except Exception:
                log.exception("Corroboration stage failed")
                failed_stages.append("corroboration")

        if "urgency" in self._enabled_stages:
            try:
                candidates = self.breaking_detector.assess(candidates)
            except Exception:
                log.exception("Urgency stage failed")
                failed_stages.append("urgency")

        if "diversity" in self._enabled_stages:
            try:
                max_per_source = self.pipeline.get("intelligence", {}).get("max_items_per_source", 3)
                candidates = enforce_source_diversity(candidates, max_per_source=max_per_source)
            except Exception:
                log.exception("Diversity stage failed")
                failed_stages.append("diversity")

        return candidates, failed_stages

    def _assemble_report(self, selected: list[CandidateItem], threads: list,
                         profile: UserProfile | None = None,
                         request_id: str = "",
                         reserve: list[CandidateItem] | None = None) -> list[ReportItem]:
        """Assemble report items from selected candidates, then apply editorial review."""
        report_items = []
        adjacent_bounds = self.pipeline.get("limits", {}).get("adjacent_reads_per_item", {"min": 2, "max": 3})
        adjacent_count = adjacent_bounds.get("max", 3)

        thread_map: dict[str, str] = {}
        for thread in threads:
            for c in thread.candidates:
                thread_map[c.candidate_id] = thread.thread_id

        for c in selected:
            # Generate smart, metadata-driven narrative text
            why = self.review_stack.refine_why(
                generate_why(c, self.credibility, profile)
            )
            outlook = self.review_stack.refine_outlook(
                generate_outlook(c, self.credibility)
            )

            # Real adjacent reads from thread siblings and reserve cache
            reads = generate_adjacent_reads(c, threads, reserve, limit=adjacent_count)

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
                    what_changed=generate_what_changed(c, self.credibility),
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

    def get_story_thread(self, user_id: str, story_index: int) -> tuple[ReportItem | None, list[CandidateItem]]:
        """Return a story and all candidates from its thread (for source comparison).

        Returns (report_item, other_candidates_in_same_thread).
        """
        item = self.get_report_item(user_id, story_index)
        if not item or not item.thread_id:
            return item, []
        threads = self._last_threads.get(user_id, [])
        for thread in threads:
            if thread.thread_id == item.thread_id:
                # Return candidates from this thread that aren't the selected story
                others = [c for c in thread.candidates if c.candidate_id != item.candidate.candidate_id]
                return item, others
        return item, []

    def show_more(self, user_id: str, topic: str, already_seen_ids: set[str], limit: int = 5) -> list[CandidateItem]:
        """Return cached candidates the user hasn't seen yet."""
        return self.cache.get_more(user_id=user_id, topic=topic, already_seen_ids=already_seen_ids, limit=limit)

    def apply_user_feedback(self, user_id: str, feedback_text: str,
                            is_admin: bool = False) -> dict[str, str]:
        log.info("Feedback from user=%s: %r", user_id, feedback_text[:80])
        results: dict[str, str] = {}

        # SECURITY: System-level configuration changes are admin-only.
        # The configurator modifies global pipeline settings (scoring weights,
        # pipeline stages, expert behavior) that affect ALL users.
        if is_admin:
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
                updated, hint = self.preferences.apply_weight_adjustment(user_id, cmd.topic, delta)
                results[f"topic:{cmd.topic}"] = str(updated.topic_weights.get(cmd.topic, 0.0))
                if hint:
                    results[f"hint:{cmd.topic}"] = hint
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
                _, src_hint = self.preferences.apply_source_weight(user_id, cmd.topic, 1.0)
                results[f"source:{cmd.topic}"] = "boosted"
                if src_hint:
                    results[f"hint:{cmd.topic}"] = src_hint
            elif cmd.action == "source_demote" and cmd.topic:
                _, src_hint = self.preferences.apply_source_weight(user_id, cmd.topic, -1.0)
                results[f"source:{cmd.topic}"] = "demoted"
                if src_hint:
                    results[f"hint:{cmd.topic}"] = src_hint
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
            "llm_backed": self.is_llm_backed(),
            "telegram_connected": self.is_telegram_connected(),
            "cache_entries": self.cache_entry_count(),
            "orchestrator_metrics": self.orchestrator.metrics(),
            "optimizer_health": self.optimizer.health_report(),
            "audit_stats": self.audit.stats(),
            "expert_influence": self.experts.chair.snapshot(),
            "config_changes": len(self.configurator.history()),
        }

    # ──────────────────────────────────────────────────────────────
    # Public API — avoid private attribute access across modules
    # ──────────────────────────────────────────────────────────────

    def last_report_items(self, user_id: str) -> list[ReportItem]:
        """Return the full ReportItem list from the user's last briefing."""
        return self._last_report_items.get(user_id, [])

    def is_telegram_connected(self) -> bool:
        """Check if the Telegram bot and communication agent are initialized."""
        return self._bot is not None and self._comm_agent is not None

    def get_comm_agent(self):
        """Return the communication agent (or None if not initialized)."""
        return self._comm_agent

    def get_bot(self):
        """Return the Telegram bot (or None if not initialized)."""
        return self._bot

    def is_llm_backed(self) -> bool:
        """Check if the expert council is using LLM-based evaluation."""
        return bool(self.experts._use_llm)

    def cache_entry_count(self) -> int:
        """Return the total number of cached candidate entries."""
        return sum(len(v) for v in self.cache._entries.values())

    def persist_preferences(self) -> None:
        """Persist current preferences to disk and D1."""
        if self._persistence:
            self._persistence.save("preferences", self.preferences.snapshot())
        self._save_d1_state()

    def _save_state(self) -> None:
        if self._persistence:
            self._persistence.save("preferences", self.preferences.snapshot())
            self._persistence.save("credibility", self.credibility.snapshot())
            self._persistence.save("georisk", self.georisk.snapshot())
            self._persistence.save("trends", self.trends.snapshot())
            self._persistence.save("optimizer", self.optimizer.snapshot())
            self._persistence.save("debate_chair", self.experts.chair.snapshot())
            if hasattr(self, "_scheduler") and self._scheduler:
                self._persistence.save("scheduler", self._scheduler.snapshot())
            if hasattr(self, "access_control"):
                self._persistence.save("access_control", self.access_control.snapshot())
        # Also persist to D1 for cross-run durability
        self._save_d1_state()

    def _save_d1_state(self) -> None:
        """Persist state to D1 so it survives across ephemeral GH Actions runs."""
        try:
            self._d1_state.save("preferences", self.preferences.snapshot())
            self._d1_state.save("credibility", self.credibility.snapshot())
            self._d1_state.save("georisk", self.georisk.snapshot())
            self._d1_state.save("trends", self.trends.snapshot())
            self._d1_state.save("optimizer", self.optimizer.snapshot())
            self._d1_state.save("debate_chair", self.experts.chair.snapshot())
            if hasattr(self, "_scheduler") and self._scheduler:
                self._d1_state.save("scheduler", self._scheduler.snapshot())
            if hasattr(self, "access_control"):
                self._d1_state.save("access_control", self.access_control.snapshot())
        except Exception:
            log.debug("D1 state save failed (non-critical)", exc_info=True)

    def _load_d1_state(self) -> None:
        """Load state from D1 on startup (fallback when file-based state is absent)."""
        try:
            prefs = self._d1_state.load("preferences")
            if prefs and isinstance(prefs, dict):
                # Delegate to existing _load_state validation logic by temporarily
                # injecting into persistence, or directly restore minimal state
                for uid, pdata in prefs.items():
                    if isinstance(pdata, dict):
                        profile = self.preferences.get_or_create(uid)
                        tw = pdata.get("topic_weights", {})
                        if isinstance(tw, dict):
                            for k, v in list(tw.items())[:50]:
                                profile.topic_weights[str(k)] = round(max(-1.0, min(1.0, float(v))), 3)
                log.info("Restored %d user preferences from D1", len(prefs))

            ac_data = self._d1_state.load("access_control")
            if ac_data and isinstance(ac_data, dict) and hasattr(self, "access_control"):
                self.access_control.restore(ac_data)
                log.info("Restored access control state from D1")

            cred = self._d1_state.load("credibility")
            if cred and isinstance(cred, dict):
                log.info("Restored credibility data from D1 (%d sources)", len(cred))
        except Exception:
            log.debug("D1 state load failed (non-critical)", exc_info=True)

    # Allowed values for validated string fields on restore.
    # IMPORTANT: These must be a superset of the values accepted by the
    # user-facing parser in memory/commands.py.  A mismatch causes user
    # settings to silently revert to defaults on every restart.
    _VALID_TONES = frozenset({
        "concise", "analyst", "brief", "deep", "executive",
        # Legacy values (pre-parser-unification) kept for backwards compat
        "detailed", "analytical", "casual",
    })
    _VALID_FORMATS = frozenset({
        "bullet", "sections", "narrative",
        "brief", "detailed",
    })
    _VALID_CADENCES = frozenset({
        "on_demand", "morning", "evening", "realtime",
        "hourly", "daily", "weekly",
    })
    _VALID_URGENCIES = frozenset({"", "routine", "elevated", "breaking", "critical"})

    def _load_state(self) -> None:
        """Restore persisted state from disk on startup.

        SECURITY: All values are re-validated against the same bounds
        enforced by setter methods.  A tampered persistence file cannot
        bypass caps or inject invalid data.
        """
        if not self._persistence:
            return

        _MAX_WEIGHTS = self.preferences.MAX_WEIGHTS
        _MAX_WATCHLIST = self.preferences.MAX_WATCHLIST_SIZE
        _MAX_MUTED = self.preferences.MAX_MUTED_TOPICS

        # Restore user preferences
        prefs_data = self._persistence.load("preferences")
        if prefs_data and isinstance(prefs_data, dict):
            for uid, pdata in prefs_data.items():
                profile = self.preferences.get_or_create(uid)

                # topic_weights: clamp values to [-1, 1], cap total entries
                if isinstance(pdata.get("topic_weights"), dict):
                    tw = pdata["topic_weights"]
                    for k, v in list(tw.items())[:_MAX_WEIGHTS]:
                        profile.topic_weights[str(k)] = round(max(-1.0, min(1.0, float(v))), 3)

                # source_weights: clamp values to [-2, 2], cap total entries
                if isinstance(pdata.get("source_weights"), dict):
                    sw = pdata["source_weights"]
                    for k, v in list(sw.items())[:_MAX_WEIGHTS]:
                        profile.source_weights[str(k)] = round(max(-2.0, min(2.0, float(v))), 3)

                if pdata.get("tone") in self._VALID_TONES:
                    profile.tone = pdata["tone"]
                if pdata.get("format") in self._VALID_FORMATS:
                    profile.format = pdata["format"]
                if pdata.get("max_items"):
                    profile.max_items = max(1, min(int(pdata["max_items"]), 50))
                if pdata.get("cadence") in self._VALID_CADENCES:
                    profile.briefing_cadence = pdata["cadence"]
                if isinstance(pdata.get("regions"), list):
                    profile.regions_of_interest = [str(r) for r in pdata["regions"][:20]]
                if isinstance(pdata.get("watchlist_crypto"), list):
                    profile.watchlist_crypto = [str(c) for c in pdata["watchlist_crypto"][:_MAX_WATCHLIST]]
                if isinstance(pdata.get("watchlist_stocks"), list):
                    profile.watchlist_stocks = [str(s) for s in pdata["watchlist_stocks"][:_MAX_WATCHLIST]]
                if pdata.get("timezone"):
                    tz = str(pdata["timezone"])[:40]
                    profile.timezone = tz
                if isinstance(pdata.get("muted_topics"), list):
                    profile.muted_topics = [str(t) for t in pdata["muted_topics"][:_MAX_MUTED]]
                if isinstance(pdata.get("tracked_stories"), list):
                    profile.tracked_stories = list(pdata["tracked_stories"][:20])
                if isinstance(pdata.get("bookmarks"), list):
                    profile.bookmarks = list(pdata["bookmarks"][:50])
                if pdata.get("email"):
                    email = str(pdata["email"]).strip()
                    # Block newlines (header injection) and require basic format
                    if "\n" not in email and "\r" not in email and "@" in email and len(email) <= 254:
                        profile.email = email
                if pdata.get("confidence_min"):
                    profile.confidence_min = max(0.0, min(float(pdata["confidence_min"]), 1.0))
                if pdata.get("urgency_min"):
                    val = str(pdata["urgency_min"]).lower()
                    if val in self._VALID_URGENCIES:
                        profile.urgency_min = val
                if pdata.get("max_per_source"):
                    profile.max_per_source = max(0, min(int(pdata["max_per_source"]), 10))
                if pdata.get("alert_georisk_threshold"):
                    profile.alert_georisk_threshold = max(0.1, min(float(pdata["alert_georisk_threshold"]), 1.0))
                if pdata.get("alert_trend_threshold"):
                    profile.alert_trend_threshold = max(1.5, min(float(pdata["alert_trend_threshold"]), 10.0))
                if isinstance(pdata.get("presets"), dict):
                    # Cap at 10 presets
                    presets = dict(list(pdata["presets"].items())[:10])
                    profile.presets = presets
                if pdata.get("webhook_url"):
                    from newsfeed.delivery.webhook import validate_webhook_url
                    url = str(pdata["webhook_url"])
                    valid, _ = validate_webhook_url(url)
                    if valid:
                        profile.webhook_url = url
            log.info("Restored preferences for %d users from disk", len(prefs_data))

        # Restore optimizer state (disabled agents, weight overrides, agent stats)
        opt_data = self._persistence.load("optimizer")
        if opt_data and isinstance(opt_data, dict):
            if isinstance(opt_data.get("disabled"), list):
                for aid in opt_data["disabled"]:
                    if isinstance(aid, str) and len(aid) <= 100:
                        self.optimizer._disabled_agents.add(aid)
            if isinstance(opt_data.get("weights"), dict):
                for aid, w in opt_data["weights"].items():
                    if isinstance(aid, str) and len(aid) <= 100:
                        self.optimizer._weight_overrides[str(aid)] = max(0.1, min(float(w), 2.0))
            log.info("Restored optimizer state: %d disabled, %d weight overrides",
                     len(self.optimizer._disabled_agents), len(self.optimizer._weight_overrides))

        # Restore credibility baselines
        cred_data = self._persistence.load("credibility")
        if cred_data and isinstance(cred_data, dict):
            for source_id, sdata in cred_data.items():
                if not isinstance(source_id, str) or not isinstance(sdata, dict):
                    continue
                sr = self.credibility.get_source(source_id)
                if isinstance(sdata.get("reliability_score"), (int, float)):
                    sr.reliability_score = max(0.0, min(1.0, float(sdata["reliability_score"])))
                if isinstance(sdata.get("corroboration_rate"), (int, float)):
                    sr.corroboration_rate = max(0.0, min(1.0, float(sdata["corroboration_rate"])))
                if isinstance(sdata.get("total_items_seen"), int):
                    sr.total_items_seen = max(0, sdata["total_items_seen"])
            log.info("Restored credibility data for %d sources", len(cred_data))

        # Restore georisk baselines
        geo_data = self._persistence.load("georisk")
        if geo_data and isinstance(geo_data, dict):
            for region, level in geo_data.items():
                if isinstance(region, str) and isinstance(level, (int, float)):
                    self.georisk._history[region] = max(0.0, min(1.0, float(level)))
            log.info("Restored georisk baselines for %d regions", len(geo_data))

        # Restore trend baselines
        trend_data = self._persistence.load("trends")
        if trend_data and isinstance(trend_data, dict):
            for topic, velocity in trend_data.items():
                if isinstance(topic, str) and isinstance(velocity, (int, float)):
                    self.trends._baseline[topic] = max(0.0, min(1.0, float(velocity)))
            log.info("Restored trend baselines for %d topics", len(trend_data))

        # Restore scheduler state (schedules, timezones) so briefings survive restarts
        if hasattr(self, "_scheduler") and self._scheduler:
            sched_data = self._persistence.load("scheduler")
            if sched_data and isinstance(sched_data, dict):
                count = self._scheduler.restore(sched_data)
                log.info("Restored %d briefing schedules from disk", count)

        log.info("State loaded from %s", self._persistence.state_dir)
