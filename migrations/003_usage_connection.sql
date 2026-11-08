-- 003: every usage row names the connection it came from, and keeps the namespaced model id.
-- Rows written before this stay as they are, with connection_id NULL.
ALTER TABLE usage_events ADD COLUMN connection_id INTEGER REFERENCES connections(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_usage_connection ON usage_events (connection_id);
