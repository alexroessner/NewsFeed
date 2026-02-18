"""Safe XML parsing — defends against entity expansion (billion laughs) attacks.

Python's ``xml.etree.ElementTree`` does not process external entities by
default, but it IS vulnerable to "billion laughs" — malicious XML with
deeply nested entity definitions that expand to gigabytes of memory.

This module strips ``<!DOCTYPE ...>`` and ``<!ENTITY ...>`` declarations
before parsing, which removes the attack surface entirely without adding
any external dependency (stdlib only).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

# Re-export ParseError so callers can replace ``import ET`` entirely.
ParseError = ET.ParseError

# Match DOCTYPE or ENTITY declarations (single-line or multi-line)
_DOCTYPE_RE_S = re.compile(r"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.DOTALL)
_ENTITY_RE_S = re.compile(r"<!ENTITY[^>]*>")
_DOCTYPE_RE_B = re.compile(rb"<!DOCTYPE[^>\[]*(\[.*?\])?\s*>", re.DOTALL)
_ENTITY_RE_B = re.compile(rb"<!ENTITY[^>]*>")


def safe_fromstring(text: str | bytes) -> ET.Element:
    """Parse an XML string with entity expansion protection.

    Accepts both ``str`` and ``bytes`` (as returned by ``urlopen().read()``).
    Strips DOCTYPE and ENTITY declarations before parsing, preventing
    billion-laughs and entity-expansion attacks from malicious RSS/Atom feeds.
    """
    if isinstance(text, bytes):
        text = _DOCTYPE_RE_B.sub(b"", text)
        text = _ENTITY_RE_B.sub(b"", text)
    else:
        text = _DOCTYPE_RE_S.sub("", text)
        text = _ENTITY_RE_S.sub("", text)
    return ET.fromstring(text)
