-- migrations/001_initial.sql
-- Начальная схема speech-local v2.0
-- Спека: раздел 8 «Схема данных», инварианты 1-8.
--
-- Инварианты неизменяемости продублированы на уровне СУБД триггерами.
-- Прикладная проверка в app/db.py — первый рубеж, триггеры — последний.
-- Обход прикладного слоя (ручной sqlite3, миграция, скрипт) не должен
-- приводить к порче raw_text или privacy_profile.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- sessions

CREATE TABLE sessions (
    id                      TEXT PRIMARY KEY,
    started_at              TEXT NOT NULL,
    ended_at                TEXT,
    meeting_title           TEXT,
    status                  TEXT NOT NULL
                            CHECK (status IN ('active', 'paused', 'finished', 'aborted')),
    default_privacy_profile TEXT NOT NULL
                            CHECK (default_privacy_profile IN ('open', 'confidential')),
    library_context_id      TEXT REFERENCES library_contexts(id) ON DELETE SET NULL,
    translation_provider    TEXT,
    draft_provider          TEXT,
    mode                    TEXT NOT NULL DEFAULT 'live_safe'
                            CHECK (mode IN ('live_literal', 'live_safe', 'post_clean'))
);

CREATE INDEX idx_sessions_status ON sessions(status, started_at DESC);

-- ----------------------------------------------------------- audio_streams

CREATE TABLE audio_streams (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('meeting', 'microphone')),
    source_language TEXT NOT NULL,
    target_language TEXT NOT NULL,
    pipewire_node   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    priority        TEXT NOT NULL DEFAULT 'secondary'
                    CHECK (priority IN ('primary', 'secondary')),
    UNIQUE (session_id, role)
);

CREATE INDEX idx_streams_session ON audio_streams(session_id);

-- --------------------------------------------------------- library_contexts

CREATE TABLE library_contexts (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    domain         TEXT,
    content_text   TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- ---------------------------------------------------------------- segments

CREATE TABLE segments (
    id                     TEXT PRIMARY KEY,
    session_id             TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    stream_id              TEXT NOT NULL REFERENCES audio_streams(id) ON DELETE CASCADE,

    t_start_ms             INTEGER NOT NULL,
    t_end_ms               INTEGER NOT NULL,
    local_audio_path       TEXT,          -- NULL после удаления по retention

    -- Профиль на момент обработки. НЕИЗМЕНЯЕМ (trigger trg_segments_immutable).
    privacy_profile        TEXT NOT NULL
                           CHECK (privacy_profile IN ('open', 'confidential')),

    -- fast  — быстрый трек, черновик, НЕ идёт в экспорт и не имеет raw_text
    -- accurate — точный трек, локальный whisper, источник raw_text
    track                  TEXT NOT NULL CHECK (track IN ('fast', 'accurate')),

    stt_model              TEXT,
    detected_language      TEXT,

    -- Заполняется ТОЛЬКО локальным whisper и ТОЛЬКО для track='accurate'.
    -- НЕИЗМЕНЯЕМ после первой непустой записи.
    raw_text               TEXT,
    stt_confidence         REAL,          -- усреднённый avg_logprob по токенам

    translation_status     TEXT NOT NULL DEFAULT 'pending'
                           CHECK (translation_status IN
                                  ('pending', 'running', 'done', 'failed', 'skipped')),
    translation_raw        TEXT,
    translation_clean      TEXT,
    edit_log_json          TEXT,

    -- Для черновика быстрого трека: ссылка на заместивший его точный сегмент.
    superseded_by_segment_id TEXT REFERENCES segments(id) ON DELETE SET NULL,

    created_at             TEXT NOT NULL,

    -- Инвариант 3 спеки: быстрый трек не имеет raw_text.
    CHECK (track = 'accurate' OR raw_text IS NULL),
    CHECK (t_end_ms >= t_start_ms)
);

CREATE INDEX idx_segments_session_time  ON segments(session_id, t_start_ms);
CREATE INDEX idx_segments_stream        ON segments(stream_id, t_start_ms);
CREATE INDEX idx_segments_track         ON segments(session_id, track);
CREATE INDEX idx_segments_translation   ON segments(translation_status)
                                        WHERE translation_status IN ('pending', 'failed');

-- ----------------------------------------------------------- draft_answers

CREATE TABLE draft_answers (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    trigger_segment_id  TEXT NOT NULL REFERENCES segments(id) ON DELETE CASCADE,

    draft_ru            TEXT NOT NULL,
    draft_translated    TEXT,
    target_language     TEXT NOT NULL,

    sources_json        TEXT NOT NULL DEFAULT '[]',
    has_gaps            INTEGER NOT NULL DEFAULT 0 CHECK (has_gaps IN (0, 1)),
    gap_note            TEXT,

    status              TEXT NOT NULL DEFAULT 'generated'
                        CHECK (status IN ('generated', 'ignored', 'copied')),
    created_at          TEXT NOT NULL
);

CREATE INDEX idx_drafts_session ON draft_answers(session_id, created_at DESC);
CREATE UNIQUE INDEX idx_drafts_trigger ON draft_answers(trigger_segment_id);

-- -------------------------------------------------------------------- jobs

CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL
                     CHECK (type IN ('stt', 'translate', 'draft', 'export')),
    segment_id       TEXT REFERENCES segments(id) ON DELETE CASCADE,
    payload_json     TEXT NOT NULL DEFAULT '{}',

    status           TEXT NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),

    -- Автоматический ретрай разрешён только для идемпотентных задач.
    idempotent       INTEGER NOT NULL DEFAULT 1 CHECK (idempotent IN (0, 1)),
    idempotency_key  TEXT,

    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    next_attempt_at  TEXT,

    -- Аренда: если воркер умер, аренда истекает и задача возвращается в queued.
    lease_owner      TEXT,
    lease_expires_at TEXT,

    -- Профиль, под которым задача поставлена. Задача, поставленная в открытом
    -- профиле и требующая облака, не выполняется после перехода в закрытый.
    privacy_profile  TEXT NOT NULL
                     CHECK (privacy_profile IN ('open', 'confidential')),
    -- Поколение профиля (fencing token), см. app/privacy.py.
    privacy_gen      INTEGER NOT NULL DEFAULT 0,

    error_code       TEXT,
    error_detail     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_jobs_idem ON jobs(idempotency_key)
       WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_jobs_pick ON jobs(status, type, next_attempt_at);
