# PATCH H1 — обработчик JobType.TRANSLATE

Уровень: Senior. Патч к `app/main.py`. Дописывает то, чего в каркасе нет:
создание провайдера/gate/KeyStore и обработчик перевода. Связка
STT→TRANSLATE (`_on_stt_result` → `enqueue(JobType.TRANSLATE, ...)`) уже
есть в коде — её не трогаем.

Все API сверены по коду репозитория (offline.py D7, base.py D1,
gemini_text.py D2, claude_text.py D3, byok.py G2, queue.py).

---

## Фаза 0 — итог чтения main.py (факт по коду)

| Пункт                         | Факт                                                      |
|:----------------------------- |:--------------------------------------------------------- |
| `register(JobType.TRANSLATE)` | ОТСУТСТВУЕТ (есть только STT) — дырка H1                  |
| STT ставит TRANSLATE          | ЕСТЬ: `_on_stt_result` при непустом тексте                |
| fence                         | фиксируется автоматически в `enqueue`; читаем `job.fence` |
| Провайдер / gate / KeyStore   | НЕ создаются в `start()` — добавляем                      |

---

## Патч 1 — импорты (шапка main.py)

```python
from app.privacy import Capability, PrivacyController, PrivacyProfile
from app.errors import StaleGenerationError, ProviderError
from app.queue import JobQueue, JobType, QueueConfig  # уже есть; убедиться в Job
from app.translation.base import (
    TranslationRequest,
    TranslationMode,
    TranslationProvider,
)
from app.translation.offline import OfflineGate, OfflineConfig
from app.translation.providers.gemini_text import GeminiTextProvider
from app.translation.providers.claude_text import ClaudeTextProvider, ClaudeConfig
from app.security.byok import KeyStore
```

---

## Патч 2 — атрибуты в `__init__`

```python
        self.keystore: KeyStore | None = None
        self.offline: OfflineGate | None = None
        self._provider: TranslationProvider | None = None
```

---

## Патч 3 — создание в `start()` (после шага 3 «очередь задач», ДО register)

Ставится сразу за созданием `self.jobs`, но регистрацию TRANSLATE делаем
ЗДЕСЬ же, рядом с регистрацией STT.

```python
        # 3b. BYOK-хранилище ключей (только RAM, TTL 60 мин).
        self.keystore = KeyStore()

        # 3c. Gate доступности облака (D7): не долбить упавший провайдер.
        self.offline = OfflineGate(OfflineConfig())

        # 3d. Текстовый провайдер перевода по конфигу.
        self._provider = self._build_provider()

        # 3e. Регистрация обработчика перевода — рядом с STT.
        self.jobs.register(JobType.TRANSLATE, self._handle_translate)
```

Порядок §8 не нарушается: провайдер/gate — часть слоя очереди задач,
создаются до `jobs.start()` (recover внутри start ставит воркеры под
зарегистрированные типы — поэтому register обязан быть ДО start).

---

## Патч 4 — выбор провайдера

Провайдер получает `key_provider` как замыкание над `keystore.get`
(D1 п. 2, G2 подсказка) — значение ключа в конструктор не передаётся.

```python
    def _build_provider(self) -> "TranslationProvider":
        assert self.privacy and self.keystore
        # active: "gemini" | "claude" | "custom" (config.py TranslationProviderSection)
        active = getattr(self._cfg, "translation_active", "gemini")

        if active == "claude":
            return ClaudeTextProvider(
                privacy=self.privacy,
                key_provider=lambda: self.keystore.get("claude"),
                config=ClaudeConfig(),  # model/endpoint из config — сверить с B1
            )
        # default gemini
        return GeminiTextProvider(
            privacy=self.privacy,
            key_provider=lambda: self.keystore.get("gemini"),
        )
```

> Точка сверки: имя поля активного провайдера в AppConfig после
> подключения config.py (B1). Пока дефолт gemini.

---

## Патч 5 — обработчик TRANSLATE (ядро)

Строго протокол §1.3: require в точке постановки (уже сделан в enqueue) →
call → validate (внутри provider.translate) → write. Обработчик fence НЕ
перезахватывает, берёт `job.fence`.

