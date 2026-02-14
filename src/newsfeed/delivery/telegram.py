from __future__ import annotations

import html
import re

from newsfeed.models.domain import (
    BriefingType,
    DeliveryPayload,
    GeoRiskEntry,
    NarrativeThread,
    ReportItem,
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
    BriefingType.MORNING_DIGEST: "Intelligence Digest",
    BriefingType.BREAKING_ALERT: "Intelligence Digest",
    BriefingType.EVENING_SUMMARY: "Evening Summary",
    BriefingType.DEEP_DIVE: "Deep Dive Analysis",
}

_BRIEFING_ICON = {
    BriefingType.MORNING_DIGEST: "\U0001f4cb",
    BriefingType.BREAKING_ALERT: "\U0001f4cb",
    BriefingType.EVENING_SUMMARY: "\U0001f319",
    BriefingType.DEEP_DIVE: "\U0001f50d",
}

_SECTION_LINE = "\u2500" * 28
_HEAVY_LINE = "\u2501" * 28


def _esc(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return html.escape(text, quote=False)


def _esc_url(url: str) -> str:
    """Escape a URL for use inside an href attribute.

    Only escapes quotes and angle brackets — ampersands must stay literal
    so URL query parameters work (Telegram's HTML parser handles this).
    """
    return url.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


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
    normalized = text.replace("\xa0", " ")
    parts = re.split(r"  +(?=[A-Z])", normalized, maxsplit=1)
    cleaned = parts[0].strip()
    cleaned = re.sub(r"^via\s+[^:]+:\s*", "", cleaned, flags=re.IGNORECASE)
    if len(cleaned) > 800:
        cut = cleaned[:800].rfind(".")
        if cut > 300:
            cleaned = cleaned[:cut + 1]
        else:
            cleaned = cleaned[:797] + "..."
    return cleaned


def _format_region(name: str) -> str:
    """Format a region name for display."""
    if "_" in name:
        return name.replace("_", " ").title()
    if "," in name or (name and name[0].isupper()):
        return name
    return name.title()


def _confidence_label(item: ReportItem) -> str:
    """Return a short confidence indicator."""
    if not item.confidence:
        return ""
    mid = item.confidence.mid
    if mid >= 0.80:
        return "High confidence"
    if mid >= 0.55:
        return "Moderate confidence"
    return "Low confidence"


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
                source_note = (
                    f" ({thread.source_count} sources)"
                    if thread.source_count > 1
                    else ""
                )
                lines.append(
                    f"  \u2022 <b>{_esc(thread.headline)}</b>{source_note}"
                )

        # Intelligence brief (main stories)
        if payload.items:
            lines.append("")
            lines.append(_section("Intelligence Brief"))
            lines.append("")

        for idx, item in enumerate(payload.items, start=1):
            card_text = self.format_story_card(item, idx)
            lines.append(card_text)
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

    def format_header(self, payload: DeliveryPayload, ticker_bar: str = "",
                      tracked_count: int = 0) -> str:
        """Format the briefing header message (ticker + geo risks + trends + threads)."""
        lines: list[str] = []

        icon = _BRIEFING_ICON.get(payload.briefing_type, "\U0001f4cb")
        header = _BRIEFING_HEADER.get(payload.briefing_type, "NewsFeed Brief")
        lines.append(f"<b>{icon} {header}</b>")
        lines.append(f"<i>{_human_time(payload.generated_at)}</i>")

        # Executive summary — one-line scan of what's in this briefing
        if payload.items:
            summary = self._build_exec_summary(payload, tracked_count)
            lines.append("")
            lines.append(summary)

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
                source_note = (
                    f" ({thread.source_count} sources)"
                    if thread.source_count > 1
                    else ""
                )
                lines.append(
                    f"  \u2022 <b>{_esc(thread.headline)}</b>{source_note}"
                )

        if payload.items:
            lines.append("")
            lines.append(f"<i>{len(payload.items)} stories follow \u2193</i>")

        return "\n".join(lines).strip()

    def format_story_card(self, item: ReportItem, index: int,
                          is_tracked: bool = False) -> str:
        """Format a single story as a rich, readable news card.

        Includes the full intelligence context: summary, why it matters,
        what changed, predictive outlook, confidence, and related reads.
        Each card is sent as a separate Telegram message (~4096 char limit).
        """
        lines: list[str] = []
        c = item.candidate

        # ── Tracked badge ──
        tracked_badge = "\U0001f4cc " if is_tracked else ""

        # ── Title + source ──
        title_esc = _esc(c.title)
        if c.url and not c.url.startswith("https://example.com"):
            title_line = (
                f'<b>{tracked_badge}{index}. '
                f'<a href="{_esc_url(c.url)}">{title_esc}</a></b>'
            )
        else:
            title_line = f"<b>{tracked_badge}{index}. {title_esc}</b>"

        source_tag = f"<i>[{_esc(c.source)}]</i>"
        lines.append(f"{title_line} {source_tag}")

        # ── Summary ──
        body = c.summary.strip() if c.summary else ""
        if body:
            body = _clean_summary(body)
            lines.append("")
            lines.append(_esc(body))

        # ── Why it matters ──
        if item.why_it_matters:
            lines.append("")
            lines.append(f"<b>Why it matters:</b> {_esc(item.why_it_matters)}")

        # ── What changed ──
        if item.what_changed:
            lines.append(f"<b>What changed:</b> {_esc(item.what_changed)}")

        # ── Predictive outlook ──
        if item.predictive_outlook:
            lines.append("")
            lines.append(f"\U0001f52e <i>{_esc(item.predictive_outlook)}</i>")

        # ── Contrarian signal ──
        if item.contrarian_note:
            lines.append("")
            lines.append(f"\u26a1 {_esc(item.contrarian_note)}")

        # ── Metadata footer ──
        meta_parts: list[str] = []

        conf = _confidence_label(item)
        if conf:
            meta_parts.append(conf)

        if c.regions:
            meta_parts.append(
                ", ".join(_format_region(r) for r in c.regions[:4])
            )

        if c.corroborated_by:
            meta_parts.append(
                f"Verified by {', '.join(c.corroborated_by[:3])}"
            )

        if meta_parts:
            lines.append("")
            dot_sep = " \u00b7 "
            lines.append(f"<i>{dot_sep.join(meta_parts)}</i>")

        # ── Adjacent reads ──
        if item.adjacent_reads:
            reads = [_esc(r) for r in item.adjacent_reads[:3] if r]
            if reads:
                lines.append("")
                lines.append("<b>Related:</b> " + " \u2022 ".join(reads))

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
            if "thread_count" in meta:
                parts.append(f"{meta['thread_count']} threads")
            if meta.get("emerging_trends", 0) > 0:
                parts.append(f"{meta['emerging_trends']} emerging")
            if parts:
                sep = " \u2502 "
                lines.append(f"<i>{sep.join(parts)}</i>")
        return "\n".join(lines).strip()

    def format_closing(self, payload: DeliveryPayload,
                       topic_weights: dict[str, float] | None = None,
                       source_weights: dict[str, float] | None = None) -> str:
        """Format the closing message with user weightings and options."""
        lines: list[str] = []
        lines.append(f"<b>{_SECTION_LINE}</b>")

        if topic_weights:
            lines.append("")
            lines.append("<b>Your Topics</b>")
            sorted_topics = sorted(topic_weights.items(), key=lambda x: x[1], reverse=True)
            topic_strs = []
            for topic, weight in sorted_topics:
                name = topic.replace("_", " ").title()
                topic_strs.append(f"{name} ({weight:.0%})")
            lines.append(", ".join(topic_strs))

        if source_weights:
            lines.append("")
            lines.append("<b>Source Preferences</b>")
            for src, sw in sorted(source_weights.items(), key=lambda x: -x[1]):
                label = "\u2191" if sw > 0 else "\u2193"
                lines.append(f"  {src}: {label} {sw:+.1f}")

        lines.append("")
        lines.append("<i>Adjust: /feedback more [topic] or less [topic]</i>")
        lines.append("<i>Sources: /feedback prefer [source] or demote [source]</i>")

        return "\n".join(lines).strip()

    def format_deep_dive(self, item: ReportItem, index: int) -> str:
        """Format a full deep-dive analysis of a single story.

        Shows everything the pipeline knows: full summary, analysis context,
        confidence band with key assumptions, evidence breakdown, discovery
        source, lifecycle stage, and related reads.
        """
        lines: list[str] = []
        c = item.candidate

        # Header
        lines.append(f"<b>\U0001f50d Deep Dive: Story #{index}</b>")
        lines.append("")

        # Title + source
        title_esc = _esc(c.title)
        if c.url and not c.url.startswith("https://example.com"):
            lines.append(
                f'<b><a href="{_esc_url(c.url)}">{title_esc}</a></b>'
            )
        else:
            lines.append(f"<b>{title_esc}</b>")
        lines.append(f"<i>[{_esc(c.source)}]</i>")

        # Full summary — no truncation for deep dive
        body = c.summary.strip() if c.summary else ""
        if body:
            body = body.replace("\xa0", " ")
            lines.append("")
            lines.append(_esc(body))

        # Analysis section
        lines.append("")
        lines.append(_section("Analysis"))

        if item.why_it_matters:
            lines.append("")
            lines.append(f"<b>Why it matters:</b> {_esc(item.why_it_matters)}")

        if item.what_changed:
            lines.append("")
            lines.append(f"<b>What changed:</b> {_esc(item.what_changed)}")

        if item.predictive_outlook:
            lines.append("")
            lines.append(f"\U0001f52e <b>Outlook:</b> {_esc(item.predictive_outlook)}")

        if item.contrarian_note:
            lines.append("")
            lines.append(f"\u26a1 <b>Contrarian signal:</b> {_esc(item.contrarian_note)}")

        # Confidence assessment
        if item.confidence:
            lines.append("")
            lines.append(_section("Confidence"))
            band = item.confidence
            label = _confidence_label(item)
            lines.append(
                f"{label} ({band.low:.0%} \u2013 {band.mid:.0%} \u2013 {band.high:.0%})"
            )
            if band.key_assumptions:
                lines.append("")
                lines.append("<b>Key assumptions:</b>")
                for assumption in band.key_assumptions[:4]:
                    lines.append(f"  \u2022 {_esc(assumption)}")

        # Source intelligence
        lines.append("")
        lines.append(_section("Source Intelligence"))

        if c.discovered_by:
            lines.append(f"Discovered by: {_esc(c.discovered_by)}")

        if c.corroborated_by:
            corr_list = ", ".join(c.corroborated_by)
            lines.append(f"Corroborated by: {_esc(corr_list)}")

        lines.append(f"Story stage: {c.lifecycle.value.title()}")
        lines.append(
            f"Scores: evidence {c.evidence_score:.0%} "
            f"\u00b7 novelty {c.novelty_score:.0%} "
            f"\u00b7 relevance {c.preference_fit:.0%}"
        )

        if c.regions:
            region_list = ", ".join(_format_region(r) for r in c.regions)
            lines.append(f"\U0001f4cd {region_list}")

        # Related reads
        if item.adjacent_reads:
            reads = [_esc(r) for r in item.adjacent_reads if r]
            if reads:
                lines.append("")
                lines.append(_section("Related"))
                for read in reads[:5]:
                    lines.append(f"  \u2022 {read}")

        return "\n".join(lines).strip()

    def _build_exec_summary(self, payload: DeliveryPayload,
                            tracked_count: int = 0) -> str:
        """Build a one-line executive summary of the briefing."""
        from collections import Counter
        topics = Counter(item.candidate.topic for item in payload.items)
        top = topics.most_common(3)
        topic_parts = []
        for topic, count in top:
            name = topic.replace("_", " ")
            topic_parts.append(f"{count} {name}" if count > 1 else name)
        summary = ", ".join(topic_parts)

        extras = []
        if tracked_count:
            extras.append(f"\U0001f4cc {tracked_count} tracked")
        escalating = [r for r in payload.geo_risks if r.is_escalating()]
        if escalating:
            extras.append(f"\u26a0\ufe0f {len(escalating)} risk alerts")
        emerging = [t for t in payload.trends if t.is_emerging]
        if emerging:
            extras.append(f"\U0001f4c8 {len(emerging)} trending")

        line = f"<b>{len(payload.items)} stories:</b> {summary}"
        if extras:
            dot_sep = " \u00b7 "
            line += f" \u2014 {dot_sep.join(extras)}"
        return line

    def format_comparison(self, item: ReportItem,
                          others: list, story_index: int) -> str:
        """Format a multi-source comparison showing how different outlets cover the same story."""
        lines: list[str] = []
        c = item.candidate

        lines.append(f"<b>\U0001f50e Source Comparison: Story #{story_index}</b>")
        lines.append("")

        # The selected story (what the user saw)
        lines.append(f"<b>\u2500\u2500\u2500 {_esc(c.source)} (selected) \u2500\u2500\u2500</b>")
        lines.append(f"<b>{_esc(c.title)}</b>")
        if c.summary:
            lines.append(_esc(_clean_summary(c.summary)))
        lines.append("")

        # Other sources covering the same story
        if not others:
            lines.append("<i>No other sources found covering this story.</i>")
            lines.append("<i>This may be an exclusive or early-breaking report.</i>")
        else:
            for other in others[:5]:
                lines.append(f"<b>\u2500\u2500\u2500 {_esc(other.source)} \u2500\u2500\u2500</b>")
                lines.append(f"<b>{_esc(other.title)}</b>")
                if other.summary:
                    lines.append(_esc(_clean_summary(other.summary)))
                if other.url and not other.url.startswith("https://example.com"):
                    lines.append(f'<a href="{_esc_url(other.url)}">Read full article</a>')
                lines.append("")

        lines.append(f"<i>{1 + len(others)} sources covering this story</i>")

        return "\n".join(lines).strip()

    def format_tracked_update(self, candidate, tracked_headline: str) -> str:
        """Format a proactive notification for a tracked story update."""
        lines: list[str] = []
        lines.append(f"<b>\U0001f4cc Tracked Story Update</b>")
        lines.append(f"<i>You're tracking: {_esc(tracked_headline)}</i>")
        lines.append("")
        title_esc = _esc(candidate.title)
        if candidate.url and not candidate.url.startswith("https://example.com"):
            lines.append(
                f'<b><a href="{_esc_url(candidate.url)}">{title_esc}</a></b>'
            )
        else:
            lines.append(f"<b>{title_esc}</b>")
        lines.append(f"<i>[{_esc(candidate.source)}]</i>")
        if candidate.summary:
            lines.append("")
            lines.append(_esc(_clean_summary(candidate.summary)))
        return "\n".join(lines).strip()

    def format_recall(self, keyword: str, items: list[dict]) -> str:
        """Format search results from past briefings."""
        lines: list[str] = []
        lines.append(f'<b>\U0001f50d Recall: "{_esc(keyword)}"</b>')
        lines.append(f"<i>{len(items)} results from past briefings</i>")
        lines.append("")

        for i, item in enumerate(items[:10], 1):
            title = _esc(item.get("title", ""))
            source = _esc(item.get("source", ""))
            topic = item.get("topic", "").replace("_", " ").title()
            url = item.get("url", "")

            if url and not url.startswith("https://example.com"):
                lines.append(f'{i}. <a href="{_esc_url(url)}">{title}</a> <i>[{source}]</i>')
            else:
                lines.append(f"{i}. <b>{title}</b> <i>[{source}]</i>")

            # Show why_it_matters if available, else summary snippet
            context = item.get("why_it_matters") or item.get("summary", "")
            if context:
                lines.append(f"   {_esc(context[:150])}")

            if topic:
                lines.append(f"   <i>{topic}</i>")
            lines.append("")

        if not items:
            lines.append("<i>No matching stories found in your briefing history.</i>")

        return "\n".join(lines).strip()

    def format_insights(self, insights: dict) -> str:
        """Format preference learning insights for the user."""
        lines: list[str] = []
        lines.append("<b>\U0001f9e0 Your Intelligence Profile</b>")
        lines.append(f"<i>Based on {insights['total_ratings']} ratings over {insights['days']} days</i>")
        lines.append("")

        if not insights["total_ratings"]:
            lines.append("<i>Not enough data yet. Rate stories with \U0001f44d/\U0001f44e to build your profile.</i>")
            return "\n".join(lines)

        # Topic affinities
        topics = insights.get("topics", [])
        if topics:
            lines.append("<b>Topic Affinities</b>")
            for t in topics[:8]:
                ups = t["ups"] or 0
                downs = t["downs"] or 0
                net = ups - downs
                total = t["total"] or 1
                pct = ups / total * 100
                name = t["topic"].replace("_", " ").title()
                if net > 0:
                    indicator = "\u2191"
                elif net < 0:
                    indicator = "\u2193"
                else:
                    indicator = "\u2192"
                lines.append(f"  {indicator} <b>{_esc(name)}</b>: {ups}\U0001f44d {downs}\U0001f44e ({pct:.0f}% positive)")
            lines.append("")

        # Source preferences
        sources = insights.get("sources", [])
        if sources:
            lines.append("<b>Source Preferences</b>")
            for s in sources[:8]:
                ups = s["ups"] or 0
                downs = s["downs"] or 0
                total = s["total"] or 1
                pct = ups / total * 100
                src = _esc(s["source"])
                if pct >= 70:
                    indicator = "\u2705"
                elif pct <= 30:
                    indicator = "\u274c"
                else:
                    indicator = "\u2796"
                lines.append(f"  {indicator} <b>{src}</b>: {pct:.0f}% approval ({total} ratings)")
            lines.append("")

        # Suggestions based on patterns
        suggestions = insights.get("suggestions", [])
        if suggestions:
            lines.append("<b>Suggestions</b>")
            for s in suggestions:
                lines.append(f"  \u2022 {_esc(s)}")
            lines.append("")

        # Applied adjustments
        applied = insights.get("applied", [])
        if applied:
            lines.append("<b>Auto-adjusted</b>")
            for a in applied:
                lines.append(f"  \u2022 {_esc(a)}")

        return "\n".join(lines).strip()

    def format_weekly(self, summary: dict) -> str:
        """Format a weekly intelligence digest."""
        lines: list[str] = []
        days = summary.get("days", 7)
        lines.append("<b>\U0001f4ca Weekly Intelligence Digest</b>")
        lines.append(f"<i>Past {days} days</i>")
        lines.append("")

        briefings = summary.get("briefing_count", 0)
        stories = summary.get("story_count", 0)
        if not stories:
            lines.append("<i>No briefings delivered this week. Run /briefing to get started.</i>")
            return "\n".join(lines)

        lines.append(f"<b>{briefings} briefings</b> delivered \u00b7 <b>{stories} stories</b> total")
        lines.append("")

        # Topic breakdown
        topic_dist = summary.get("topic_distribution", [])
        if topic_dist:
            lines.append(_section("Coverage by Topic"))
            for t in topic_dist[:8]:
                name = t["topic"].replace("_", " ").title()
                count = t["count"]
                bar = "\u2588" * max(1, min(count, 15))
                lines.append(f"  {_esc(name)}: {count} {bar}")
            lines.append("")

        # Source breakdown
        source_dist = summary.get("source_distribution", [])
        if source_dist:
            lines.append(_section("Top Sources"))
            for s in source_dist[:6]:
                lines.append(f"  {_esc(s['source'])}: {s['count']} stories")
            lines.append("")

        # Engagement stats
        rating_total = summary.get("rating_total", 0)
        if rating_total:
            ups = summary.get("rating_ups", 0)
            downs = summary.get("rating_downs", 0)
            lines.append(_section("Your Engagement"))
            lines.append(f"  {rating_total} ratings: {ups} \U0001f44d {downs} \U0001f44e")
            # Top rated stories
            top_rated = summary.get("top_rated", [])
            if top_rated:
                lines.append("  <b>Your favorites:</b>")
                for tr in top_rated[:3]:
                    title = _esc(tr.get("title", "")[:60])
                    lines.append(f"    \u2022 {title}")
            lines.append("")

        # Trends
        trends = summary.get("trends", [])
        if trends:
            lines.append(_section("Trending This Week"))
            for t in trends:
                name = t["topic"].replace("_", " ").title()
                score = t["anomaly_score"]
                lines.append(f"  \U0001f4c8 <b>{_esc(name)}</b>: {score:.1f}x baseline")
            lines.append("")

        # Geo-risk summary
        georisks = summary.get("georisks", [])
        escalating = [g for g in georisks if g.get("escalation_delta", 0) > 0.05]
        if escalating:
            lines.append(_section("Geo Risk Watch"))
            for g in escalating[:4]:
                region = _format_region(g["region"])
                level = g["risk_level"]
                delta = g["escalation_delta"]
                lines.append(f"  \u26a0\ufe0f <b>{_esc(region)}</b>: {level:.0%} (\u2191{delta:.0%})")
            lines.append("")

        lines.append("<i>Adjust coverage: /feedback more [topic] or less [topic]</i>")

        return "\n".join(lines).strip()

    def format_intelligence_alert(self, alert_type: str,
                                  data: dict) -> str:
        """Format a proactive intelligence alert (geo-risk or trend spike)."""
        lines: list[str] = []

        if alert_type == "georisk":
            region = _format_region(data["region"])
            level = data["risk_level"]
            delta = data["escalation_delta"]
            lines.append(f"<b>\U0001f6a8 Geo-Risk Alert: {_esc(region)}</b>")
            lines.append(f"Risk level: <b>{level:.0%}</b> (\u2191{delta:.0%} change)")
            drivers = data.get("drivers", [])
            if drivers:
                lines.append("")
                lines.append("<b>Drivers:</b>")
                for d in drivers[:3]:
                    lines.append(f"  \u2022 {_esc(d)}")
            lines.append("")
            lines.append("<i>Run /briefing for full analysis</i>")

        elif alert_type == "trend":
            topic = data["topic"].replace("_", " ").title()
            score = data["anomaly_score"]
            lines.append(f"<b>\U0001f4c8 Trend Alert: {_esc(topic)}</b>")
            lines.append(f"Activity at <b>{score:.1f}x</b> baseline")
            lines.append("")
            lines.append("<i>Run /briefing for full coverage</i>")

        return "\n".join(lines).strip()
