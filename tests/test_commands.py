from __future__ import annotations

import unittest

from newsfeed.memory.commands import parse_preference_commands


class CommandParserTests(unittest.TestCase):
    def test_parse_mixed_commands(self) -> None:
        text = "more geopolitics and less celebrity news; tone: analyst format sections"
        commands = parse_preference_commands(text)

        topic_updates = {c.topic: c.value for c in commands if c.action == "topic_delta" and c.topic}
        self.assertEqual(topic_updates.get("geopolitics"), "+0.2")
        self.assertEqual(topic_updates.get("celebrity_news"), "-0.2")

        tone = [c.value for c in commands if c.action == "tone"]
        fmt = [c.value for c in commands if c.action == "format"]
        self.assertEqual(tone, ["analyst"])
        self.assertEqual(fmt, ["sections"])

    def test_region_command(self) -> None:
        text = "region: middle_east"
        commands = parse_preference_commands(text)
        regions = [c for c in commands if c.action == "region"]
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].value, "middle_east")

    def test_cadence_command(self) -> None:
        text = "cadence: morning"
        commands = parse_preference_commands(text)
        cadences = [c for c in commands if c.action == "cadence"]
        self.assertEqual(len(cadences), 1)
        self.assertEqual(cadences[0].value, "morning")

    def test_max_items_command(self) -> None:
        text = "max: 15"
        commands = parse_preference_commands(text)
        max_cmds = [c for c in commands if c.action == "max_items"]
        self.assertEqual(len(max_cmds), 1)
        self.assertEqual(max_cmds[0].value, "15")

    def test_custom_deltas(self) -> None:
        text = "more crypto less politics"
        commands = parse_preference_commands(text, deltas={"more": 0.35, "less": -0.15})
        topic_updates = {c.topic: c.value for c in commands if c.action == "topic_delta" and c.topic}
        self.assertEqual(topic_updates.get("crypto"), "+0.35")
        self.assertEqual(topic_updates.get("politics"), "-0.15")

    def test_empty_text_returns_no_commands(self) -> None:
        commands = parse_preference_commands("")
        self.assertEqual(commands, [])

    def test_combined_new_commands(self) -> None:
        text = "more ai_policy region europe cadence evening max 20 tone executive"
        commands = parse_preference_commands(text)
        actions = {c.action for c in commands}
        self.assertIn("topic_delta", actions)
        self.assertIn("region", actions)
        self.assertIn("cadence", actions)
        self.assertIn("max_items", actions)
        self.assertIn("tone", actions)


if __name__ == "__main__":
    unittest.main()
