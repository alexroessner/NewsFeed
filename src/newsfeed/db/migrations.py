"""Simple SQL migration runner for NewsFeed.

Tracks schema versions and applies numbered migration files in order.
Works with both local SQLite and Cloudflare D1 backends.

Migration files live in db/migrations/ as numbered SQL files:
    001_initial_schema.sql
    002_add_state_kv.sql
    003_add_indexes.sql

Each migration runs exactly once and is recorded in the schema_migrations table.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  REAL NOT NULL
);
"""


class MigrationRunner:
    """Applies SQL migrations in order, tracking which have been applied."""

    def __init__(self, db: Any) -> None:
        """Initialize with an AnalyticsDB or compatible DB wrapper."""
        self._db = db
        self._init_tracking_table()

    def _execute(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute SQL against the underlying backend."""
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
            log.warning("Migration SQL failed: %s", sql[:80], exc_info=True)
            return []

    def _execute_script(self, script: str) -> None:
        """Execute a multi-statement SQL script."""
        if hasattr(self._db, '_d1') and self._db._d1:
            self._db._d1.execute_script(script)
        elif hasattr(self._db, '_local'):
            conn = getattr(self._db._local, 'conn', None)
            if conn:
                conn.executescript(script)
                conn.commit()

    def _init_tracking_table(self) -> None:
        """Create the migration tracking table if needed."""
        try:
            self._execute_script(_MIGRATION_TABLE_SQL)
        except Exception:
            log.warning("Failed to create schema_migrations table", exc_info=True)

    def applied_versions(self) -> set[int]:
        """Return set of already-applied migration version numbers."""
        rows = self._execute("SELECT version FROM schema_migrations")
        return {int(r["version"]) for r in rows}

    def pending_migrations(self) -> list[tuple[int, str, Path]]:
        """Find migration files that haven't been applied yet."""
        if not _MIGRATIONS_DIR.exists():
            return []

        applied = self.applied_versions()
        pending = []

        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            # Extract version number from filename: "001_initial_schema.sql" -> 1
            name = path.stem
            parts = name.split("_", 1)
            try:
                version = int(parts[0])
            except ValueError:
                continue
            migration_name = parts[1] if len(parts) > 1 else name
            if version not in applied:
                pending.append((version, migration_name, path))

        return sorted(pending, key=lambda x: x[0])

    def apply_all(self) -> int:
        """Apply all pending migrations. Returns count applied."""
        pending = self.pending_migrations()
        if not pending:
            log.info("No pending migrations")
            return 0

        applied = 0
        for version, name, path in pending:
            try:
                sql = path.read_text(encoding="utf-8")
                log.info("Applying migration %03d: %s", version, name)
                self._execute_script(sql)
                self._execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, time.time()),
                )
                applied += 1
                log.info("Migration %03d applied successfully", version)
            except Exception:
                log.error("Migration %03d failed: %s", version, name, exc_info=True)
                break  # Stop on first failure

        return applied

    def current_version(self) -> int:
        """Return the highest applied migration version, or 0."""
        rows = self._execute("SELECT MAX(version) as v FROM schema_migrations")
        if rows and rows[0].get("v") is not None:
            return int(rows[0]["v"])
        return 0
