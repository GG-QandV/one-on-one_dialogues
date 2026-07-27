# H1 — сквозная сборка: спека и инструкция

Уровень: Senior. H1 — не написание сборки с нуля: `main.py` (✅ F3)
содержит `Application` с жизненным циклом и порядком старта/остановки.
H1 — **дописать обработчики `JobType.TRANSLATE` и `JobType.DRAFT`,
подключить провайдеры, gate и fence-протокол** в существующий каркас и
устранить дрейф на стыках.

Роадмап ставит H1 риск срыва 40%: порознь готовые модули не складываются
с первого раза. Поэтому фаза 0 — обязательное чтение фактического кода.

---

## ФАЗА 0 — прочитать перед единой строкой кода

H1 нельзя писать по памяти о контрактах. Открыть и прочитать:

1. **`app/main.py` целиком** — что уже подключено. Ключевое: где
   создаётся `JobQueue`, вызывается ли уже `queue.register(...)` для
   каких-то типов, как устроен `start_session`, где живёт
   `PrivacyController`, как STT-результат попадает в БД. **Без этого
   файла H1 писать нельзя** — можно продублировать то, что есть, или
   разойтись с фактической структурой.
2. **`app/queue.py`** — сигнатура `Handler`, контракт обработчика
   (успех = без исключения, провал = исключение, ретрай по
   `exc.retryable` + `job.idempotent`, обработчик **не трогает статус в
   БД**), `Job.fence`, `Job.payload`, `enqueue(..., idempotency_key)`.
3. **`app/translation/base.py`** (после правок волны 1) — сигнатура
   `translate(req, *, fence)`, что провайдер делает с fence.
4. **`app/translation/offline.py`** (D7, принят) — `should_attempt`,
   `mark_unavailable`, `mark_available`, `catch_up`.
5. **Провайдеры** `gemini_text.py`, `claude_text.py` — конструктор:
   какой `key_provider`, какой `privacy`, как выбирается по конфигу.
6. **`app/privacy.py`** — `require`/`validate`/`fence`/`switch`,
   протокол §1.3 (require → call → validate → write).
7. **`app/db.py`** — как писать `translation_raw`/`translation_clean`/
   `translation_status`, как читать сегмент по id.

Выход фазы 0: понимание, что в `main.py` уже есть, а что дописывать.
Если STT-обработчик уже зарегистрирован — брать его за образец стиля.

---

## ФАЗА 1 — обработчик JobType.TRANSLATE

Ядро H1. Задача `TRANSLATE` ставится, когда сегмент готов к переводу
(из STT-обработчика или из `catch_up`). Обработчик переводит и пишет
результат.

### Протокол (строго §1.3 privacy)

```python
async def _handle_translate(self, job: Job) -> None:
    # 1. gate: провайдер доступен?
    provider = self._select_provider()          # по конфигу: gemini|claude
    if not self._offline.should_attempt(provider.name):
        # провайдер DEGRADED/BLOCKED — не долбить.
        # НЕ ошибка задачи: вернуть управление, catch_up догонит позже.
        # Но задача не должна молча пропасть — перепоставить с delay
        # ИЛИ бросить retryable, чтобы queue отложила. Выбрать по коду queue.
        raise ProviderUnavailable(f"{provider.name} gated")

    # 2. собрать запрос
    segment = await self._load_segment(job.segment_id)
    req = self._build_request(segment)           # text, языки, mode, context(D6)

    # 3. fence приходит ОТ JOB — job.fence, захвачен при постановке
    fence = job.fence

    # 4. перевод (провайдер сам делает validate ПОСЛЕ _call)
    try:
        result = await provider.translate(req, fence=fence)
    except ProviderAuthError as e:
        self._offline.mark_unavailable(provider.name, e)   # -> BLOCKED
        raise                                              # не retryable
    except (ProviderUnavailable, ProviderRateLimited) as e:
        self._offline.mark_unavailable(provider.name, e)   # счётчик к DEGRADED
        raise                                              # retryable -> queue ретраит
    except StaleGenerationError:
        # профиль переключился, пока задача ждала/выполнялась.
        # результат невалиден, но это НЕ отказ провайдера.
        # задача отменяется, не ретраится (fence устарел навсегда).
        return                                             # тихо завершить

    # 5. успех -> сбросить gate, записать
    self._offline.mark_available(provider.name)
    await self._write_translation(job.segment_id, result)
```

