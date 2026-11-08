-- 004 runs: a chat turn is a background task, so it is not tied to the request that started it.
-- The events of a live run are kept in a bounded in-memory buffer (see runs.py); this table is the
-- record of what happened and of what a client should do when it comes back.
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  started TEXT NOT NULL,
  finished TEXT,
  error TEXT NOT NULL DEFAULT '',
  -- Stays 0 until the user has seen the result, which is what the dot in the sidebar reads.
  seen INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_user_status ON runs (user_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_conversation ON runs (conversation_id);
