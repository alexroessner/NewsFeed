"""Editorial review agents — rewrite and optimize report output for reader preferences.

Two agents form the editorial layer:
- StyleReviewAgent: Voice, tone, personalization, audience-appropriate language
- ClarityReviewAgent: Clarity, structure, concision, actionable framing

Both support LLM-backed rewriting (when API key available) and sophisticated
heuristic rewriting (always available). The persona files in personas/ provide
additional cognitive context for each review pass.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from newsfeed.models.domain import CandidateItem, ReportItem, UrgencyLevel, UserProfile

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Tone templates — used in heuristic mode
# ──────────────────────────────────────────────────────────────────────

# Default tone templates — overridden by editorial_review.tone_templates in config
# Tone templates — prefixes are empty because the formatter already adds field labels
# ("Why it matters:", "Changed:", "Outlook:").  Templates control content style only.
_DEFAULT_TONE_TEMPLATES: dict[str, dict[str, str]] = {
    "concise": {
        "why_prefix": "",
        "outlook_prefix": "",
        "changed_prefix": "",
        "style": "Short, direct sentences. No filler. Lead with the key fact.",
    },
    "analyst": {
        "why_prefix": "",
        "outlook_prefix": "",
        "changed_prefix": "",
        "style": "Technical, evidence-anchored language. Quantify when possible.",
    },
    "executive": {
        "why_prefix": "",
        "outlook_prefix": "",
        "changed_prefix": "",
        "style": "High-level framing. Decision-relevant. Skip operational detail.",
    },
    "brief": {
        "why_prefix": "",
        "outlook_prefix": "",
        "changed_prefix": "",
        "style": "Minimum viable context. One sentence per field.",
    },
    "deep": {
        "why_prefix": "",
        "outlook_prefix": "",
        "changed_prefix": "",
        "style": "Thorough analysis. Include nuance, uncertainty, alternative readings.",
    },
}

_DEFAULT_URGENCY_FRAMING: dict[str, str] = {
    "critical": "Immediate attention required. ",
    "breaking": "Developing rapidly. ",
    "elevated": "Worth monitoring closely. ",
    "routine": "",
}

_DEFAULT_WATCHPOINTS: dict[str, str] = {
    "geopolitics": "Watch for official statements and alliance responses in next 24-48h.",
    "ai_policy": "Track regulatory body announcements and industry response.",
    "markets": "Monitor market open and sector rotation for follow-through.",
    "technology": "Watch for adoption signals and competitive responses.",
    "crypto": "Track on-chain metrics and exchange flows for confirmation.",
    "climate": "Monitor policy responses and institutional commitments.",
    "science": "Watch for peer review outcomes and replication attempts.",
}

_DEFAULT_FILLER_PATTERNS: list[tuple[str, str]] = [
    (r"\bit is worth noting that\b", ""),
    (r"\bit should be noted that\b", ""),
    (r"\bin terms of\b", "regarding"),
    (r"\bat this point in time\b", "now"),
    (r"\bat the end of the day\b", "ultimately"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bin order to\b", "to"),
    (r"\ba significant amount of\b", "substantial"),
    (r"\bthe fact that\b", "that"),
    (r"\bin the process of\b", ""),
    (r"\bon a going-forward basis\b", "going forward"),
]


class StyleReviewAgent:
    """Review agent focused on voice, tone, personalization, and audience language.

    Responsibilities (from vision):
    - Enforce consistent voice across all report items
    - Adapt language to user's preferred tone (concise, analyst, executive, etc.)
    - Personalize framing based on user's topic weights and interests
    - Maintain factual integrity while reshaping presentation
    """

    agent_id = "review_agent_style"

    def __init__(
        self,
        persona_context: list[str] | None = None,
        llm_api_key: str = "",
        llm_model: str = "claude-sonnet-4-5-20250929",
        llm_base_url: str = "https://api.anthropic.com/v1",
        editorial_cfg: dict | None = None,
    ) -> None:
        self._persona_context = persona_context or []
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._llm_base_url = llm_base_url
        self._use_llm = bool(llm_api_key)

        cfg = editorial_cfg or {}
        self._tone_templates = cfg.get("tone_templates", _DEFAULT_TONE_TEMPLATES)
        # Build urgency framing from config (string keys) → UrgencyLevel keys
        raw_uf = cfg.get("urgency_framing", _DEFAULT_URGENCY_FRAMING)
        self._urgency_framing = {
            UrgencyLevel.CRITICAL: raw_uf.get("critical", "Immediate attention required. "),
            UrgencyLevel.BREAKING: raw_uf.get("breaking", "Developing rapidly. "),
            UrgencyLevel.ELEVATED: raw_uf.get("elevated", "Worth monitoring closely. "),
            UrgencyLevel.ROUTINE: raw_uf.get("routine", ""),
        }

    def review(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Apply style review to a report item, rewriting fields for voice and tone."""
        if self._use_llm:
            return self._review_llm(item, profile)
        return self._review_heuristic(item, profile)

    def _review_heuristic(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Apply tone-aware heuristic rewriting."""
        tone = profile.tone
        templates = self._tone_templates.get(tone, self._tone_templates.get("concise", _DEFAULT_TONE_TEMPLATES["concise"]))
        c = item.candidate

        # Rewrite why_it_matters with tone-specific prefix and topic personalization
        why = self._rewrite_why(item.why_it_matters, c, profile, templates)
        item.why_it_matters = why

        # Rewrite what_changed with urgency framing
        changed = self._rewrite_changed(item.what_changed, c, templates)
        item.what_changed = changed

        # Rewrite predictive_outlook
        outlook = self._rewrite_outlook(item.predictive_outlook, c, profile, templates)
        item.predictive_outlook = outlook

        return item

    def _rewrite_why(self, base: str, c: CandidateItem, profile: UserProfile,
                     templates: dict[str, str]) -> str:
        # Use the narrative-generated base text — it explains *why* the story
        # matters (source quality, corroboration, urgency, user alignment).
        # The summary is already shown separately; duplicating it here wastes
        # the reader's time and discards the pipeline's analytical value.
        text = base.strip()
        if not text:
            # Fallback only if narrative generation returned nothing
            text = c.title.strip()

        # Add urgency framing if not already present
        urgency_prefix = self._urgency_framing.get(c.urgency, "")
        if urgency_prefix and urgency_prefix.strip().rstrip(".").lower() not in text.lower():
            text = f"{urgency_prefix}{text}"

        return text

    def _rewrite_changed(self, base: str, c: CandidateItem,
                         templates: dict[str, str]) -> str:
        prefix = templates["changed_prefix"]

        # Ground the "what changed" in actual story content, not generic labels.
        # Use the first key phrase from the title as context.
        title_hint = c.title[:80].rstrip(".")
        parts = []

        if c.urgency in (UrgencyLevel.BREAKING, UrgencyLevel.CRITICAL):
            parts.append(f"{title_hint} — developing now")
        elif c.lifecycle.value == "developing":
            parts.append(f"{title_hint} — new development")
        else:
            parts.append(title_hint)

        if c.corroborated_by:
            parts.append(f"confirmed by {', '.join(c.corroborated_by[:2])}")

        return f"{prefix}{'; '.join(parts)}."

    def _rewrite_outlook(self, base: str, c: CandidateItem, profile: UserProfile,
                         templates: dict[str, str]) -> str:
        prefix = templates["outlook_prefix"]

        parts = []
        if c.prediction_signal > 0.7:
            parts.append("Strong forward-looking signal")
        elif c.prediction_signal > 0.4:
            parts.append("Moderate predictive indicators present")
        else:
            parts.append("Limited predictive signal at this stage")

        if c.regions:
            parts.append(f"regional focus: {', '.join(c.regions[:2])}")

        # Match user's region interest
        user_regions = set(profile.regions_of_interest)
        story_regions = set(c.regions)
        overlap = user_regions & story_regions
        if overlap:
            parts.append(f"intersects your region focus ({', '.join(overlap)})")

        if c.contrarian_signal:
            parts.append("contrarian perspective worth noting")

        return f"{prefix}{'; '.join(parts)}."

    @staticmethod
    def _sanitize_for_prompt(value: str, max_len: int = 50) -> str:
        """Sanitize user-controlled values before embedding in LLM prompts.

        Strips characters that could be used for prompt injection:
        newlines, control sequences, and instruction-like prefixes.
        """
        # Remove newlines and control chars
        cleaned = re.sub(r"[\n\r\x00-\x1f]", " ", str(value))
        # Collapse whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Truncate to prevent overlong injections
        return cleaned[:max_len]

    def _review_llm(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Use LLM to rewrite report item fields for style."""
        c = item.candidate
        safe_tone = self._sanitize_for_prompt(profile.tone, 20)
        safe_format = self._sanitize_for_prompt(profile.format, 20)
        safe_topics = ", ".join(
            self._sanitize_for_prompt(k, 30)
            for k, v in profile.topic_weights.items() if v > 0.3
        )
        system_prompt = (
            "You are an editorial style agent for a personal news intelligence system. "
            "Your job is to rewrite three text fields so they match the user's preferred tone "
            "and voice. Maintain all factual content — only reshape the presentation.\n\n"
            f"User's preferred tone: {safe_tone}\n"
            f"User's preferred format: {safe_format}\n"
            f"User's high-priority topics: {safe_topics}\n"
        )
        if self._persona_context:
            system_prompt += f"Review lenses to apply: {'; '.join(self._persona_context)}\n"

        user_message = (
            f"Rewrite these fields for a briefing item about: {c.title} (source: {c.source}, "
            f"topic: {c.topic}, urgency: {c.urgency.value})\n\n"
            f"why_it_matters: {item.why_it_matters}\n"
            f"what_changed: {item.what_changed}\n"
            f"predictive_outlook: {item.predictive_outlook}\n\n"
            "Respond in JSON: {\"why_it_matters\": string, \"what_changed\": string, "
            "\"predictive_outlook\": string}"
        )

        try:
            body = json.dumps({
                "model": self._llm_model,
                "max_tokens": 400,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._llm_base_url}/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            content = result.get("content", [{}])[0].get("text", "{}")
            parsed = _parse_json(content)
            if parsed.get("why_it_matters"):
                item.why_it_matters = parsed["why_it_matters"][:300]
            if parsed.get("what_changed"):
                item.what_changed = parsed["what_changed"][:300]
            if parsed.get("predictive_outlook"):
                item.predictive_outlook = parsed["predictive_outlook"][:300]
            return item

        except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("Style LLM review failed, using heuristic: %s", e)
            return self._review_heuristic(item, profile)


class ClarityReviewAgent:
    """Review agent focused on clarity, structure, concision, and actionable framing.

    Responsibilities (from vision):
    - Ensure every sentence carries information value (no filler)
    - Structure for scannability (lead with the key fact)
    - Frame actionably (what should the reader watch for?)
    - Preserve factual integrity while compressing
    - Enforce consistency across all items in a briefing
    """

    agent_id = "review_agent_clarity"

    def __init__(
        self,
        llm_api_key: str = "",
        llm_model: str = "claude-sonnet-4-5-20250929",
        llm_base_url: str = "https://api.anthropic.com/v1",
        editorial_cfg: dict | None = None,
    ) -> None:
        self._llm_api_key = llm_api_key
        self._llm_model = llm_model
        self._llm_base_url = llm_base_url
        self._use_llm = bool(llm_api_key)

        cfg = editorial_cfg or {}
        self._watchpoints = cfg.get("watchpoints", _DEFAULT_WATCHPOINTS)
        self._filler_patterns = [
            (p, r) for p, r in cfg.get("filler_patterns", _DEFAULT_FILLER_PATTERNS)
        ]
        self._topic_adjacent_reads = cfg.get("topic_adjacent_reads", {})
        self._default_adjacent_reads = cfg.get("default_adjacent_reads", [])

    def review(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Apply clarity review to a report item."""
        if self._use_llm:
            return self._review_llm(item, profile)
        return self._review_heuristic(item, profile)

    def review_batch(self, items: list[ReportItem], profile: UserProfile) -> list[ReportItem]:
        """Review a batch of items, enforcing cross-item consistency."""
        seen_phrases: set[str] = set()
        for item in items:
            item = self.review(item, profile)
            # Deduplicate boilerplate across items
            item.why_it_matters = self._deduplicate(item.why_it_matters, seen_phrases)
            item.what_changed = self._deduplicate(item.what_changed, seen_phrases)
        return items

    def _review_heuristic(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Apply clarity rules: compress, remove filler, add actionable framing."""
        # Apply clarity passes
        item.why_it_matters = self._compress(item.why_it_matters)
        item.what_changed = self._compress(item.what_changed)
        item.predictive_outlook = self._compress(item.predictive_outlook)

        # Add actionable watchpoint if missing
        c = item.candidate
        if not any(w in item.predictive_outlook.lower() for w in ("watch", "monitor", "track", "expect")):
            item.predictive_outlook = self._add_watchpoint(item.predictive_outlook, c)

        # Ensure adjacent reads are actionable, not boilerplate
        if item.adjacent_reads:
            item.adjacent_reads = self._improve_adjacent_reads(item.adjacent_reads, c)

        return item

    def _compress(self, text: str) -> str:
        """Remove filler words and tighten prose."""
        result = text
        for pattern, replacement in self._filler_patterns:
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        # Collapse double spaces
        result = re.sub(r"  +", " ", result).strip()
        return result

    def _add_watchpoint(self, outlook: str, c: CandidateItem) -> str:
        """Add an actionable watchpoint to the outlook."""
        watchpoint = self._watchpoints.get(c.topic, "Monitor for follow-up developments.")
        return f"{outlook} {watchpoint}"

    def _improve_adjacent_reads(self, reads: list[str], c: CandidateItem) -> list[str]:
        """Replace generic adjacent reads with topic-specific suggestions."""
        region = ", ".join(c.regions[:1]) or "this region"

        if self._topic_adjacent_reads and c.topic in self._topic_adjacent_reads:
            templates = self._topic_adjacent_reads[c.topic]
            specific = [t.format(region=region, topic=c.topic) for t in templates]
        elif self._default_adjacent_reads:
            specific = [t.format(region=region, topic=c.topic) for t in self._default_adjacent_reads]
        else:
            # Hardcoded fallback for backward compatibility
            _fallback_reads = {
                "geopolitics": [
                    f"Historical context: prior escalation patterns in {region}",
                    "Stakeholder analysis: key actors and their stated positions",
                    "Timeline: sequence of events leading to current development",
                ],
                "ai_policy": [
                    "Technical assessment: capabilities and limitations at play",
                    "Regulatory landscape: existing and proposed frameworks",
                    "Industry response: major player positions and commitments",
                ],
                "markets": [
                    "Sector impact analysis: direct and indirect exposure",
                    "Historical parallel: similar market events and outcomes",
                    "Policy implications: regulatory and central bank response potential",
                ],
                "technology": [
                    "Technical deep-dive: architecture and implementation details",
                    "Competitive landscape: market positioning and alternatives",
                    "Adoption trajectory: deployment timeline and barriers",
                ],
            }
            specific = _fallback_reads.get(c.topic, [
                f"Background context for {c.topic}",
                f"Expert analysis on {c.topic} implications",
                f"Related developments in {c.topic}",
            ])
        return specific[:len(reads)]

    def _deduplicate(self, text: str, seen: set[str]) -> str:
        """Ensure no repeated boilerplate phrases across items."""
        # Extract key phrases (3+ word sequences)
        words = text.split()
        for i in range(len(words) - 2):
            phrase = " ".join(words[i:i + 3]).lower()
            if phrase in seen and len(phrase) > 15:
                # This phrase appeared in another item — flag for diversity
                pass
            seen.add(phrase)
        return text

    def _review_llm(self, item: ReportItem, profile: UserProfile) -> ReportItem:
        """Use LLM for clarity optimization."""
        c = item.candidate
        system_prompt = (
            "You are an editorial clarity agent. Your job is to make news briefing text "
            "maximally clear, concise, and actionable. Rules:\n"
            "1. Every sentence must carry information value — no filler\n"
            "2. Lead with the most important fact\n"
            "3. End with an actionable watchpoint or next step\n"
            "4. Keep total length short — each field should be 1-2 sentences\n"
            "5. Preserve all factual claims exactly"
        )

        user_message = (
            f"Optimize these fields for clarity and concision:\n\n"
            f"Item: {c.title} (source: {c.source})\n"
            f"why_it_matters: {item.why_it_matters}\n"
            f"what_changed: {item.what_changed}\n"
            f"predictive_outlook: {item.predictive_outlook}\n"
            f"adjacent_reads: {json.dumps(item.adjacent_reads)}\n\n"
            "Respond in JSON: {\"why_it_matters\": string, \"what_changed\": string, "
            "\"predictive_outlook\": string, \"adjacent_reads\": [string, ...]}"
        )

        try:
            body = json.dumps({
                "model": self._llm_model,
                "max_tokens": 500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{self._llm_base_url}/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._llm_api_key,
                    "anthropic-version": "2023-06-01",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            content = result.get("content", [{}])[0].get("text", "{}")
            parsed = _parse_json(content)
            if parsed.get("why_it_matters"):
                item.why_it_matters = parsed["why_it_matters"][:300]
            if parsed.get("what_changed"):
                item.what_changed = parsed["what_changed"][:300]
            if parsed.get("predictive_outlook"):
                item.predictive_outlook = parsed["predictive_outlook"][:300]
            if parsed.get("adjacent_reads"):
                item.adjacent_reads = [str(r)[:200] for r in parsed["adjacent_reads"][:3]]
            return item

        except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError) as e:
            log.warning("Clarity LLM review failed, using heuristic: %s", e)
            return self._review_heuristic(item, profile)


def _parse_json(text: str) -> dict:
    """Extract JSON from LLM response."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*?\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}