### Ключевые решения фазы 1 (обосновать в PR)

**Fence берётся из `job.fence`, не захватывается в обработчике.** Это
итог правок волны 1: `Job` несёт `privacy_profile` + `privacy_gen`,
`job.fence` — property. Задача поставлена под поколением, актуальным на
момент постановки; если профиль переключился, пока задача ждала в
очереди, `validate` внутри провайдера бросит `StaleGenerationError`.
**Не вызывать `privacy.require()` в обработчике** — это перезахват,
ровно дефект, который чинили в base.py и I4.

**`StaleGenerationError` → тихое завершение, не ретрай.** Устаревший
fence не станет свежим повтором. Задача завершается без записи. Но:
`queue` уже отменяет queued-задачи при switch (`cancel_by_fence`), так
что до обработчика устаревшая задача чаще не дойдёт. Обработка здесь —
страховка для задачи, которая переключилась во время выполнения.

**Gate перед вызовом.** `should_attempt(provider.name) == False` →
не звать провайдера. Как именно вернуть задачу (retryable-исключение
или enqueue с delay) — зависит от того, как `queue` обрабатывает
retryable и delay. Прочитать `_worker_loop` в queue.py, выбрать
механизм, который не теряет задачу и не долбит провайдер.

**Запись только после успеха.** `translation_raw`, `translation_clean`,
`changes` (в `edit_log_json`), `translation_status = 'done'`. При
провале статус остаётся `pending`/`failed` — `catch_up` подберёт.
Обработчик **не** трогает статус самой задачи в `jobs` — это делает
`queue`.

**Провайдер выбирается по конфигу.** `provider.translation.active`
(gemini|claude|custom) из B1. Один активный на сессию; переключение —
через настройки. Fallback между провайдерами в MVP не делать (не в
контракте).

---

## ФАЗА 2 — обработчик JobType.DRAFT

Ставится, когда `trigger` (I3) детектировал вопрос клиента. Генерирует
черновик (I2, пишет Senior отдельно) и переводит его (I4).

```python
async def _handle_draft(self, job: Job) -> None:
    if not self._offline.should_attempt(provider.name):
        raise ProviderUnavailable("gated")
    # 1. генерация черновика (I2 DraftProvider) — RU
    draft_ru = await self._draft_provider.generate(job.payload)   # I2
    draft_id = await self._store_draft(job.segment_id, draft_ru)  # через DraftGuard
    # 2. перевод черновика (I4) — fence от job
    translated = await self._draft_translator.translate_draft(
        draft_id, draft_ru.text, target_language,
        fence=job.fence,                      # НЕ require() внутри
    )
    # 3. прикрепить перевод (если прошёл проверку дрейфа)
    if translated is not None:
        await self._guard.attach_translation(draft_id, translated)
```

**Зависит от I2 (Senior, ещё не готов).** Фаза 2 пишется после I2. Для
живого теста ядра (только перевод) фаза 2 **не нужна** — можно
зарегистрировать заглушку-обработчик DRAFT или не регистрировать тип,
если черновики в первом прогоне выключены.

---

## ФАЗА 3 — регистрация обработчиков в start()

В `Application.start()` (или где создаётся queue), после создания
провайдеров и gate:

```python
self._offline = OfflineGate(config=offline_cfg)
self._translate_provider = self._build_provider(config)   # gemini|claude
self._queue.register(JobType.TRANSLATE, self._handle_translate)
# DRAFT — только если черновики включены и I2 готов:
if self._drafts_enabled:
    self._queue.register(JobType.DRAFT, self._handle_draft)
```

