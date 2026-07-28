<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" style="height:64px;margin-right:32px"/>

# @mnemo-03

Проанализируй разбери и опиши логику для создания спеки и напиши промпт из разбора этой сессии: Итог
Выбор для первого рабочего локального варианта:

text
whisper.cpp + ggml-base.bin (multilingual)

+ встроенный VAD в streaming-режиме
+ PipeWire
+ SQLite
+ облачный API: переводчик + редактор

Ниже — логика спецификации и готовый промпт для разработки MVP локального speech‑pipeline: **локальная транскрибация через whisper.cpp, перевод и редактура через облачный API**. Архитектура специально разделяет аудио и текст: сырой звук и исходная стенограмма остаются локально, а во внешний API уходят только короткие текстовые сегменты для перевода/очистки. Это соответствует вашему ограничению по памяти и модели безопасности с маскированием данных перед облачной обработкой.[^1]

## Цель MVP

Сервис должен работать на Linux Mint Cinnamon во время звонков Zoom, Google Meet, Teams и аналогичных сервисов:

```text
Аудио встречи / микрофон
-> локальная транскрибация
-> локальная исходная стенограмма
-> облачный перевод и редактура текста
-> live captions в браузере
-> сохранение и экспорт итогов
```

Поддерживаемые направления первого этапа:

- EN/ES -> RU/UA: для чтения перевода речи клиента.
- RU/UA -> EN/ES: для перевода вашей речи клиенту текстом.
- Исходный и целевой язык должны выбираться отдельно для каждого аудиоканала.
- Минимальный продукт не обязан сразу озвучивать перевод обратно в Zoom; достаточно live‑текста и сохранённой стенограммы. TTS — отдельный следующий этап.


## Границы ответственности

| Компонент | Где работает | Что делает |
| :-- | :-- | :-- |
| PipeWire | Локально | Разделяет звук встречи и микрофон |
| Audio capture/normalizer | Локально | Приводит поток к 16 kHz mono PCM |
| VAD | Локально | Отсекает тишину, обозначает границы реплик |
| `whisper.cpp` + `ggml-base.bin` | Локально | Распознаёт EN/ES/RU/UA, возвращает исходный текст и таймкоды |
| SQLite | Локально | Хранит сессии, сегменты, сырые/переведённые/очищенные тексты |
| Translation/editor API | Облако | Переводит и нормализует текст по строгим правилам |
| Local Web UI | Локально | Показывает live‑субтитры, статусы, историю и экспорт |
| Watchdog/resource manager | Локально | Не даёт сервису превысить заданный RAM‑порог |

`whisper.cpp` — удачный движок под этот MVP, поскольку это C/C++‑реализация Whisper с отдельным streaming‑примером. Модель `base` занимает ориентировочно 388 МБ RAM, тогда как `small` — около 852 МБ, `medium` — около 2.1 ГБ; поэтому `base` — разумный компромисс качества и предсказуемого потребления памяти.[^2][^3]

## Почему именно `base` multilingual

Использовать нужно **`ggml-base.bin`, не `base.en`**. Версия без `.en` мультиязычная и нужна для английского, испанского, русского и украинского потока.[^4]


| Модель | Ориентировочная RAM | Роль |
| :-- | --: | :-- |
| `tiny` | ~273 МБ | Деградация при перегрузке или слабом CPU |
| **`base`** | **~388 МБ** | Основной live‑режим |
| `small` | ~852 МБ | Только post‑processing записей при свободной RAM |
| `medium` | ~2.1 ГБ | Не подходит |
| `large` | ~3.9 ГБ | Не подходит |

Для live‑режима качество зависит также от CPU: если обработка десятисекундного фрагмента длится дольше десяти секунд, очередь будет накапливаться. В таком случае pipeline обязан снизить качество до `tiny`, увеличить окно обработки или перейти в запись с последующей обработкой; не пытаться параллельно запускать вторую модель.[^3][^2]

## Рабочий поток

### Два аудиоканала

