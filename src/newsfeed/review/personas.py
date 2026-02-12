from __future__ import annotations

from pathlib import Path


class PersonaReviewStack:
    def __init__(self, personas_dir: Path, active_personas: list[str], persona_notes: dict[str, str]) -> None:
        self.personas_dir = personas_dir
        self.active_personas = active_personas
        self.persona_notes = persona_notes
        self._persona_cache: dict[str, str] = {}

    def load_persona_text(self, persona_id: str) -> str:
        if persona_id in self._persona_cache:
            return self._persona_cache[persona_id]
        path = self.personas_dir / f"{persona_id}.md"
        if not path.exists():
            raise FileNotFoundError(f"Missing persona file: {path}")
        text = path.read_text(encoding="utf-8").strip()
        self._persona_cache[persona_id] = text
        return text

    def active_context(self) -> list[str]:
        context = []
        for persona_id in self.active_personas:
            _ = self.load_persona_text(persona_id)
            context.append(self.persona_notes.get(persona_id, persona_id))
        return context

    def refine_why(self, base: str) -> str:
        notes = self.active_context()
        if not notes:
            return base
        return f"{base} [Review lenses: {'; '.join(notes)}]"

    def refine_outlook(self, base: str) -> str:
        if "forecaster" in self.active_personas:
            return f"{base} Include confidence bands and key assumption tracking."
        return base
