# Контекст для следующей сессии: финализация speech-local

Репо: `GG-QandV/one-on-one_dialogues`. Подключено к проекту Claude, читается
через индекс. **Индекс отстаёт от пушей** (кэш, ошибки upstream 500) —
при расхождении отчёта с кодом арбитр только `git show <commit>:<file>`,
не индекс. Правило всей работы: **читать код, не отчёты**. «N passed» и
«✅ в реестре» за сессию трижды расходились с кодом (base.py, E1, волна 1).

Стиль ответов владельцу: русский, кратко, таблицы/пункты, факты не
умозаключения, честная фиксация невыполненного, ✓ фиксация сделанного.
Критично для владельца: недостоверность = вред. Никогда не заявлять
«готово», если не проверено кодом.

---

## АРХИТЕКТУРНЫЕ ПРАВИЛА (жёсткие, от владельца)

1. **Нет промпта в промпте.** Никогда не вкладывать промпт в промпт. Два
   промпта → либо последовательно (2 вызова), либо второй как условия
   первого. Для генерации черновиков → отдельный режим DRAFT (не
   эксплуатация POST_CLEAN). См. PATCH_draft_mode.md.
2. **raw_text неизменяем**, источник только whisper.cpp.
3. **fence приходит СНАРУЖИ** (от вызывающего, точка постановки задачи),
   провайдер/I2/I4 не делают require() внутри. Протокол §1.3: require →
   call → validate → write.
4. **Заглушки роутов запрещены** — роут пишется вместе со своим модулем,
   разрыв документируется комментарием.
5. **Верификацию делает не автор кода.** Самопроверка не работает.
6. Ресурсы жёстко: max 3 демо-стека, whisper 550-700 МБ, бюджет 13 ГБ.
7. Пороги/языки/лимиты из конфига, не хардкод.

---

## ЧТО ПРОВЕРИТЬ — приоритетно (не прочитано по коду)

### P1 — самостоятельные правки мидла (вне контрактов, НЕ читал)
Мидл сделал их на своё усмотрение, честно вынес списком. Проверить по коду:

| Файл | Что проверить | Риск |
| :-- | :-- | :-- |
| `app/ui/static/js/app.js` | bootstrap-мост Stream↔вкладки↔команды. Написан С НУЛЯ, не было в дереве/контракте. Проверить: не ведёт ли состояние сегментов (должно быть в E2/stream.js); `window.commands` глобал — не ломает ли инвариант «логика в E2» | Средний |
| `app/ui/server.py` `_clipboard_handler` | добавлен для /api/clipboard. Делегирует в delivery.clipboard.copy? Не логирует текст? | Средний |
| `app/ui/routes.py` `/api/clipboard` | соответствует паттерну routes E1? привязка 127.0.0.1? | Средний |
| `window.commands` bridge | глобал для copyDraft из tab_drafts. Циклическую зависимость обошёл — но не создал ли глобальное состояние? | Средний |

### P2 — C3 normalizer (ловушка + не читал)
`app/audio/normalizer.py` — мидл заявил реализацию + `test_normalize_odd_length_rounded`.
- **Версия Python проекта** → если 3.13+, `audioop` удалён, нужен
  `audioop-lts`. Проверить, чем сделан ресемплинг.
- C3 должен быть resampling БЕЗ FFmpeg (он fallback ОТ FFmpeg). Цепочка:
  pw-record → FFmpeg(16k) → C3(python audioop). Если C3 зовёт FFmpeg — дефект.
- Два теста: (1) C3 включается когда FFmpeg упал, (2) эквивалентность
  FFmpeg vs C3 по формату (16k/mono/s16le); качество — LIVE.
- Блокер: **решение владельца №1** (отклонение §7, §2.3 INTERFACES) —
  утверждено ли.

### P3 — tab_drafts.js (не читал)
Инвариант: нет автокопирования (§21.11), has_gaps виден, состояние из E2,
копирование только через commands.copyDraft→G3.

### P4 — доверификация волны 1 после fix-коммита a76d2f8 (частично)
Подтверждён по коду только I4 (fence-параметр, detect_drift убран). НЕ
перепроверены по коду после fix:
- base.py: fence-параметр (не require внутри) — заявлено, видел новый код
  частично, подтвердить целиком;
- D2 gemini: finishReason + blockReason проверки — заявлены, НЕ читал после fix;
- D3 claude: stop_reason + восстановление `{` — заявлены, НЕ читал после fix;
- тест `test_switch_before_translate_with_stale_fence_raises` — должен
  переключать профиль ДО translate (не внутри _call). Проверить:
  `git show a76d2f8:tests/test_base_D1.py | grep -c "test_switch_before_translate"` → 2