Нельзя смешивать микрофон и звук собеседника в один поток: иначе сложно определить направление перевода и роли.

```text
meeting_audio
  -> VAD
  -> whisper.cpp base
  -> raw text EN/ES
  -> Cloud API: RU/UA translation + live-safe editing
  -> UI: оригинал + перевод

mic_audio
  -> VAD
  -> whisper.cpp base
  -> raw text RU/UA
  -> Cloud API: EN/ES translation + live-safe editing
  -> UI: оригинал + перевод
```

На первом этапе можно поддерживать только `meeting_audio` — это даст самый быстрый и наглядный результат: клиент говорит на английском/испанском, а вы сразу читаете русский/украинский текст. Двусторонний режим надо включать после проверки стабильности раздельного захвата звука.

### VAD и сегментация

1. PipeWire отдаёт аудио с двух `source`.
2. Нормализатор приводит каждый поток к `mono / 16,000 Hz / PCM s16le`.
3. VAD определяет старт/конец речи.
4. После 500–700 мс тишины сегмент закрывается.
5. Предпочтительный размер реплики: 1.5–8 секунд; максимум — 20 секунд.
6. Сегмент кладётся в дисковую очередь, затем отправляется в `whisper.cpp`.
7. Исходный аудиофайл удаляется после заданного retention либо хранится только с явного согласия.

У `whisper.cpp` есть streaming‑пример и VAD‑режим, поэтому в первом релизе можно обойтись встроенной VAD‑логикой без отдельного Python/PyTorch‑стека.[^3]

## Структура данных

SQLite — единое локальное хранилище для MVP; оно исключает необходимость поднимать PostgreSQL только ради одной голосовой сессии.

```text
sessions
- id
- started_at
- ended_at
- meeting_title
- source_lang
- target_lang
- provider
- mode
- status

audio_streams
- id
- session_id
- role: meeting | microphone
- source_language
- target_language
- pipewire_node
- enabled

segments
- id
- session_id
- stream_id
- t_start_ms
- t_end_ms
- local_audio_path
- stt_model
- detected_language
- raw_text
- stt_confidence
- translation_status
- translation_raw
- translation_clean
- edit_log_json
- created_at

jobs
- id
- type: stt | translate | export
- segment_id
- status: queued | running | done | failed
- attempts
- error_code
- created_at
- updated_at
```

**Ключевое правило:** `raw_text` неизменяемый. Нельзя заменять его редакторским вариантом; это основа аудита и возможность проверить, что LLM не изменила смысл.

## Правила для облачного API

В API отправляется только текстовый сегмент с минимальным контекстом: язык, направление, режим и несколько предыдущих сегментов при необходимости для связности. Аудио, API‑ключи клиента и локальные пути файлов в запрос не попадают.

### Режим `live_literal`

Для переговоров, сумм, сроков и договорённостей.

```text
Переведи текст с {source_language} на {target_language}.
Не добавляй и не удаляй факты.
Сохрани числа, суммы, валюты, даты, имена, названия компаний,
артикулы, номера, ссылки и единицы измерения без изменений.
Не объясняй перевод. Верни только перевод.
```


### Режим `live_safe`

Для обычного разговора.

```text
Переведи текст с {source_language} на {target_language}.
Удаляй только отдельные очевидные междометия и слова-паразиты:
«ээ», «эээ», «эм», «эмм», «мм», «ммм», «uh», «um», «er».
Не удаляй слова, если они могут менять смысл.
Сохраняй все факты, числа, имена, даты и названия.
Верни только перевод.
```


### Режим `post_clean`

Только после встречи, для чистовой стенограммы.

```text
Отредактируй стенограмму без изменения смысла.
Удали очевидные слова-паразиты, ложные старты и бессмысленные повторы.
Восстанови пунктуацию и абзацы.
Не меняй числа, даты, валюты, имена, названия, ссылки,
обязательства, решения и степень уверенности говорящего.
Верни JSON:
{
  "clean_text": "...",
  "changes": [
    {"type": "filler_removed", "original": "...", "replacement": ""}
  ]
}
```

