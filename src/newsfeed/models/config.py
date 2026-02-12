from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    pass


class RuntimeConfig:
    def __init__(self, agents: dict[str, Any], pipeline: dict[str, Any], personas: dict[str, Any]) -> None:
        self.agents = agents
        self.pipeline = pipeline
        self.personas = personas

    def validate(self) -> list[str]:
        errors: list[str] = []

        required_agent_sections = ["control_agents", "research_agents", "expert_agents", "review_agents"]
        for section in required_agent_sections:
            if section not in self.agents:
                errors.append(f"Missing agent section: {section}")

        for agent in self.agents.get("research_agents", []):
            for key in ("id", "source", "mandate"):
                if key not in agent:
                    errors.append(f"Research agent missing required field '{key}': {agent}")

        if "stages" not in self.pipeline:
            errors.append("Missing pipeline stages")

        if "default_personas" not in self.personas:
            errors.append("Missing default personas configuration")

        if errors:
            raise ConfigError("Configuration validation failed:\n  " + "\n  ".join(errors))

        return []


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {path}: {e}") from e


def load_runtime_config(config_dir: Path) -> RuntimeConfig:
    if not config_dir.is_dir():
        raise ConfigError(f"Config directory not found: {config_dir}")

    agents = load_json(config_dir / "agents.json")
    pipeline = load_json(config_dir / "pipelines.json")
    personas = load_json(config_dir / "review_personas.json")
    cfg = RuntimeConfig(agents=agents, pipeline=pipeline, personas=personas)
    cfg.validate()
    return cfg