### P5 — G2 byok (беглая сверка, не полная)
`app/security/byok.py` — НАПИСАН (реестр врёт ⬜). Беглый просмотр чист:
только память, TTL 3600, __getstate__ бросает, redactor add/remove,
ключ не в repr/snapshot. Нужна полная верификация по коду как для D7.

### P6 — I1 library (беглая, не полная)
`app/drafts/library.py` — НАПИСАН (реестр врёт ⬜). API:
`get(context_id)->LibraryContext`, текст в `.content_text` (НЕ get_text).
Полная верификация не делалась.

---

## H1 — СБОРКА (главный незакрытый этап)

`main.py` теперь виден (18K, был недоступен). H1 = дописать обработчики
в готовый каркас Application. Спека: **H1_assembly_spec.md** (в outputs).

**Фаза 0 H1 — прочитать main.py целиком.** Установить:
- зарегистрирован ли уже JobType.TRANSLATE в queue.register;
- ставит ли STT-обработчик задачу TRANSLATE после записи raw_text;
- где создаётся PrivacyController, JobQueue;
- как enqueue фиксирует fence (авто из privacy или параметром).

Ключевые решения H1 (из спеки):
- обработчик TRANSLATE: gate.should_attempt → provider.translate(req,
  fence=job.fence) → mark_available/unavailable → write. Fence из JOB, не
  require в обработчике;
- StaleGenerationError → тихое завершение, не ретрай;
- захват fence ТОЛЬКО в точке постановки (STT-обработчик, catch_up);
- DRAFT-обработчик — после I2, для ядра не нужен.

Риск срыва H1: 40% (дрейф интерфейсов на стыках). Senior.

---

## ФАКТИЧЕСКОЕ СОСТОЯНИЕ КОДА (по репо, не реестру)

Реестр `CONTRACTS/README.md` ОТСТАЁТ — многое помечено ⬜ при живом коде.
Верить репо. Обновить реестр — отдельная junior-задача.

### Готово и верифицировано по коду
- context.py (D6) — чист
- offline.py (D7) — чист, тесты бьют в инварианты
- clipboard.py (G3) — корректен
- stream.js (E2) — обработчики черновиков + снапшот-дедуп есть
- translate.py (I4) после fix — fence-параметр, detect_drift убран

### Готово, беглая сверка (нужна полная верификация)
- byok.py (G2), library.py (I1), base.py (D1 после fix)

### Готово по отчёту (не читал код)
- E1 server.py + routes.py — 10 тестов, lifecycle-фикс подтверждён отчётом
- prompts.py (D4), gemini (D2), claude (D3) после fix
- I3 trigger, F1 memory — заявлены мидлом, не читал
- UI: topbar/settings/diagnostics/history — заявлены, не читал
- systemd, scripts, README(H6) — заявлены, не читал

