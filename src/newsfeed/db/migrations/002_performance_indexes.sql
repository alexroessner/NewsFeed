-- Migration 002: Add performance indexes for common queries
CREATE INDEX IF NOT EXISTS idx_interactions_cmd ON interactions(command);
CREATE INDEX IF NOT EXISTS idx_briefings_user ON briefings(user_id);
CREATE INDEX IF NOT EXISTS idx_candidates_request ON candidates(request_id);
CREATE INDEX IF NOT EXISTS idx_expert_votes_request ON expert_votes(request_id);
