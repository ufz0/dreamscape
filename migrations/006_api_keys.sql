-- 006 api keys: the OpenAI-compatible API (/api/v1) is entered with a Bearer key, not a session
-- cookie. A key is stored only as its SHA-256 hash; the first 8 characters are kept so the UI can
-- show something recognisable. The key itself is shown once, at creation.
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  prefix TEXT NOT NULL,
  token_hash TEXT NOT NULL,
  created TEXT NOT NULL,
  -- Optional. An empty expiry means the key does not expire.
  expires TEXT,
  last_used TEXT,
  revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);

-- API access is per user as well as app-wide. A key stops working the moment its user is disabled
-- or deleted, so this is checked at use time, not stored on the key.
ALTER TABLE users ADD COLUMN api_access INTEGER NOT NULL DEFAULT 1;

-- Which key a call came through. Kept when the key is revoked or the user is deleted, so the usage
-- numbers for a period do not change.
ALTER TABLE usage_events ADD COLUMN key_id TEXT;

CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events (key_id);