Это разделение обязательно: «красивый» редактор нельзя применять к live‑переговорам по умолчанию.

## Интерфейс MVP

Одна локальная страница, например `http://127.0.0.1:8790`.

### Верхняя панель

- Статус: `Готов` / `Слушаю` / `Транскрибирую` / `API недоступен`.
- Выбор входного потока: `Звук встречи` / `Микрофон`.
- Язык источника и язык перевода.
- Режим: `Точный live`, `Очищенный live`, `Чистовая после встречи`.
- Кнопки: `Начать`, `Пауза`, `Завершить`, `Экспорт`.


### Основная область

Каждая реплика — карточка:

```text
[00:05:32] Клиент · ES
Bueno, eh, necesitamos revisar el contrato.

RU · live_safe
Нам необходимо проверить договор.

Статус: STT ✓ | Перевод ✓ | Редактура ✓
```


### Экспорт

- `TXT`: исходный и переведённый текст.
- `SRT/VTT`: субтитры с таймкодами.
- `JSON`: полный audit‑след, включая raw/clean текст и ошибки job.
- `DOCX/PDF`: можно добавить после MVP.


## RAM и устойчивость

При лимите прикладного контура до 2 ГБ нельзя держать одновременно локальную STT, диаризацию, локальный переводчик и LLM‑редактор. Базовый сервис должен состоять из одной модели `whisper.cpp base`, лёгкой очереди, SQLite и UI; перевод/редактор работают по API.


| Слой | Целевой бюджет |
| :-- | --: |
| PipeWire + аудиомаршрутизация | 100–200 МБ |
| Нормализация/захват аудио | 40–100 МБ |
| `whisper.cpp base` | 388–650 МБ |
| Gateway + UI | 150–300 МБ |
| SQLite, буферы, очередь | 50–150 МБ |
| Резерв на пики | 500–700 МБ |
| **Итого** | **≈1.23–2.1 ГБ** |

Поэтому целевой режим должен держаться около **1.3–1.6 ГБ**, а не стремиться к потолку. На уровне systemd/cgroup задайте `MemoryHigh=1600M`, `MemoryMax=1850M`; выше soft‑порога сервис прекращает накапливать очередь в RAM и пишет сегменты на диск. `MemoryHigh` создаёт memory pressure до достижения жёсткого `MemoryMax`, после которого cgroup может подвергнуться OOM‑kill.[^5][^6]

## Деградация без потери данных

```text
Норма:
  base + live STT + API translate/editor

Очередь STT растёт:
  уменьшить длину сегментов;
  отключить word timestamps;
  показывать raw-частичный текст;
  записывать audio segments на диск.

API перевода не отвечает:
  продолжать локальную STT;
  записывать jobs translate в SQLite;
  показать «перевод будет восстановлен»;
  выполнить переводы после восстановления API.

RAM > 1.6 ГБ:
  остановить необязательные функции;
  сбросить cache;
  не брать новые live jobs сверх лимита.

RAM > 1.75 ГБ:
  корректно остановить STT worker;
  сохранить аудио на диск;
  переключиться в recording-only до освобождения RAM.
```

Это важнее, чем попытка любой ценой выдавать live‑перевод: потеря данных и аварийное убийство процесса хуже задержки в несколько секунд.

## Безопасность и BYOK

Для публичного демо используйте свой ограниченный ключ и тестовые данные. При тесте клиента с собственной моделью используйте BYOK‑сессию: ключ передаётся по HTTPS, живёт только в RAM изолированного session worker, не записывается в SQLite, логи, аналитические системы или конфигурационные файлы и удаляется по TTL/ручной кнопке `Revoke`. Секреты нельзя помещать в frontend, URL, cookie, `localStorage`, debug‑логи или общий `.env`; OWASP отдельно подчёркивает необходимость защищённого управления secrets, а OpenAI запрещает раскрывать API‑ключи в клиентском коде.[^7][^8][^9]

