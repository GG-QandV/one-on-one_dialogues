# Миграции speech-local

Порядок применения — строго последовательный, только вперёд.

## Правила

1. Миграции применяются однократно, в порядке нумерации.
2. Откат миграции (rollback) не предусмотрен архитектурой.
   При необходимости — новая миграция, отменяющая изменения.
3. Каждая миграция — идемпотентна: `INSERT OR IGNORE` / `IF NOT EXISTS`.
4. `user_version` обновляется после каждой миграции.

## Применение

```bash
sqlite3 data/speech.db < migrations/001_initial.sql
sqlite3 data/speech.db < migrations/002_privacy_profile.sql
sqlite3 data/speech.db < migrations/003_draft_answers.sql
```

Или через `app/db.py`:

```python
from app.db import Database
db = Database(DbConfig(db_path="data/speech.db"))
await db.migrate()
```

## Текущая схема

| Миграция | Версия | Описание |
|----------|--------|----------|
| 001      | 1      | Начальная: sessions, audio_streams, segments, draft_answers, jobs, privacy_audit_log + триггеры |
| 002      | 2      | Expanded privacy: fencing token, teardown metrics |
| 003      | 3      | Draft features: gap tracking, copy audit |
