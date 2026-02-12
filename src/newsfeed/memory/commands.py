from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class PreferenceCommand:
    action: str
    topic: str | None = None
    value: str | None = None


_MORE_RE = re.compile(r"\bmore\s+(.+?)(?=\b(?:and\s+less|less|tone|format)\b|[.,;]|$)", re.IGNORECASE)
_LESS_RE = re.compile(r"\bless\s+(.+?)(?=\b(?:and\s+more|more|tone|format)\b|[.,;]|$)", re.IGNORECASE)
_TONE_RE = re.compile(r"\btone\s*[:=]?\s*(concise|analyst|brief|deep|executive)\b", re.IGNORECASE)
_FORMAT_RE = re.compile(r"\bformat\s*[:=]?\s*(bullet|sections|narrative)\b", re.IGNORECASE)


def _clean_topic(raw: str) -> str:
    cleaned = "_".join(raw.strip().lower().split())
    return cleaned.strip("_")


def parse_preference_commands(text: str) -> list[PreferenceCommand]:
    commands: list[PreferenceCommand] = []

    for m in _MORE_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            commands.append(PreferenceCommand(action="topic_delta", topic=topic, value="+0.2"))

    for m in _LESS_RE.finditer(text):
        topic = _clean_topic(m.group(1))
        if topic:
            commands.append(PreferenceCommand(action="topic_delta", topic=topic, value="-0.2"))

    tone = _TONE_RE.search(text)
    if tone:
        commands.append(PreferenceCommand(action="tone", value=tone.group(1).lower()))

    fmt = _FORMAT_RE.search(text)
    if fmt:
        commands.append(PreferenceCommand(action="format", value=fmt.group(1).lower()))

    return commands
