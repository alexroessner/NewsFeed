from __future__ import annotations

import html

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
    UrgencyLevel.ELEVATED: "\u26a0\ufe0f",      # warning sign
    UrgencyLevel.BREAKING: "\U0001f534",          # red circle
    UrgencyLevel.CRITICAL: "\U0001f6a8",          # rotating light
}

_BRIEFING_HEADER = {
    BriefingType.MORNING_DIGEST: "\U0001f4cb Morning Intelligence Digest",
    BriefingType.BREAKING_ALERT: "\U0001f6a8 BREAKING ALERT",
    BriefingType.EVENING_SUMMARY: "\U0001f319 Evening Summary",
    BriefingType.DEEP_DIVE: "\U0001f50d Deep Dive Analysis",
}


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return html.escape(text, quote=False)


def _human_time(dt) -> str:
    """Format datetime for human readability."""
    return dt.strftime("%b %d, %Y %H:%M UTC")


class TelegramFormatter:
    def format(self, payload: DeliveryPayload) -> str:
        lines: list[str] = []

        header = _BRIEFING_HEADER.get(payload.briefing_type, "NewsFeed Brief")
        lines.append(f"<b>{header}</b>")
        lines.append(f"<i>{_human_time(payload.generated_at)}</i>")
        lines.append("")

        # Geo risk alerts
        if payload.geo_risks:
            escalating = [r for r in payload.geo_risks if r.is_escalating()]
            if escalating:
                lines.append("<b>\u2014 Geo Risk Alerts \u2014</b>")
                for risk in escalating[:4]:
                    arrow = "\u2191" if risk.escalation_delta > 0 else "\u2193"
                    lines.append(
                        f"  \u2022 <b>{_esc(risk.region)}</b>: {risk.risk_level:.0%} "
                        f"({arrow}{abs(risk.escalation_delta):.0%})"
                    )
                lines.append("")

        # Emerging trends
        if payload.trends:
            emerging = [t for t in payload.trends if t.is_emerging]
            if emerging:
                lines.append("<b>\u2014 Emerging Trends \u2014</b>")
                for trend in emerging[:4]:
                    lines.append(
                        f"  \u2022 <b>{_esc(trend.topic)}</b>: "
                        f"{trend.anomaly_score:.1f}x baseline"
                    )
                lines.append("")

        # Narrative threads (compact)
        if payload.threads:
            lines.append("<b>\u2014 Narrative Threads \u2014</b>")
            for thread in payload.threads[:5]:
                icon = _URGENCY_ICON.get(thread.urgency, "")
                prefix = f"{icon} " if icon else ""
                source_note = f"({thread.source_count} sources)" if thread.source_count > 1 else ""
                lines.append(f"  {prefix}{_esc(thread.headline)} {source_note}")
            lines.append("")

        # Main stories
        for idx, item in enumerate(payload.items, start=1):
            c = item.candidate
            icon = _URGENCY_ICON.get(c.urgency, "")
            prefix = f"{icon} " if icon else ""

            # Title as clickable link if URL available
            title_esc = _esc(c.title)
            if c.url and not c.url.startswith("https://example.com"):
                title_line = f'<b>{idx}. {prefix}<a href="{_esc(c.url)}">{title_esc}</a></b>'
            else:
                title_line = f"<b>{idx}. {prefix}{title_esc}</b>"

            source_tag = f"[{_esc(c.source)}]"
            lines.append(f"{title_line} {source_tag}")

            # Why it matters
            lines.append(f"   {_esc(item.why_it_matters)}")

            # What changed
            if item.what_changed:
                lines.append(f"   <i>Changed:</i> {_esc(item.what_changed)}")

            # Outlook
            if item.predictive_outlook:
                lines.append(f"   <i>Outlook:</i> {_esc(item.predictive_outlook)}")

            # Confidence
            if item.confidence:
                lines.append(
                    f"   <i>Confidence:</i> {item.confidence.label()} "
                    f"({item.confidence.low:.0%}-{item.confidence.high:.0%})"
                )

            # Corroboration
            if c.corroborated_by:
                lines.append(f"   <i>Corroborated by:</i> {', '.join(c.corroborated_by)}")

            lines.append("")

        # Footer
        meta = payload.metadata
        if meta:
            parts = []
            if "selected_count" in meta:
                parts.append(f"{meta['selected_count']} items")
            if "debate_vote_count" in meta:
                parts.append(f"{meta['debate_vote_count']} votes")
            if parts:
                lines.append(f"<i>{' | '.join(parts)}</i>")

        return "\n".join(lines).strip()