CREATE INDEX idx_jobs_lease ON jobs(status, lease_expires_at) WHERE status = 'running';
CREATE INDEX idx_jobs_segment ON jobs(segment_id);

-- ------------------------------------------------------- privacy_audit_log

-- Журнал переключений профиля. Нужен для приёмки (критерий 7 спеки) и разбора
-- инцидентов: под каким профилем работала система в каждый момент времени.
CREATE TABLE privacy_audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    at            TEXT NOT NULL,
    from_profile  TEXT,
    to_profile    TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    teardown_ms   INTEGER,                 -- фактическое время закрытия облака
    reason        TEXT
);

CREATE INDEX idx_privacy_audit_session ON privacy_audit_log(session_id, at);

-- ============================================================== ТРИГГЕРЫ

-- Инвариант 1: raw_text неизменяем после первой непустой записи.
CREATE TRIGGER trg_segments_raw_text_immutable
BEFORE UPDATE OF raw_text ON segments
FOR EACH ROW
WHEN OLD.raw_text IS NOT NULL AND OLD.raw_text <> ''
     AND (NEW.raw_text IS NULL OR NEW.raw_text <> OLD.raw_text)
BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_RAW_TEXT');
END;

-- Инвариант 2: privacy_profile неизменяем.
CREATE TRIGGER trg_segments_privacy_immutable
BEFORE UPDATE OF privacy_profile ON segments
FOR EACH ROW
WHEN NEW.privacy_profile <> OLD.privacy_profile
BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_PRIVACY_PROFILE');
END;

-- Трек сегмента неизменяем: fast не может «стать» accurate.
CREATE TRIGGER trg_segments_track_immutable
BEFORE UPDATE OF track ON segments
FOR EACH ROW
WHEN NEW.track <> OLD.track
BEGIN
    SELECT RAISE(ABORT, 'IMMUTABLE_TRACK');
END;

-- Инвариант 3: результат быстрого трека не попадает в raw_text.
CREATE TRIGGER trg_segments_fast_no_raw_insert
BEFORE INSERT ON segments
FOR EACH ROW
WHEN NEW.track = 'fast' AND NEW.raw_text IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'FAST_TRACK_CANNOT_WRITE_RAW_TEXT');
END;

CREATE TRIGGER trg_segments_fast_no_raw_update
BEFORE UPDATE OF raw_text ON segments
FOR EACH ROW
WHEN NEW.track = 'fast' AND NEW.raw_text IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'FAST_TRACK_CANNOT_WRITE_RAW_TEXT');
END;

-- Замещать можно только черновик и только точным сегментом.
CREATE TRIGGER trg_segments_supersede_direction
BEFORE UPDATE OF superseded_by_segment_id ON segments
FOR EACH ROW
WHEN NEW.superseded_by_segment_id IS NOT NULL
     AND (OLD.track <> 'fast'
          OR (SELECT track FROM segments WHERE id = NEW.superseded_by_segment_id)
             <> 'accurate')
BEGIN
    SELECT RAISE(ABORT, 'INVALID_SUPERSEDE_DIRECTION');
END;

-- Инвариант 5: черновик ответа не может ссылаться на сегмент чужой сессии.
CREATE TRIGGER trg_drafts_same_session
BEFORE INSERT ON draft_answers
FOR EACH ROW
WHEN (SELECT session_id FROM segments WHERE id = NEW.trigger_segment_id)
     <> NEW.session_id
BEGIN
    SELECT RAISE(ABORT, 'DRAFT_SESSION_MISMATCH');
END;

PRAGMA user_version = 1;
