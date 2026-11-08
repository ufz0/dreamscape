-- 002 connections: the inference endpoints an admin manages, and the models each one reports.
-- api_key and the header values are stored encrypted (see cipher.py); nothing else here is secret.
CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  preset TEXT NOT NULL DEFAULT 'custom',
  base_url TEXT NOT NULL,
  api_key TEXT NOT NULL DEFAULT '',
  headers TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  visibility TEXT NOT NULL DEFAULT 'all',
  timeout_seconds INTEGER NOT NULL DEFAULT 120,
  max_concurrent INTEGER NOT NULL DEFAULT 2,
  body_overrides TEXT NOT NULL DEFAULT '{}',
  path_models TEXT NOT NULL DEFAULT '/models',
  path_chat TEXT NOT NULL DEFAULT '/chat/completions',
  reasoning_map TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL,
  last_test TEXT,
  test_result TEXT NOT NULL DEFAULT '{}'
);

-- Model ids are namespaced as <connection slug>/<model id> when they are used outside this table,
-- so two engines that both call a model "gpt-4o" cannot collide.
CREATE TABLE IF NOT EXISTS connection_models (
  id INTEGER PRIMARY KEY,
  connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  hidden INTEGER NOT NULL DEFAULT 0,
  -- NULL means "not known". A 0 or 1 is what the admin set by hand, and a test result only fills
  -- the unknowns; it never replaces a value the admin chose.
  supports_vision INTEGER,
  supports_tools INTEGER,
  supports_reasoning INTEGER,
  supports_usage INTEGER,
  context_length INTEGER,
  body_overrides TEXT NOT NULL DEFAULT '',
  reasoning_map TEXT NOT NULL DEFAULT '',
  added TEXT NOT NULL,
  last_seen TEXT,
  UNIQUE (connection_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_connection_models ON connection_models (connection_id);
