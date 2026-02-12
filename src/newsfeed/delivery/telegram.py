from __future__ import annotations

from newsfeed.models.domain import (
    BriefingType,
    DeliveryPayload,
    GeoRiskEntry,
    NarrativeThread,
    TrendSnapshot,
    UrgencyLevel,
)

_URGENCY_ICON = {
    UrgencyLevel.ROUTINE: "",
    UrgencyLevel.ELEVATED: "[ELEVATED]",
    UrgencyLevel.BREAKING: "[BREAKING]",
    UrgencyLevel.CRITICAL: "[CRITICAL]",
}

_BRIEFING_HEADER = {
    BriefingType.MORNING_DIGEST: "Morning Intelligence Digest",
    BriefingType.BREAKING_ALERT: "BREAKING ALERT",
    BriefingType.EVENING_SUMMARY: "Evening Summary",
    BriefingType.DEEP_DIVE: "Deep Dive Analysis",
}


class TelegramFormatter:
    def format(self, payload: DeliveryPayload) -> str:
        lines: list[str] = []

        header = _BRIEFING_HEADER.get(payload.briefing_type, "NewsFeed Brief")
        lines.append(f"{header} ({payload.generated_at.isoformat()})")
        lines.append("")

        if payload.geo_risks:
            escalating = [r for r in payload.geo_risks if r.is_escalating()]
            if escalating:
                lines.append("--- GEO RISK ALERTS ---")
                for risk in escalating[:5]:
                    arrow = "^" if risk.escalation_delta > 0 else "v"
                    lines.append(
                        f"  {risk.region}: risk {risk.risk_level:.0%} ({arrow}{abs(risk.escalation_delta):.0%})"
                    )
                    for d in risk.drivers[:2]:
                        lines.append(f"    - {d}")
                lines.append("")

        if payload.trends:
            emerging = [t for t in payload.trends if t.is_emerging]
            if emerging:
                lines.append("--- EMERGING TRENDS ---")
                for trend in emerging[:5]:
                    lines.append(
                        f"  {trend.topic}: velocity {trend.velocity:.0%} "
                        f"(anomaly {trend.anomaly_score:.1f}x baseline)"
                    )
                lines.append("")

        if payload.threads:
            lines.append("--- NARRATIVE THREADS ---")
            for thread in payload.threads[:8]:
                urgency = _URGENCY_ICON.get(thread.urgency, "")
                prefix = f"{urgency} " if urgency else ""
                source_note = f"({thread.source_count} sources)" if thread.source_count > 1 else "(single source)"
                lines.append(f"  {prefix}{thread.headline} {source_note}")
                lines.append(f"    Lifecycle: {thread.lifecycle.value} | Score: {thread.thread_score():.2f}")
                if thread.confidence:
                    lines.append(
                        f"    Confidence: {thread.confidence.label()} "
                        f"({thread.confidence.low:.0%}-{thread.confidence.high:.0%})"
                    )
                    for assumption in thread.confidence.key_assumptions[:2]:
                        lines.append(f"      - {assumption}")
                lines.append(f"    Stories: {len(thread.candidates)}")
            lines.append("")

        for idx, item in enumerate(payload.items, start=1):
            urgency = _URGENCY_ICON.get(item.candidate.urgency, "")
            prefix = f"{urgency} " if urgency else ""
            lifecycle = f"[{item.candidate.lifecycle.value}]"

            lines.append(f"{idx}. {prefix}{item.candidate.title} [{item.candidate.source}] {lifecycle}")
            lines.append(f"   Why it matters: {item.why_it_matters}")
            lines.append(f"   Changed: {item.what_changed}")
            lines.append(f"   Outlook: {item.predictive_outlook}")

            if item.confidence:
                lines.append(
                    f"   Confidence: {item.confidence.label()} "
                    f"({item.confidence.low:.0%}-{item.confidence.high:.0%})"
                )
                if item.confidence.key_assumptions:
                    lines.append(f"   Assumptions: {'; '.join(item.confidence.key_assumptions[:3])}")

            if item.contrarian_note:
                lines.append(f"   Contrarian view: {item.contrarian_note}")

            if item.candidate.corroborated_by:
                lines.append(f"   Corroborated by: {', '.join(item.candidate.corroborated_by)}")

            if item.candidate.regions:
                lines.append(f"   Regions: {', '.join(item.candidate.regions)}")

            if item.adjacent_reads:
                lines.append("   Adjacent reads:")
                for read in item.adjacent_reads:
                    lines.append(f"   - {read}")
            lines.append("")

        meta = payload.metadata
        if meta:
            lines.append("---")
            parts = []
            if "selected_count" in meta:
                parts.append(f"{meta['selected_count']} items selected")
            if "debate_vote_count" in meta:
                parts.append(f"{meta['debate_vote_count']} expert votes")
            if "thread_count" in meta:
                parts.append(f"{meta['thread_count']} narrative threads")
            if "intelligence_stages" in meta:
                parts.append(f"Intelligence: {', '.join(meta['intelligence_stages'])}")
            if parts:
                lines.append("  " + " | ".join(parts))

        return "\n".join(lines).strip()
