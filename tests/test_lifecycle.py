"""Tests for daily-use lifecycle fixes: schedule persistence, validation
set alignment, failed briefing notifications, and cadence sync.
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

from newsfeed.delivery.bot import BriefingScheduler


# ══════════════════════════════════════════════════════════════════════════
# Scheduler Persistence
# ══════════════════════════════════════════════════════════════════════════


class TestSchedulerRestore(unittest.TestCase):
    """Verify that scheduler state survives save/restore cycle."""

    def test_snapshot_includes_timezones(self):
        scheduler = BriefingScheduler()
        scheduler.set_user_timezone("u1", "US/Eastern")
        scheduler.set_schedule("u1", "morning", "07:30")
        snap = scheduler.snapshot()
        assert "timezones" in snap
        assert snap["timezones"]["u1"] == "US/Eastern"

    def test_restore_basic_schedule(self):
        scheduler = BriefingScheduler()
        scheduler.set_user_timezone("u1", "US/Eastern")
        scheduler.set_schedule("u1", "morning", "07:30")
        snap = scheduler.snapshot()

        # Restore into a fresh scheduler
        new_scheduler = BriefingScheduler()
        count = new_scheduler.restore(snap)
        assert count == 1
        new_snap = new_scheduler.snapshot()
        assert new_snap["schedules"]["u1"]["type"] == "morning"
        assert new_snap["schedules"]["u1"]["time"] == "07:30"
        assert new_snap["timezones"]["u1"] == "US/Eastern"

    def test_restore_multiple_schedules(self):
        scheduler = BriefingScheduler()
        scheduler.set_schedule("u1", "morning", "07:00")
        scheduler.set_schedule("u2", "evening", "18:30")
        scheduler.set_schedule("u3", "realtime")
        snap = scheduler.snapshot()

        new_scheduler = BriefingScheduler()
        count = new_scheduler.restore(snap)
        assert count == 3

    def test_restore_filters_invalid_types(self):
        """Restore rejects unknown schedule types."""
        data = {
            "schedules": {
                "u1": {"type": "morning", "time": "08:00"},
                "u2": {"type": "bogus", "time": "99:99"},
            },
        }
        scheduler = BriefingScheduler()
        count = scheduler.restore(data)
        assert count == 1  # Only "morning" is valid

    def test_restore_empty_data(self):
        scheduler = BriefingScheduler()
        count = scheduler.restore({})
        assert count == 0

    def test_restore_malformed_data(self):
        """Restore handles garbage data gracefully."""
        scheduler = BriefingScheduler()
        count = scheduler.restore({"schedules": "not_a_dict"})
        assert count == 0

    def test_restore_caps_timezone_length(self):
        """Timezone strings longer than 40 chars are rejected."""
        data = {
            "timezones": {
                "u1": "A" * 41,
                "u2": "US/Eastern",
            },
        }
        scheduler = BriefingScheduler()
        scheduler.restore(data)
        assert "u2" in scheduler._user_timezones
        assert "u1" not in scheduler._user_timezones

    def test_full_roundtrip(self):
        """Save → restore → snapshot produces equivalent state."""
        original = BriefingScheduler()
        original.set_user_timezone("u1", "Europe/London")
        original.set_user_timezone("u2", "Asia/Tokyo")
        original.set_schedule("u1", "morning", "06:00")
        original.set_schedule("u2", "evening", "19:00")
        snap1 = original.snapshot()

        restored = BriefingScheduler()
        restored.restore(snap1)
        snap2 = restored.snapshot()

        # Schedules and timezones should match
        assert snap1["schedules"] == snap2["schedules"]
        assert snap1["timezones"] == snap2["timezones"]

    def test_off_schedule_not_persisted(self):
        """When user turns schedule off, it should not appear in snapshot."""
        scheduler = BriefingScheduler()
        scheduler.set_schedule("u1", "morning")
        scheduler.set_schedule("u1", "off")
        snap = scheduler.snapshot()
        assert "u1" not in snap["schedules"]


# ══════════════════════════════════════════════════════════════════════════
# Validation Set Alignment
# ══════════════════════════════════════════════════════════════════════════


class TestValidationSetAlignment(unittest.TestCase):
    """Verify engine's _load_state validation sets include all parser values."""

    def test_parser_tones_accepted_by_engine(self):
        """Every tone the parser accepts must survive engine persistence."""
        from newsfeed.memory.commands import _VALID_TONES as parser_tones
        from newsfeed.orchestration.engine import NewsFeedEngine
        engine_tones = NewsFeedEngine._VALID_TONES
        for tone in parser_tones:
            assert tone in engine_tones, (
                f"Parser accepts tone '{tone}' but engine would reject it on restore"
            )

    def test_parser_formats_accepted_by_engine(self):
        from newsfeed.memory.commands import _VALID_FORMATS as parser_fmts
        from newsfeed.orchestration.engine import NewsFeedEngine
        engine_fmts = NewsFeedEngine._VALID_FORMATS
        for fmt in parser_fmts:
            assert fmt in engine_fmts, (
                f"Parser accepts format '{fmt}' but engine would reject it on restore"
            )

    def test_parser_cadences_accepted_by_engine(self):
        from newsfeed.memory.commands import _VALID_CADENCES as parser_cads
        from newsfeed.orchestration.engine import NewsFeedEngine
        engine_cads = NewsFeedEngine._VALID_CADENCES
        for cad in parser_cads:
            assert cad in engine_cads, (
                f"Parser accepts cadence '{cad}' but engine would reject it on restore"
            )

    def test_schedule_types_in_cadence_set(self):
        """Schedule types (morning/evening/realtime) must be valid cadences."""
        from newsfeed.orchestration.engine import NewsFeedEngine
        engine_cads = NewsFeedEngine._VALID_CADENCES
        for stype in ("morning", "evening", "realtime", "on_demand"):
            assert stype in engine_cads, (
                f"Schedule type '{stype}' not in engine valid cadences"
            )


