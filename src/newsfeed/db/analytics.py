"""Persistent analytics database — captures ALL user data, interactions, and pipeline events.

SQLite-backed storage that records every touchpoint:
- User registration and profile snapshots
- Every command, message, and callback from every user
- Full pipeline execution traces (candidates, votes, selections)
- Briefing deliveries with all items
- Feedback, ratings, and preference changes
- Intelligence snapshots (geo-risk, trends, credibility, expert influence)
- Agent performance per request

All data is keyed by user_id and timestamped for full historical analysis.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
-- ============================================================
-- USERS
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    chat_id         TEXT,
    first_seen_at   REAL NOT NULL,          -- epoch
    last_active_at  REAL NOT NULL,
    total_requests  INTEGER DEFAULT 0,
    total_briefings INTEGER DEFAULT 0,
    total_feedback  INTEGER DEFAULT 0,
    total_ratings   INTEGER DEFAULT 0
);

-- ============================================================
-- INTERACTIONS — every single message / command / callback
-- ============================================================
CREATE TABLE IF NOT EXISTS interactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,           -- epoch
    user_id         TEXT NOT NULL,
    chat_id         TEXT,
    interaction_type TEXT NOT NULL,           -- command, preference, feedback, rate, mute, unknown
    command         TEXT,                     -- /briefing, /more, etc.
    args            TEXT,                     -- command arguments
    raw_text        TEXT,                     -- full message text
    result_action   TEXT,                     -- action returned by handler
    result_data     TEXT                      -- JSON of full result dict
);
CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id);
CREATE INDEX IF NOT EXISTS idx_interactions_ts ON interactions(ts);

-- ============================================================
-- REQUESTS — pipeline execution records
-- ============================================================
CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    prompt          TEXT,
    weighted_topics TEXT,                     -- JSON
    max_items       INTEGER,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    total_elapsed_s REAL,
    candidate_count INTEGER DEFAULT 0,
    selected_count  INTEGER DEFAULT 0,
    briefing_type   TEXT,
    status          TEXT DEFAULT 'running'    -- running, completed, failed
);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(started_at);

-- ============================================================
-- CANDIDATES — every candidate from every research cycle
-- ============================================================
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    candidate_id    TEXT NOT NULL,
    title           TEXT,
    source          TEXT,
    topic           TEXT,
    url             TEXT,
    summary         TEXT,
    evidence_score  REAL,
    novelty_score   REAL,
    preference_fit  REAL,
    prediction_signal REAL,
    composite_score REAL,
    discovered_by   TEXT,
    urgency         TEXT,
    lifecycle       TEXT,
    regions         TEXT,                     -- JSON array
    corroborated_by TEXT,                     -- JSON array
    contrarian_signal TEXT,
    created_at      TEXT,                     -- ISO timestamp
    was_selected    INTEGER DEFAULT 0,
    selection_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_request ON candidates(request_id);
CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source);
CREATE INDEX IF NOT EXISTS idx_candidates_topic ON candidates(topic);

-- ============================================================
-- EXPERT VOTES — every vote from every expert on every candidate
-- ============================================================
CREATE TABLE IF NOT EXISTS expert_votes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    expert_id       TEXT NOT NULL,
    candidate_id    TEXT NOT NULL,
    keep            INTEGER NOT NULL,         -- 0 or 1
    confidence      REAL,
    rationale       TEXT,
    risk_note       TEXT,
    arbitrated      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_votes_request ON expert_votes(request_id);
CREATE INDEX IF NOT EXISTS idx_votes_expert ON expert_votes(expert_id);

-- ============================================================
-- BRIEFINGS — delivered briefing payloads
-- ============================================================
CREATE TABLE IF NOT EXISTS briefings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    delivered_at    REAL NOT NULL,
    briefing_type   TEXT,
    item_count      INTEGER,
    thread_count    INTEGER,
    geo_risk_count  INTEGER,
    emerging_trends INTEGER,
    metadata        TEXT                      -- JSON of full metadata dict
);
CREATE INDEX IF NOT EXISTS idx_briefings_user ON briefings(user_id);
CREATE INDEX IF NOT EXISTS idx_briefings_ts ON briefings(delivered_at);

-- ============================================================
-- BRIEFING ITEMS — each story delivered in a briefing
-- ============================================================
CREATE TABLE IF NOT EXISTS briefing_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    item_index      INTEGER,
    candidate_id    TEXT,
    title           TEXT,
    source          TEXT,
    topic           TEXT,
    url             TEXT,
    summary         TEXT,
    why_it_matters  TEXT,
    what_changed    TEXT,
    predictive_outlook TEXT,
    confidence_low  REAL,
    confidence_mid  REAL,
    confidence_high REAL,
    thread_id       TEXT,
    urgency         TEXT,
    lifecycle       TEXT,
    composite_score REAL,
    contrarian_note TEXT,
    delivered_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_bitems_user ON briefing_items(user_id);
CREATE INDEX IF NOT EXISTS idx_bitems_request ON briefing_items(request_id);

-- ============================================================
-- FEEDBACK — user text feedback events
-- ============================================================
CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    user_id         TEXT NOT NULL,
    feedback_text   TEXT,
    changes_applied TEXT                      -- JSON of {key: value} changes
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);

-- ============================================================
-- RATINGS — per-item thumbs up/down
-- ============================================================
CREATE TABLE IF NOT EXISTS ratings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    user_id         TEXT NOT NULL,
    item_index      INTEGER,
    direction       TEXT,                     -- up / down
    topic           TEXT,
    source          TEXT,
    title           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ratings_user ON ratings(user_id);

-- ============================================================
-- PREFERENCE CHANGES — full audit trail of every preference modification
-- ============================================================
CREATE TABLE IF NOT EXISTS preference_changes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    user_id         TEXT NOT NULL,
    change_type     TEXT,                     -- topic_delta, tone, format, region, cadence, etc.
    field           TEXT,                     -- specific field changed
    old_value       TEXT,
    new_value       TEXT,
    source          TEXT                      -- command, feedback, rate, preference_callback
);
CREATE INDEX IF NOT EXISTS idx_prefchanges_user ON preference_changes(user_id);

-- ============================================================
-- USER PROFILE SNAPSHOTS — periodic full profile captures
-- ============================================================
CREATE TABLE IF NOT EXISTS profile_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    user_id         TEXT NOT NULL,
    profile_data    TEXT NOT NULL             -- JSON of full profile
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user ON profile_snapshots(user_id);

-- ============================================================
-- GEO-RISK SNAPSHOTS — regional risk over time
-- ============================================================
CREATE TABLE IF NOT EXISTS georisk_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    request_id      TEXT,
    region          TEXT NOT NULL,
    risk_level      REAL,
    previous_level  REAL,
    escalation_delta REAL,
    drivers         TEXT                      -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_georisk_ts ON georisk_snapshots(ts);

-- ============================================================
-- TREND SNAPSHOTS — topic trends over time
-- ============================================================
CREATE TABLE IF NOT EXISTS trend_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    request_id      TEXT,
    topic           TEXT NOT NULL,
    velocity        REAL,
    baseline_velocity REAL,
    anomaly_score   REAL,
    is_emerging     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trends_ts ON trend_snapshots(ts);

-- ============================================================
-- SOURCE CREDIBILITY SNAPSHOTS — source scores over time
-- ============================================================
CREATE TABLE IF NOT EXISTS credibility_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    request_id      TEXT,
    source_name     TEXT NOT NULL,
    reliability     REAL,
    accuracy        REAL,
    corroboration   REAL,
    items_seen      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cred_ts ON credibility_snapshots(ts);

-- ============================================================
-- EXPERT INFLUENCE SNAPSHOTS — expert performance over time
-- ============================================================
CREATE TABLE IF NOT EXISTS expert_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    request_id      TEXT,
    expert_id       TEXT NOT NULL,
    influence       REAL,
    accuracy        REAL,
    total_votes     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_expert_ts ON expert_snapshots(ts);

-- ============================================================
-- AGENT PERFORMANCE — per-agent stats per request
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_performance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    request_id      TEXT,
    agent_id        TEXT NOT NULL,
    candidates_produced INTEGER,
    candidates_selected INTEGER,
    latency_ms      REAL
);
CREATE INDEX IF NOT EXISTS idx_agentperf_agent ON agent_performance(agent_id);

-- ============================================================
-- SCHEMA VERSION
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY
);
"""


