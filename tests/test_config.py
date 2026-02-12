from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsfeed.models.config import ConfigError, RuntimeConfig, load_json, load_runtime_config


class ConfigErrorTests(unittest.TestCase):
    def test_missing_config_dir_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_runtime_config(Path("/nonexistent/dir"))
        self.assertIn("not found", str(ctx.exception))

    def test_missing_config_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ConfigError) as ctx:
                load_json(Path(tmpdir) / "missing.json")
            self.assertIn("not found", str(ctx.exception))

    def test_invalid_json_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.json"
            bad_file.write_text("{invalid json", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_json(bad_file)
            self.assertIn("Invalid JSON", str(ctx.exception))

    def test_validation_missing_sections(self) -> None:
        cfg = RuntimeConfig(
            agents={},
            pipeline={},
            personas={},
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        error_msg = str(ctx.exception)
        self.assertIn("Missing agent section", error_msg)
        self.assertIn("Missing pipeline stages", error_msg)
        self.assertIn("Missing default personas", error_msg)

    def test_validation_missing_research_agent_fields(self) -> None:
        cfg = RuntimeConfig(
            agents={
                "control_agents": [], "research_agents": [{"id": "test"}],
                "expert_agents": [], "review_agents": [],
            },
            pipeline={"stages": []},
            personas={"default_personas": []},
        )
        with self.assertRaises(ConfigError) as ctx:
            cfg.validate()
        self.assertIn("source", str(ctx.exception))

    def test_valid_config_passes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cfg = load_runtime_config(root / "config")
        self.assertIsInstance(cfg.pipeline.get("version"), int)


if __name__ == "__main__":
    unittest.main()
