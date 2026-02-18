"""Alerting system for NewsFeed operational health.

Monitors key metrics and fires alerts when thresholds are breached:
- Agent failure rate too high
- Pipeline latency spikes
- Error rate increases
- Database growth warnings

Alerts are delivered via the Telegram bot to admin users.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class AlertRule:
    """A single alert rule with threshold and cooldown."""

    def __init__(
        self,
        name: str,
        check_fn: Callable[[], float],
        threshold: float,
        comparison: str = "gt",  # "gt" or "lt"
        cooldown_seconds: int = 3600,
        severity: str = "warning",
        message_template: str = "",
    ) -> None:
        self.name = name
        self.check_fn = check_fn
        self.threshold = threshold
        self.comparison = comparison
        self.cooldown_seconds = cooldown_seconds
        self.severity = severity
        self.message_template = message_template or f"Alert: {name}"
        self._last_fired: float = 0

    def evaluate(self) -> tuple[bool, str]:
        """Check if the alert should fire.

        Returns (should_fire, message).
        """
        now = time.monotonic()
        if now - self._last_fired < self.cooldown_seconds:
            return False, ""

        try:
            value = self.check_fn()
        except Exception:
            return False, ""

        fired = False
        if self.comparison == "gt" and value > self.threshold:
            fired = True
        elif self.comparison == "lt" and value < self.threshold:
            fired = True

        if fired:
            self._last_fired = now
            msg = self.message_template.format(
                name=self.name, value=value, threshold=self.threshold,
                severity=self.severity,
            )
            return True, msg

        return False, ""


class AlertManager:
    """Manages alert rules and dispatches notifications."""

    def __init__(self) -> None:
        self._rules: list[AlertRule] = []
        self._alert_handlers: list[Callable[[str, str, str], None]] = []
        self._fired_history: list[dict[str, Any]] = []
        # Cap history to prevent unbounded growth
        self._MAX_HISTORY = 200

    def add_rule(self, rule: AlertRule) -> None:
        """Register an alert rule."""
        self._rules.append(rule)

    def add_handler(self, handler: Callable[[str, str, str], None]) -> None:
        """Register an alert handler: fn(name, severity, message)."""
        self._alert_handlers.append(handler)

    def check_all(self) -> list[dict[str, str]]:
        """Evaluate all rules and fire alerts for any breached thresholds.

        Returns list of fired alerts with name, severity, and message.
        """
        fired = []
        for rule in self._rules:
            should_fire, msg = rule.evaluate()
            if should_fire:
                log.warning("Alert fired: %s — %s", rule.name, msg)
                alert = {
                    "name": rule.name,
                    "severity": rule.severity,
                    "message": msg,
                    "ts": time.time(),
                }
                fired.append(alert)
                self._fired_history.append(alert)
                if len(self._fired_history) > self._MAX_HISTORY:
                    self._fired_history = self._fired_history[-100:]

                for handler in self._alert_handlers:
                    try:
                        handler(rule.name, rule.severity, msg)
                    except Exception:
                        log.debug("Alert handler failed", exc_info=True)

        return fired

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent alert history."""
        return self._fired_history[-limit:]

    def status_summary(self) -> dict[str, Any]:
        """Return alert system status."""
        return {
            "rules_count": len(self._rules),
            "handlers_count": len(self._alert_handlers),
            "alerts_fired_total": len(self._fired_history),
            "recent_alerts": len(self.recent_alerts(10)),
        }


def create_default_alerts(engine: Any) -> AlertManager:
    """Create standard alert rules for a NewsFeed engine.

    Args:
        engine: NewsFeedEngine instance to monitor
    """
    mgr = AlertManager()

    # Alert: Too many agents failing
    def agent_failure_rate() -> float:
        cb = engine.optimizer.circuit_breaker
        total = len(cb._circuits) if hasattr(cb, '_circuits') else 0
        open_count = sum(1 for c in getattr(cb, '_circuits', {}).values()
                        if getattr(c, 'state', '') == 'open')
        return open_count / max(total, 1)

    mgr.add_rule(AlertRule(
        name="high_agent_failure_rate",
        check_fn=agent_failure_rate,
        threshold=0.5,
        comparison="gt",
        cooldown_seconds=1800,
        severity="critical",
        message_template="Agent failure rate at {value:.0%} — {threshold:.0%} threshold breached. Check agent connectivity.",
    ))

    # Alert: Low source coverage
    def source_coverage() -> float:
        total = len(engine.config.get("research_agents", []))
        # Check optimizer health for active agents
        health = engine.optimizer.health_snapshot()
        active = health.get("active_agents", total)
        return active / max(total, 1)

    mgr.add_rule(AlertRule(
        name="low_source_coverage",
        check_fn=source_coverage,
        threshold=0.3,
        comparison="lt",
        cooldown_seconds=3600,
        severity="warning",
        message_template="Source coverage dropped to {value:.0%} — below {threshold:.0%} minimum. Briefing quality degraded.",
    ))

    return mgr
