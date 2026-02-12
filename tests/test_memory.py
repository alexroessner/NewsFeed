from __future__ import annotations

import unittest

from newsfeed.memory.store import PreferenceStore


class PreferenceStoreTests(unittest.TestCase):
    def test_preference_updates(self) -> None:
        store = PreferenceStore()
        profile = store.apply_weight_adjustment("u2", "ai_policy", 0.25)
        self.assertEqual(profile.topic_weights["ai_policy"], 0.25)

        profile = store.apply_style_update("u2", tone="analyst", fmt="sections")
        self.assertEqual(profile.tone, "analyst")
        self.assertEqual(profile.format, "sections")


if __name__ == "__main__":
    unittest.main()