### Senior-слой ✅ — НИКОГДА не читал по коду (принято из реестра)
db.py, queue.py(тело), privacy.py(тело switch/teardown), audio/*
(capture/discovery/pcm/segmenter/vad), stt/* (runner/scheduler/parser
C6/fallback/language C8), supersede.py(D8), openai_realtime.py(D5),
degradation.py(F2 — частично читал, содержит встроенный MemoryReader).

---

## КЛЮЧЕВЫЕ ИНТЕРФЕЙСЫ (чтобы не перечитывать)

### privacy.py
`PrivacyController(initial: PrivacyProfile)`; `require(cap)->Fence`;
`validate(fence, cap)` бросает StaleGenerationError при рассогласовании
поколения; `switch(profile)` меняет поколение; `allows(cap)->bool`.
Profiles: OPEN, CONFIDENTIAL. CAPABILITY_MATRIX: CONFIDENTIAL = всё кроме
AUDIO_TO_CLOUD. Capability: TEXT_TO_CLOUD, AUDIO_TO_CLOUD, DRAFT_GENERATION.

### base.py (D1)
`TranslationProvider.translate(req, *, fence) -> TranslationResult`.
`TranslationMode`: LIVE_LITERAL, LIVE_SAFE, POST_CLEAN (+ DRAFT после патча).
`TranslationRequest(text, source_language, target_language, mode,
context=(), segment_id=None)`.
`TranslationResult(translation_raw, translation_clean=None, changes=())`.
`Change(type, ...)` type='lost_entity' для дрейфа.

### queue.py
`register(job_type, handler)`; `enqueue(job_type, segment_id,
idempotency_key, ...)`; Job несёт fence (privacy_profile+privacy_gen,
`job.fence` property); handler: успех=без исключения, провал=исключение,
ретрай по exc.retryable + job.idempotent; handler НЕ трогает статус в БД;
`cancel_by_fence` отменяет queued при switch. JobType: STT, TRANSLATE, DRAFT.

### offline.py (D7)
`OfflineGate`: should_attempt(provider)->bool; mark_unavailable(provider,
exc); mark_available(provider); catch_up()->задачи; snapshot(). States:
AVAILABLE/DEGRADED/BLOCKED. AuthError→BLOCKED сразу. 3 отказа→DEGRADED.

### library.py (I1)
`FactLibrary.get(context_id)->LibraryContext`; list()->list (без
content_text); upsert(name,domain,content_text)->id; estimate_tokens;
check_limit(text, 30000)->(ok, est). LibraryContext(id, name, domain,
content_text, token_estimate, updated_at). Сверх лимита → LibraryTooLarge.

### guardrails.py (I5)
`DraftGuard.verify(candidate, library_text)->Verdict`; store(candidate,
verdict)->draft_id|None; attach_translation(draft_id, text).
`DraftCandidate(session_id, trigger_segment_id, draft_ru, target_language,
sources, has_gaps_claimed, gap_note)`. strict_numbers=True дефолт.
extract_numbers — проверка чисел против библиотеки.

### stream.js (E2)
Stream: subscribe(type, cb); getState(); обработчики segment.partial/final/
translated, draft.created/translated, privacy.changed, status; снапшот с
дедупликацией по sequence. Вкладки читают отсюда, состояние НЕ ведут.

### degradation.py (F2)
`DegradationCascade(actions, config, *, memory_reader, latency_source,
backlog_source)`; tick()->Level; Level: NORMAL/LATENCY/MEMORY_SOFT/
MEMORY_HARD. Содержит встроенный `MemoryReader` (source, current_mb,
cgroup→vmrss). F1 расширяет его (опрос, история, дети whisper).

### SSE формат §7.1 (E1↔E2)
event-типы С ТОЧКАМИ: segment.partial, segment.final, segment.translated,
draft.created, draft.translated, privacy.changed, status. Поля: event:/
data:(JSON ensure_ascii=False)/id:(sequence монотонный). E2 слушает по
этим именам — расхождение разделителя = молчащая вкладка.

---

## ОСТАЛОСЬ ДЛЯ ТИРА 1 (сводка)

**Код (проверить/дописать):**
- H1 сборка (Senior) — главное
- C3 normalizer (P2) — решение владельца №1 + версия Python
- I2 provider — применить PATCH_draft_mode.md (DRAFT-режим вместо POST_CLEAN)
- UI-вкладки доверификация (P1, P3)

**Верификация (Senior read-only):**
- P1-P6 выше
- Senior-слой ✅ (если нужна гарантия «без долгов» — большой объём)
- Аудит Part II раздел A (10 критич. инвариантов) — TASK_senior_audit.md

**Механика (Junior):**
- обновить реестр CONTRACTS/README.md (G2/I1/D6/D7/E1... → ✅)
- удалить 8 пустых контрактов (B4,B5,C1,C2,C4,C5,D5,D8)
- свести имя D1 (_interfaces vs _base)

**Решения владельца:**
- №1: отклонение §7 (C3 fallback) — утвердить/нет
- №2: строгий DraftGuard дефолт — I5 уже True, подтвердить
- промпт режима DRAFT (новый, не из §11) — утвердить

**Инфраструктура (проверить, заявлены мидлом):**
- systemd*3, scripts*4, README(H6), config.toml рабочий

**Данные:**
- library_knowledge_base.md → загрузить в FactLibrary через upsert,
  проверить лимит 30k токенов (не верификация кода)

---

## АРТЕФАКТЫ СЕССИИ (в /mnt/user-data/outputs, если нужны)
- H1_assembly_spec.md — спека сборки
- PATCH_draft_mode.md — DRAFT-режим + правка имён I1 (4 патча)
- I2_provider_spec.md, provider.py, test_provider_I2.py — I2 (под POST_CLEAN,
  ПЕРЕДЕЛАТЬ по PATCH_draft_mode)
- TASK_wave1_fixes.md — 10 правок волны 1 (применены в a76d2f8)
- TASK_senior_audit.md — аудит Part II
- TASK_remaining_code.md — F1/I3/C3/UI инструкции
- TASK_middle_tier1.md — весь middle-объём
- C3_normalizer.md, E4-E8 контракты вкладок, E6/E7

## ПЕРВЫЙ ШАГ СЛЕДУЮЩЕЙ СЕССИИ
Прочитать main.py целиком (теперь доступен) → фаза 0 H1. Параллельно
доверифицировать P1 (app.js/clipboard — самостоятельные правки мидла) и
P4 (D2/D3 после fix). Это разблокирует H1 и закроет волну 1 формально.