Для постоянного корпоративного режима лучше предложить локальный connector в инфраструктуре клиента: ключ остаётся у клиента, а ваша платформа отправляет только подписанные задания и получает результат/статус. Это самый сильный вариант доверия, хотя для MVP не обязателен.[^8][^10]

## Критерии приёмки

MVP считается готовым, если:

1. Сервис запускается на Linux Mint Cinnamon без Docker, GPU и тяжёлого Python‑ML‑стека.
2. Захватывает выбранный PipeWire‑поток и микрофон раздельно.
3. Распознаёт тестовые EN, ES, RU и UA фрагменты локально через `ggml-base.bin`.
4. Сохраняет raw‑стенограмму с таймкодами в SQLite.
5. Отправляет в облако только текст, получает перевод и показывает его в UI.
6. API‑недоступность не останавливает локальную запись/транскрибацию.
7. В режиме `live_literal` числа, даты и имена не должны изменяться редактором.
8. В режиме `post_clean` создаётся отдельная версия текста с `edit_log`.
9. Экспорт TXT, SRT/VTT и JSON работает из завершённой сессии.
10. При memory pressure сервис пишет очередь на диск и не допускает аварийного превышения `MemoryMax`.
11. Ключ BYOK не сохраняется на диск и не появляется в логах.
12. Остановка и повторный запуск не повреждают SQLite‑сессию и позволяют восстановить незавершённые `translate`‑jobs.

## Готовый промпт для разработки

