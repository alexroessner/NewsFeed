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
    zero_yield_streak: int = 0
    total_zero_yields: int = 0

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


class CircuitBreaker:
    """Per-agent circuit breaker with automatic recovery.

    States:
    - CLOSED  (normal):  agent runs every request
    - OPEN    (tripped): agent is skipped after *failure_threshold* consecutive failures
    - HALF_OPEN:         after *recovery_seconds*, ONE probe request is allowed through;
                         success → CLOSED, failure → re-OPEN

    This prevents a persistently failing agent (network down, rate-limited,
    malformed source) from wasting latency budget on every request while
    still allowing it to automatically recover when the underlying issue
    resolves.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold: int = 3, recovery_seconds: float = 120.0) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_seconds = recovery_seconds
        # agent_id → (state, consecutive_failures, last_failure_time)
        self._breakers: dict[str, tuple[str, int, float]] = {}

    def allow_request(self, agent_id: str) -> bool:
        """Return True if the agent should run this cycle."""
        state, failures, last_fail = self._breakers.get(agent_id, (self.CLOSED, 0, 0.0))
        if state == self.CLOSED:
            return True
        if state == self.OPEN:
            # Check if recovery window has elapsed → transition to HALF_OPEN
            if time.monotonic() - last_fail >= self._recovery_seconds:
                self._breakers[agent_id] = (self.HALF_OPEN, failures, last_fail)
                log.debug("Circuit breaker HALF_OPEN for %s (probing)", agent_id)
                return True
            return False
        # HALF_OPEN: allow exactly one probe
        return True

    def record_success(self, agent_id: str) -> None:
        """Record a successful agent run — reset to CLOSED."""
        state, _, _ = self._breakers.get(agent_id, (self.CLOSED, 0, 0.0))
        if state != self.CLOSED:
            log.info("Circuit breaker CLOSED for %s (recovered)", agent_id)
        self._breakers[agent_id] = (self.CLOSED, 0, 0.0)

    def record_failure(self, agent_id: str) -> None:
        """Record an agent failure — may trip to OPEN.

        Only counts *consecutive* failures since the last success.
        record_success() resets the counter to 0, so a recovered agent
        needs failure_threshold fresh failures to trip again.
        """
        state, failures, _ = self._breakers.get(agent_id, (self.CLOSED, 0, 0.0))
        failures += 1
        now = time.monotonic()
        if failures >= self._failure_threshold:
            if state != self.OPEN:
                log.warning("Circuit breaker OPEN for %s (%d consecutive failures)", agent_id, failures)
            self._breakers[agent_id] = (self.OPEN, failures, now)
        else:
            self._breakers[agent_id] = (state, failures, now)

    def get_state(self, agent_id: str) -> str:
        """Return the current circuit state for an agent."""
        state, _, _ = self._breakers.get(agent_id, (self.CLOSED, 0, 0.0))
        return state

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return circuit breaker state for reporting."""
        result: dict[str, dict[str, Any]] = {}
        for agent_id, (state, failures, last_fail) in self._breakers.items():
            if state != self.CLOSED or failures > 0:
                result[agent_id] = {
                    "state": state,
                    "consecutive_failures": failures,
                    "last_failure_ago_s": round(time.monotonic() - last_fail, 1) if last_fail else 0,
                }
        return result


class SystemOptimizationAgent:
    """Global configuration and policy optimizer.

    Responsibilities:
    1. Track per-agent performance metrics (latency, yield, keep rate, errors)
    2. Monitor pipeline stage health
    3. Generate tuning recommendations
    4. Produce health reports for observability
    5. Circuit breaker: auto-disable failing agents with gradual recovery
    """

    agent_id = "system_optimization_agent"

    def __init__(
        self,
        error_rate_threshold: float = 0.3,
        min_yield_threshold: float = 0.5,
        latency_threshold_ms: float = 10000.0,
        keep_rate_threshold: float = 0.1,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: float = 120.0,
    ) -> None:
        self._agents: dict[str, AgentMetric] = {}
        self._stages: dict[str, StageMetric] = {}
        self._error_rate_threshold = error_rate_threshold
        self._min_yield_threshold = min_yield_threshold
        self._latency_threshold_ms = latency_threshold_ms
        self._keep_rate_threshold = keep_rate_threshold
        self._disabled_agents: set[str] = set()
        self._weight_overrides: dict[str, float] = {}
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_seconds=circuit_recovery_seconds,
        )

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
        # Track consecutive zero-yield runs (agent returns [] without error).
        if candidate_count == 0 and not error:
            m.zero_yield_streak += 1
            m.total_zero_yields += 1
        else:
            m.zero_yield_streak = 0

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

            # Silent zero-yield: agent runs without error but produces nothing.
            # This catches scenarios like an API changing its response format
            # or a source going permanently empty without raising exceptions.
            streak = m.zero_yield_streak
            if streak >= 5:
                recommendations.append(TuningRecommendation(
                    agent_id=agent_id,
                    action="investigate",
                    reason=f"Agent has returned 0 candidates for {streak} consecutive "
                           f"runs without error — source may be silently broken",
                    severity="high",
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
            "circuit_breakers": self.circuit_breaker.snapshot(),
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
