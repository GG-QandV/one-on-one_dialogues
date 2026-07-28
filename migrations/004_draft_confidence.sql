-- Миграция: служебные поля черновика (I2/I5 под новый DRAFT-промпт).
-- confidence, suggested_clarification — маркер умозаключения (случай B/C).
-- lang_ok — прошла ли языковая проверка после retry.
-- Номер файла (004) — сверить с фактической последовательностью в migrations/.

ALTER TABLE draft_answers ADD COLUMN confidence REAL;
ALTER TABLE draft_answers ADD COLUMN suggested_clarification TEXT;
ALTER TABLE draft_answers ADD COLUMN lang_ok INTEGER NOT NULL DEFAULT 1;
