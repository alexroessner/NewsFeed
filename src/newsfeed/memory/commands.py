from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches


@dataclass(slots=True)
class PreferenceCommand:
    action: str
    topic: str | None = None
    value: str | None = None


@dataclass(slots=True)
class ParseResult:
    """Result of preference parsing with diagnostics for user feedback."""
    commands: list[PreferenceCommand] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)  # "Did you mean X?"
    unrecognized: list[str] = field(default_factory=list)  # Unrecognized parts


# Valid tone/format/cadence values for fuzzy matching
_VALID_TONES = ("concise", "analyst", "brief", "deep", "executive")
_VALID_FORMATS = ("bullet", "sections", "narrative")
_VALID_CADENCES = ("on_demand", "morning", "evening", "realtime")


_MORE_RE = re.compile(r"\bmore\s+(.+?)(?=\b(?:and\s+less|less|tone|format|region|cadence)\b|[.,;]|$)", re.IGNORECASE)
_LESS_RE = re.compile(r"\bless\s+(.+?)(?=\b(?:and\s+more|more|tone|format|region|cadence)\b|[.,;]|$)", re.IGNORECASE)
_TONE_RE = re.compile(r"\btone\s*[:=]?\s*(concise|analyst|brief|deep|executive)\b", re.IGNORECASE)
_FORMAT_RE = re.compile(r"\bformat\s*[:=]?\s*(bullet|sections|narrative)\b", re.IGNORECASE)
_REGION_RE = re.compile(r"\bregion\s*[:=]?\s*(\w[\w\s]*?)(?=\b(?:tone|format|more|less|cadence)\b|[.,;]|$)", re.IGNORECASE)
_CADENCE_RE = re.compile(r"\bcadence\s*[:=]?\s*(on_demand|morning|evening|realtime)\b", re.IGNORECASE)
_MAX_ITEMS_RE = re.compile(r"\bmax\s*[:=]?\s*(\d+)\b", re.IGNORECASE)
_SOURCE_PREFER_RE = re.compile(r"\b(?:prefer|trust|boost)\s+(\w{2,}?)(?:\s+source)?(?=\b|[.,;]|$)", re.IGNORECASE)
_SOURCE_DEMOTE_RE = re.compile(r"\b(?:demote|distrust|penalize)\s+(\w{2,}?)(?:\s+source)?(?=\b|[.,;]|$)", re.IGNORECASE)
# Common English words that should NOT be treated as source names
_SOURCE_NOISE = {"your", "my", "the", "this", "that", "it", "its", "our", "all",
                 "any", "more", "less", "a", "an", "in", "on", "is", "performance",
                 "judgment", "judgement"}
_REMOVE_REGION_RE = re.compile(r"\b(?:remove|drop)\s+region\s*[:=]?\s*(\w[\w\s]*?)(?=\b|[.,;]|$)", re.IGNORECASE)
_RESET_RE = re.compile(r"\breset\s+(?:all\s+)?preferences?\b", re.IGNORECASE)


def _clean_topic(raw: str) -> str:
    cleaned = "_".join(raw.strip().lower().split())
    return cleaned.strip("_")


def fuzzy_correct_topic(topic: str, known_topics: set[str],
                        cutoff: float = 0.6) -> tuple[str, str | None]:
    """Fuzzy-match a topic against known topics.

    Returns (corrected_topic, suggestion_message).
    If topic is already valid, returns (topic, None).
    If a close match is found, returns (match, "Did you mean X?").
    If no match found, returns (topic, None) — allow new topics.
    """
    if topic in known_topics:
        return topic, None
    matches = get_close_matches(topic, known_topics, n=1, cutoff=cutoff)
    if matches:
        return matches[0], f'Did you mean "{matches[0].replace("_", " ")}"? Applied as "{matches[0].replace("_", " ")}".'
    return topic, None


