-- Migration 001: Add state_kv table for cross-run state persistence
CREATE TABLE IF NOT EXISTS state_kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
