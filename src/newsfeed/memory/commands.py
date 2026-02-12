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

    return commands