Порядок из §8 не нарушать: БД → профили → **очередь (register до start)**
→ STT. `register` до `queue.start()` — воркеры поднимаются под
зарегистрированные типы.

---

## ФАЗА 4 — связка STT → TRANSLATE

Проверить (в существующем STT-обработчике `main.py`): после записи
`raw_text` ставится ли `JobType.TRANSLATE`? Если STT-обработчик уже
пишет сегмент, но не ставит перевод — **дописать enqueue**:

```python
# в конце обработки STT-сегмента, точный трек:
if segment.track == "accurate":
    fence = self._privacy.require(Capability.TEXT_TO_CLOUD)  # захват ЗДЕСЬ
    await self._queue.enqueue(
        JobType.TRANSLATE,
        segment_id=segment.id,
        idempotency_key=segment.id,
        # fence прокидывается через privacy_profile/gen задачи —
        # проверить, как enqueue принимает fence (из privacy текущего)
    )
```

**Здесь fence захватывается — потому что это точка постановки задачи,
вызывающий по протоколу §1.3.** Обработчик перевода уже получит его
через `job.fence`. Проверить, как `enqueue` фиксирует profile/gen: если
автоматически из текущего `PrivacyController` — ничего не передавать;
если параметром — передать. **Прочитать `enqueue` в queue.py.**

---

## ФАЗА 5 — сквозной тест на фикстурах

Не живое железо — фикстуры. Проверить поток целиком:
- подать фикстурный аудио-сегмент (или сразу `raw_text` в БД) →
- STT-обработчик пишет сегмент, ставит TRANSLATE →
- обработчик TRANSLATE берёт подменённый провайдер (fake `_call`
  возвращает перевод) → пишет `translation_raw`, статус `done` →
- проверить: перевод в БД, статус done, gate AVAILABLE.

Плюс негативные:
- провайдер бросает `ProviderUnavailable` → gate считает отказ,
  статус сегмента не `done`, задача ретраится/откладывается;
- переключение профиля во время задачи → `StaleGenerationError` →
  перевод не записан;
- gate в BLOCKED → обработчик не зовёт провайдер.

---

## Границы H1

- Не переписывать готовые модули — только подключать.
- Не захватывать fence в обработчиках — брать `job.fence`. Захват
  только в точке постановки задачи (STT-обработчик, catch_up).
- Не делать fallback между провайдерами (не в контракте MVP).
- Не трогать статус задачи в `jobs` из обработчика — это queue.
- DRAFT-фаза — после I2 (Senior). Для ядра можно без неё.
- Не изобретать протокол privacy — строго §1.3.

## Критерий завершения H1

- обработчик TRANSLATE зарегистрирован, переводит, пишет результат;
- fence идёт из job, не перезахватывается;
- gate интегрирован: отказы считаются, BLOCKED не долбится,
  восстановление триггерит catch_up;
- STT-сегмент точного трека ставит TRANSLATE;
- сквозной тест на фикстурах зелёный, включая 3 негативных;
- порядок старта/остановки §8 не нарушен;
- весь набор тестов проходит вместе, число названо.

---

## Почему это спека, а не готовый код

Фактический `main.py` в момент написания недоступен для чтения целиком
(индекс отдаёт INTERFACES и acceptance, но не тело `main.py`). Писать
обработчики, не видя, что уже подключено в `start()` и STT-обработчике, —
риск дублирования и дрейфа. Фаза 0 (чтение main.py) обязательна и
первична. Код фаз 1–5 пишется по этой спеке **после** чтения, с
подстройкой под фактическую структуру `main.py` — сигнатуры методов
(`_load_segment`, `_write_translation`, `_select_provider`) выведены из
контрактов и подлежат сверке с тем, что реально есть в файле.
