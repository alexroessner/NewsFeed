from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class RuntimeConfig:
    def __init__(self, agents: dict[str, Any], pipeline: dict[str, Any], personas: dict[str, Any]) -> None:
        self.agents = agents
        self.pipeline = pipeline
        self.personas = personas
    def __init__(self, agents: dict[str, Any], pipeline: dict[str, Any]) -> None:
        self.agents = agents
        self.pipeline = pipeline

    def validate(self) -> None:
        required_agent_sections = ["control_agents", "research_agents", "expert_agents", "review_agents"]
        for section in required_agent_sections:
            if section not in self.agents:
                raise ValueError(f"Missing agent section: {section}")

        if "stages" not in self.pipeline:
            raise ValueError("Missing pipeline stages")

        if "default_personas" not in self.personas:
            raise ValueError("Missing default personas configuration")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_runtime_config(config_dir: Path) -> RuntimeConfig:
    agents = load_json(config_dir / "agents.json")
    pipeline = load_json(config_dir / "pipelines.json")
    personas = load_json(config_dir / "review_personas.json")
    cfg = RuntimeConfig(agents=agents, pipeline=pipeline, personas=personas)
    cfg = RuntimeConfig(agents=agents, pipeline=pipeline)
    cfg.validate()
    return cfg
