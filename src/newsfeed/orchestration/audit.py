"""Audit trail system — tracks every decision, vote, and change for full observability.

Records:
- Request lifecycle events with timing
- Expert votes with rationale (per candidate, per expert)
- Editorial review changes
- Configuration changes
- Preference updates
- Agent performance per cycle
- Selection/rejection reasons for every candidate
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditEvent:
    """A single auditable event in the pipeline."""
    timestamp: float
    event_type: str    # "research", "vote", "selection", "review", "config", "preference", "delivery"
    request_id: str
    details: dict[str, Any]

    def summary(self) -> str:
        return f"[{self.event_type}] {self.details.get('summary', '')}"


class AuditTrail:
    """Full decision audit for every pipeline execution.

    Designed for:
    1. Post-hoc review: "Why was this story included/excluded?"
    2. Expert accountability: "How did each expert vote and why?"
    3. System debugging: "Which stage caused the bottleneck?"
    4. User transparency: "Show me the reasoning behind my briefing."
    """

    def __init__(self, max_requests: int = 50) -> None:
        self._events: list[AuditEvent] = []
        self._max_requests = max_requests
        self._request_index: dict[str, list[int]] = defaultdict(list)

    def record(self, event_type: str, request_id: str, **details: Any) -> None:
        """Record an audit event."""
        idx = len(self._events)
        event = AuditEvent(
            timestamp=time.time(),
            event_type=event_type,
            request_id=request_id,
            details=details,
        )
        self._events.append(event)
        self._request_index[request_id].append(idx)
        self._trim()

    # ──────────────────────────────────────────────────────────────
    # Convenience recording methods
    # ──────────────────────────────────────────────────────────────

    def record_research(self, request_id: str, agent_id: str, source: str,
                        candidate_count: int, latency_ms: float) -> None:
        self.record("research", request_id,
                    agent_id=agent_id, source=source,
                    candidate_count=candidate_count, latency_ms=round(latency_ms, 1),
                    summary=f"{agent_id} produced {candidate_count} candidates in {latency_ms:.0f}ms")

    def record_vote(self, request_id: str, expert_id: str, candidate_id: str,
                    keep: bool, confidence: float, rationale: str, risk_note: str,
                    arbitrated: bool = False) -> None:
        self.record("vote", request_id,
                    expert_id=expert_id, candidate_id=candidate_id,
                    keep=keep, confidence=confidence,
                    rationale=rationale, risk_note=risk_note,
                    arbitrated=arbitrated,
                    summary=f"{expert_id} {'KEEP' if keep else 'DROP'} {candidate_id} "
                            f"(conf={confidence:.2f}){' [arbitrated]' if arbitrated else ''}")

    def record_selection(self, request_id: str, candidate_id: str, title: str,
                         selected: bool, reason: str, composite_score: float) -> None:
        self.record("selection", request_id,
                    candidate_id=candidate_id, title=title,
                    selected=selected, reason=reason,
                    composite_score=round(composite_score, 3),
                    summary=f"{'SELECTED' if selected else 'REJECTED'} {title[:50]} "
                            f"(score={composite_score:.3f}): {reason}")

    def record_review(self, request_id: str, reviewer_id: str, candidate_id: str,
                      field_name: str, before: str, after: str) -> None:
        changed = before != after
        self.record("review", request_id,
                    reviewer_id=reviewer_id, candidate_id=candidate_id,
                    field=field_name, changed=changed,
                    before_len=len(before), after_len=len(after),
                    summary=f"{reviewer_id} {'rewrote' if changed else 'kept'} "
                            f"{field_name} for {candidate_id}")

    def record_config_change(self, request_id: str, path: str,
                             old_value: Any, new_value: Any, source: str) -> None:
        self.record("config", request_id,
                    path=path, old=old_value, new=new_value, source=source,
                    summary=f"Config {path}: {old_value} → {new_value} (by {source})")

    def record_preference(self, request_id: str, user_id: str,
                          action: str, details: str) -> None:
        self.record("preference", request_id,
                    user_id=user_id, action=action, detail=details,
                    summary=f"Preference update for {user_id}: {action} — {details}")

    def record_delivery(self, request_id: str, user_id: str,
                        item_count: int, briefing_type: str,
                        total_elapsed_s: float) -> None:
        self.record("delivery", request_id,
                    user_id=user_id, item_count=item_count,
                    briefing_type=briefing_type,
                    total_elapsed_s=round(total_elapsed_s, 3),
                    summary=f"Delivered {item_count} items ({briefing_type}) to {user_id} "
                            f"in {total_elapsed_s:.2f}s")

    # ──────────────────────────────────────────────────────────────
    # Query methods
    # ──────────────────────────────────────────────────────────────

    def get_request_trace(self, request_id: str) -> list[dict]:
        """Get full audit trace for a request."""
        indices = self._request_index.get(request_id, [])
        return [
            {"ts": self._events[i].timestamp, "type": self._events[i].event_type,
             "summary": self._events[i].summary(), **self._events[i].details}
            for i in indices
        ]

    def get_candidate_trace(self, request_id: str, candidate_id: str) -> list[dict]:
        """Get all events for a specific candidate in a request."""
        trace = self.get_request_trace(request_id)
        return [e for e in trace if e.get("candidate_id") == candidate_id]

    def get_expert_votes(self, request_id: str) -> dict[str, list[dict]]:
        """Get all expert votes grouped by expert."""
        trace = self.get_request_trace(request_id)
        votes: dict[str, list[dict]] = defaultdict(list)
        for event in trace:
            if event["type"] == "vote":
                votes[event["expert_id"]].append(event)
        return dict(votes)

    def get_recent_requests(self, limit: int = 10) -> list[str]:
        """Get IDs of recent requests."""
        seen: list[str] = []
        for event in reversed(self._events):
            if event.request_id not in seen:
                seen.append(event.request_id)
                if len(seen) >= limit:
                    break
        return seen

    def format_request_report(self, request_id: str) -> str:
        """Generate a human-readable audit report for a request."""
        trace = self.get_request_trace(request_id)
        if not trace:
            return f"No audit data for request {request_id}"

        lines = [f"AUDIT REPORT: {request_id}", "=" * 60]

        # Group by type
        by_type: dict[str, list[dict]] = defaultdict(list)
        for event in trace:
            by_type[event["type"]].append(event)

        # Research phase
        if "research" in by_type:
            lines.append("\n--- RESEARCH PHASE ---")
            total_candidates = 0
            for e in by_type["research"]:
                lines.append(f"  {e['summary']}")
                total_candidates += e.get("candidate_count", 0)
            lines.append(f"  Total raw candidates: {total_candidates}")

        # Expert votes
        if "vote" in by_type:
            lines.append("\n--- EXPERT COUNCIL ---")
            vote_summary: dict[str, dict] = defaultdict(lambda: {"keep": 0, "drop": 0})
            for e in by_type["vote"]:
                key = e["candidate_id"]
                if e["keep"]:
                    vote_summary[key]["keep"] += 1
                else:
                    vote_summary[key]["drop"] += 1
            for cid, counts in vote_summary.items():
                verdict = "ACCEPTED" if counts["keep"] > counts["drop"] else "REJECTED"
                lines.append(f"  {cid}: {counts['keep']} keep / {counts['drop']} drop → {verdict}")
            arb = sum(1 for e in by_type["vote"] if e.get("arbitrated"))
            if arb:
                lines.append(f"  ({arb} votes revised through arbitration)")

        # Selection
        if "selection" in by_type:
            lines.append("\n--- SELECTION ---")
            for e in by_type["selection"]:
                lines.append(f"  {e['summary']}")

        # Editorial review
        if "review" in by_type:
            lines.append("\n--- EDITORIAL REVIEW ---")
            rewritten = sum(1 for e in by_type["review"] if e.get("changed"))
            total = len(by_type["review"])
            lines.append(f"  {rewritten}/{total} fields rewritten by editorial agents")

        # Delivery
        if "delivery" in by_type:
            lines.append("\n--- DELIVERY ---")
            for e in by_type["delivery"]:
                lines.append(f"  {e['summary']}")

        # Config changes
        if "config" in by_type:
            lines.append("\n--- CONFIGURATION CHANGES ---")
            for e in by_type["config"]:
                lines.append(f"  {e['summary']}")

        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        """Return aggregate audit statistics."""
        by_type: dict[str, int] = defaultdict(int)
        for event in self._events:
            by_type[event.event_type] += 1
        return {
            "total_events": len(self._events),
            "tracked_requests": len(self._request_index),
            "events_by_type": dict(by_type),
        }

    def _trim(self) -> None:
        """Keep only the most recent N requests.

        Optimized to batch eviction: only triggers when 20% over capacity
        to amortize the cost of the index rebuild.  Without batching, the
        full O(n) rebuild would run on every record() call at capacity.
        """
        overshoot = len(self._request_index) - self._max_requests
        if overshoot < max(1, self._max_requests // 5):
            return  # Wait until 20% over before trimming (amortize cost)

        # Find oldest requests to drop
        requests_by_first = sorted(
            self._request_index.keys(),
            key=lambda r: self._request_index[r][0] if self._request_index[r] else 0,
        )
        to_drop = set(requests_by_first[:overshoot])
        drop_indices = set()
        for rid in to_drop:
            drop_indices.update(self._request_index.pop(rid))

        if drop_indices:
            old_events = self._events
            self._events = [e for i, e in enumerate(old_events) if i not in drop_indices]
            # Rebuild index (amortized — runs infrequently due to batch threshold)
            self._request_index.clear()
            for i, event in enumerate(self._events):
                self._request_index[event.request_id].append(i)
