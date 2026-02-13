from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class PersonaReviewStack:
    def __init__(self, personas_dir: Path, active_personas: list[str], persona_notes: dict[str, str]) -> None:
        self.personas_dir = personas_dir
        self.active_personas = active_personas
        self.persona_notes = persona_notes
        self._persona_cache: dict[str, str] = {}

        # Validate all personas at startup
        for persona_id in active_personas:
            path = personas_dir / f"{persona_id}.md"
            if not path.exists():
                log.warning("Persona file missing at startup: %s", path)

    def load_persona_text(self, persona_id: str) -> str:
        if persona_id in self._persona_cache:
            return self._persona_cache[persona_id]
        path = self.personas_dir / f"{persona_id}.md"
        if not path.exists():
            log.warning("Missing persona file: %s — skipping", path)
            self._persona_cache[persona_id] = ""
            return ""
        text = path.read_text(encoding="utf-8").strip()
        self._persona_cache[persona_id] = text
        return text

    def active_context(self) -> list[str]:
        context = []
        for persona_id in self.active_personas:
            text = self.load_persona_text(persona_id)
            if text:
                context.append(self.persona_notes.get(persona_id, persona_id))
        return context

    def refine_why(self, base: str) -> str:
        # Persona notes are internal guidance for LLM review agents —
        # they must NOT be appended to user-visible text.
        return base

    def refine_outlook(self, base: str) -> str:
        # Same: confidence band instructions are internal, not output text.
        return base
