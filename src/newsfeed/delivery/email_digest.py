"""Professional HTML email digest delivery.

Generates beautifully formatted, responsive email briefings from
DeliveryPayload data. Uses inline CSS for maximum email client compatibility.

SMTP configuration via environment variables or pipeline config:
  - SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
  - SMTP_FROM (sender address)
  - SMTP_USE_TLS (default: true)
"""
from __future__ import annotations

import html
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from newsfeed.models.domain import DeliveryPayload, ReportItem

log = logging.getLogger(__name__)


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


_SAFE_SCHEMES = frozenset({"http", "https", "ftp"})


def _safe_url(url: str) -> bool:
    """Reject javascript:, data:, and other dangerous URI schemes."""
    scheme = url.split(":", 1)[0].lower().strip() if ":" in url else ""
    return scheme in _SAFE_SCHEMES


def _esc_attr(text: str) -> str:
    """Escape text for use inside an HTML attribute (quotes must be escaped)."""
    return html.escape(text, quote=True)


def _confidence_label(item: ReportItem) -> str:
    if not item.confidence:
        return ""
    mid = item.confidence.mid
    if mid >= 0.80:
        return "High confidence"
    if mid >= 0.55:
        return "Moderate confidence"
    return "Low confidence"


# ── Inline CSS (email-safe) ──────────────────────────────────────────

_STYLES = """
body { margin: 0; padding: 0; background-color: #f4f4f7; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; }
.wrapper { width: 100%; background-color: #f4f4f7; padding: 24px 0; }
.container { max-width: 640px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); padding: 32px 24px; text-align: center; }
.header h1 { color: #ffffff; font-size: 22px; margin: 0 0 6px; font-weight: 600; letter-spacing: 0.5px; }
.header .subtitle { color: #a0aec0; font-size: 13px; margin: 0; }
.exec-summary { background-color: #f7fafc; border-bottom: 1px solid #e2e8f0; padding: 16px 24px; font-size: 14px; color: #4a5568; }
.exec-summary strong { color: #1a202c; }
.section { padding: 0 24px; }
.section-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.2px; color: #718096; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; margin: 24px 0 16px; }
.story-card { border-left: 3px solid #4299e1; padding: 16px; margin-bottom: 16px; background-color: #f7fafc; border-radius: 0 6px 6px 0; }
.story-card.tracked { border-left-color: #ed8936; }
.story-title { font-size: 16px; font-weight: 600; color: #1a202c; margin: 0 0 4px; line-height: 1.4; }
.story-title a { color: #2b6cb0; text-decoration: none; }
.story-title a:hover { text-decoration: underline; }
.story-source { font-size: 12px; color: #718096; margin-bottom: 10px; }
.story-body { font-size: 14px; color: #4a5568; line-height: 1.6; margin-bottom: 10px; }
.story-meta { font-size: 13px; color: #2d3748; }
.story-meta strong { color: #1a202c; }
.outlook { font-style: italic; color: #6b46c1; font-size: 13px; margin-top: 8px; }
.confidence { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 10px; background-color: #edf2f7; color: #4a5568; margin-top: 6px; }
.meta-row { font-size: 12px; color: #718096; margin-top: 8px; }
.alert-box { padding: 12px 16px; margin: 8px 0; border-radius: 6px; font-size: 13px; }
.alert-risk { background-color: #fff5f5; border: 1px solid #feb2b2; color: #c53030; }
.alert-trend { background-color: #f0fff4; border: 1px solid #9ae6b4; color: #276749; }
.footer { background-color: #1a1a2e; padding: 24px; text-align: center; }
.footer p { color: #a0aec0; font-size: 12px; margin: 4px 0; }
.footer a { color: #63b3ed; text-decoration: none; }
.tracked-badge { display: inline-block; background-color: #ed8936; color: #fff; font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-right: 4px; vertical-align: middle; }
.divider { height: 1px; background-color: #e2e8f0; margin: 20px 24px; }
"""


