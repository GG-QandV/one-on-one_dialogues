# Патч H1 — обработчик JobType.TRANSLATE (финальный, сверен по коду)

Применять в `app/main.py`. Все 5 точек сверки раннего наброска разрешены
по фактическому коду; две правки против наброска:
- языки — JOIN `segments`→`audio_streams` (НЕ конфиг, НЕ stream_id==role);
- UI — `ui_server.publish(EventType.SEGMENT_TRANSLATED, data)`, синхронный
  вызов (не `emit`, не `await`), payload по §7.1.

Новых файлов нет — только правки main.py. Тесты (позитив + 3 негатива)
прогоняешь у себя: исполняемого проекта в моей среде нет.

---

## Что уже есть в коде (фаза 0, подтверждено)

- `_on_stt_result` ставит `enqueue(JobType.TRANSLATE, segment_id=..., idempotency_key="tr:...")` — связка STT→TRANSLATE готова.
- `enqueue` фиксирует fence автоматически (`Job.fence` из privacy_profile+privacy_gen).
- Зарегистрирован только `JobType.STT`. **TRANSLATE-обработчик отсутствует — дыра H1.**
- Провайдер/gate/KeyStore в `start()` не создаются.

---

## 1. Импорты (шапка main.py)

ДОБАВИТЬ (сверить, что часть уже импортирована):
```python
from app.errors import StaleGenerationError, ProviderError
from app.translation.base import (
    TranslationRequest, TranslationMode, TranslationProvider,
)
from app.translation.offline import OfflineGate, OfflineConfig
from app.translation.providers.gemini_text import GeminiTextProvider
from app.translation.providers.claude_text import ClaudeTextProvider, ClaudeConfig
from app.security.byok import KeyStore
from app.ui.server import EventType
import sqlite3
```

## 2. Атрибуты в `__init__`

ДОБАВИТЬ:
```python
        self.keystore: KeyStore | None = None
        self.offline: OfflineGate | None = None
        self._provider: TranslationProvider | None = None
```

## 3. Создание + регистрация в `start()`

Вставить рядом с созданием очереди и регистрацией STT, ДО `jobs.start()`
(register обязан быть до старта воркеров):

```python
        # BYOK-хранилище ключей (RAM, TTL 60 мин).
        self.keystore = KeyStore()
        # Gate доступности облака (D7).
        self.offline = OfflineGate(OfflineConfig())
        # Текстовый провайдер перевода.
        self._provider = self._build_provider()
        # Обработчик перевода — рядом с register(JobType.STT, ...).
        self.jobs.register(JobType.TRANSLATE, self._handle_translate)
```

## 4. Выбор провайдера

ДОБАВИТЬ метод. `key_provider` — замыкание над `keystore.get` (D1 п.2):

```python
    def _build_provider(self) -> TranslationProvider:
        assert self.privacy and self.keystore
        active = getattr(self._cfg, "translation_active", "gemini")
        if active == "claude":
            return ClaudeTextProvider(
                privacy=self.privacy,
                key_provider=lambda: self.keystore.get("claude"),
                config=ClaudeConfig(),
            )
        return GeminiTextProvider(
            privacy=self.privacy,
            key_provider=lambda: self.keystore.get("gemini"),
        )
```

> Точка сверки: имя поля активного провайдера в AppConfig (`translation_active`
> — предположение). Есть также `sessions.translation_provider` в БД: если
> провайдер выбирается per-session, брать оттуда. Для MVP — один на app.

## 5. Обработчик TRANSLATE (ядро)

Контракт queue.py: успех = без исключения; провал = исключение (ретрай по
`exc.retryable`); статус задачи обработчик не трогает. fence — из `job.fence`,
не перезахватывать.

