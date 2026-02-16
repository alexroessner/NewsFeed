"""Tests for Round 12 — Final pass: security, robustness, UX hardening.

Covers:
- SSRF IP validation in article fetch
- Circuit breaker stuck-OPEN fix
- Audit trail memory cap
- Scheduled briefing time-window matching
- HTML tag closure on card truncation
- /reset confirmation guard
- LLM prompt sanitization
- Zero-yield agent detection
- Source-add rate limiting
- Alert dedup sentinel fix
- Secrets file cleanup
- Analytics DB TTL cleanup
- Store thread safety (RLock)
"""
from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── 1. SSRF IP Validation in Article Fetch ──────────────────────────


class TestFetchArticleSSRF(unittest.TestCase):
    """Verify that fetch_article blocks private/reserved/metadata IPs."""

    def test_blocks_localhost_url(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://127.0.0.1/admin"))

    def test_blocks_private_ip(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://10.0.0.1:8080/api"))

    def test_blocks_metadata_ip(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://169.254.169.254/latest"))

    def test_blocks_ipv6_loopback(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://[::1]/"))

    def test_blocks_ipv4_mapped_ipv6(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://[::ffff:127.0.0.1]/"))

    def test_allows_public_ip(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        # 8.8.8.8 is public (Google DNS); should pass
        self.assertTrue(_check_fetch_url_ip("https://8.8.8.8/"))

    def test_blocks_empty_hostname(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http:///path"))

    def test_blocks_link_local(self):
        from newsfeed.intelligence.enrichment import _check_fetch_url_ip

        self.assertFalse(_check_fetch_url_ip("http://169.254.1.1/"))


# ── 2. Circuit Breaker Stuck-OPEN Fix ────────────────────────────────


class TestCircuitBreakerRecovery(unittest.TestCase):
    """Verify the circuit breaker resets properly after recovery."""

    def test_success_resets_failure_counter(self):
        from newsfeed.orchestration.optimizer import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, recovery_seconds=0.01)

        # Trip the breaker: 3 failures
        cb.record_failure("agent_x")
        cb.record_failure("agent_x")
        cb.record_failure("agent_x")
        self.assertEqual(cb.get_state("agent_x"), "open")

        # Wait for recovery window
        time.sleep(0.02)
        self.assertTrue(cb.allow_request("agent_x"))  # half_open
        cb.record_success("agent_x")  # recovered!
        self.assertEqual(cb.get_state("agent_x"), "closed")

        # Now: 1 failure should NOT trip breaker (needs 3 fresh failures)
        cb.record_failure("agent_x")
        self.assertEqual(cb.get_state("agent_x"), "closed")
        # 2 failures: still closed
        cb.record_failure("agent_x")
        self.assertEqual(cb.get_state("agent_x"), "closed")
        # 3 failures: NOW it trips
        cb.record_failure("agent_x")
        self.assertEqual(cb.get_state("agent_x"), "open")

    def test_success_resets_to_zero(self):
        from newsfeed.orchestration.optimizer import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, recovery_seconds=0)
        cb.record_failure("a")
        cb.record_failure("a")
        cb.record_success("a")
        # Internal state should be (CLOSED, 0, 0.0)
        state, failures, _ = cb._breakers["a"]
        self.assertEqual(state, "closed")
        self.assertEqual(failures, 0)


# ── 3. Audit Trail Memory Cap ────────────────────────────────────────


class TestAuditTrailMemoryCap(unittest.TestCase):
    """Verify the audit trail enforces a hard cap on total events."""

    def test_events_capped(self):
        from newsfeed.orchestration.audit import AuditTrail

        trail = AuditTrail(max_requests=5)
        # Temporarily lower the cap for testing
        trail._MAX_EVENTS = 100

        # Generate events that exceed the cap
        for i in range(200):
            trail.record("test", f"req_{i}", detail=f"event_{i}")

        # Events should be trimmed to below the cap
        self.assertLessEqual(len(trail._events), 120)  # allow trim batch overhead

    def test_trim_preserves_recent_requests(self):
        from newsfeed.orchestration.audit import AuditTrail

        trail = AuditTrail(max_requests=3)
        trail._MAX_EVENTS = 50

        for i in range(20):
            trail.record("test", f"req_{i}", detail=f"event_{i}")

        # Most recent requests should still be accessible
        recent = trail.get_recent_requests(limit=3)
        self.assertTrue(len(recent) <= 3)
        self.assertIn("req_19", recent)


# ── 4. Scheduled Briefing Time Window ────────────────────────────────


class TestScheduleTimeWindow(unittest.TestCase):
    """Verify time-window matching prevents missed briefings."""

    def test_exact_match(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertTrue(BriefingScheduler._time_within_window("08:00", "08:00"))

    def test_one_minute_after(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertTrue(BriefingScheduler._time_within_window("08:00", "08:01"))

    def test_two_minutes_after_excluded(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertFalse(BriefingScheduler._time_within_window("08:00", "08:02"))

    def test_before_schedule(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertFalse(BriefingScheduler._time_within_window("08:00", "07:59"))

    def test_midnight_wrap(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertTrue(BriefingScheduler._time_within_window("23:59", "00:00"))

    def test_midnight_wrap_too_late(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertFalse(BriefingScheduler._time_within_window("23:59", "00:02"))

    def test_invalid_time_returns_false(self):
        from newsfeed.delivery.bot import BriefingScheduler

        self.assertFalse(BriefingScheduler._time_within_window("25:00", "08:00"))
        self.assertFalse(BriefingScheduler._time_within_window("", "08:00"))


# ── 5. HTML Tag Closure on Truncation ────────────────────────────────


class TestCloseUnclosedHtmlTags(unittest.TestCase):
    """Verify unclosed HTML tags are properly closed after truncation."""

    def test_closes_unclosed_bold(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        result = _close_unclosed_html_tags("<b>Hello world")
        self.assertEqual(result, "<b>Hello world</b>")

    def test_closes_nested_tags(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        result = _close_unclosed_html_tags("<b>Hello <i>world")
        self.assertEqual(result, "<b>Hello <i>world</i></b>")

    def test_already_closed_unchanged(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        text = "<b>Hello</b> <i>world</i>"
        result = _close_unclosed_html_tags(text)
        self.assertEqual(result, text)

    def test_closes_unclosed_link(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        result = _close_unclosed_html_tags('<a href="https://example.com">link text')
        self.assertTrue(result.endswith("</a>"))

    def test_ignores_non_telegram_tags(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        # <div> is not a Telegram-supported tag; should not be closed
        result = _close_unclosed_html_tags("<div>hello")
        self.assertEqual(result, "<div>hello")

    def test_empty_string(self):
        from newsfeed.delivery.telegram import _close_unclosed_html_tags

        self.assertEqual(_close_unclosed_html_tags(""), "")


# ── 6. /reset Confirmation Guard ─────────────────────────────────────


class TestResetConfirmation(unittest.TestCase):
    """Verify /reset requires confirmation before destroying preferences."""

    @patch("newsfeed.orchestration.communication.MarketTicker")
    @patch("newsfeed.orchestration.communication.HandlerContext")
    def _make_agent(self, mock_hctx, mock_mt):
        """Create a CommunicationAgent with mocked dependencies."""
        from newsfeed.orchestration.communication import CommunicationAgent

        engine = MagicMock()
        engine.preferences = MagicMock()
        engine.audit = MagicMock()
        bot = MagicMock()
        agent = CommunicationAgent(engine=engine, bot=bot)
        return agent, engine, bot

    def test_first_reset_asks_for_confirmation(self):
        agent, engine, bot = self._make_agent()
        result = agent._handle_command(123, "user1", "reset", "")
        self.assertEqual(result["action"], "reset_pending")
        # Preferences should NOT be reset
        engine.preferences.reset.assert_not_called()
        # User should be warned
        bot.send_message.assert_called()

    def test_reset_confirm_executes_immediately(self):
        agent, engine, bot = self._make_agent()
        result = agent._handle_command(123, "user1", "reset", "confirm")
        self.assertEqual(result["action"], "reset")
        engine.preferences.reset.assert_called_once_with("user1")


# ── 7. LLM Prompt Sanitization ──────────────────────────────────────


class TestPromptSanitization(unittest.TestCase):
    """Verify user input is sanitized before embedding in LLM prompts."""

    def test_strips_newlines(self):
        from newsfeed.review.agents import StyleReviewAgent

        result = StyleReviewAgent._sanitize_for_prompt("hello\nworld\r\nfoo")
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)

    def test_truncates_long_input(self):
        from newsfeed.review.agents import StyleReviewAgent

        result = StyleReviewAgent._sanitize_for_prompt("a" * 200, max_len=50)
        self.assertEqual(len(result), 50)

    def test_strips_control_chars(self):
        from newsfeed.review.agents import StyleReviewAgent

        result = StyleReviewAgent._sanitize_for_prompt("hello\x00\x01world")
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x01", result)

    def test_collapses_whitespace(self):
        from newsfeed.review.agents import StyleReviewAgent

        result = StyleReviewAgent._sanitize_for_prompt("hello    world")
        self.assertEqual(result, "hello world")

    def test_normal_input_unchanged(self):
        from newsfeed.review.agents import StyleReviewAgent

        result = StyleReviewAgent._sanitize_for_prompt("concise", max_len=50)
        self.assertEqual(result, "concise")


# ── 8. Zero-Yield Agent Detection ────────────────────────────────────


class TestZeroYieldDetection(unittest.TestCase):
    """Verify that agents returning 0 candidates are flagged."""

    def test_detects_consecutive_zero_yields(self):
        from newsfeed.orchestration.optimizer import SystemOptimizationAgent

        opt = SystemOptimizationAgent()

        # Record 6 zero-yield runs (threshold is 5)
        for _ in range(6):
            opt.record_agent_run("dead_agent", "source_x", candidate_count=0, latency_ms=100)

        recs = opt.analyze()
        zero_yield_recs = [r for r in recs if "0 candidates" in r.reason]
        self.assertTrue(len(zero_yield_recs) >= 1, f"Expected zero-yield recommendation, got: {recs}")

    def test_resets_on_nonzero_yield(self):
        from newsfeed.orchestration.optimizer import SystemOptimizationAgent

        opt = SystemOptimizationAgent()

        # Record 4 zero-yield runs, then 1 success
        for _ in range(4):
            opt.record_agent_run("agent_y", "src", candidate_count=0, latency_ms=100)
        opt.record_agent_run("agent_y", "src", candidate_count=3, latency_ms=100)

        recs = opt.analyze()
        zero_yield_recs = [r for r in recs if "0 candidates" in r.reason and r.agent_id == "agent_y"]
        self.assertEqual(len(zero_yield_recs), 0, "Should not flag after non-zero yield")


# ── 9. Source-Add Rate Limiting ──────────────────────────────────────


class TestSourceAddRateLimit(unittest.TestCase):
    """Verify that /source add is rate-limited."""

    def _make_agent(self):
        from newsfeed.orchestration.communication import CommunicationAgent

        engine = MagicMock()
        engine.preferences = MagicMock()
        engine.preferences.get_custom_sources = MagicMock(return_value=[])
        bot = MagicMock()
        agent = CommunicationAgent(engine=engine, bot=bot)
        return agent, engine, bot

    @patch("newsfeed.agents.dynamic_sources.discover_feed")
    def test_rate_limits_after_max_additions(self, mock_discover):
        agent, engine, bot = self._make_agent()

        # Mock discover_feed to return invalid (so we don't need real network)
        mock_result = MagicMock()
        mock_result.valid = False
        mock_result.error = "test"
        mock_discover.return_value = mock_result

        # Make _SOURCE_ADD_MAX calls (should succeed)
        for i in range(agent._SOURCE_ADD_MAX):
            agent._source_add(123, "user1", f"https://feed{i}.example.com")

        # Next call should be rate-limited
        result = agent._source_add(123, "user1", "https://feed99.example.com")
        self.assertEqual(result["action"], "source_add_rate_limited")


# ── 10. Alert Dedup Sentinel Fix ──────────────────────────────────────


class TestAlertDedupSentinel(unittest.TestCase):
    """Verify alert dedup doesn't suppress the first alert on fresh processes.

    Bug: default sentinel of 0 for missing keys meant that if
    time.monotonic() < ALERT_COOLDOWN (process running < 1 hour), the
    first alert was incorrectly suppressed.
    """

    def test_first_alert_not_suppressed(self):
        """First alert should always fire regardless of process uptime."""
        from newsfeed.orchestration.communication import CommunicationAgent

        engine = MagicMock()
        engine.preferences.get_or_create.return_value = MagicMock(
            webhook_url="", email="", topic_weights={}, source_weights={},
            regions_of_interest=[], muted_topics=[], tracked_stories=[],
            alert_keywords=[], watchlist_crypto=[], watchlist_stocks=[],
            confidence_min=0, urgency_min="", max_per_source=0,
            max_items=10, tone="concise", format="sections",
            briefing_cadence="morning", timezone="UTC",
            alert_georisk_threshold=0.5, alert_trend_threshold=3.0,
            presets={},
        )
        engine.preferences.snapshot.return_value = {
            "user1": {
                "alert_georisk_threshold": 0.5,
                "regions": ["Test Region"],
                "topic_weights": {},
                "alert_trend_threshold": 3.0,
            }
        }
        engine.georisk.snapshot.return_value = {"test_region": 0.9}
        engine.trends.snapshot.return_value = {}
        engine.formatter.format_intelligence_alert.return_value = "Alert!"
        engine.analytics.get_user_summary.return_value = {"chat_id": "123"}

        bot = MagicMock()
        agent = CommunicationAgent(engine=engine, bot=bot)

        # This should send 1 alert even if time.monotonic() is small
        sent = agent.check_intelligence_alerts()
        self.assertEqual(sent, 1)


# ── 11. Secrets Cleanup in run_scheduled ──────────────────────────────


class TestSecretsCleanup(unittest.TestCase):
    """Verify _inject_env_secrets returns whether it created a file."""

    @patch.dict("os.environ", {
        "TELEGRAM_BOT_TOKEN": "", "GEMINI_API_KEY": "",
        "GUARDIAN_API_KEY": "", "ANTHROPIC_API_KEY": "",
        "X_BEARER_TOKEN": "",
    }, clear=False)
    def test_returns_false_when_no_env_vars(self):
        from newsfeed.run_scheduled import _inject_env_secrets
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _inject_env_secrets(Path(tmpdir))
            self.assertFalse(result)

    def test_returns_false_when_file_exists(self):
        from newsfeed.run_scheduled import _inject_env_secrets
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            secrets_path = Path(tmpdir) / "secrets.json"
            secrets_path.write_text("{}")
            result = _inject_env_secrets(Path(tmpdir))
            self.assertFalse(result)

    @patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test_token_123"})
    def test_returns_true_when_created(self):
        from newsfeed.run_scheduled import _inject_env_secrets
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            result = _inject_env_secrets(Path(tmpdir))
            self.assertTrue(result)
            # Verify file was written
            secrets_path = Path(tmpdir) / "secrets.json"
            self.assertTrue(secrets_path.exists())


# ── 12. Analytics DB Cleanup/TTL ──────────────────────────────────────


class TestAnalyticsCleanup(unittest.TestCase):
    """Verify the analytics DB cleanup method removes old records."""

    def test_cleanup_returns_dict(self):
        import tempfile
        from newsfeed.db.analytics import AnalyticsDB

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = AnalyticsDB(f.name)
            result = db.cleanup_old_records(retention_days=90)
            self.assertIsInstance(result, dict)

    def test_cleanup_deletes_old_interactions(self):
        import tempfile
        from newsfeed.db.analytics import AnalyticsDB

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = AnalyticsDB(f.name)
            # Insert old and new records
            old_ts = time.time() - 200 * 86400  # 200 days ago
            new_ts = time.time() - 5 * 86400     # 5 days ago
            conn = db._conn()
            conn.execute(
                "INSERT INTO interactions (ts, user_id, interaction_type) VALUES (?, ?, ?)",
                (old_ts, "u1", "test_old"),
            )
            conn.execute(
                "INSERT INTO interactions (ts, user_id, interaction_type) VALUES (?, ?, ?)",
                (new_ts, "u1", "test_new"),
            )
            conn.commit()

            result = db.cleanup_old_records(retention_days=90)
            self.assertEqual(result.get("interactions", 0), 1)

            # Verify new record still exists
            row = conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE interaction_type = 'test_new'"
            ).fetchone()
            self.assertEqual(row[0], 1)


# ── 13. Store Thread Safety ──────────────────────────────────────────


class TestStoreThreadSafety(unittest.TestCase):
    """Verify PreferenceStore uses reentrant lock and get_or_create is safe."""

    def test_get_or_create_is_thread_safe(self):
        import threading
        from newsfeed.memory.store import PreferenceStore

        store = PreferenceStore()
        errors: list[Exception] = []

        def create_user(uid):
            try:
                for _ in range(50):
                    store.get_or_create(uid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=create_user, args=(f"user_{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        # All 10 users should exist
        for i in range(10):
            self.assertIn(f"user_{i}", store._profiles)

    def test_rlock_allows_nested_calls(self):
        """apply_weight_adjustment holds lock and calls get_or_create — should not deadlock."""
        from newsfeed.memory.store import PreferenceStore

        store = PreferenceStore()
        profile, hint = store.apply_weight_adjustment("u1", "tech", 0.5)
        self.assertEqual(profile.topic_weights["tech"], 0.5)

    def test_reset_is_thread_safe(self):
        """reset() should work concurrently without errors."""
        import threading
        from newsfeed.memory.store import PreferenceStore

        store = PreferenceStore()
        store.get_or_create("u1")
        store.apply_weight_adjustment("u1", "tech", 0.8)

        errors: list[Exception] = []

        def reset_user():
            try:
                store.reset("u1")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reset_user) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main()
