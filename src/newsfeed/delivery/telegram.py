from __future__ import annotations

import html
import re

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


def _clean_summary(text: str) -> str:
    """Clean up RSS summary artifacts (Google News multi-headline concatenations, etc.)."""
    # Normalize non-breaking spaces (\xa0) to regular spaces first
    normalized = text.replace("\xa0", " ")
    # Google News RSS concatenates headlines: "Story  SourceOther story  Source2..."
    # Split on double-space followed by a capitalized word (source name boundary)
    parts = re.split(r"  +(?=[A-Z])", normalized, maxsplit=1)
    cleaned = parts[0].strip()
    # Also remove "via SourceName: " prefix from web aggregator summaries
    cleaned = re.sub(r"^via\s+[^:]+:\s*", "", cleaned, flags=re.IGNORECASE)
    # Cap at a readable length
    if len(cleaned) > 280:
        # Cut at last sentence boundary before 280
        cut = cleaned[:280].rfind(".")
        if cut > 100:
            cleaned = cleaned[:cut + 1]
        else:
            cleaned = cleaned[:277] + "..."
    return cleaned


def _format_region(name: str) -> str:
    """Format a region name for display."""
    return name.replace("_", " ").title()


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
                        f"  \u2022 <b>{_esc(_format_region(risk.region))}</b>: {risk.risk_level:.0%} "
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

    # ── Multi-message formatters ──────────────────────────────────

    def format_header(self, payload: DeliveryPayload, ticker_bar: str = "") -> str:
        """Format the briefing header message (ticker + geo risks + trends + threads)."""
        lines: list[str] = []

        icon = _BRIEFING_ICON.get(payload.briefing_type, "\U0001f4cb")
        header = _BRIEFING_HEADER.get(payload.briefing_type, "NewsFeed Brief")
        lines.append(f"<b>{_HEAVY_LINE}</b>")
        lines.append(f"<b>{icon} {header}</b>")
        lines.append(f"<i>{_human_time(payload.generated_at)}</i>")
        lines.append(f"<b>{_HEAVY_LINE}</b>")

        if ticker_bar:
            lines.append("")
            lines.append(ticker_bar)

        if payload.geo_risks:
            escalating = [r for r in payload.geo_risks if r.is_escalating()]
            if escalating:
                lines.append("")
                lines.append(_section("Geo Risk Alerts"))
                for risk in escalating[:4]:
                    arrow = "\u2191" if risk.escalation_delta > 0 else "\u2193"
                    lines.append(
                        f"  \u2022 <b>{_esc(_format_region(risk.region))}</b>: {risk.risk_level:.0%} "
                        f"({arrow}{abs(risk.escalation_delta):.0%})"
                    )

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

        if payload.items:
            lines.append("")
            lines.append(f"<i>\U0001f4ca {len(payload.items)} stories follow</i>")

        return "\n".join(lines).strip()

    def format_story_card(self, item: ReportItem, index: int) -> str:
        """Format a single story as a clean, readable news card."""
        lines: list[str] = []
        c = item.candidate
        urgency_icon = _URGENCY_ICON.get(c.urgency, "")
        prefix = f"{urgency_icon} " if urgency_icon else ""

        # Title as clickable link + source
        title_esc = _esc(c.title)
        if c.url and not c.url.startswith("https://example.com"):
            title_line = (
                f'<b>{index}. {prefix}'
                f'<a href="{_esc(c.url)}">{title_esc}</a></b>'
            )
        else:
            title_line = f"<b>{index}. {prefix}{title_esc}</b>"

        source_tag = f"<i>[{_esc(c.source)}]</i>"
        lines.append(f"{title_line} {source_tag}")

        # Main body: the actual story content
        body = c.summary.strip() if c.summary else ""
        if body:
            body = _clean_summary(body)
            lines.append("")
            lines.append(_esc(body))

        # Contrarian signal — genuinely interesting when present
        if item.contrarian_note:
            lines.append("")
            lines.append(f"\u26a1 {_esc(item.contrarian_note)}")

        # Compact metadata footer: regions + corroboration
        meta_parts: list[str] = []
        if c.regions:
            meta_parts.append(
                f"\U0001f4cd {', '.join(r.replace('_', ' ').title() for r in c.regions[:3])}"
            )
        if c.corroborated_by:
            meta_parts.append(
                f"\u2713 {', '.join(c.corroborated_by[:3])}"
            )
        if meta_parts:
            lines.append("")
            dot_sep = " \u00b7 "
            lines.append(f"<i>{dot_sep.join(meta_parts)}</i>")

        return "\n".join(lines).strip()

    def format_footer(self, payload: DeliveryPayload) -> str:
        """Format the footer stats message."""
        lines: list[str] = []
        lines.append(f"<b>{_SECTION_LINE}</b>")
        meta = payload.metadata
        if meta:
            parts: list[str] = []
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
