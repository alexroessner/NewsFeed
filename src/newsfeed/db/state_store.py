"""Persistent state store — saves preferences, credibility, trends, etc. to D1 or SQLite.

Replaces the JSON file-based StatePersistence for production use.
State survives across GitHub Actions runs when using Cloudflare D1.

The store uses a simple key-value schema:
    state_kv(key TEXT PRIMARY KEY, value TEXT, updated_at REAL)

Values are JSON-serialized dicts. Keys are validated with the same regex
as StatePersistence to prevent injection.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

_VALID_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


class D1StateStore:
    """Key-value state store backed by D1 or local SQLite.

    Drop-in replacement for StatePersistence that persists across
    ephemeral GitHub Actions runs when Cloudflare D1 is configured.
    """

    def __init__(self, db: Any) -> None:
        """Initialize with an AnalyticsDB instance (which wraps D1 or SQLite)."""
        self._db = db
        self._init_schema()

    def _init_schema(self) -> None:
        """Create the state_kv table if it doesn't exist."""
        try:
            if hasattr(self._db, '_d1') and self._db._d1:
                self._db._d1.execute_script(_STATE_SCHEMA)
            elif hasattr(self._db, '_local'):
                conn = getattr(self._db._local, 'conn', None)
                if conn:
                    conn.executescript(_STATE_SCHEMA)
                    conn.commit()
        except Exception:
            log.warning("Failed to initialize state_kv schema", exc_info=True)

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute SQL against the underlying DB backend."""
        try:
            if hasattr(self._db, '_d1') and self._db._d1:
                return self._db._d1.execute(sql, params)
            elif hasattr(self._db, '_local'):
                conn = getattr(self._db._local, 'conn', None)
                if conn:
                    cursor = conn.execute(sql, params)
                    if cursor.description:
                        cols = [d[0] for d in cursor.description]
                        return [dict(zip(cols, row)) for row in cursor.fetchall()]
                    conn.commit()
            return []
        except Exception:
            log.warning("State store SQL failed: %s", sql[:80], exc_info=True)
            return []

    def save(self, key: str, data: dict) -> None:
        """Save a state dict under a key."""
        if not _VALID_KEY_RE.match(key):
            log.warning("Invalid state key rejected: %r", key)
            return
        value = json.dumps(data, default=str)
        now = time.time()
        self._execute(
            "INSERT OR REPLACE INTO state_kv (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, now),
        )

    def save_many(self, items: dict[str, dict]) -> None:
        """Save multiple state dicts in a single batched call.

        Much faster than calling save() in a loop when using D1, because
        it sends all writes in one HTTP request instead of one per key.
        """
        if not items:
            return
        now = time.time()
        params_list: list[tuple] = []
        for key, data in items.items():
            if not _VALID_KEY_RE.match(key):
                log.warning("Invalid state key rejected: %r", key)
                continue
            value = json.dumps(data, default=str)
            params_list.append((key, value, now))

        if not params_list:
            return

        # Use execute_many for batching on D1
        try:
            if hasattr(self._db, '_d1') and self._db._d1:
                self._db._d1.execute_many(
                    "INSERT OR REPLACE INTO state_kv (key, value, updated_at) VALUES (?, ?, ?)",
                    params_list,
                )
            elif hasattr(self._db, '_local'):
                conn = getattr(self._db._local, 'conn', None)
                if conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO state_kv (key, value, updated_at) VALUES (?, ?, ?)",
                        params_list,
                    )
                    conn.commit()
        except Exception:
            log.warning("Batch state save failed, falling back to individual saves", exc_info=True)
            for key, data in items.items():
                try:
                    self.save(key, data)
                except Exception:
                    pass

    def load(self, key: str) -> dict | None:
        """Load a state dict by key. Returns None if not found."""
        if not _VALID_KEY_RE.match(key):
            return None
        rows = self._execute("SELECT value FROM state_kv WHERE key = ?", (key,))
        if rows:
            try:
                return json.loads(rows[0]["value"])
            except (json.JSONDecodeError, KeyError, TypeError):
                return None
        return None

    def delete(self, key: str) -> None:
        """Delete a state entry."""
        if _VALID_KEY_RE.match(key):
            self._execute("DELETE FROM state_kv WHERE key = ?", (key,))

    def keys(self) -> list[str]:
        """List all stored keys."""
        rows = self._execute("SELECT key FROM state_kv ORDER BY key")
        return [r["key"] for r in rows]
