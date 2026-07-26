-- migrations/003_draft_answers.sql
-- Draft features: copy audit, draft versioning
-- Добавляет на 001:
--   1. copied_at — метка времени доставки (G3 clipboard)
--   2. draft_source — какой провайдер сгенерировал ('gemini', 'claude', 'local')
--   3. version — поколение черновика (для supersede chain)

PRAGMA user_version = 3;

ALTER TABLE draft_answers ADD COLUMN copied_at TEXT;

ALTER TABLE draft_answers ADD COLUMN draft_source TEXT NOT NULL DEFAULT 'gemini';

ALTER TABLE draft_answers ADD COLUMN version INTEGER NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_drafts_unanswered
    ON draft_answers(session_id, status, created_at DESC);
