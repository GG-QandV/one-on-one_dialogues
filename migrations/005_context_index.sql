-- Миграция: индекс под горячий путь D6 build_context и TRANSLATE-обработчик.
-- Фильтр segments по (stream_id, track, t_start_ms). Без составного
-- индекса — скан таблицы к середине часовой сессии.

CREATE INDEX IF NOT EXISTS idx_segments_context
    ON segments(stream_id, track, t_start_ms);
