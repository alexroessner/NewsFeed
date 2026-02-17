"""Operational correctness tests — covers code paths that require
sophisticated mocking infrastructure:

1. Bootstrap startup dashboard (log output verification)
2. Hot reload path (signal mocking)
3. Graceful shutdown sequence (signal mocking)
4. _safe_exec_many transaction rollback (DB error injection)
5. Stale connection reconnection (mocking broken SQLite)
"""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from newsfeed.db.analytics import AnalyticsDB


# ══════════════════════════════════════════════════════════════════════════
# 1. Bootstrap Startup Dashboard — log output verification
# ══════════════════════════════════════════════════════════════════════════


class TestBootstrapDashboard(unittest.TestCase):
    """Verify that main() logs the full startup dashboard."""

    def _make_config(self, *, simulated_ids=None, missing_keys=None,
                     enabled_stages=None, telegram=False, llm=False):
        """Build a RuntimeConfig-compatible mock."""
        from newsfeed.models.config import RuntimeConfig

        research_agents = []
        # Create agents — some real, some simulated
        for i in range(3):
            research_agents.append({
                "id": f"agent_{i}", "source": f"src_{i}",
                "mandate": "test", "api_key_env": f"KEY_{i}",
            })

        api_keys = {
            "guardian": "key1", "newsapi": "key2",
            "x_bearer_token": "key3", "reddit_client_id": "key4",
            "anthropic_api_key": "key5" if llm else "",
        }
        if missing_keys:
            for k in missing_keys:
                api_keys[k] = ""

        pipeline = {
            "version": "test-1.0",
            "stages": [{"id": f"stage_{i}"} for i in range(5)],
            "intelligence": {"enabled_stages": enabled_stages or ["credibility", "urgency"]},
            "api_keys": api_keys,
            "scoring": {},
        }
        if telegram:
            api_keys["telegram_bot_token"] = "fake-token"

        agents = {"research_agents": research_agents, "expert_agents": [],
                  "control_agents": [], "review_agents": []}
        personas = {"default_personas": [], "persona_notes": {}}
        return RuntimeConfig(agents=agents, pipeline=pipeline, personas=personas)

    @patch("newsfeed.orchestration.bootstrap.NewsFeedEngine")
    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_dashboard_logs_agent_counts(self, mock_load, MockEngine):
        """Dashboard prints real vs simulated agent counts."""
        cfg = self._make_config()
        mock_load.return_value = cfg
        engine = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics.backend = "sqlite"
        engine.is_telegram_connected.return_value = False
        engine.is_llm_backed.return_value = False
        MockEngine.return_value = engine

        # create_agent will return simulated agents for all (no real keys)
        def make_sim_agent(*a, **kw):
            m = MagicMock()
            m.__class__ = type("SimulatedResearchAgent", (), {})
            return m

        with self.assertLogs("newsfeed", level="INFO") as cm:
            with patch("newsfeed.agents.registry.create_agent", side_effect=make_sim_agent):
                from newsfeed.orchestration.bootstrap import main
                main()

        log_text = "\n".join(cm.output)
        self.assertIn("STARTUP STATUS", log_text)
        self.assertIn("Agents:", log_text)
        self.assertIn("Database:", log_text)
        self.assertIn("Telegram:", log_text)
        self.assertIn("LLM-backed experts:", log_text)
        self.assertIn("Intelligence stages:", log_text)

    @patch("newsfeed.orchestration.bootstrap.NewsFeedEngine")
    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_dashboard_shows_missing_keys(self, mock_load, MockEngine):
        """Dashboard lists missing API key names."""
        cfg = self._make_config(missing_keys=["guardian", "anthropic_api_key"])
        mock_load.return_value = cfg
        engine = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics.backend = "sqlite"
        engine.is_telegram_connected.return_value = False
        engine.is_llm_backed.return_value = False
        MockEngine.return_value = engine

        def make_sim_agent(*a, **kw):
            m = MagicMock()
            m.__class__ = type("SimulatedResearchAgent", (), {})
            return m

        with self.assertLogs("newsfeed", level="INFO") as cm:
            with patch("newsfeed.agents.registry.create_agent", side_effect=make_sim_agent):
                from newsfeed.orchestration.bootstrap import main
                main()

        log_text = "\n".join(cm.output)
        self.assertIn("GUARDIAN", log_text)
        self.assertIn("ANTHROPIC_API_KEY", log_text)

    @patch("newsfeed.orchestration.bootstrap.NewsFeedEngine")
    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_dashboard_shows_real_agents(self, mock_load, MockEngine):
        """Dashboard distinguishes real from simulated agents."""
        cfg = self._make_config()
        mock_load.return_value = cfg
        engine = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics.backend = "sqlite"
        engine.is_telegram_connected.return_value = True
        engine.is_llm_backed.return_value = True
        MockEngine.return_value = engine

        def make_real_agent(*a, **kw):
            m = MagicMock()
            m.__class__ = type("BBCAgent", (), {})
            return m

        with self.assertLogs("newsfeed", level="INFO") as cm:
            with patch("newsfeed.agents.registry.create_agent", side_effect=make_real_agent):
                with patch("newsfeed.orchestration.bootstrap._run_bot_loop"):
                    from newsfeed.orchestration.bootstrap import main
                    main()

        log_text = "\n".join(cm.output)
        self.assertIn("3 real", log_text)
        self.assertIn("0 simulated", log_text)
        self.assertIn("connected", log_text)

    @patch("newsfeed.orchestration.bootstrap.NewsFeedEngine")
    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_dashboard_demo_mode_when_no_telegram(self, mock_load, MockEngine):
        """Without Telegram token, runs demo cycle instead of bot loop."""
        cfg = self._make_config(telegram=False)
        mock_load.return_value = cfg
        engine = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics.backend = "sqlite"
        engine.is_telegram_connected.return_value = False
        engine.is_llm_backed.return_value = False
        engine.handle_request.return_value = "Demo report line 1\nLine 2\nLine 3"
        MockEngine.return_value = engine

        def make_sim_agent(*a, **kw):
            m = MagicMock()
            m.__class__ = type("SimulatedResearchAgent", (), {})
            return m

        with self.assertLogs("newsfeed", level="INFO") as cm:
            with patch("newsfeed.agents.registry.create_agent", side_effect=make_sim_agent):
                from newsfeed.orchestration.bootstrap import main
                main()

        log_text = "\n".join(cm.output)
        self.assertIn("No Telegram token", log_text)
        engine.handle_request.assert_called_once()

    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_config_error_exits(self, mock_load):
        """ConfigError during startup causes sys.exit(1)."""
        from newsfeed.models.config import ConfigError
        mock_load.side_effect = ConfigError("bad config")

        with self.assertRaises(SystemExit) as ctx:
            with self.assertLogs("newsfeed", level="ERROR"):
                from newsfeed.orchestration.bootstrap import main
                main()
        self.assertEqual(ctx.exception.code, 1)


