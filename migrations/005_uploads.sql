-- 005 uploads: files a user attached to a message. The bytes live on disk under
-- data/users/<user_id>/uploads/<uuid>/ with a generated name, so this table holds only the metadata:
-- what the file is, how big it is, which chat it belongs to, and what the model can read from it.
-- The extracted text is cached as a sidecar file (text.md) in the same folder, not here, so the
-- database stays small and a transcript stores attachment metadata only.
CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- The name as it should be shown, sanitized. Never a path: the path is the generated folder.
  name TEXT NOT NULL,
  stored TEXT NOT NULL,
  -- image | text | pdf | docx | other. Decided by content sniffing, not only by extension.
  kind TEXT NOT NULL DEFAULT 'other',
  media_type TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  -- The chat the file was attached to. The row is removed when that chat is deleted, and so is the
  -- folder on disk (see app.py).
  conversation_id TEXT,
  -- What the model can read from it: characters cached in text.md, whether that is the whole file,
  -- and the estimated cost of including it in a turn.
  text_chars INTEGER NOT NULL DEFAULT 0,
  truncated INTEGER NOT NULL DEFAULT 0,
  estimated_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_uploads_user ON uploads (user_id);
CREATE INDEX IF NOT EXISTS idx_uploads_conversation ON uploads (conversation_id);
