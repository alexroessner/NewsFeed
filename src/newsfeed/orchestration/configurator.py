"""Universal plain-text system configurator.

Allows users to modify ANY system parameter through natural language commands.
Covers: scoring weights, expert behavior, pipeline stages, agent management,
persona switching, source priorities, delivery preferences, and more.

Every change is validated, bounded, and logged for auditability.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ConfigChange:
    """A single validated configuration change."""
    path: str           # Dot-separated config path, e.g. "scoring.composite_weights.evidence"
    old_value: Any
    new_value: Any
    source: str         # "user_command", "optimizer", "system"
    description: str


# ──────────────────────────────────────────────────────────────────────
# Pattern matchers for plain-text configuration commands
# ──────────────────────────────────────────────────────────────────────

_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # Scoring weights
    (re.compile(r"\b(?:set|change|adjust)\s+evidence\s+weight\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "scoring.composite_weights.evidence", "float"),
    (re.compile(r"\b(?:set|change|adjust)\s+novelty\s+weight\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "scoring.composite_weights.novelty", "float"),
    (re.compile(r"\b(?:set|change|adjust)\s+preference\s+(?:fit\s+)?weight\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "scoring.composite_weights.preference_fit", "float"),
    (re.compile(r"\b(?:set|change|adjust)\s+prediction\s+(?:signal\s+)?weight\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "scoring.composite_weights.prediction_signal", "float"),
    (re.compile(r"\bweight\s+(?:evidence|novelty|preference|prediction)\s+(?:more|higher)", re.I),
     "_weight_increase", "hint"),
    (re.compile(r"\bweight\s+(?:evidence|novelty|preference|prediction)\s+(?:less|lower)", re.I),
     "_weight_decrease", "hint"),

    # Expert council
    (re.compile(r"\b(?:set|change)\s+keep\s+threshold\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "expert_council.keep_threshold", "float"),
    (re.compile(r"\b(?:make|set)\s+experts?\s+(?:more\s+)?strict(?:er)?", re.I),
     "expert_council.keep_threshold", "strict"),
    (re.compile(r"\b(?:make|set)\s+experts?\s+(?:more\s+)?(?:lenient|relaxed|loose)", re.I),
     "expert_council.keep_threshold", "lenient"),
    (re.compile(r"\b(?:set\s+)?voting\s+(?:to\s+)?unanimous", re.I),
     "expert_council.min_votes_to_accept", "literal:unanimous"),
    (re.compile(r"\b(?:set\s+)?voting\s+(?:to\s+)?majority", re.I),
     "expert_council.min_votes_to_accept", "literal:majority"),

    # Pipeline stages
    (re.compile(r"\bdisable\s+(credibility|corroboration|urgency|diversity|clustering|georisk|trends)", re.I),
     "intelligence.disable_stage", "stage"),
    (re.compile(r"\benable\s+(credibility|corroboration|urgency|diversity|clustering|georisk|trends)", re.I),
     "intelligence.enable_stage", "stage"),

    # Agent management
    (re.compile(r"\bdisable\s+(?:agent\s+)?(\w+_agent\w*)", re.I),
     "agents.disable", "agent_id"),
    (re.compile(r"\benable\s+(?:agent\s+)?(\w+_agent\w*)", re.I),
     "agents.enable", "agent_id"),

    # Source priorities
    (re.compile(r"\bprioritize\s+(\w+)\s+over\s+(\w+)", re.I),
     "source_priority", "pair"),
    (re.compile(r"\b(?:trust|prefer)\s+(\w+)\s+(?:source|more)", re.I),
     "source_boost", "source"),
    (re.compile(r"\b(?:distrust|demote)\s+(\w+)\s+(?:source|less)", re.I),
     "source_demote", "source"),

    # Persona management
    (re.compile(r"\b(?:add|enable)\s+(?:persona\s+)?(?:the\s+)?(engineer|source_critic|audience|forecaster)", re.I),
     "personas.add", "persona"),
    (re.compile(r"\b(?:remove|disable)\s+(?:persona\s+)?(?:the\s+)?(engineer|source_critic|audience|forecaster)", re.I),
     "personas.remove", "persona"),
    (re.compile(r"\b(?:switch|set)\s+(?:persona|lens)\s+(?:to\s+)?(engineer|source_critic|audience|forecaster)", re.I),
     "personas.set_primary", "persona"),

    # Max items
    (re.compile(r"\b(?:show|display|limit)\s+(?:me\s+)?(\d+)\s+(?:items?|stories)", re.I),
     "limits.default_max_items", "int"),
    (re.compile(r"\bmax\s+(?:items?\s+)?(?:per\s+source\s+)?(?:to\s+)?(\d+)", re.I),
     "intelligence.max_items_per_source", "int"),

    # Delivery preferences (extending existing)
    (re.compile(r"\btone\s*[:=]?\s*(concise|analyst|brief|deep|executive)", re.I),
     "user.tone", "str"),
    (re.compile(r"\bformat\s*[:=]?\s*(bullet|sections|narrative)", re.I),
     "user.format", "str"),
    (re.compile(r"\bcadence\s*[:=]?\s*(on_demand|morning|evening|realtime)", re.I),
     "user.cadence", "str"),
    (re.compile(r"\bregion\s*[:=]?\s*(\w[\w\s]*?)(?=[.,;]|$)", re.I),
     "user.region", "str"),

    # Clustering / similarity
    (re.compile(r"\b(?:set\s+)?clustering\s+(?:similarity\s+)?(?:to\s+)?(\d*\.?\d+)", re.I),
     "intelligence.clustering_similarity", "float"),
    (re.compile(r"\b(?:set\s+)?anomaly\s+threshold\s+(?:to\s+)?(\d*\.?\d+)", re.I),
     "intelligence.anomaly_threshold", "float"),
]

# Bounds for safe parameter ranges
_BOUNDS: dict[str, tuple[float, float]] = {
    "scoring.composite_weights.evidence": (0.0, 1.0),
    "scoring.composite_weights.novelty": (0.0, 1.0),
    "scoring.composite_weights.preference_fit": (0.0, 1.0),
    "scoring.composite_weights.prediction_signal": (0.0, 1.0),
    "expert_council.keep_threshold": (0.3, 0.95),
    "intelligence.clustering_similarity": (0.1, 0.99),
    "intelligence.anomaly_threshold": (0.5, 10.0),
    "intelligence.max_items_per_source": (1, 20),
    "limits.default_max_items": (1, 50),
}


class SystemConfigurator:
    """Parses plain-text commands and applies validated configuration changes.

    Supports modifying any system parameter through natural language:
    - "set evidence weight to 0.4"
    - "make experts stricter"
    - "disable clustering"
    - "prioritize reuters over reddit"
    - "switch persona to forecaster"
    - "show me 15 items"
    """

    def __init__(self, pipeline_cfg: dict, agents_cfg: dict, personas_cfg: dict) -> None:
        self._pipeline = pipeline_cfg
        self._agents = agents_cfg
        self._personas = personas_cfg
        self._change_history: list[ConfigChange] = []

    _MAX_TEXT_LEN = 5000  # Defense-in-depth; Telegram caps at 4096 chars

    def parse_and_apply(self, text: str, source: str = "user_command") -> list[ConfigChange]:
        """Parse natural language text and apply all recognized configuration changes."""
        text = text[:self._MAX_TEXT_LEN]
        changes: list[ConfigChange] = []
        text_lower = text.lower()

        for pattern, config_path, value_type in _PATTERNS:
            match = pattern.search(text)
            if not match:
                continue

            change = self._resolve_change(match, config_path, value_type, source)
            if change:
                self._apply_change(change)
                changes.append(change)
                self._change_history.append(change)

        if changes:
            log.info("Configurator applied %d changes from: %r", len(changes), text[:80])

        return changes

    def _resolve_change(self, match: re.Match, config_path: str,
                        value_type: str, source: str) -> ConfigChange | None:
        """Resolve a regex match into a validated ConfigChange."""
        try:
            if value_type == "float":
                raw = float(match.group(1))
                bounds = _BOUNDS.get(config_path, (0.0, 1.0))
                new_value = max(bounds[0], min(bounds[1], raw))
                old_value = self._get_nested(self._pipeline, config_path)
                return ConfigChange(
                    path=config_path, old_value=old_value, new_value=new_value,
                    source=source, description=f"Set {config_path} = {new_value}",
                )

            if value_type == "int":
                raw = int(match.group(1))
                bounds = _BOUNDS.get(config_path, (1, 100))
                new_value = max(int(bounds[0]), min(int(bounds[1]), raw))
                old_value = self._get_nested(self._pipeline, config_path)
                return ConfigChange(
                    path=config_path, old_value=old_value, new_value=new_value,
                    source=source, description=f"Set {config_path} = {new_value}",
                )

            if value_type == "strict":
                old = self._get_nested(self._pipeline, config_path) or 0.62
                new_value = min(0.95, old + 0.08)
                return ConfigChange(
                    path=config_path, old_value=old, new_value=round(new_value, 3),
                    source=source, description=f"Increased expert strictness: {old:.2f} → {new_value:.2f}",
                )

            if value_type == "lenient":
                old = self._get_nested(self._pipeline, config_path) or 0.62
                new_value = max(0.3, old - 0.08)
                return ConfigChange(
                    path=config_path, old_value=old, new_value=round(new_value, 3),
                    source=source, description=f"Decreased expert strictness: {old:.2f} → {new_value:.2f}",
                )

            if value_type.startswith("literal:"):
                literal = value_type.split(":", 1)[1]
                old_value = self._get_nested(self._pipeline, config_path)
                return ConfigChange(
                    path=config_path, old_value=old_value, new_value=literal,
                    source=source, description=f"Set {config_path} = {literal}",
                )

            if value_type == "stage":
                stage_name = match.group(1).lower()
                is_enable = "enable" in config_path
                enabled = self._pipeline.get("intelligence", {}).get("enabled_stages", [])
                old_enabled = list(enabled)
                if is_enable and stage_name not in enabled:
                    enabled.append(stage_name)
                elif not is_enable and stage_name in enabled:
                    enabled.remove(stage_name)
                action = "enabled" if is_enable else "disabled"
                return ConfigChange(
                    path="intelligence.enabled_stages", old_value=old_enabled,
                    new_value=list(enabled), source=source,
                    description=f"Pipeline stage '{stage_name}' {action}",
                )

            if value_type == "agent_id":
                agent_id = match.group(1)
                is_enable = "enable" in config_path
                action = "enabled" if is_enable else "disabled"
                return ConfigChange(
                    path=f"agents.{agent_id}.enabled", old_value=not is_enable,
                    new_value=is_enable, source=source,
                    description=f"Agent '{agent_id}' {action}",
                )

            if value_type == "pair":
                preferred = match.group(1).lower()
                demoted = match.group(2).lower()
                return ConfigChange(
                    path="source_priority_override",
                    old_value=None, new_value={"prefer": preferred, "demote": demoted},
                    source=source,
                    description=f"Source priority: {preferred} > {demoted}",
                )

            if value_type == "source":
                src = match.group(1).lower()
                is_boost = "boost" in config_path
                action = "boosted" if is_boost else "demoted"
                return ConfigChange(
                    path=f"source_weight.{src}",
                    old_value=1.0, new_value=1.2 if is_boost else 0.7,
                    source=source,
                    description=f"Source '{src}' {action}",
                )

            if value_type == "persona":
                persona_id = match.group(1).lower()
                current = list(self._personas.get("default_personas", []))
                if "add" in config_path or "set_primary" in config_path:
                    if persona_id not in current:
                        current.insert(0, persona_id)
                    else:
                        current.remove(persona_id)
                        current.insert(0, persona_id)
                    action = "activated"
                elif "remove" in config_path:
                    if persona_id in current:
                        current.remove(persona_id)
                    action = "deactivated"
                else:
                    return None
                old = list(self._personas.get("default_personas", []))
                self._personas["default_personas"] = current
                return ConfigChange(
                    path="personas.default_personas", old_value=old, new_value=current,
                    source=source, description=f"Persona '{persona_id}' {action}",
                )

            if value_type == "str":
                val = match.group(1).strip().lower()
                return ConfigChange(
                    path=config_path, old_value=None, new_value=val,
                    source=source, description=f"Set {config_path} = {val}",
                )

        except (ValueError, IndexError, KeyError) as e:
            log.warning("Failed to resolve config change for %s: %s", config_path, e)

        return None

    def _apply_change(self, change: ConfigChange) -> None:
        """Apply a validated change to the live config."""
        parts = change.path.split(".")

        # Route to correct config dict
        if parts[0] in ("scoring", "expert_council", "intelligence", "limits",
                         "georisk", "cache_policy", "preference_deltas"):
            self._set_nested(self._pipeline, change.path, change.new_value)
        elif parts[0] == "personas":
            pass  # Already applied in _resolve_change
        elif parts[0] in ("agents", "source_weight", "source_priority_override"):
            pass  # These need engine-level application
        elif parts[0] == "user":
            pass  # Routed to preference store by caller

    def _get_nested(self, d: dict, path: str) -> Any:
        """Get a value from a nested dict using dot-separated path."""
        parts = path.split(".")
        current = d
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current

    def _set_nested(self, d: dict, path: str, value: Any) -> None:
        """Set a value in a nested dict using dot-separated path."""
        parts = path.split(".")
        current = d
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    def history(self) -> list[dict]:
        """Return change history for audit."""
        return [
            {"path": c.path, "old": c.old_value, "new": c.new_value,
             "source": c.source, "description": c.description}
            for c in self._change_history
        ]

    def snapshot(self) -> dict:
        """Return current effective configuration state."""
        return {
            "scoring": self._pipeline.get("scoring", {}),
            "expert_council": self._pipeline.get("expert_council", {}),
            "intelligence_stages": self._pipeline.get("intelligence", {}).get("enabled_stages", []),
            "limits": self._pipeline.get("limits", {}),
            "personas": self._personas.get("default_personas", []),
            "changes_applied": len(self._change_history),
        }