# ══════════════════════════════════════════════════════════════════════════
# 2. Hot Reload Path — signal mocking
# ══════════════════════════════════════════════════════════════════════════


class TestHotReload(unittest.TestCase):
    """Verify SIGHUP-triggered config reload in the bot loop."""

    def test_sighup_handler_sets_reload_flag(self):
        """_handle_reload sets the global _reload flag."""
        import newsfeed.orchestration.bootstrap as bs
        bs._reload = False
        bs._handle_reload(signal.SIGHUP, None)
        self.assertTrue(bs._reload)

    def test_sigint_handler_sets_shutdown_flag(self):
        """_handle_signal sets the global _shutdown flag."""
        import newsfeed.orchestration.bootstrap as bs
        bs._shutdown = False
        bs._handle_signal(signal.SIGINT, None)
        self.assertTrue(bs._shutdown)

    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_reload_reloads_scoring_config(self, mock_load_cfg):
        """When _reload is True, bot loop reloads scoring config."""
        import newsfeed.orchestration.bootstrap as bs
        from newsfeed.models.config import RuntimeConfig

        # Build a minimal config that load_runtime_config will return
        new_cfg = RuntimeConfig(
            agents={"research_agents": [], "expert_agents": [],
                    "control_agents": [], "review_agents": []},
            pipeline={"version": "2.0", "stages": [], "scoring": {"novelty_weight": 0.9}},
            personas={"default_personas": []},
        )
        mock_load_cfg.return_value = new_cfg

        engine = MagicMock()
        bot = engine.get_bot.return_value
        comm = engine.get_comm_agent.return_value

        # get_me succeeds
        bot.get_me.return_value = {"username": "test_bot", "first_name": "Test"}

        # First iteration: _reload triggers reload, then _shutdown stops loop
        call_count = 0

        def fake_get_updates(timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                bs._reload = True  # trigger reload
                return []
            bs._shutdown = True  # stop loop after reload
            return []

        bot.get_updates.side_effect = fake_get_updates
        comm.run_scheduled_briefings.return_value = 0

        # Reset flags
        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with patch("newsfeed.orchestration.bootstrap.time") as mock_time:
                mock_time.time.return_value = 1000000.0
                with self.assertLogs("newsfeed", level="INFO") as cm:
                    bs._run_bot_loop(engine, Path("/tmp/fake-config"))

        log_text = "\n".join(cm.output)
        self.assertIn("Reloading configuration", log_text)
        self.assertIn("Scoring config reloaded", log_text)
        mock_load_cfg.assert_called_once_with(Path("/tmp/fake-config"))

    @patch("newsfeed.orchestration.bootstrap.load_runtime_config")
    def test_reload_failure_continues(self, mock_load_cfg):
        """Config reload failure is logged but loop continues."""
        import newsfeed.orchestration.bootstrap as bs

        mock_load_cfg.side_effect = Exception("parse error")

        engine = MagicMock()
        bot = engine.get_bot.return_value
        comm = engine.get_comm_agent.return_value
        bot.get_me.return_value = {"username": "test_bot", "first_name": "Test"}

        call_count = 0

        def fake_get_updates(timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                bs._reload = True
                return []
            bs._shutdown = True
            return []

        bot.get_updates.side_effect = fake_get_updates
        comm.run_scheduled_briefings.return_value = 0

        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with patch("newsfeed.orchestration.bootstrap.time") as mock_time:
                mock_time.time.return_value = 1000000.0
                with self.assertLogs("newsfeed", level="INFO") as cm:
                    bs._run_bot_loop(engine, Path("/tmp/fake-config"))

        log_text = "\n".join(cm.output)
        self.assertIn("Config reload failed", log_text)
        # Loop continued — shutdown sequence ran
        self.assertIn("Shutdown signal received", log_text)


# ══════════════════════════════════════════════════════════════════════════
# 3. Graceful Shutdown Sequence — signal mocking
# ══════════════════════════════════════════════════════════════════════════


class TestGracefulShutdown(unittest.TestCase):
    """Verify that _run_bot_loop performs cleanup on shutdown."""

    def _run_loop_with_immediate_shutdown(self, engine):
        """Helper: start bot loop, shutdown immediately."""
        import newsfeed.orchestration.bootstrap as bs

        bot = engine.get_bot.return_value
        comm = engine.get_comm_agent.return_value
        bot.get_me.return_value = {"username": "test_bot", "first_name": "Test"}
        bot.get_updates.side_effect = lambda timeout=30: self._trigger_shutdown(bs)
        comm.run_scheduled_briefings.return_value = 0

        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with patch("newsfeed.orchestration.bootstrap.time") as mock_time:
                mock_time.time.return_value = 1000000.0
                with self.assertLogs("newsfeed", level="INFO") as cm:
                    bs._run_bot_loop(engine, Path("/tmp/fake-config"))
        return cm

    def _trigger_shutdown(self, bs):
        bs._shutdown = True
        return []

    def test_shutdown_persists_preferences(self):
        """Shutdown sequence calls engine.persist_preferences()."""
        engine = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics._local = threading.local()

        cm = self._run_loop_with_immediate_shutdown(engine)
        engine.persist_preferences.assert_called_once()

        log_text = "\n".join(cm.output)
        self.assertIn("Shutdown signal received", log_text)
        self.assertIn("Bot stopped", log_text)

    def test_shutdown_closes_analytics_connection(self):
        """Shutdown closes the SQLite analytics connection."""
        engine = MagicMock()
        mock_conn = MagicMock()
        engine.analytics = MagicMock()
        engine.analytics._local = threading.local()
        engine.analytics._local.conn = mock_conn

        cm = self._run_loop_with_immediate_shutdown(engine)
        mock_conn.close.assert_called_once()

        log_text = "\n".join(cm.output)
        self.assertIn("Analytics DB connection closed", log_text)

    def test_shutdown_handles_preference_error(self):
        """Shutdown continues even if persist_preferences fails."""
        engine = MagicMock()
        engine.persist_preferences.side_effect = RuntimeError("disk full")
        engine.analytics = MagicMock()
        engine.analytics._local = threading.local()

        cm = self._run_loop_with_immediate_shutdown(engine)

        log_text = "\n".join(cm.output)
        self.assertIn("Failed to persist preferences", log_text)
        self.assertIn("Bot stopped", log_text)

    def test_shutdown_handles_analytics_close_error(self):
        """Shutdown continues even if analytics connection close fails."""
        engine = MagicMock()
        mock_conn = MagicMock()
        mock_conn.close.side_effect = sqlite3.OperationalError("lock")
        engine.analytics = MagicMock()
        engine.analytics._local = threading.local()
        engine.analytics._local.conn = mock_conn

        cm = self._run_loop_with_immediate_shutdown(engine)

        log_text = "\n".join(cm.output)
        self.assertIn("Failed to close analytics DB", log_text)
        self.assertIn("Bot stopped", log_text)

    def test_shutdown_without_analytics_local(self):
        """Shutdown works when analytics has no _local attribute."""
        engine = MagicMock()
        engine.analytics = MagicMock(spec=[])  # no _local attribute

        cm = self._run_loop_with_immediate_shutdown(engine)

        log_text = "\n".join(cm.output)
        self.assertIn("Bot stopped", log_text)

    def test_bot_verify_failure_returns_early(self):
        """If get_me returns None, loop returns without polling."""
        import newsfeed.orchestration.bootstrap as bs

        engine = MagicMock()
        bot = engine.get_bot.return_value
        bot.get_me.return_value = None

        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with self.assertLogs("newsfeed", level="WARNING") as cm:
                bs._run_bot_loop(engine, Path("/tmp/fake-config"))

        log_text = "\n".join(cm.output)
        self.assertIn("Could not verify bot token", log_text)
        bot.get_updates.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
# 4. _safe_exec_many Transaction Rollback — DB error injection
# ══════════════════════════════════════════════════════════════════════════


class TestSafeExecManyRollback(unittest.TestCase):
    """Verify _safe_exec_many rolls back on error and never raises."""

    def _make_db(self, tmp_path=None):
        """Create an in-memory AnalyticsDB for testing."""
        db = AnalyticsDB(":memory:")
        return db

    def test_successful_batch_insert(self):
        """Successful batch insert commits all rows."""
        db = self._make_db()
        db._safe_exec_many(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            [(1.0, "u1", "good"), (2.0, "u2", "great")],
        )
        rows = db._query("SELECT COUNT(*) as c FROM feedback")
        self.assertEqual(rows[0]["c"], 2)

    def test_empty_params_list_is_noop(self):
        """Empty params_list returns immediately without touching DB."""
        db = self._make_db()
        # Should not raise or execute anything
        db._safe_exec_many("INSERT INTO feedback (ts, user_id) VALUES (?, ?)", [])
        rows = db._query("SELECT COUNT(*) as c FROM feedback")
        self.assertEqual(rows[0]["c"], 0)

    def test_rollback_on_constraint_violation(self):
        """If any row in the batch violates a constraint, all are rolled back."""
        db = self._make_db()
        # Insert a user
        db._safe_exec(
            "INSERT INTO users (user_id, first_seen_at, last_active_at) VALUES (?, ?, ?)",
            ("u1", 1.0, 1.0),
        )
        # Batch insert: first row OK, second violates PRIMARY KEY
        db._safe_exec_many(
            "INSERT INTO users (user_id, first_seen_at, last_active_at) VALUES (?, ?, ?)",
            [("u2", 2.0, 2.0), ("u1", 3.0, 3.0)],  # u1 already exists
        )
        # u2 should NOT be inserted (rollback)
        rows = db._query("SELECT user_id FROM users ORDER BY user_id")
        user_ids = [r["user_id"] for r in rows]
        self.assertEqual(user_ids, ["u1"])

    def test_rollback_on_sql_error(self):
        """SQL syntax errors in batch trigger rollback and logging."""
        db = self._make_db()
        # First insert some data
        db._safe_exec(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            (1.0, "u1", "existing"),
        )

        # Now try a batch with wrong number of params (triggers error)
        with self.assertLogs("newsfeed.db.analytics", level="ERROR") as cm:
            db._safe_exec_many(
                "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
                [(2.0, "u2")],  # Too few params
            )

        log_text = "\n".join(cm.output)
        self.assertIn("batch write failed", log_text)

        # Original data still intact
        rows = db._query("SELECT COUNT(*) as c FROM feedback")
        self.assertEqual(rows[0]["c"], 1)

    def test_rollback_preserves_prior_data(self):
        """Failed batch doesn't corrupt prior committed data."""
        db = self._make_db()
        # Commit some data first
        db._safe_exec_many(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            [(1.0, "u1", "first"), (2.0, "u2", "second")],
        )

        # Now a failing batch — inject an error via wrong column count
        db._safe_exec_many(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            [(3.0, "u3", "third"), (4.0,)],  # Second row malformed
        )

        # Original 2 rows intact, no partial third
        rows = db._query("SELECT feedback_text FROM feedback ORDER BY ts")
        texts = [r["feedback_text"] for r in rows]
        self.assertEqual(texts, ["first", "second"])

    def test_connection_usable_after_rollback(self):
        """After a failed batch + rollback, connection remains usable."""
        db = self._make_db()

        # Trigger a rollback
        db._safe_exec_many(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            [(1.0,)],  # Malformed
        )

        # Connection should still work
        db._safe_exec(
            "INSERT INTO feedback (ts, user_id, feedback_text) VALUES (?, ?, ?)",
            (1.0, "u1", "after_rollback"),
        )
        rows = db._query("SELECT feedback_text FROM feedback")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["feedback_text"], "after_rollback")

    def test_safe_exec_many_with_mock_d1(self):
        """_safe_exec_many delegates to D1 client in cloud mode."""
        db = AnalyticsDB.__new__(AnalyticsDB)
        db._d1 = MagicMock()
        db._local = threading.local()
        db._init_lock = threading.Lock()
        db._initialized = True
        db._backend = "d1"
        db._db_path = "d1://test"

        db._safe_exec_many(
            "INSERT INTO feedback VALUES (?, ?)",
            [(1, "a"), (2, "b")],
        )
        db._d1.execute_many.assert_called_once()

    def test_safe_exec_many_d1_error_logged(self):
        """D1 errors are logged but never raise."""
        db = AnalyticsDB.__new__(AnalyticsDB)
        db._d1 = MagicMock()
        db._d1.execute_many.side_effect = Exception("D1 timeout")
        db._local = threading.local()
        db._init_lock = threading.Lock()
        db._initialized = True
        db._backend = "d1"
        db._db_path = "d1://test"

        with self.assertLogs("newsfeed.db.analytics", level="ERROR") as cm:
            db._safe_exec_many(
                "INSERT INTO feedback VALUES (?, ?)",
                [(1, "a")],
            )

        self.assertIn("batch write failed", "\n".join(cm.output))


# ══════════════════════════════════════════════════════════════════════════
# 5. Stale Connection Reconnection — mocking broken SQLite
# ══════════════════════════════════════════════════════════════════════════


class TestStaleConnectionReconnection(unittest.TestCase):
    """Verify that _conn() detects and recovers from stale connections."""

    def test_healthy_connection_reused(self):
        """A working connection is reused without reconnect."""
        db = AnalyticsDB(":memory:")
        conn1 = db._conn()
        conn2 = db._conn()
        self.assertIs(conn1, conn2)

    def test_stale_connection_replaced(self):
        """A connection that fails SELECT 1 is replaced with a fresh one."""
        db = AnalyticsDB(":memory:")
        original_conn = db._conn()

        # Simulate stale connection: close it behind the scenes
        # but don't clear _local.conn — let _conn() detect it
        mock_stale = MagicMock(spec=sqlite3.Connection)
        mock_stale.execute.side_effect = sqlite3.OperationalError("disk I/O error")
        db._local.conn = mock_stale

        with self.assertLogs("newsfeed.db.analytics", level="WARNING") as cm:
            new_conn = db._conn()

        self.assertIsNot(new_conn, mock_stale)
        self.assertIsInstance(new_conn, sqlite3.Connection)
        self.assertIn("Stale SQLite connection", "\n".join(cm.output))
        mock_stale.close.assert_called_once()

    def test_stale_connection_close_failure_still_reconnects(self):
        """If closing the stale connection also fails, we still reconnect."""
        db = AnalyticsDB(":memory:")

        mock_stale = MagicMock(spec=sqlite3.Connection)
        mock_stale.execute.side_effect = sqlite3.DatabaseError("corrupt")
        mock_stale.close.side_effect = sqlite3.OperationalError("close failed")
        db._local.conn = mock_stale

        with self.assertLogs("newsfeed.db.analytics", level="WARNING"):
            new_conn = db._conn()

        self.assertIsNot(new_conn, mock_stale)
        self.assertIsInstance(new_conn, sqlite3.Connection)

    def test_reconnected_connection_has_pragmas(self):
        """Fresh connection after reconnect has WAL and busy_timeout set."""
        db = AnalyticsDB(":memory:")

        # Force reconnect by injecting a stale connection
        mock_stale = MagicMock(spec=sqlite3.Connection)
        mock_stale.execute.side_effect = sqlite3.OperationalError("stale")
        db._local.conn = mock_stale

        with self.assertLogs("newsfeed.db.analytics", level="WARNING"):
            new_conn = db._conn()

        # Verify pragmas
        journal = new_conn.execute("PRAGMA journal_mode").fetchone()[0]
        # In-memory databases may report 'memory' for journal_mode
        self.assertIn(journal, ("wal", "memory"))

        busy = new_conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(busy, 5000)

    def test_no_connection_creates_new(self):
        """When _local.conn is None, a new connection is created."""
        db = AnalyticsDB(":memory:")
        db._local.conn = None  # Clear any existing connection

        conn = db._conn()
        self.assertIsInstance(conn, sqlite3.Connection)
        self.assertIs(db._local.conn, conn)

    def test_database_error_triggers_reconnect(self):
        """DatabaseError (not just OperationalError) triggers reconnect."""
        db = AnalyticsDB(":memory:")

        mock_stale = MagicMock(spec=sqlite3.Connection)
        mock_stale.execute.side_effect = sqlite3.DatabaseError("malformed")
        db._local.conn = mock_stale

        with self.assertLogs("newsfeed.db.analytics", level="WARNING") as cm:
            new_conn = db._conn()

        self.assertIsNot(new_conn, mock_stale)
        self.assertIn("Stale SQLite connection", "\n".join(cm.output))

    def test_thread_isolation(self):
        """Each thread gets its own connection via thread-local storage."""
        db = AnalyticsDB(":memory:")
        main_conn = db._conn()
        thread_conn = [None]
        error = [None]

        def worker():
            try:
                thread_conn[0] = db._conn()
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        self.assertIsNone(error[0])
        # Thread-local connections should be different objects
        # (In-memory DBs are separate per connection, so they are distinct)
        self.assertIsNot(main_conn, thread_conn[0])

    def test_write_after_reconnect_succeeds(self):
        """Data operations work correctly after a reconnection event."""
        import tempfile, os
        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = AnalyticsDB(tmp_path)

            # Force a reconnect
            mock_stale = MagicMock(spec=sqlite3.Connection)
            mock_stale.execute.side_effect = sqlite3.OperationalError("broken pipe")
            db._local.conn = mock_stale

            with self.assertLogs("newsfeed.db.analytics", level="WARNING"):
                db._conn()

            # Write and read should work (reconnected to same file with schema)
            db.record_user_seen("test-user", "chat-1")
            rows = db._query("SELECT user_id FROM users WHERE user_id = ?", ("test-user",))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["user_id"], "test-user")
        finally:
            os.unlink(tmp_path)