```text
Ты senior Linux/Python/C++ developer и архитектор систем локальной обработки речи.
Создай рабочий MVP для Linux Mint Cinnamon: локальная транскрибация речи из онлайн-встреч с облачным переводом и редактурой текста.

Цель:
Во время Zoom, Google Meet, Microsoft Teams и других звонков приложение захватывает выбранные аудиопотоки, транскрибирует речь локально, сохраняет исходную стенограмму на устройстве и отправляет в облачный LLM API только текстовые сегменты для перевода EN/ES/RU/UA и безопасной редакторской очистки.

Строгие границы:
- Локально: PipeWire, аудиозахват, VAD, сегментация, whisper.cpp, SQLite, очередь, локальный Web UI, экспорт.
- В API: только перевод и редактура текста. Никогда не передавать аудио, локальные file paths, секреты или API-ключи.
- Не использовать Docker, Ollama, локальную LLM, pyannote, NLLB, PostgreSQL, Qdrant или тяжёлый Python ML runtime в MVP.
- Не использовать GPU.
- Лимит всего прикладного контура: 2 ГБ RAM; рабочая цель 1.3–1.6 ГБ. При росте памяти система должна деградировать безопасно, а не падать.
- Linux Mint Cinnamon, PipeWire/WirePlumber, x86_64 CPU.
- Интерфейс и логи должны быть на русском; код и README могут быть на английском.

Обязательный стек:
1. whisper.cpp как отдельный локальный процесс или библиотека.
2. Модель: ggml-base.bin multilingual. Не использовать base.en.
3. Встроенный VAD/streaming режим whisper.cpp для первого релиза.
4. PipeWire для выбора двух независимых источников:
   - meeting_audio: системный звук участников.
   - mic_audio: микрофон пользователя.
5. FFmpeg или GStreamer для приведения входа к mono, 16 kHz, PCM s16le.
6. SQLite в WAL-режиме для состояния, сегментов, очереди и истории.
7. Лёгкий backend на Python 3.11+ asyncio + FastAPI или aiohttp.
8. Локальный web UI без тяжёлого frontend framework: HTML + CSS + vanilla JavaScript, SSE/WebSocket для live captions.
9. TranslationProvider interface для переключения между OpenAI-compatible, Anthropic-compatible и custom HTTP API.
10. systemd user service files и отдельный watchdog/resource manager.

Основной поток:
meeting_audio или mic_audio
-> normalizer 16 kHz mono PCM
-> VAD
-> сегменты речи 1.5–8 сек, закрывать после 500–700 мс тишины, максимум 20 сек
-> дисковая SQLite/filesystem queue
-> whisper.cpp base multilingual
-> raw_text + language + timestamps
-> SQLite
-> text-only API translation/editor
-> translated_raw + translated_clean + edit_log
-> local UI and export.

Не смешивай meeting_audio и mic_audio в один поток. Для каждого stream хранить роль, source_language, target_language, PipeWire node и enabled status.

Поддерживаемые направления:
- EN/ES -> RU/UA для речи собеседника.
- RU/UA -> EN/ES для речи пользователя.
- Языки задаются отдельно для каждого потока.
- На MVP основной сценарий: односторонний meeting_audio EN/ES -> RU/UA.
- Двусторонний режим включается как feature flag после проверки.

Создай SQLite schema и миграцию для таблиц:
sessions:
id, started_at, ended_at, meeting_title, status, provider, mode.

audio_streams:
id, session_id, role(meeting|microphone), source_language, target_language, pipewire_node, enabled.

segments:
id, session_id, stream_id, t_start_ms, t_end_ms, local_audio_path,
stt_model, detected_language, raw_text, stt_confidence,
translation_status, translation_raw, translation_clean,
edit_log_json, created_at.

jobs:
id, type(stt|translate|export), segment_id,
status(queued|running|done|failed), attempts, error_code,
created_at, updated_at.

Правила целостности:
- raw_text неизменяем после записи.
- Перевод, очищенный текст и edit_log всегда отдельные поля.
- Аудиофайлы хранятся только в папке сессии, с configurable retention.
- При недоступности API перевод ставится в SQLite queue, но STT и запись продолжаются.
- Не держать более 30–60 секунд необработанного аудио в RAM: всё остальное на диск.

Реализуй TranslationProvider:
translate(segment, source_language, target_language, mode, context) -> TranslationResult.
Провайдер должен возвращать JSON-структуру:
{
  "translation_raw": "string",
  "translation_clean": "string or null",
  "changes": [{"type": "filler_removed|punctuation|other", "original": "string", "replacement": "string"}],
  "provider_request_id": "string or null"
}

Сделай три жёстких режима prompt:
1. live_literal:
- только перевод;
- не менять и не удалять факты;
- точно сохранять числа, даты, валюты, имена, компании, номера, URL, артикулы, единицы измерения;
- вернуть только перевод.

2. live_safe:
- перевод;
- разрешено удалить только очевидные отдельные filler words: «ээ», «эээ», «эм», «эмм», «мм», «ммм», «uh», «um», «er»;
- не удалять слова и повторы, если они могут менять смысл;
- сохранять все факты и вернуть только перевод.

3. post_clean:
- вызывается только после встречи;
- удалить явные слова-паразиты, ложные старты и бессмысленные повторы;
- восстановить пунктуацию и абзацы;
- не менять числа, даты, валюты, имена, названия, ссылки, обязательства, решения, оговорки и модальность;
- вернуть строгий JSON с clean_text и changes.

UI на http://127.0.0.1:8790:
- верхняя панель: status, input stream, source/target languages, mode, Start/Pause/Stop/Export.
- live cards: timestamp, role, language, raw text, translation, status STT/translation.
- настройки: PipeWire source selection, API provider selection, retention, privacy mode.
- исторические сессии: открыть, повторить перевод неуспешных сегментов, экспортировать.
- экспорт: TXT, SRT, VTT, JSON. SRT/VTT должны использовать исходные timestamp.
- не отображать API key после ввода, только маску.

BYOK:
- ключ принимается только в HTTPS POST body и только для пилотного режима.
- не сохранять API-ключ в SQLite, файлах, URL, localStorage, cookies, аналитике, exception traces, access/debug logs.
- ключ живёт только в RAM изолированного session worker, TTL 60 минут, есть ручная кнопка Revoke.
- сделать LogRedactor, который маскирует все известные ключевые паттерны до записи любого лога.
- для локального single-user режима разрешить ключ через systemd EnvironmentFile с правами 0600; не коммитить .env в Git.
- README должен честно объяснять: ephemeral BYOK не означает, что сервер «не видит» ключ в момент обработки; он означает отсутствие записи на диск и автоматическое удаление после TTL.

Memory / watchdog:
- целевое RAM потребление 1.3–1.6 GB.
- systemd user unit для gateway: MemoryHigh=1600M, MemoryMax=1850M.
- memory watchdog проверяет cgroup memory.current раз в 2 секунды.
- при >1600M: отключить необязательные cache/word timestamps, прекратить запуск новых jobs, перевести буферы на диск.
- при >1750M: остановить STT worker корректно, сохранить очередь и перейти в recording-only.
- не запускать параллельно более одного экземпляра whisper.cpp base.
- не запускать локальную диаризацию, переводчик, LLM, OCR или вторую Whisper-модель.
- при заполнении очереди показывать пользователю состояние degraded, но не терять аудио.

Надёжность:
- health endpoints /health и /ready.
- graceful shutdown: остановить intake, завершить текущий segment, сбросить SQLite WAL, сохранить job state.
- при старте восстановить jobs со статусом queued/running и корректно повторить только idempotent translation requests.
- не повторять запросы к API бесконечно: exponential backoff, максимум 3 попытки.
- для SQLite включить WAL, foreign_keys, busy_timeout; все операции записи выполнять через один writer queue.
- добавить тесты для schema, job recovery, prompt mode validation, log redaction и export SRT/VTT.

PipeWire:
- реализуй discovery входных источников через wpctl/pw-dump либо pactl совместимый слой.
- UI должен позволять выбрать node для meeting_audio и mic_audio.
- не обещай автоматический универсальный захват системного звука: в README опиши, как пользователь создаёт virtual sink/source через PipeWire/WirePlumber для Zoom/Meet/Teams.
- добавь диагностический экран: выбранный node, sample rate, RMS/VAD status, seconds of queued audio.

Структура проекта:
speech-local/
  README.md
  LICENSE
  pyproject.toml
  config.example.toml
  systemd/
    speech-gateway.service
    speech-watchdog.service
  scripts/
    install_whispercpp.sh
    download_model.sh
    create_pipewire_sources.sh
  app/
    main.py
    config.py
    db.py
    models.py
    queue.py
    audio/
    stt/
    translation/
    exports/
    ui/
    security/
    watchdog/
  migrations/
  tests/

Сначала дай:
1. Архитектурный план и список решений/компромиссов.
2. Полную структуру файлов.
3. Schema SQLite и конфигурацию TOML.
4. Код MVP по файлам.
5. Инструкцию установки Linux Mint.
6. Инструкцию подключения Zoom, Google Meet и Teams через PipeWire.
7. Команды smoke-test.
8. Таблицу ограничений MVP и план этапа 2: двусторонний режим, TTS, diarization, полноценное шифрование credential vault, Docker/production deployment.

Не используй mock-логику вместо реально работающих компонент. Если универсальный системный audio capture нельзя гарантировать без конкретного PipeWire node, реализуй диагностику и пошаговый manual setup, а не скрывай ограничение.
```

<span style="display:none">[^11][^12][^13][^14]</span>

<div align="center">⁂</div>

[^1]: Polnaia_perepiska_s_zakazchikom.md

[^2]: https://github.com/ggml-org/whisper.cpp

[^3]: https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream

[^4]: https://github.com/ggml-org/whisper.cpp/issues/2614

[^5]: https://manpages.debian.org/testing/systemd/systemd.resource-control.5.en.html

[^6]: https://manpages.ubuntu.com/manpages/oracular/man8/systemd-oomd.8.html

[^7]: https://help.openai.com/en/articles/5112595-best-practices-for-api-key-safety

[^8]: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html

[^9]: https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html

[^10]: https://support.anthropic.com/es/articles/9767949-mejores-practicas-para-claves-api-manteniendo-sus-claves-seguras-y-protegidas

[^11]: WIN10_11_INSTALL_GUIDE.md

[^12]: tezy-vidpovidey-50min.md

[^13]: konkurenty.txt

[^14]: 2026-07-17_Anastasiia_RAG_response.md