def _fuzzy_match_value(raw: str, valid: tuple[str, ...],
                       cutoff: float = 0.6) -> str | None:
    """Fuzzy-match a value against valid options."""
    val = raw.strip().lower()
    if val in valid:
        return val
    matches = get_close_matches(val, valid, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def parse_preference_commands(text: str, deltas: dict[str, float] | None = None) -> list[PreferenceCommand]:
    d = deltas or {}
    more_delta = str(d.get("more", 0.2))
    less_delta = str(d.get("less", -0.2))

    commands: list[PreferenceCommand] = []

    for m in _MORE_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            commands.append(PreferenceCommand(action="topic_delta", topic=topic, value=f"+{more_delta}"))

    for m in _LESS_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            commands.append(PreferenceCommand(action="topic_delta", topic=topic, value=str(less_delta)))

    tone = _TONE_RE.search(text)
    if tone:
        commands.append(PreferenceCommand(action="tone", value=tone.group(1).lower()))

    fmt = _FORMAT_RE.search(text)
    if fmt:
        commands.append(PreferenceCommand(action="format", value=fmt.group(1).lower()))

    # Check remove/drop region FIRST so we can skip _REGION_RE if it matched
    rm_region = _REMOVE_REGION_RE.search(text)
    if rm_region:
        commands.append(PreferenceCommand(action="remove_region", value=_clean_topic(rm_region.group(1))))
    else:
        region = _REGION_RE.search(text)
        if region:
            commands.append(PreferenceCommand(action="region", value=_clean_topic(region.group(1))))

    cadence = _CADENCE_RE.search(text)
    if cadence:
        commands.append(PreferenceCommand(action="cadence", value=cadence.group(1).lower()))

    max_items = _MAX_ITEMS_RE.search(text)
    if max_items:
        commands.append(PreferenceCommand(action="max_items", value=max_items.group(1)))

    for m in _SOURCE_PREFER_RE.finditer(text):
        src = m.group(1).lower()
        if src not in _SOURCE_NOISE:
            commands.append(PreferenceCommand(action="source_boost", topic=src, value="+1.0"))

    for m in _SOURCE_DEMOTE_RE.finditer(text):
        src = m.group(1).lower()
        if src not in _SOURCE_NOISE:
            commands.append(PreferenceCommand(action="source_demote", topic=src, value="-1.0"))

    if _RESET_RE.search(text):
        commands.append(PreferenceCommand(action="reset"))

    return commands


def parse_preference_commands_rich(
    text: str,
    known_topics: set[str] | None = None,
    deltas: dict[str, float] | None = None,
) -> ParseResult:
    """Parse preference commands with fuzzy matching and diagnostics.

    Like parse_preference_commands but returns corrections and hints
    when the user makes typos or uses invalid values.
    """
    d = deltas or {}
    more_delta = str(d.get("more", 0.2))
    less_delta = str(d.get("less", -0.2))
    topics = known_topics or set()

    result = ParseResult()

    for m in _MORE_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            corrected, hint = fuzzy_correct_topic(topic, topics)
            if hint:
                result.corrections.append(hint)
            result.commands.append(PreferenceCommand(
                action="topic_delta", topic=corrected, value=f"+{more_delta}"))

    for m in _LESS_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            corrected, hint = fuzzy_correct_topic(topic, topics)
            if hint:
                result.corrections.append(hint)
            result.commands.append(PreferenceCommand(
                action="topic_delta", topic=corrected, value=str(less_delta)))

    # Tone with fuzzy matching
    tone_match = _TONE_RE.search(text)
    if tone_match:
        result.commands.append(PreferenceCommand(action="tone", value=tone_match.group(1).lower()))
    else:
        # Check for "tone:" prefix with invalid value
        raw_tone = re.search(r"\btone\s*[:=]?\s*(\w+)\b", text, re.IGNORECASE)
        if raw_tone:
            fuzzy = _fuzzy_match_value(raw_tone.group(1), _VALID_TONES)
            if fuzzy:
                result.commands.append(PreferenceCommand(action="tone", value=fuzzy))
                result.corrections.append(
                    f'Tone "{raw_tone.group(1)}" corrected to "{fuzzy}".')
            else:
                valid = ", ".join(_VALID_TONES)
                result.unrecognized.append(
                    f'Unknown tone "{raw_tone.group(1)}". Valid: {valid}')

    # Format with fuzzy matching
    fmt_match = _FORMAT_RE.search(text)
    if fmt_match:
        result.commands.append(PreferenceCommand(action="format", value=fmt_match.group(1).lower()))
    else:
        raw_fmt = re.search(r"\bformat\s*[:=]?\s*(\w+)\b", text, re.IGNORECASE)
        if raw_fmt:
            fuzzy = _fuzzy_match_value(raw_fmt.group(1), _VALID_FORMATS)
            if fuzzy:
                result.commands.append(PreferenceCommand(action="format", value=fuzzy))
                result.corrections.append(
                    f'Format "{raw_fmt.group(1)}" corrected to "{fuzzy}".')
            else:
                valid = ", ".join(_VALID_FORMATS)
                result.unrecognized.append(
                    f'Unknown format "{raw_fmt.group(1)}". Valid: {valid}')

    # Cadence with fuzzy matching
    cadence_match = _CADENCE_RE.search(text)
    if cadence_match:
        result.commands.append(PreferenceCommand(action="cadence", value=cadence_match.group(1).lower()))
    else:
        raw_cad = re.search(r"\bcadence\s*[:=]?\s*(\w+)\b", text, re.IGNORECASE)
        if raw_cad:
            fuzzy = _fuzzy_match_value(raw_cad.group(1), _VALID_CADENCES)
            if fuzzy:
                result.commands.append(PreferenceCommand(action="cadence", value=fuzzy))
                result.corrections.append(
                    f'Cadence "{raw_cad.group(1)}" corrected to "{fuzzy}".')
            else:
                valid = ", ".join(_VALID_CADENCES)
                result.unrecognized.append(
                    f'Unknown cadence "{raw_cad.group(1)}". Valid: {valid}')

    # Standard regex-based parsing for the rest
    rm_region = _REMOVE_REGION_RE.search(text)
    if rm_region:
        result.commands.append(PreferenceCommand(action="remove_region", value=_clean_topic(rm_region.group(1))))
    else:
        region = _REGION_RE.search(text)
        if region:
            result.commands.append(PreferenceCommand(action="region", value=_clean_topic(region.group(1))))

    max_items = _MAX_ITEMS_RE.search(text)
    if max_items:
        result.commands.append(PreferenceCommand(action="max_items", value=max_items.group(1)))

    for m in _SOURCE_PREFER_RE.finditer(text):
        src = m.group(1).lower()
        if src not in _SOURCE_NOISE:
            result.commands.append(PreferenceCommand(action="source_boost", topic=src, value="+1.0"))

    for m in _SOURCE_DEMOTE_RE.finditer(text):
        src = m.group(1).lower()
        if src not in _SOURCE_NOISE:
            result.commands.append(PreferenceCommand(action="source_demote", topic=src, value="-1.0"))

    if _RESET_RE.search(text):
        result.commands.append(PreferenceCommand(action="reset"))

    return result
