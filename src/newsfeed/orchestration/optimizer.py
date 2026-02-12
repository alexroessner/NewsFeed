"""System optimization agent — monitors pipeline health and tunes agent performance.

The system optimization agent (control agent in the vision) tracks:
- Per-agent performance (latency, yield, keep rate)
- Pipeline stage health (failures, error rates)
- Dynamic tuning recommendations
- Health report generation for observability
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentMetric:
    """Performance metrics for a single research agent."""
    agent_id: str
    source: str
    total_runs: int = 0
    total_candidates: int = 0
    total_selected: int = 0
    total_latency_ms: float = 0.0
    error_count: int = 0
    last_run_at: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / max(self.total_runs, 1), 1)

    @property
    def avg_yield(self) -> float:
        return round(self.total_candidates / max(self.total_runs, 1), 1)

    @property
    def keep_rate(self) -> float:
        return round(self.total_selected / max(self.total_candidates, 1), 3)

    @property
    def error_rate(self) -> float:
        return round(self.error_count / max(self.total_runs, 1), 3)


@dataclass(slots=True)
class StageMetric:
    """Health metrics for a pipeline stage."""
    stage_name: str
    total_runs: int = 0
    total_latency_ms: float = 0.0
    failure_count: int = 0

    @property
    def avg_latency_ms(self) -> float:
        return round(self.total_latency_ms / max(self.total_runs, 1), 1)

    @property
    def failure_rate(self) -> float:
        return round(self.failure_count / max(self.total_runs, 1), 3)


@dataclass(slots=True)
class TuningRecommendation:
    """A recommendation from the optimization agent."""
    agent_id: str
    action: str  # "disable", "reduce_weight", "increase_weight", "investigate"
    reason: str
    severity: str  # "low", "medium", "high"


class SystemOptimizationAgent:
    """Global configuration and policy optimizer.

    Responsibilities:
    1. Track per-agent performance metrics (latency, yield, keep rate, errors)
    2. Monitor pipeline stage health
    3. Generate tuning recommendations
    4. Produce health reports for observability
    """

    agent_id = "system_optimization_agent"

    def __init__(
        self,
        error_rate_threshold: float = 0.3,
        min_yield_threshold: float = 0.5,
        latency_threshold_ms: float = 10000.0,
        keep_rate_threshold: float = 0.1,
    ) -> None:
        self._agents: dict[str, AgentMetric] = {}
        self._stages: dict[str, StageMetric] = {}
        self._error_rate_threshold = error_rate_threshold
        self._min_yield_threshold = min_yield_threshold
        self._latency_threshold_ms = latency_threshold_ms
        self._keep_rate_threshold = keep_rate_threshold
        self._disabled_agents: set[str] = set()
        self._weight_overrides: dict[str, float] = {}

    # ──────────────────────────────────────────────────────────────
    # Recording methods (called by the engine during pipeline execution)
    # ──────────────────────────────────────────────────────────────

    def record_agent_run(
        self,
        agent_id: str,
        source: str,
        candidate_count: int,
        latency_ms: float,
        error: bool = False,
    ) -> None:
        """Record a single agent research run."""
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentMetric(agent_id=agent_id, source=source)

        m = self._agents[agent_id]
        m.total_runs += 1
        m.total_candidates += candidate_count
        m.total_latency_ms += latency_ms
        m.last_run_at = time.time()
        if error:
            m.error_count += 1

    def record_agent_selection(self, agent_id: str, selected_count: int) -> None:
        """Record how many of an agent's candidates survived expert selection."""
        if agent_id in self._agents:
            self._agents[agent_id].total_selected += selected_count

    def record_stage_run(self, stage_name: str, latency_ms: float, failed: bool = False) -> None:
        """Record a pipeline stage execution."""
        if stage_name not in self._stages:
            self._stages[stage_name] = StageMetric(stage_name=stage_name)

        m = self._stages[stage_name]
        m.total_runs += 1
        m.total_latency_ms += latency_ms
        if failed:
            m.failure_count += 1

    # ──────────────────────────────────────────────────────────────
    # Analysis and tuning
    # ──────────────────────────────────────────────────────────────

    def analyze(self) -> list[TuningRecommendation]:
        """Analyze all metrics and generate tuning recommendations."""
        recommendations: list[TuningRecommendation] = []

        for agent_id, m in self._agents.items():
            if m.total_runs < 3:
                continue  # Not enough data

            # High error rate
            if m.error_rate > self._error_rate_threshold:
                recommendations.append(TuningRecommendation(
                    agent_id=agent_id,
                    action="investigate",
                    reason=f"Error rate {m.error_rate:.0%} exceeds threshold "
                           f"({self._error_rate_threshold:.0%})",
                    severity="high" if m.error_rate > 0.5 else "medium",
                ))

            # Low yield (agent producing very few candidates)
            if m.avg_yield < self._min_yield_threshold:
                recommendations.append(TuningRecommendation(
                    agent_id=agent_id,
                    action="investigate",
                    reason=f"Average yield {m.avg_yield:.1f} below minimum "
                           f"({self._min_yield_threshold:.1f})",
                    severity="medium",
                ))

            # Very low keep rate (expert council rejecting most candidates)
            if m.total_candidates > 10 and m.keep_rate < self._keep_rate_threshold:
                recommendations.append(TuningRecommendation(
                    agent_id=agent_id,
                    action="reduce_weight",
                    reason=f"Keep rate {m.keep_rate:.0%} — experts consistently "
                           f"rejecting this agent's candidates",
                    severity="medium",
                ))

            # High latency
            if m.avg_latency_ms > self._latency_threshold_ms:
                recommendations.append(TuningRecommendation(
                    agent_id=agent_id,
                    action="investigate",
                    reason=f"Average latency {m.avg_latency_ms:.0f}ms exceeds threshold "
                           f"({self._latency_threshold_ms:.0f}ms)",
                    severity="low",
                ))

        # Check pipeline stages
        for stage_name, m in self._stages.items():
            if m.total_runs >= 3 and m.failure_rate > self._error_rate_threshold:
                recommendations.append(TuningRecommendation(
                    agent_id=f"stage:{stage_name}",
                    action="investigate",
                    reason=f"Pipeline stage '{stage_name}' failure rate "
                           f"{m.failure_rate:.0%}",
                    severity="high",
                ))

        return recommendations

    def apply_recommendations(self, auto_disable: bool = False) -> list[str]:
        """Apply tuning recommendations. Returns list of actions taken."""
        recs = self.analyze()
        actions: list[str] = []

        for rec in recs:
            if rec.severity == "high" and rec.action == "investigate" and auto_disable:
                if rec.agent_id not in self._disabled_agents and not rec.agent_id.startswith("stage:"):
                    self._disabled_agents.add(rec.agent_id)
                    actions.append(f"Disabled {rec.agent_id}: {rec.reason}")

            if rec.action == "reduce_weight":
                current = self._weight_overrides.get(rec.agent_id, 1.0)
                self._weight_overrides[rec.agent_id] = max(0.1, current * 0.7)
                actions.append(f"Reduced weight for {rec.agent_id} to "
                             f"{self._weight_overrides[rec.agent_id]:.2f}")

        if actions:
            log.info("Optimization applied %d actions: %s", len(actions), "; ".join(actions))

        return actions

    def is_agent_disabled(self, agent_id: str) -> bool:
        """Check if an agent has been disabled by the optimizer."""
        return agent_id in self._disabled_agents

    def get_weight_override(self, agent_id: str) -> float:
        """Get the weight multiplier for an agent (1.0 = no override)."""
        return self._weight_overrides.get(agent_id, 1.0)

    # ──────────────────────────────────────────────────────────────
    # Reporting
    # ──────────────────────────────────────────────────────────────

    def health_report(self) -> dict[str, Any]:
        """Generate a comprehensive health report."""
        agent_metrics = {}
        for agent_id, m in self._agents.items():
            agent_metrics[agent_id] = {
                "source": m.source,
                "runs": m.total_runs,
                "avg_yield": m.avg_yield,
                "keep_rate": m.keep_rate,
                "avg_latency_ms": m.avg_latency_ms,
                "error_rate": m.error_rate,
                "disabled": agent_id in self._disabled_agents,
            }

        stage_metrics = {}
        for stage_name, m in self._stages.items():
            stage_metrics[stage_name] = {
                "runs": m.total_runs,
                "avg_latency_ms": m.avg_latency_ms,
                "failure_rate": m.failure_rate,
            }

        recs = self.analyze()

        return {
            "agents": agent_metrics,
            "stages": stage_metrics,
            "recommendations": [
                {"agent": r.agent_id, "action": r.action,
                 "reason": r.reason, "severity": r.severity}
                for r in recs
            ],
            "disabled_agents": sorted(self._disabled_agents),
            "weight_overrides": dict(self._weight_overrides),
        }

    def snapshot(self) -> dict[str, Any]:
        """Compact snapshot for persistence."""
        return {
            "disabled": sorted(self._disabled_agents),
            "weights": dict(self._weight_overrides),
            "agent_stats": {
                aid: {"runs": m.total_runs, "errors": m.error_count,
                      "candidates": m.total_candidates, "selected": m.total_selected}
                for aid, m in self._agents.items()
            },
        }