```python
    async def _handle_translate(self, job) -> None:
        assert self.db and self.offline and self._provider
        segment_id = job.segment_id
        if not segment_id:
            return

        provider = self._provider

        # 1. Gate: провайдер доступен?
        if not self.offline.should_attempt(provider.name):
            # Не долбить упавший провайдер. Отложить — догонит после
            # mark_available; не завершать «успехом» без результата.
            await self.jobs.enqueue(
                JobType.TRANSLATE, segment_id=segment_id,
                idempotency_key=f"tr:{segment_id}", delay_s=30.0,
            )
            return

        # 2. Вход: raw_text + языки. Языки — из audio_streams через JOIN.
        loaded = await self._load_translate_input(segment_id)
        if loaded is None:
            return
        raw_text, source_lang, target_lang = loaded

        req = TranslationRequest(
            text=raw_text,
            source_language=source_lang,
            target_language=target_lang,
            mode=TranslationMode.LIVE_LITERAL,   # accurate-трек, сохранение сущностей
            context=(),                          # TODO(D6): подключить ContextBuilder
            segment_id=segment_id,
        )

        # 3. Вызов провайдера. fence ИЗ JOB.
        try:
            result = await provider.translate(req, fence=job.fence)
        except StaleGenerationError:
            return                               # профиль сменился — тихо, без записи
        except ProviderError as exc:
            self.offline.mark_unavailable(provider.name, exc)
            raise                                # queue решит ретрай по exc.retryable

        # 4. Успех.
        self.offline.mark_available(provider.name)

        # 5. Запись. translation_status='done' — в допустимых значениях схемы.
        raw = result.translation_raw
        clean = result.translation_clean

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE segments SET translation_raw = ?, translation_clean = ?, "
                "translation_status = 'done' WHERE id = ?",
                (raw, clean, segment_id),
            )
        await self.db.write(_tx)

        # 6. UI. publish — СИНХРОННЫЙ, не async. Payload по §7.1.
        if self.ui_server:
            self.ui_server.publish(
                EventType.SEGMENT_TRANSLATED,
                {
                    "segment_id": segment_id,
                    "translation": clean or raw,
                    "mode": req.mode.value,
                    "superseded_ids": [],
                },
            )
```

## 6. Загрузка входа перевода (JOIN audio_streams)

ДОБАВИТЬ метод. Языки берутся из потока, к которому привязан сегмент:

```python
    async def _load_translate_input(
        self, segment_id: str
    ) -> tuple[str, str, str] | None:
        assert self.db
        row = await self.db.fetch_one(
            "SELECT s.raw_text AS raw_text, "
            "       st.source_language AS src, "
            "       st.target_language AS tgt "
            "FROM segments s "
            "JOIN audio_streams st ON st.id = s.stream_id "
            "WHERE s.id = ?",
            (segment_id,),
        )
        if row is None:
            return None
        raw_text = row["raw_text"]
        if not raw_text or not raw_text.strip():
            return None                          # accurate ещё без raw_text — нечего
        return (raw_text.strip(), row["src"], row["tgt"])
```

## 7. Догон после простоя облака (опционально, D7)

`mark_available` в обработчике уже поднимает провайдер. Для добора
`pending`/`failed` сегментов — вызвать `catch_up` в старте сессии:

```python
        # в start_session, после поднятия захвата:
        if self.offline:
            await self.offline.catch_up(self.db, self.jobs)
```

---

## Границы (не в H1)

- DRAFT-обработчик — не здесь (отдельная ветка, промпт-цепочка уже готова).
- Fallback между провайдерами — не MVP.
- Статус задачи в jobs из обработчика — не трогать.
- `context=()` временно; D6 ContextBuilder — отдельным шагом.
- Провайдер per-session (`sessions.translation_provider`) — tier 2.

## Тесты H1 (фикстуры)

Позитив:
1. сегмент accurate с raw_text + fake-провайдер → `translation_raw`/`clean`
   в БД, `translation_status='done'`, gate AVAILABLE, событие
   `segment.translated` опубликовано.

Негативы (3):
2. провайдер бросает `ProviderUnavailable` → `mark_unavailable`, статус НЕ
   'done', исключение пробрасывается (ретрай);
3. switch профиля между enqueue и translate → `StaleGenerationError` →
   перевод НЕ записан, задача завершена тихо;
4. gate BLOCKED (после `ProviderAuthError`) → `should_attempt=False` →
   провайдер не вызван, задача отложена (delay_s).

Критерий: весь набор зелёный вместе, число названо.

## Точки сверки при применении (проверки, не решения)

| Что | Где |
| :-- | :-- |
| Имя `self.ui_server` в Application | main.py __init__/start |
| `translation_active` в AppConfig | config.py B1 |
| `provider.translate(req, *, fence)` сигнатура | base.py D1 (подтверждена) |
| `enqueue(..., delay_s=)` параметр | queue.py — есть ли delay_s |
| `result.translation_clean` у провайдера перевода | D2/D3 — заполняется ли clean для LIVE_LITERAL |
