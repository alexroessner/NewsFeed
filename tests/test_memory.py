from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from newsfeed.memory.store import CandidateCache, PreferenceStore, StatePersistence
from newsfeed.models.domain import CandidateItem


def _make_candidate(cid: str = "c1", topic: str = "geo", source: str = "reuters", minutes_ago: int = 5) -> CandidateItem:
    return CandidateItem(
        candidate_id=cid, title=f"Title {cid}", source=source,
        summary="Summary", url="https://example.com", topic=topic,
        evidence_score=0.8, novelty_score=0.7, preference_fit=0.9,
        prediction_signal=0.6, discovered_by="agent",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


class PreferenceStoreTests(unittest.TestCase):
    def test_preference_updates(self) -> None:
        store = PreferenceStore()
        profile = store.apply_weight_adjustment("u2", "ai_policy", 0.25)
        self.assertEqual(profile.topic_weights["ai_policy"], 0.25)

        profile = store.apply_style_update("u2", tone="analyst", fmt="sections")
        self.assertEqual(profile.tone, "analyst")
        self.assertEqual(profile.format, "sections")

    def test_apply_region(self) -> None:
        store = PreferenceStore()
        profile = store.apply_region("u1", "europe")
        self.assertIn("europe", profile.regions_of_interest)
        # Adding same region again should not duplicate
        store.apply_region("u1", "europe")
        self.assertEqual(profile.regions_of_interest.count("europe"), 1)

    def test_apply_cadence(self) -> None:
        store = PreferenceStore()
        profile = store.apply_cadence("u1", "morning")
        self.assertEqual(profile.briefing_cadence, "morning")

    def test_apply_max_items(self) -> None:
        store = PreferenceStore()
        profile = store.apply_max_items("u1", 20)
        self.assertEqual(profile.max_items, 20)

    def test_apply_max_items_clamped(self) -> None:
        store = PreferenceStore()
        profile = store.apply_max_items("u1", 100)
        self.assertEqual(profile.max_items, 50)
        profile = store.apply_max_items("u1", 0)
        self.assertEqual(profile.max_items, 1)

    def test_snapshot(self) -> None:
        store = PreferenceStore()
        store.apply_weight_adjustment("u1", "crypto", 0.5)
        store.apply_region("u1", "asia")
        snap = store.snapshot()
        self.assertIn("u1", snap)
        self.assertEqual(snap["u1"]["topic_weights"]["crypto"], 0.5)
        self.assertIn("asia", snap["u1"]["regions"])


class CandidateCacheTests(unittest.TestCase):
    def test_put_and_get_fresh(self) -> None:
        cache = CandidateCache(stale_after_minutes=180)
        candidates = [_make_candidate(cid=f"c{i}") for i in range(3)]
        cache.put("u1", "geo", candidates)
        fresh = cache.get_fresh("u1", "geo")
        self.assertEqual(len(fresh), 3)

    def test_stale_items_filtered(self) -> None:
        cache = CandidateCache(stale_after_minutes=10)
        old = _make_candidate(cid="old", minutes_ago=60)
        new = _make_candidate(cid="new", minutes_ago=1)
        cache.put("u1", "geo", [old, new])
        fresh = cache.get_fresh("u1", "geo")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].candidate_id, "new")

    def test_get_more_excludes_seen(self) -> None:
        cache = CandidateCache()
        candidates = [_make_candidate(cid=f"c{i}") for i in range(5)]
        cache.put("u1", "geo", candidates)
        more = cache.get_more("u1", "geo", already_seen_ids={"c0", "c1"}, limit=10)
        ids = {c.candidate_id for c in more}
        self.assertNotIn("c0", ids)
        self.assertNotIn("c1", ids)

    def test_empty_cache_returns_empty(self) -> None:
        cache = CandidateCache()
        self.assertEqual(cache.get_fresh("nobody", "nothing"), [])


class StatePersistenceTests(unittest.TestCase):
    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sp = StatePersistence(Path(tmpdir))
            sp.save("test_key", {"foo": "bar", "count": 42})
            loaded = sp.load("test_key")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["foo"], "bar")
            self.assertEqual(loaded["count"], 42)

    def test_load_nonexistent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sp = StatePersistence(Path(tmpdir))
            self.assertIsNone(sp.load("missing_key"))

    def test_save_creates_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "sub" / "dir"
            sp = StatePersistence(nested)
            sp.save("data", {"x": 1})
            self.assertTrue(nested.exists())
            loaded = sp.load("data")
            self.assertEqual(loaded["x"], 1)

    def test_corrupt_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sp = StatePersistence(Path(tmpdir))
            path = Path(tmpdir) / "bad.json"
            path.write_text("not valid json {{", encoding="utf-8")
            self.assertIsNone(sp.load("bad"))


if __name__ == "__main__":
    unittest.main()
