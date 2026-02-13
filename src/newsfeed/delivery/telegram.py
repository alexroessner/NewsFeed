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
    BriefingType.MORNING_DIGEST: "Morning Intelligence Digest",
    BriefingType.BREAKING_ALERT: "BREAKING ALERT",
    BriefingType.EVENING_SUMMARY: "Evening Summary",
    BriefingType.DEEP_DIVE: "Deep Dive Analysis",
}

_BRIEFING_ICON = {
    BriefingType.MORNING_DIGEST: "\U0001f4cb",
    BriefingType.BREAKING_ALERT: "\U0001f6a8",
    BriefingType.EVENING_SUMMARY: "\U0001f319",
    BriefingType.DEEP_DIVE: "\U0001f50d",
}

_SECTION_LINE = "\u2500" * 28
_HEAVY_LINE = "\u2501" * 28


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return html.escape(text, quote=False)


def _human_time(dt) -> str:
    """Format datetime for human readability."""
    return dt.strftime("%b %d, %Y \u00b7 %H:%M UTC")


def _section(title: str) -> str:
    """Format a section header with trailing line."""
    pad = max(0, 28 - len(title) - 4)
    trail = "\u2500" * pad
    return f"<b>\u2500\u2500\u2500 {title} {trail}</b>"


class TelegramFormatter:
    def format(self, payload: DeliveryPayload, ticker_bar: str = "") -> str:
        lines: list[str] = []

        # Header
        icon = _BRIEFING_ICON.get(payload.briefing_type, "\U0001f4cb")
        header = _BRIEFING_HEADER.get(payload.briefing_type, "NewsFeed Brief")
        lines.append(f"<b>{_HEAVY_LINE}</b>")
        lines.append(f"<b>{icon} {header}</b>")
        lines.append(f"<i>{_human_time(payload.generated_at)}</i>")
        lines.append(f"<b>{_HEAVY_LINE}</b>")

        # Market ticker bar
        if ticker_bar:
            lines.append("")
            lines.append(ticker_bar)

        # Geo risk alerts
        if payload.geo_risks:
            escalating = [r for r in payload.geo_risks if r.is_escalating()]
            if escalating:
                lines.append("")
                lines.append(_section("Geo Risk Alerts"))
                for risk in escalating[:4]:
                    arrow = "\u2191" if risk.escalation_delta > 0 else "\u2193"
                    lines.append(
                        f"  \u2022 <b>{_esc(risk.region)}</b>: {risk.risk_level:.0%} "
                        f"({arrow}{abs(risk.escalation_delta):.0%})"
                    )

        # Emerging trends
        if payload.trends:
            emerging = [t for t in payload.trends if t.is_emerging]
            if emerging:
                lines.append("")
                lines.append(_section("Emerging Trends"))
                for trend in emerging[:4]:
                    lines.append(
                        f"  \u2022 <b>{_esc(trend.topic)}</b>: "
                        f"{trend.anomaly_score:.1f}x baseline"
                    )

        # Narrative threads
        if payload.threads:
            lines.append("")
            lines.append(_section("Narrative Threads"))
            for thread in payload.threads[:5]:
                urgency_icon = _URGENCY_ICON.get(thread.urgency, "")
                prefix = f"{urgency_icon} " if urgency_icon else "  "
                source_note = (
                    f" ({thread.source_count} sources)"
                    if thread.source_count > 1
                    else ""
                )
                conf = ""
                if thread.confidence:
                    conf = f" \u00b7 {thread.confidence.label()}"
                lines.append(
                    f"{prefix}<b>{_esc(thread.headline)}</b>{source_note}{conf}"
                )

        # Intelligence brief (main stories)
        if payload.items:
            lines.append("")
            lines.append(_section("Intelligence Brief"))
            lines.append("")

        for idx, item in enumerate(payload.items, start=1):
            c = item.candidate
            urgency_icon = _URGENCY_ICON.get(c.urgency, "")
            prefix = f"{urgency_icon} " if urgency_icon else ""

            # Title as clickable link
            title_esc = _esc(c.title)
            if c.url and not c.url.startswith("https://example.com"):
                title_line = (
                    f'<b>{idx}. {prefix}'
                    f'<a href="{_esc(c.url)}">{title_esc}</a></b>'
                )
            else:
                title_line = f"<b>{idx}. {prefix}{title_esc}</b>"

            source_tag = f"<i>[{_esc(c.source)}]</i>"
            lines.append(f"{title_line} {source_tag}")

            # Why it matters
            lines.append(f"   {_esc(item.why_it_matters)}")

            # What changed
            if item.what_changed:
                lines.append(
                    f"   \u25b8 <i>Changed:</i> {_esc(item.what_changed)}"
                )

            # Outlook
            if item.predictive_outlook:
                lines.append(
                    f"   \u25b8 <i>Outlook:</i> {_esc(item.predictive_outlook)}"
                )

            # Confidence
            if item.confidence:
                lines.append(
                    f"   \u25b8 <i>Confidence:</i> {item.confidence.label()} "
                    f"({item.confidence.low:.0%}\u2013{item.confidence.high:.0%})"
                )

            # Corroboration
            if c.corroborated_by:
                lines.append(
                    f"   \u25b8 <i>Verified by:</i> {', '.join(c.corroborated_by)}"
                )

            # Contrarian note
            if item.contrarian_note:
                lines.append(
                    f"   \u26a1 <i>{_esc(item.contrarian_note)}</i>"
                )

            lines.append("")

        # Footer
        lines.append(f"<b>{_SECTION_LINE}</b>")
        meta = payload.metadata
        if meta:
            parts = []
            if "selected_count" in meta:
                parts.append(f"{meta['selected_count']} items")
            if "debate_vote_count" in meta:
                parts.append(f"{meta['debate_vote_count']} votes")
            if "thread_count" in meta:
                parts.append(f"{meta['thread_count']} threads")
            if meta.get("emerging_trends", 0) > 0:
                parts.append(f"{meta['emerging_trends']} emerging")
            if parts:
                sep = " \u2502 "
                lines.append(f"<i>{sep.join(parts)}</i>")

        return "\n".join(lines).strip()