class AnalyticsDB:
    """Thread-safe SQLite analytics database.

    All write methods are fire-and-forget — errors are logged but never
    propagate to the caller, so analytics can never break the pipeline.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _conn(self) -> sqlite3.Connection:
        """Get thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._conn()
            conn.executescript(_SCHEMA_SQL)
            # Check/set version
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            conn.commit()
            self._initialized = True
            log.info("Analytics DB initialized at %s (schema v%d)", self._db_path, _SCHEMA_VERSION)

    def _safe_exec(self, sql: str, params: tuple = ()) -> None:
        """Execute SQL with error isolation — never raises."""
        try:
            conn = self._conn()
            conn.execute(sql, params)
            conn.commit()
        except Exception:
            log.exception("Analytics DB write failed: %s", sql[:100])

    def _safe_exec_many(self, sql: str, params_list: list[tuple]) -> None:
        """Execute many SQL statements with error isolation."""
        try:
            conn = self._conn()
            conn.executemany(sql, params_list)
            conn.commit()
        except Exception:
            log.exception("Analytics DB batch write failed: %s", sql[:100])

    # ──────────────────────────────────────────────────────────────
    # USER TRACKING
    # ──────────────────────────────────────────────────────────────

    def record_user_seen(self, user_id: str, chat_id: str | int | None = None) -> None:
        """Record a user being active. Creates or updates."""
        now = time.time()
        chat_str = str(chat_id) if chat_id else None
        self._safe_exec(
            """INSERT INTO users (user_id, chat_id, first_seen_at, last_active_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   last_active_at = excluded.last_active_at,
                   chat_id = COALESCE(excluded.chat_id, users.chat_id)""",
            (user_id, chat_str, now, now),
        )

    def increment_user_counter(self, user_id: str, field: str) -> None:
        """Increment a user counter (total_requests, total_briefings, etc.)."""
        allowed = {"total_requests", "total_briefings", "total_feedback", "total_ratings"}
        if field not in allowed:
            return
        self._safe_exec(
            f"UPDATE users SET {field} = {field} + 1 WHERE user_id = ?",
            (user_id,),
        )

    # ──────────────────────────────────────────────────────────────
    # INTERACTIONS
    # ──────────────────────────────────────────────────────────────

    def record_interaction(self, user_id: str, chat_id: str | int | None,
                           interaction_type: str, command: str | None,
                           args: str | None, raw_text: str | None,
                           result_action: str | None = None,
                           result_data: dict | None = None) -> None:
        """Record every single user interaction."""
        self._safe_exec(
            """INSERT INTO interactions
               (ts, user_id, chat_id, interaction_type, command, args, raw_text,
                result_action, result_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, str(chat_id) if chat_id else None,
             interaction_type, command, args, raw_text,
             result_action, json.dumps(result_data, default=str) if result_data else None),
        )
        self.record_user_seen(user_id, chat_id)

    # ──────────────────────────────────────────────────────────────
    # PIPELINE REQUESTS
    # ──────────────────────────────────────────────────────────────

    def record_request_start(self, request_id: str, user_id: str, prompt: str,
                             weighted_topics: dict, max_items: int) -> None:
        self._safe_exec(
            """INSERT INTO requests
               (request_id, user_id, prompt, weighted_topics, max_items, started_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (request_id, user_id, prompt,
             json.dumps(weighted_topics, default=str), max_items, time.time()),
        )
        self.increment_user_counter(user_id, "total_requests")

    def record_request_complete(self, request_id: str, candidate_count: int,
                                selected_count: int, briefing_type: str,
                                total_elapsed_s: float) -> None:
        self._safe_exec(
            """UPDATE requests SET completed_at = ?, candidate_count = ?,
               selected_count = ?, briefing_type = ?, total_elapsed_s = ?,
               status = 'completed'
               WHERE request_id = ?""",
            (time.time(), candidate_count, selected_count,
             briefing_type, total_elapsed_s, request_id),
        )

    def record_request_failed(self, request_id: str, error: str) -> None:
        self._safe_exec(
            "UPDATE requests SET status = 'failed', completed_at = ? WHERE request_id = ?",
            (time.time(), request_id),
        )

    # ──────────────────────────────────────────────────────────────
    # CANDIDATES
    # ──────────────────────────────────────────────────────────────

    def record_candidates(self, request_id: str, candidates: list,
                          selected_ids: set[str] | None = None) -> None:
        """Record all candidates from a research cycle."""
        selected_ids = selected_ids or set()
        rows = []
        for c in candidates:
            rows.append((
                request_id, c.candidate_id, c.title, c.source, c.topic,
                c.url, c.summary[:500] if c.summary else None,
                c.evidence_score, c.novelty_score, c.preference_fit,
                c.prediction_signal, c.composite_score(),
                c.discovered_by, c.urgency.value, c.lifecycle.value,
                json.dumps(c.regions), json.dumps(c.corroborated_by),
                c.contrarian_signal or None,
                c.created_at.isoformat() if c.created_at else None,
                1 if c.candidate_id in selected_ids else 0,
                "selected" if c.candidate_id in selected_ids else "not_selected",
            ))
        self._safe_exec_many(
            """INSERT INTO candidates
               (request_id, candidate_id, title, source, topic, url, summary,
                evidence_score, novelty_score, preference_fit, prediction_signal,
                composite_score, discovered_by, urgency, lifecycle, regions,
                corroborated_by, contrarian_signal, created_at, was_selected,
                selection_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    # ──────────────────────────────────────────────────────────────
    # EXPERT VOTES
    # ──────────────────────────────────────────────────────────────

    def record_expert_votes(self, request_id: str, votes: list) -> None:
        """Record all expert votes from a debate round."""
        rows = []
        for v in votes:
            rows.append((
                request_id, v.expert_id, v.candidate_id,
                1 if v.keep else 0, v.confidence,
                v.rationale, v.risk_note,
                1 if "arbitration" in v.rationale.lower() else 0,
            ))
        self._safe_exec_many(
            """INSERT INTO expert_votes
               (request_id, expert_id, candidate_id, keep, confidence,
                rationale, risk_note, arbitrated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    # ──────────────────────────────────────────────────────────────
    # BRIEFINGS & ITEMS
    # ──────────────────────────────────────────────────────────────

    def record_briefing(self, request_id: str, user_id: str, payload: Any) -> None:
        """Record a full briefing delivery with all items."""
        now = time.time()
        meta = payload.metadata if hasattr(payload, "metadata") else {}
        self._safe_exec(
            """INSERT INTO briefings
               (request_id, user_id, delivered_at, briefing_type, item_count,
                thread_count, geo_risk_count, emerging_trends, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request_id, user_id, now,
             payload.briefing_type.value if hasattr(payload.briefing_type, "value") else str(payload.briefing_type),
             len(payload.items), meta.get("thread_count", 0),
             meta.get("geo_risk_regions", 0), meta.get("emerging_trends", 0),
             json.dumps(meta, default=str)),
        )
        self.increment_user_counter(user_id, "total_briefings")

        # Record each item
        rows = []
        for idx, item in enumerate(payload.items, start=1):
            c = item.candidate
            conf = item.confidence
            rows.append((
                request_id, user_id, idx, c.candidate_id,
                c.title, c.source, c.topic, c.url,
                c.summary[:500] if c.summary else None,
                item.why_it_matters, item.what_changed,
                item.predictive_outlook,
                conf.low if conf else None,
                conf.mid if conf else None,
                conf.high if conf else None,
                item.thread_id, c.urgency.value, c.lifecycle.value,
                c.composite_score(), item.contrarian_note or None, now,
            ))
        self._safe_exec_many(
            """INSERT INTO briefing_items
               (request_id, user_id, item_index, candidate_id, title, source,
                topic, url, summary, why_it_matters, what_changed,
                predictive_outlook, confidence_low, confidence_mid,
                confidence_high, thread_id, urgency, lifecycle,
                composite_score, contrarian_note, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    # ──────────────────────────────────────────────────────────────
    # FEEDBACK & RATINGS
    # ──────────────────────────────────────────────────────────────

    def record_feedback(self, user_id: str, feedback_text: str,
                        changes: dict[str, str] | None = None) -> None:
        self._safe_exec(
            "INSERT INTO feedback (ts, user_id, feedback_text, changes_applied) VALUES (?, ?, ?, ?)",
            (time.time(), user_id, feedback_text,
             json.dumps(changes, default=str) if changes else None),
        )
        self.increment_user_counter(user_id, "total_feedback")

    def record_rating(self, user_id: str, item_index: int, direction: str,
                      topic: str | None = None, source: str | None = None,
                      title: str | None = None) -> None:
        self._safe_exec(
            "INSERT INTO ratings (ts, user_id, item_index, direction, topic, source, title) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), user_id, item_index, direction, topic, source, title),
        )
        self.increment_user_counter(user_id, "total_ratings")

    # ──────────────────────────────────────────────────────────────
    # PREFERENCE CHANGES
    # ──────────────────────────────────────────────────────────────

    def record_preference_change(self, user_id: str, change_type: str,
                                 field: str, old_value: Any, new_value: Any,
                                 source: str = "unknown") -> None:
        self._safe_exec(
            """INSERT INTO preference_changes
               (ts, user_id, change_type, field, old_value, new_value, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, change_type, field,
             json.dumps(old_value, default=str) if old_value is not None else None,
             json.dumps(new_value, default=str) if new_value is not None else None,
             source),
        )

    def record_profile_snapshot(self, user_id: str, profile_data: dict) -> None:
        """Take a full snapshot of a user's profile."""
        self._safe_exec(
            "INSERT INTO profile_snapshots (ts, user_id, profile_data) VALUES (?, ?, ?)",
            (time.time(), user_id, json.dumps(profile_data, default=str)),
        )

    # ──────────────────────────────────────────────────────────────
    # INTELLIGENCE SNAPSHOTS
    # ──────────────────────────────────────────────────────────────

    def record_georisk_snapshot(self, request_id: str, geo_risks: list) -> None:
        now = time.time()
        rows = []
        for gr in geo_risks:
            rows.append((
                now, request_id, gr.region, gr.risk_level,
                gr.previous_level, gr.escalation_delta,
                json.dumps(gr.drivers) if gr.drivers else None,
            ))
        self._safe_exec_many(
            """INSERT INTO georisk_snapshots
               (ts, request_id, region, risk_level, previous_level,
                escalation_delta, drivers)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def record_trend_snapshot(self, request_id: str, trends: list) -> None:
        now = time.time()
        rows = []
        for t in trends:
            rows.append((
                now, request_id, t.topic, t.velocity,
                t.baseline_velocity, t.anomaly_score,
                1 if t.is_emerging else 0,
            ))
        self._safe_exec_many(
            """INSERT INTO trend_snapshots
               (ts, request_id, topic, velocity, baseline_velocity,
                anomaly_score, is_emerging)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def record_credibility_snapshot(self, request_id: str, cred_data: dict) -> None:
        now = time.time()
        rows = []
        for source_name, data in cred_data.items():
            if not isinstance(data, dict):
                continue
            rows.append((
                now, request_id, source_name,
                data.get("reliability", 0), data.get("accuracy", 0),
                data.get("corroboration", 0), data.get("seen", 0),
            ))
        self._safe_exec_many(
            """INSERT INTO credibility_snapshots
               (ts, request_id, source_name, reliability, accuracy,
                corroboration, items_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def record_expert_snapshot(self, request_id: str, chair_data: dict) -> None:
        now = time.time()
        influence = chair_data.get("influence", {})
        accuracy = chair_data.get("accuracy", {})
        total_votes = chair_data.get("total_votes", {})
        rows = []
        for expert_id in influence:
            rows.append((
                now, request_id, expert_id,
                influence.get(expert_id, 1.0),
                accuracy.get(expert_id, 0.0),
                total_votes.get(expert_id, 0),
            ))
        self._safe_exec_many(
            """INSERT INTO expert_snapshots
               (ts, request_id, expert_id, influence, accuracy, total_votes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def record_agent_performance(self, request_id: str, agent_id: str,
                                 candidates_produced: int,
                                 candidates_selected: int = 0,
                                 latency_ms: float = 0.0) -> None:
        self._safe_exec(
            """INSERT INTO agent_performance
               (ts, request_id, agent_id, candidates_produced,
                candidates_selected, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), request_id, agent_id,
             candidates_produced, candidates_selected, latency_ms),
        )

    # ──────────────────────────────────────────────────────────────
    # ADMIN QUERIES
    # ──────────────────────────────────────────────────────────────

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a read query and return list of dicts."""
        try:
            conn = self._conn()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            conn.row_factory = None
            return [dict(r) for r in rows]
        except Exception:
            log.exception("Analytics query failed: %s", sql[:100])
            return []

    def get_all_users(self) -> list[dict]:
        return self._query(
            """SELECT user_id, chat_id, first_seen_at, last_active_at,
                      total_requests, total_briefings, total_feedback, total_ratings
               FROM users ORDER BY last_active_at DESC"""
        )

    def get_user_summary(self, user_id: str) -> dict | None:
        rows = self._query(
            """SELECT user_id, chat_id, first_seen_at, last_active_at,
                      total_requests, total_briefings, total_feedback, total_ratings
               FROM users WHERE user_id = ?""",
            (user_id,),
        )
        return rows[0] if rows else None

    def get_user_interactions(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT ts, interaction_type, command, args, raw_text, result_action
               FROM interactions WHERE user_id = ? ORDER BY ts DESC LIMIT ?""",
            (user_id, limit),
        )

    def get_user_briefings(self, user_id: str, limit: int = 20) -> list[dict]:
        return self._query(
            """SELECT request_id, delivered_at, briefing_type, item_count, metadata
               FROM briefings WHERE user_id = ? ORDER BY delivered_at DESC LIMIT ?""",
            (user_id, limit),
        )

    def get_user_ratings(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT ts, item_index, direction, topic, source, title
               FROM ratings WHERE user_id = ? ORDER BY ts DESC LIMIT ?""",
            (user_id, limit),
        )

    def get_user_preference_history(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT ts, change_type, field, old_value, new_value, source
               FROM preference_changes WHERE user_id = ? ORDER BY ts DESC LIMIT ?""",
            (user_id, limit),
        )

    def get_user_feedback_history(self, user_id: str, limit: int = 50) -> list[dict]:
        return self._query(
            """SELECT ts, feedback_text, changes_applied
               FROM feedback WHERE user_id = ? ORDER BY ts DESC LIMIT ?""",
            (user_id, limit),
        )

    def get_recent_requests(self, limit: int = 20) -> list[dict]:
        return self._query(
            """SELECT request_id, user_id, prompt, started_at, total_elapsed_s,
                      candidate_count, selected_count, briefing_type, status
               FROM requests ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        )

    def get_request_detail(self, request_id: str) -> dict:
        """Get full detail for a request including candidates and votes."""
        req = self._query("SELECT * FROM requests WHERE request_id = ?", (request_id,))
        candidates = self._query(
            "SELECT * FROM candidates WHERE request_id = ? ORDER BY composite_score DESC",
            (request_id,),
        )
        votes = self._query(
            "SELECT * FROM expert_votes WHERE request_id = ?", (request_id,),
        )
        items = self._query(
            "SELECT * FROM briefing_items WHERE request_id = ?", (request_id,),
        )
        return {
            "request": req[0] if req else None,
            "candidates": candidates,
            "votes": votes,
            "items": items,
        }

    def get_top_topics(self, days: int = 30, limit: int = 20) -> list[dict]:
        cutoff = time.time() - (days * 86400)
        return self._query(
            """SELECT topic, COUNT(*) as count,
                      AVG(composite_score) as avg_score,
                      SUM(was_selected) as times_selected
               FROM candidates WHERE created_at > ?
               GROUP BY topic ORDER BY count DESC LIMIT ?""",
            (datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(), limit),
        )

    def get_top_sources(self, days: int = 30, limit: int = 20) -> list[dict]:
        cutoff = time.time() - (days * 86400)
        return self._query(
            """SELECT source, COUNT(*) as total_candidates,
                      SUM(was_selected) as times_selected,
                      AVG(composite_score) as avg_score
               FROM candidates WHERE created_at > ?
               GROUP BY source ORDER BY total_candidates DESC LIMIT ?""",
            (datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(), limit),
        )

    def get_system_stats(self) -> dict:
        """High-level system statistics."""
        stats = {}
        for table, key in [
            ("users", "total_users"),
            ("interactions", "total_interactions"),
            ("requests", "total_requests"),
            ("candidates", "total_candidates"),
            ("expert_votes", "total_votes"),
            ("briefings", "total_briefings"),
            ("feedback", "total_feedback"),
            ("ratings", "total_ratings"),
        ]:
            rows = self._query(f"SELECT COUNT(*) as c FROM {table}")
            stats[key] = rows[0]["c"] if rows else 0
        return stats