# ══════════════════════════════════════════════════════════════════════════
# 6. Polling Loop Error Handling
# ══════════════════════════════════════════════════════════════════════════


class TestPollingLoopErrors(unittest.TestCase):
    """Verify polling loop recovers from errors and processes updates."""

    def test_update_handling_error_continues(self):
        """Exception in handle_update doesn't crash the polling loop."""
        import newsfeed.orchestration.bootstrap as bs

        engine = MagicMock()
        bot = engine.get_bot.return_value
        comm = engine.get_comm_agent.return_value
        bot.get_me.return_value = {"username": "test_bot", "first_name": "Test"}

        call_count = 0

        def fake_get_updates(timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [{"update_id": 1, "message": "bad"}]
            bs._shutdown = True
            return []

        bot.get_updates.side_effect = fake_get_updates
        comm.handle_update.side_effect = ValueError("parse error")
        comm.run_scheduled_briefings.return_value = 0

        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with patch("newsfeed.orchestration.bootstrap.time") as mock_time:
                mock_time.time.return_value = 1000000.0
                with self.assertLogs("newsfeed", level="INFO") as cm:
                    bs._run_bot_loop(engine, Path("/tmp/fake-config"))

        log_text = "\n".join(cm.output)
        self.assertIn("Failed to handle update", log_text)
        # Loop still completed shutdown sequence
        self.assertIn("Bot stopped", log_text)

    def test_scheduled_briefings_delivered(self):
        """Scheduled briefings are checked and delivery count is logged."""
        import newsfeed.orchestration.bootstrap as bs

        engine = MagicMock()
        bot = engine.get_bot.return_value
        comm = engine.get_comm_agent.return_value
        bot.get_me.return_value = {"username": "test_bot", "first_name": "Test"}

        call_count = 0

        def fake_get_updates(timeout=30):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return []
            bs._shutdown = True
            return []

        bot.get_updates.side_effect = fake_get_updates
        comm.run_scheduled_briefings.return_value = 3

        bs._shutdown = False
        bs._reload = False

        with patch("newsfeed.orchestration.bootstrap.signal"):
            with patch("newsfeed.orchestration.bootstrap.time") as mock_time:
                # last_scheduler_check starts at 0, so time.time() - 0 > 60
                mock_time.time.return_value = 1000000.0
                with self.assertLogs("newsfeed", level="INFO") as cm:
                    bs._run_bot_loop(engine, Path("/tmp/fake-config"))

        log_text = "\n".join(cm.output)
        self.assertIn("Delivered 3 scheduled briefings", log_text)


if __name__ == "__main__":
    unittest.main()