# ══════════════════════════════════════════════════════════════════════════
# Failed Scheduled Briefing Notification
# ══════════════════════════════════════════════════════════════════════════


class TestScheduledBriefingFailure(unittest.TestCase):
    """Verify that users are notified when scheduled briefings fail."""

    def _make_comm_agent(self):
        """Build a minimal CommunicationAgent with mocked dependencies."""
        from newsfeed.orchestration.communication import CommunicationAgent

        engine = MagicMock()
        engine.preferences.get_or_create.return_value = MagicMock(
            timezone="UTC", briefing_cadence="morning",
        )
        bot = MagicMock()
        scheduler = BriefingScheduler()
        comm = CommunicationAgent(engine=engine, bot=bot, scheduler=scheduler)
        return comm, engine, bot, scheduler

    def test_failure_sends_notification(self):
        comm, engine, bot, scheduler = self._make_comm_agent()

        # Set up a due user
        scheduler.set_schedule("u1", "morning", "08:00")

        # Make _run_briefing raise
        with patch.object(comm, "_run_briefing", side_effect=RuntimeError("API down")):
            # Force get_due_users to return our user
            with patch.object(scheduler, "get_due_users", return_value=["u1"]):
                sent = comm.run_scheduled_briefings()

        assert sent == 0  # Briefing failed, count stays at 0
        # User should have been notified
        calls = bot.send_message.call_args_list
        assert len(calls) >= 1
        msg = calls[0][0][1]  # Second arg is the message text
        assert "scheduled briefing" in msg.lower()
        assert "/briefing" in msg

    def test_notification_failure_doesnt_crash(self):
        """If even the notification fails, run_scheduled_briefings still returns."""
        comm, engine, bot, scheduler = self._make_comm_agent()

        with patch.object(comm, "_run_briefing", side_effect=RuntimeError("API down")):
            # Make send_message also fail
            bot.send_message.side_effect = RuntimeError("Network down too")
            with patch.object(scheduler, "get_due_users", return_value=["u1"]):
                sent = comm.run_scheduled_briefings()
        assert sent == 0  # No crash, returns cleanly


# ══════════════════════════════════════════════════════════════════════════
# Schedule ↔ Profile Cadence Sync
# ══════════════════════════════════════════════════════════════════════════


class TestScheduleCadenceSync(unittest.TestCase):
    """Verify that /schedule updates the user profile cadence."""

    def _make_comm_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent

        engine = MagicMock()
        profile = MagicMock(timezone="UTC")
        engine.preferences.get_or_create.return_value = profile
        bot = MagicMock()
        scheduler = BriefingScheduler()
        comm = CommunicationAgent(engine=engine, bot=bot, scheduler=scheduler)
        return comm, engine, bot, scheduler

    def test_schedule_morning_syncs_cadence(self):
        comm, engine, bot, scheduler = self._make_comm_agent()
        comm._set_schedule("chat1", "u1", "morning 07:30")
        engine.preferences.apply_cadence.assert_called_once_with("u1", "morning")

    def test_schedule_evening_syncs_cadence(self):
        comm, engine, bot, scheduler = self._make_comm_agent()
        comm._set_schedule("chat1", "u1", "evening")
        engine.preferences.apply_cadence.assert_called_once_with("u1", "evening")

    def test_schedule_off_syncs_on_demand(self):
        comm, engine, bot, scheduler = self._make_comm_agent()
        comm._set_schedule("chat1", "u1", "off")
        engine.preferences.apply_cadence.assert_called_once_with("u1", "on_demand")

    def test_schedule_persists_prefs(self):
        comm, engine, bot, scheduler = self._make_comm_agent()
        comm._set_schedule("chat1", "u1", "morning")
        engine.persist_preferences.assert_called_once()
