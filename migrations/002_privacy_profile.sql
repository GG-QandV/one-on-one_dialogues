-- migrations/002_privacy_profile.sql
-- Expanded privacy: fencing generation + teardown metrics
-- Добавляет на 001:
--   1. privacy_generation в sessions — текущее поколение fencing (privacy.py)
--   2. Индекс по generation для быстрых запросов аудита

PRAGMA user_version = 2;

ALTER TABLE sessions ADD COLUMN privacy_generation INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_privacy_audit_generation
    ON privacy_audit_log(session_id, generation DESC);
