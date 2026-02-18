"""Operator dashboard for NewsFeed.

Provides a comprehensive view of system health, pipeline performance,
agent success rates, and user metrics — delivered via Telegram or JSON API.

This module generates the data; delivery is handled by:
- /admin dashboard (Telegram command)
- /metrics (Prometheus endpoint)
- /health (JSON API)
"""
from __future__ import annotations

import html
import time
from typing import Any


class OperatorDashboard:
    """Generates operational health dashboards from engine state."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def full_snapshot(self) -> dict[str, Any]:
        """Generate a complete operational snapshot (JSON-serializable)."""
        return {
            "timestamp": time.time(),
            "pipeline": self._pipeline_health(),
            "agents": self._agent_health(),
            "users": self._user_metrics(),
            "intelligence": self._intelligence_health(),
            "system": self._system_health(),
        }

    def _pipeline_health(self) -> dict[str, Any]:
        """Pipeline stage health and latency."""
        opt = self._engine.optimizer
        stages = {}
        for stage_name in ["research", "intelligence", "expert_council", "article_enrichment", "editorial_review"]:
            stage_metrics = opt.get_stage_metrics(stage_name) if hasattr(opt, 'get_stage_metrics') else {}
            stages[stage_name] = stage_metrics

        return {
            "stages": stages,
            "enabled_stages": list(self._engine._enabled_stages),
            "total_agents_configured": len(self._engine.config.get("research_agents", [])),
        }

    def _agent_health(self) -> dict[str, Any]:
        """Per-agent success rates and circuit breaker status."""
        cb = self._engine.optimizer.circuit_breaker
        agents = {}

        for agent_cfg in self._engine.config.get("research_agents", []):
            aid = agent_cfg.get("id", "")
            circuit = cb._circuits.get(aid) if hasattr(cb, '_circuits') else None
            agents[aid] = {
                "source": agent_cfg.get("source", ""),
                "circuit_state": getattr(circuit, 'state', 'closed') if circuit else "closed",
                "disabled": aid in self._engine._disabled_agents,
            }

        return {
            "agents": agents,
            "total": len(agents),
            "healthy": sum(1 for a in agents.values() if a["circuit_state"] == "closed"),
            "open": sum(1 for a in agents.values() if a["circuit_state"] == "open"),
            "disabled": sum(1 for a in agents.values() if a["disabled"]),
        }

    def _user_metrics(self) -> dict[str, Any]:
        """User activity metrics."""
        try:
            stats = self._engine.analytics.get_system_stats()
        except Exception:
            stats = {}

        ac = self._engine.access_control
        return {
            "access_control": ac.get_user_count(),
            "analytics": stats,
        }

    def _intelligence_health(self) -> dict[str, Any]:
        """Intelligence pipeline component health."""
        return {
            "credibility_sources_tracked": len(self._engine.credibility._sources),
            "trend_baselines": len(self._engine.trends._baselines) if hasattr(self._engine.trends, '_baselines') else 0,
            "georisk_regions_tracked": len(self._engine.georisk._index) if hasattr(self._engine.georisk, '_index') else 0,
            "expert_council": {
                "expert_count": len(self._engine.experts._expert_ids) if hasattr(self._engine.experts, '_expert_ids') else 0,
                "influence_rankings": [
                    {"expert_id": eid, "influence": f"{inf:.2f}"}
                    for eid, inf, _ in self._engine.experts.chair.rankings()
                ] if hasattr(self._engine.experts, 'chair') else [],
            },
        }

    def _system_health(self) -> dict[str, Any]:
        """System-level health metrics."""
        return {
            "preferences_loaded": len(self._engine.preferences._profiles) if hasattr(self._engine.preferences, '_profiles') else 0,
            "cache_size": len(self._engine.cache._cache) if hasattr(self._engine.cache, '_cache') else 0,
            "persistence_enabled": self._engine._persistence is not None,
            "d1_state_enabled": hasattr(self._engine, '_d1_state'),
        }

    def format_telegram_dashboard(self) -> str:
        """Format the dashboard as a Telegram HTML message."""
        snap = self.full_snapshot()
        lines: list[str] = []

        lines.append("<b>Operator Dashboard</b>")
        lines.append("")

        # Pipeline Health
        pipe = snap.get("pipeline", {})
        lines.append("<b>Pipeline</b>")
        lines.append(f"  Agents configured: {pipe.get('total_agents_configured', '?')}")
        lines.append(f"  Stages enabled: {', '.join(pipe.get('enabled_stages', []))}")
        lines.append("")

        # Agent Health
        agents = snap.get("agents", {})
        healthy = agents.get("healthy", 0)
        total = agents.get("total", 0)
        open_cb = agents.get("open", 0)
        disabled = agents.get("disabled", 0)

        if total:
            ratio = healthy / total
            icon = "\u2705" if ratio >= 0.75 else ("\u26a0\ufe0f" if ratio >= 0.5 else "\U0001f534")
        else:
            icon = "\u2753"
        lines.append(f"<b>{icon} Agents</b>")
        lines.append(f"  Healthy: {healthy}/{total}")
        if open_cb:
            lines.append(f"  Circuit breaker open: {open_cb}")
        if disabled:
            lines.append(f"  Disabled: {disabled}")
        lines.append("")

        # Users
        users = snap.get("users", {})
        ac = users.get("access_control", {})
        lines.append("<b>Users</b>")
        lines.append(f"  Allowed: {ac.get('allowed', '?')}")
        lines.append(f"  Admins: {ac.get('admin', '?')}")
        lines.append(f"  Pending: {ac.get('pending', 0)}")
        lines.append("")

        # Intelligence
        intel = snap.get("intelligence", {})
        lines.append("<b>Intelligence</b>")
        lines.append(f"  Sources tracked: {intel.get('credibility_sources_tracked', '?')}")
        lines.append(f"  Trend baselines: {intel.get('trend_baselines', '?')}")
        lines.append(f"  Geo-risk regions: {intel.get('georisk_regions_tracked', '?')}")

        # Expert Council
        council = intel.get("expert_council", {})
        if council.get("influence_rankings"):
            lines.append("")
            lines.append("<b>Expert Influence</b>")
            for rank in council["influence_rankings"][:5]:
                lines.append(f"  {html.escape(rank['expert_id'])}: {rank['influence']}")

        # System
        sys_h = snap.get("system", {})
        lines.append("")
        lines.append("<b>System</b>")
        lines.append(f"  Preferences loaded: {sys_h.get('preferences_loaded', '?')}")
        lines.append(f"  Cache entries: {sys_h.get('cache_size', '?')}")
        lines.append(f"  Persistence: {'D1' if sys_h.get('d1_state_enabled') else ('file' if sys_h.get('persistence_enabled') else 'none')}")

        return "\n".join(lines)
