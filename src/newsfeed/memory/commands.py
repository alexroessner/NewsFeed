from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class PreferenceCommand:
    action: str
    topic: str | None = None
    value: str | None = None


_MORE_RE = re.compile(r"\bmore\s+(.+?)(?=\b(?:and\s+less|less|tone|format|region|cadence)\b|[.,;]|$)", re.IGNORECASE)
_LESS_RE = re.compile(r"\bless\s+(.+?)(?=\b(?:and\s+more|more|tone|format|region|cadence)\b|[.,;]|$)", re.IGNORECASE)
_TONE_RE = re.compile(r"\btone\s*[:=]?\s*(concise|analyst|brief|deep|executive)\b", re.IGNORECASE)
_FORMAT_RE = re.compile(r"\bformat\s*[:=]?\s*(bullet|sections|narrative)\b", re.IGNORECASE)
_REGION_RE = re.compile(r"\bregion\s*[:=]?\s*(\w[\w\s]*?)(?=\b(?:tone|format|more|less|cadence)\b|[.,;]|$)", re.IGNORECASE)
_CADENCE_RE = re.compile(r"\bcadence\s*[:=]?\s*(on_demand|morning|evening|realtime)\b", re.IGNORECASE)
_MAX_ITEMS_RE = re.compile(r"\bmax\s*[:=]?\s*(\d+)\b", re.IGNORECASE)
_SOURCE_PREFER_RE = re.compile(r"\b(?:prefer|trust|boost)\s+(\w+?)(?:\s+source)?(?=\b|[.,;]|$)", re.IGNORECASE)
_SOURCE_DEMOTE_RE = re.compile(r"\b(?:demote|distrust|penalize)\s+(\w+?)(?:\s+source)?(?=\b|[.,;]|$)", re.IGNORECASE)
_REMOVE_REGION_RE = re.compile(r"\b(?:remove|drop)\s+region\s*[:=]?\s*(\w[\w\s]*?)(?=\b|[.,;]|$)", re.IGNORECASE)
_RESET_RE = re.compile(r"\breset\s+(?:all\s+)?preferences?\b", re.IGNORECASE)


def _clean_topic(raw: str) -> str:
    cleaned = "_".join(raw.strip().lower().split())
    return cleaned.strip("_")


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
        commands.append(PreferenceCommand(action="source_boost", topic=src, value="+1.0"))

    for m in _SOURCE_DEMOTE_RE.finditer(text):
        src = m.group(1).lower()
        commands.append(PreferenceCommand(action="source_demote", topic=src, value="-1.0"))

    rm_region = _REMOVE_REGION_RE.search(text)
    if rm_region:
        commands.append(PreferenceCommand(action="remove_region", value=_clean_topic(rm_region.group(1))))

    if _RESET_RE.search(text):
        commands.append(PreferenceCommand(action="reset"))

    return commands
