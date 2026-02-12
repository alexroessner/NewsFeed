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


if __name__ == "__main__":
    unittest.main()