class EmailDigest:
    """Generate and send professional HTML email briefings."""

    def __init__(self, smtp_cfg: dict[str, Any] | None = None) -> None:
        cfg = smtp_cfg or {}
        self._host = cfg.get("host") or os.environ.get("SMTP_HOST", "")
        self._port = int(cfg.get("port") or os.environ.get("SMTP_PORT", "587"))
        self._user = cfg.get("user") or os.environ.get("SMTP_USER", "")
        self._password = cfg.get("password") or os.environ.get("SMTP_PASSWORD", "")
        self._from = cfg.get("from_address") or os.environ.get("SMTP_FROM", "")
        use_tls = cfg.get("use_tls", os.environ.get("SMTP_USE_TLS", "true"))
        self._use_tls = str(use_tls).lower() in ("true", "1", "yes")

    @property
    def is_configured(self) -> bool:
        return bool(self._host and self._from)

    def render(self, payload: DeliveryPayload,
               tracked_flags: list[bool] | None = None,
               weekly_summary: dict | None = None) -> str:
        """Render a complete HTML email from a DeliveryPayload."""
        if tracked_flags is None:
            tracked_flags = [False] * len(payload.items)

        parts: list[str] = []
        parts.append(self._html_head())
        parts.append(self._render_header(payload))
        parts.append(self._render_exec_summary(payload, tracked_flags))

        # Geo-risk alerts
        escalating = [r for r in payload.geo_risks if r.is_escalating()]
        if escalating:
            parts.append(self._render_section_title("Geo-Risk Alerts"))
            for risk in escalating[:4]:
                parts.append(self._render_risk_alert(risk))

        # Emerging trends
        emerging = [t for t in payload.trends if t.is_emerging]
        if emerging:
            parts.append(self._render_section_title("Emerging Trends"))
            for trend in emerging[:4]:
                parts.append(self._render_trend_alert(trend))

        # Story cards
        if payload.items:
            parts.append(self._render_section_title("Intelligence Brief"))
            for idx, item in enumerate(payload.items):
                is_tracked = tracked_flags[idx] if idx < len(tracked_flags) else False
                parts.append(self._render_story(item, idx + 1, is_tracked))

        # Weekly summary section (optional)
        if weekly_summary:
            parts.append(self._render_weekly_section(weekly_summary))

        parts.append(self._render_footer())
        parts.append("</div></body></html>")

        return "\n".join(parts)

    def send(self, to_address: str, subject: str, html_body: str) -> bool:
        """Send an HTML email via SMTP."""
        if not self.is_configured:
            log.warning("Email not configured — skipping send to %s", to_address)
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = to_address

        # Plain-text fallback
        plain = "Your NewsFeed Intelligence Briefing is ready. View this email in an HTML-capable client."
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            if self._use_tls:
                server = smtplib.SMTP(self._host, self._port, timeout=15)
                server.ehlo()
                server.starttls()
            else:
                server = smtplib.SMTP(self._host, self._port, timeout=15)

            if self._user and self._password:
                server.login(self._user, self._password)

            server.sendmail(self._from, [to_address], msg.as_string())
            server.quit()
            log.info("Email sent to %s", to_address)
            return True
        except Exception:
            log.exception("Failed to send email to %s", to_address)
            return False

    # ── Private HTML builders ────────────────────────────────────

    def _html_head(self) -> str:
        return (
            '<!DOCTYPE html>'
            '<html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
            f'<style>{_STYLES}</style>'
            '</head>'
            '<body><div class="wrapper"><div class="container">'
        )

    def _render_header(self, payload: DeliveryPayload) -> str:
        ts = payload.generated_at.strftime("%B %d, %Y &middot; %H:%M UTC")
        return (
            '<div class="header">'
            '<h1>&#128225; Intelligence Digest</h1>'
            f'<p class="subtitle">{ts}</p>'
            '</div>'
        )

    def _render_exec_summary(self, payload: DeliveryPayload,
                              tracked_flags: list[bool]) -> str:
        from collections import Counter
        topics = Counter(item.candidate.topic for item in payload.items)
        top = topics.most_common(3)
        topic_parts = []
        for topic, count in top:
            name = _esc(topic.replace("_", " "))
            topic_parts.append(f"{count} {name}" if count > 1 else name)
        summary = ", ".join(topic_parts)

        extras = []
        tracked_count = sum(tracked_flags)
        if tracked_count:
            extras.append(f"&#128204; {tracked_count} tracked")
        escalating = [r for r in payload.geo_risks if r.is_escalating()]
        if escalating:
            extras.append(f"&#9888;&#65039; {len(escalating)} risk alerts")

        line = f"<strong>{len(payload.items)} stories:</strong> {summary}"
        if extras:
            line += f" &mdash; {' &middot; '.join(extras)}"

        return f'<div class="exec-summary">{line}</div>'

    def _render_section_title(self, title: str) -> str:
        return f'<div class="section"><div class="section-title">{_esc(title)}</div></div>'

    def _render_risk_alert(self, risk) -> str:
        region = risk.region.replace("_", " ").title()
        delta = abs(risk.escalation_delta)
        return (
            '<div class="section">'
            f'<div class="alert-box alert-risk">'
            f'<strong>{_esc(region)}</strong>: {risk.risk_level:.0%} risk '
            f'(&#8593;{delta:.0%} change)'
            '</div></div>'
        )

    def _render_trend_alert(self, trend) -> str:
        topic = trend.topic.replace("_", " ").title()
        return (
            '<div class="section">'
            f'<div class="alert-box alert-trend">'
            f'<strong>{_esc(topic)}</strong>: {trend.anomaly_score:.1f}x baseline activity'
            '</div></div>'
        )

    def _render_story(self, item: ReportItem, index: int,
                      is_tracked: bool) -> str:
        c = item.candidate
        card_class = "story-card tracked" if is_tracked else "story-card"

        # Title
        tracked_badge = '<span class="tracked-badge">TRACKED</span>' if is_tracked else ""
        title_esc = _esc(c.title)
        if c.url and not c.url.startswith("https://example.com") and _safe_url(c.url):
            title_html = (
                f'<div class="story-title">{tracked_badge}'
                f'{index}. <a href="{_esc_attr(c.url)}">{title_esc}</a></div>'
            )
        else:
            title_html = f'<div class="story-title">{tracked_badge}{index}. {title_esc}</div>'

        source_html = f'<div class="story-source">{_esc(c.source)}</div>'

        # Body
        body_parts = []
        if c.summary:
            summary = c.summary.strip()
            if len(summary) > 800:
                cut = summary[:800].rfind(".")
                summary = summary[:cut + 1] if cut > 300 else summary[:797] + "..."
            body_parts.append(f'<div class="story-body">{_esc(summary)}</div>')

        # Analysis
        meta_parts = []
        if item.why_it_matters:
            meta_parts.append(
                f'<div class="story-meta"><strong>Why it matters:</strong> {_esc(item.why_it_matters)}</div>'
            )
        if item.what_changed:
            meta_parts.append(
                f'<div class="story-meta"><strong>What changed:</strong> {_esc(item.what_changed)}</div>'
            )
        if item.predictive_outlook:
            meta_parts.append(
                f'<div class="outlook">&#128302; {_esc(item.predictive_outlook)}</div>'
            )

        # Confidence + meta
        footer_parts = []
        conf = _confidence_label(item)
        if conf:
            footer_parts.append(f'<span class="confidence">{conf}</span>')

        meta_line_parts = []
        if c.regions:
            region_str = ", ".join(_esc(r.replace("_", " ").title()) for r in c.regions[:3])
            meta_line_parts.append(region_str)
        if c.corroborated_by:
            corr = ", ".join(_esc(s) for s in c.corroborated_by[:3])
            meta_line_parts.append(f"Verified by {corr}")
        if meta_line_parts:
            footer_parts.append(
                f'<div class="meta-row">{" &middot; ".join(meta_line_parts)}</div>'
            )

        # Related reads
        if item.adjacent_reads:
            reads = [_esc(r) for r in item.adjacent_reads[:3] if r]
            if reads:
                footer_parts.append(
                    f'<div class="meta-row"><strong>Related:</strong> {" &bull; ".join(reads)}</div>'
                )

        return (
            f'<div class="section"><div class="{card_class}">'
            f'{title_html}{source_html}'
            f'{"".join(body_parts)}'
            f'{"".join(meta_parts)}'
            f'{"".join(footer_parts)}'
            '</div></div>'
        )

    def _render_weekly_section(self, summary: dict) -> str:
        parts = []
        parts.append(self._render_section_title("Weekly Overview"))

        briefings = summary.get("briefing_count", 0)
        stories = summary.get("story_count", 0)
        parts.append(
            f'<div class="section">'
            f'<p style="font-size:14px;color:#4a5568;">'
            f'<strong>{briefings}</strong> briefings &middot; '
            f'<strong>{stories}</strong> stories delivered this week'
            f'</p></div>'
        )

        # Topic distribution
        topic_dist = summary.get("topic_distribution", [])
        if topic_dist:
            rows = []
            max_count = max(t["count"] for t in topic_dist) if topic_dist else 1
            for t in topic_dist[:6]:
                name = t["topic"].replace("_", " ").title()
                count = t["count"]
                pct = count / max_count * 100
                rows.append(
                    f'<div style="margin:6px 0;">'
                    f'<span style="display:inline-block;width:120px;font-size:13px;color:#2d3748;">{_esc(name)}</span>'
                    f'<span style="display:inline-block;width:60%;background:#edf2f7;border-radius:4px;height:18px;">'
                    f'<span style="display:block;background:#4299e1;border-radius:4px;height:18px;width:{pct:.0f}%;"></span>'
                    f'</span>'
                    f'<span style="font-size:12px;color:#718096;margin-left:8px;">{count}</span>'
                    f'</div>'
                )
            parts.append(f'<div class="section">{"".join(rows)}</div>')

        return "\n".join(parts)

    def _render_footer(self) -> str:
        return (
            '<div class="footer">'
            '<p><strong>NewsFeed Intelligence</strong></p>'
            '<p>Personalized intelligence briefings from 23+ sources</p>'
            '<p style="margin-top:12px;">Manage preferences via Telegram: /settings</p>'
            '</div></div>'
        )