```python
    async def _handle_translate(self, job) -> None:
        """Перевод одного сегмента. Handler-контракт queue.py:
        успех = без исключения; провал = исключение (ретрай по exc.retryable);
        статус задачи в БД обработчик НЕ трогает.
        """
        assert self.db and self.offline and self._provider

        segment_id = job.segment_id
        if not segment_id:
            return  # нечего переводить, задача завершена

        provider = self._provider

        # 1. Gate: провайдер доступен?
        if not self.offline.should_attempt(provider.name):
            # DEGRADED/BLOCKED — не долбить. Не ошибка задачи: перевод
            # догонит catch_up после mark_available. Задачу откладываем,
            # чтобы она не завершилась «успехом» без результата.
            await self.jobs.enqueue(
                JobType.TRANSLATE, segment_id=segment_id,
                idempotency_key=f"tr:{segment_id}", delay_s=30.0,
            )
            return

        # 2. Загрузить сегмент: raw_text + языки потока.
        loaded = await self._load_translate_input(segment_id)
        if loaded is None:
            return  # сегмент исчез или raw_text пуст — переводить нечего
        raw_text, source_lang, target_lang = loaded

        req = TranslationRequest(
            text=raw_text,
            source_language=source_lang,
            target_language=target_lang,
            mode=TranslationMode.LIVE_LITERAL,  # accurate-трек, сохранение сущностей
            context=(),                         # TODO(D6): подключить ContextBuilder
            segment_id=segment_id,
        )

        # 3. Вызов провайдера. fence — ИЗ JOB, не перезахватывать.
        try:
            result = await provider.translate(req, fence=job.fence)
        except StaleGenerationError:
            # Профиль переключён между постановкой и выполнением.
            # Тихое завершение: не ретрай, не запись. (H1-спека)
            return
        except ProviderError as exc:
            # Учитываем отказ в gate и пробрасываем — queue решит ретрай.
            self.offline.mark_unavailable(provider.name, exc)
            raise

        # 4. Успех: провайдер снова доступен.
        self.offline.mark_available(provider.name)

        # 5. Запись перевода. Зеркало _tx из _on_stt_result.
        clean = result.translation_clean
        raw = result.translation_raw

        def _tx(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE segments SET translation_raw = ?, "
                "translation_clean = ?, translation_status = 'done' "
                "WHERE id = ?",
                (raw, clean, segment_id),
            )

        await self.db.write(_tx)

        # 6. Уведомить UI (если поднят). superseded_ids — из supersede-слоя.
        if self.ui_server:
            await self.ui_server.emit(
                "segment.translated",
                {"segment_id": segment_id,
                 "translation": clean or raw,
                 "mode": req.mode.value,
                 "superseded_ids": []},
            )
```

---

## Патч 6 — загрузка входа перевода

```python
    async def _load_translate_input(
        self, segment_id: str
    ) -> tuple[str, str, str] | None:
        """Вернуть (raw_text, source_lang, target_lang) или None."""
        assert self.db
        row = await self.db.fetch_one(
            "SELECT raw_text, stream_id FROM segments WHERE id = ?",
            (segment_id,),
        )
        if not row:
            return None
        raw_text = row["raw_text"]
        if not raw_text or not raw_text.strip():
            return None
        # stream_id == роль потока ("microphone" | "meeting"); языки из конфига.
        settings = self._cfg.stream_settings(row["stream_id"])
        return (
            raw_text.strip(),
            settings.get("source_language", "auto"),
            settings.get("target_language", "en"),
        )
```

> Точка сверки: метод чтения (`fetch_one` vs `write(lambda conn:...)`) —
> сверить с фактическим API db.py. Если у `Database` нет `fetch_one` —
> использовать тот же паттерн, что STT-чтение в существующем коде.

---

## Патч 7 — восстановление после простоя облака

`mark_available` внутри обработчика уже переводит провайдер в AVAILABLE.
Добавить периодический `catch_up`, чтобы догнать `pending`/`failed`
сегменты (D7). Минимальный вариант — в существующий фон/тик, если он
есть; иначе разово при старте сессии:

```python
        # в start_session, после поднятия захвата:
        if self.offline and not self.offline.is_degraded():
            await self.offline.catch_up(self.db, self.jobs)
```

---

## Границы (не делать в H1)

- DRAFT-обработчик — НЕ пишем (решение владельца «Обсудить» по промпту;
  ядру не нужен).
- Fallback между провайдерами — не в MVP.
- Статус задачи в `jobs` из обработчика — не трогать (queue сам).
- context=() — временно; подключение D6 ContextBuilder отдельным шагом
  после сверки его сигнатуры по `context.py`.

---

## Тесты H1 (фикстуры, не железо)

Позитив:

1. фикстурный сегмент с raw_text → TRANSLATE ставится → fake-провайдер
   (`_call` возвращает перевод) → `translation_raw` в БД, статус `done`,
   gate AVAILABLE.

Негатив (3 обязательных):
2. провайдер бросает `ProviderUnavailable` → `mark_unavailable`,
   статус НЕ `done`, задача ретраится по exc.retryable;
3. switch профиля между enqueue и translate → `StaleGenerationError` →
   перевод НЕ записан, задача завершена тихо;
4. gate в BLOCKED (после `ProviderAuthError`) → `should_attempt=False` →
   провайдер не вызывается, задача отложена.

Критерий завершения: весь набор тестов зелёный вместе, число названо.

---

## Незакрытые точки сверки (честно)

| Что                                   | Где сверить           | Почему не зафиксировано                      |
|:------------------------------------- |:--------------------- |:-------------------------------------------- |
| `translation_clean` колонка есть?     | схема БД / migrations | писал по INTERFACES §7.1, не видел DDL       |
| `db.fetch_one` в `Database`           | db.py                 | API чтения не читал целиком                  |
| `ui_server.emit(event, data)`         | server.py E1          | метод рассылки SSE не читал по имени         |
| поле активного провайдера в AppConfig | config.py B1          | config не подключён к AppConfig              |
| `stream_id == role`                   | segmenter/capture     | вывод из `_accept_final`, не подтверждён DDL |

Эти пять — не выдуманы под рабочие, а помечены как требующие одной
проверки по коду перед запуском. Логика обработчика от них не зависит,
только имена методов/колонок.
