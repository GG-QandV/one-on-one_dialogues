# Спецификация: speech-local MVP v1.0

Самостоятельный проект. Роль: демо-версия для солюшена (этап 2).

---

## 1. Назначение

Локальный сервис live-транскрибации и перевода речи во время звонков (Zoom, Google Meet, MS Teams) на Linux Mint Cinnamon.

**Ключевой принцип разделения:**
- **Локально:** аудио, VAD, STT, raw-стенограмма, SQLite, UI, экспорт.
- **В облако:** только короткие текстовые сегменты для перевода/редактуры. Никогда: аудио, file paths, ключи, секреты.

```
Аудио встречи / микрофон
-> нормализация 16 kHz mono PCM
-> VAD -> сегменты речи
-> whisper.cpp (локальная транскрибация)
-> SQLite (raw-стенограмма + таймкоды)
-> Cloud API (перевод + редактура текста)
-> live captions в локальном Web UI
-> экспорт TXT / SRT / VTT / JSON
```

---

## 2. Платформа и ограничения

| Параметр | Значение |
| :-- | :-- |
| ОС | Linux Mint Cinnamon, x86_64 |
| Аудиосистема | PipeWire / WirePlumber |
| GPU | Не используется |
| Docker | Не используется в MVP |
| Запрещено в MVP | Ollama, локальная LLM, pyannote, NLLB, PostgreSQL, Qdrant, тяжёлый Python-ML runtime |
| RAM лимит контура | 2 ГБ жёсткий; рабочая цель 1.3–1.6 ГБ |
| Язык UI и логов | Русский; код и README — английский допустим |

---

## 3. Стек

| Компонент | Технология |
| :-- | :-- |
| STT | whisper.cpp, модель `ggml-base.bin` multilingual (~388 МБ RAM). **НЕ `base.en`** |
| Fallback-модель | `tiny` (~273 МБ) при перегрузке CPU/RAM |
| VAD | Встроенный VAD/streaming-режим whisper.cpp (без отдельного Python/PyTorch) |
| Аудиозахват | PipeWire, 2 независимых источника |
| Нормализация | FFmpeg или GStreamer → mono / 16 000 Hz / PCM s16le |
| Хранилище | SQLite (WAL, foreign_keys, busy_timeout, один writer queue) |
| Backend | Python 3.11+ asyncio; FastAPI или aiohttp |
| Web UI | HTML + CSS + vanilla JavaScript; SSE или WebSocket для live captions |
| Перевод | Облачный API через интерфейс TranslationProvider; **MVP: Google Gemini и Anthropic Claude**, подключение через конфиг + панель управления |
| Сервисы | systemd user units: gateway + watchdog |

---

## 4. Аудиоканалы

Два раздельных потока, смешивание запрещено:

| Поток | Источник | Направление перевода |
| :-- | :-- | :-- |
| `meeting_audio` | Системный звук участников (PipeWire node) | EN/ES → RU/UA |
| `mic_audio` | Микрофон пользователя | RU/UA → EN/ES |

- Языки задаются отдельно для каждого потока.
- **Основной сценарий MVP:** односторонний `meeting_audio` EN/ES → RU/UA.
- Двусторонний режим — feature flag, включается после проверки стабильности раздельного захвата.

### Определение языка (два режима, оба обязательны)

1. **Ручная установка:** `source_language` задаётся в настройках потока — приоритетный режим.
2. **Автораспознавание:** language auto-detect whisper.cpp; результат пишется в `segments.detected_language`.
3. При конфликте (detected ≠ заданный): использовать заданный язык для перевода, зафиксировать расхождение в логе сегмента и показать индикатор в UI. Переключение на detected — только вручную пользователем.

---

## 5. VAD и сегментация

| Правило | Значение |
| :-- | :-- |
| Закрытие сегмента | После 500–700 мс тишины |
| Предпочтительная длина реплики | 1.5–8 секунд |
| Максимальная длина | 20 секунд (принудительная нарезка) |
| Очередь сегментов | Дисковая (SQLite/filesystem), не RAM |
| Аудио в RAM | Не более 30–60 секунд необработанного; остальное на диск |

---

## 6. Схема данных (SQLite)

```sql
sessions:
  id, started_at, ended_at, meeting_title, status, provider, mode,
  source_lang, target_lang

audio_streams:
  id, session_id, role (meeting | microphone),
  source_language, target_language, pipewire_node, enabled

segments:
  id, session_id, stream_id, t_start_ms, t_end_ms,
  local_audio_path, stt_model, detected_language,
  raw_text,                -- НЕИЗМЕНЯЕМ после записи
  stt_confidence,          -- усреднённый avg_logprob по токенам сегмента;
                           -- вычисляется из уже готовых данных whisper.cpp,
                           -- нулевая доп. нагрузка, распознавание не замедляет
  translation_status, translation_raw, translation_clean,
  edit_log_json, created_at

jobs:
  id, type (stt | translate | export), segment_id,
  status (queued | running | done | failed),
  attempts, error_code, created_at, updated_at
```

### Инварианты целостности

1. `raw_text` неизменяем — основа аудита, проверка что LLM не исказила смысл.
2. `translation_raw`, `translation_clean`, `edit_log_json` — всегда отдельные поля; редактор никогда не перезаписывает оригинал.
3. Аудиофайлы — только в папке сессии.
4. Недоступность API перевода не останавливает STT и запись: translate-jobs в очередь.
5. Все записи в SQLite — через один writer queue.

### Retention аудио

- **Настраиваемый параметр** в config.toml и UI.
- **Значение по умолчанию: автоудаление аудиофайла сразу после успешного STT** (raw_text записан в SQLite).
- Хранение аудио дольше — только при явной установке retention пользователем.

---

## 7. TranslationProvider

### Интерфейс

```
translate(segment, source_language, target_language, mode, context) -> TranslationResult
```

### Формат ответа

```json
{
  "translation_raw": "string",
  "translation_clean": "string or null",
  "changes": [
    {"type": "filler_removed | punctuation | other",
     "original": "string", "replacement": "string"}
  ],
  "provider_request_id": "string or null"
}
```

### Провайдеры

| Провайдер | Статус |
| :-- | :-- |
| **Google Gemini API** | MVP, подключается через конфиг |
| **Anthropic Claude API** | MVP, подключается через конфиг |
| OpenAI-compatible / Custom HTTP | Через тот же интерфейс, вторичный |

Параметры провайдера (endpoint, модель, ключ, лимиты) — **только из конфига**, без хардкода. Выбор и настройка активного провайдера — в панели управления (дашборд), значения пишутся в config.

### Контекст перевода (динамическое окно)

Размер скользящего окна предыдущих сегментов того же потока зависит от длины текущего сегмента (баланс: не терять контекст / не замедлять):

| Длина сегмента (знаков) | Окно контекста |
| :-- | :-- |
| < 100 | 4 сегмента |
| 100–200 | 3 сегмента |
| > 200 | 2 сегмента |

- Пороги и размеры окна — настраиваемые в конфиге (дефолты выше).
- Перевод потоковый и непрерывный: контекст прикладывается к каждому запросу и не задерживает обработку.

### Ретраи

- Exponential backoff, максимум 3 попытки.
- Повторяются только idempotent-запросы.
- При исчерпании попыток: job → failed, повтор доступен вручную из UI (история сессий).

---

## 8. Режимы перевода/редактуры (промпты)

### 8.1 `live_literal` — «Точный live»

Для переговоров: суммы, сроки, договорённости.

```
Переведи текст с {source_language} на {target_language}.
Не добавляй и не удаляй факты.
Сохрани числа, суммы, валюты, даты, имена, названия компаний,
артикулы, номера, ссылки и единицы измерения без изменений.
Не объясняй перевод. Верни только перевод.
```

### 8.2 `live_safe` — «Очищенный live»

Для обычного разговора.

```
Переведи текст с {source_language} на {target_language}.
Удаляй только отдельные очевидные междометия и слова-паразиты:
«ээ», «эээ», «эм», «эмм», «мм», «ммм», «uh», «um», «er».
Не удаляй слова, если они могут менять смысл.
Сохраняй все факты, числа, имена, даты и названия.
Верни только перевод.
```

### 8.3 `post_clean` — «Чистовая после встречи»

Только после завершения сессии.

```
Отредактируй стенограмму без изменения смысла.
Удали очевидные слова-паразиты, ложные старты и бессмысленные повторы.
Восстанови пунктуацию и абзацы.
Не меняй числа, даты, валюты, имена, названия, ссылки,
обязательства, решения и степень уверенности говорящего.
Верни JSON: {"clean_text": "...", "changes": [...]}
```

**Правило:** «красивый» редактор (post_clean) никогда не применяется к live-переговорам по умолчанию.

---

## 9. Web UI

Адрес: `http://127.0.0.1:8790`

### Верхняя панель
- Статус: `Готов` / `Слушаю` / `Транскрибирую` / `API недоступен` / `Degraded`.
- Выбор потока: `Звук встречи` / `Микрофон`.
- Языки источника и перевода (раздельно на поток).
- Режим: `Точный live` / `Очищенный live` / `Чистовая после встречи`.
- Кнопки: `Начать`, `Пауза`, `Завершить`, `Экспорт`.

### Live-карточки (по одной на реплику)
```
[00:05:32] Клиент · ES
Bueno, eh, necesitamos revisar el contrato.

RU · live_safe
Нам необходимо проверить договор.

Статус: STT ✓ | Перевод ✓ | Редактура ✓
```

### Настройки (панель управления)
Все значения читаются/пишутся в config — UI не содержит собственных хардкод-значений:
- Выбор PipeWire node на каждый поток.
- Провайдер API: выбор Gemini/Claude/другой, endpoint, модель, ключ (маскируется).
- Языки: ручная установка source/target на поток + вкл/выкл автодетект.
- Retention аудио.
- Пороги окна контекста перевода.
- Privacy mode.

### История сессий
- Открытие прошлых сессий.
- Ручной повтор перевода failed-сегментов.
- Экспорт.

### Диагностический экран
- Выбранный node, sample rate, RMS/VAD status, секунды аудио в очереди.

### Экспорт
| Формат | Содержимое |
| :-- | :-- |
| TXT | Исходный + переведённый текст |
| SRT / VTT | Субтитры; **обязательно исходные таймкоды** |
| JSON | Полный audit-след: raw/clean, edit_log, ошибки jobs |
| DOCX / PDF | После MVP |

---

## 10. Память и watchdog

| Слой | Бюджет |
| :-- | --: |
| PipeWire + маршрутизация | 100–200 МБ |
| Захват/нормализация | 40–100 МБ |
| whisper.cpp base | 388–650 МБ |
| Gateway + UI | 150–300 МБ |
| SQLite, буферы, очередь | 50–150 МБ |
| Резерв на пики | 500–700 МБ |

**systemd:** `MemoryHigh=1600M`, `MemoryMax=1850M`. Watchdog читает cgroup `memory.current` каждые 2 секунды.

### Каскад деградации (без потери данных)

| Триггер | Действие |
| :-- | :-- |
| Очередь STT растёт | Короче сегменты; отключить word timestamps; показывать raw-частичный текст; аудио на диск |
| API перевода недоступен | STT продолжается; translate-jobs в SQLite queue; UI: «перевод будет восстановлен»; догон после восстановления |
| RAM > 1600 МБ | Отключить необязательные cache; не брать новые jobs; буферы на диск |
| RAM > 1750 МБ | Корректно остановить STT worker; сохранить очередь; recording-only до освобождения RAM |
| CPU не успевает (обработка 10 с фрагмента > 10 с) | Переход base → tiny ИЛИ увеличение окна ИЛИ запись с post-обработкой |

**Запреты:** не более одного экземпляра whisper.cpp; никакой параллельной второй модели, диаризации, локального переводчика, LLM, OCR. Приоритет: сохранность данных > live-скорость.

---

## 11. Безопасность и BYOK

| Правило | Реализация |
| :-- | :-- |
| Передача ключа | Только HTTPS POST body, только пилотный режим |
| Хранение | Только RAM изолированного session worker; TTL 60 минут; кнопка Revoke |
| Запрещено | SQLite, файлы, URL, localStorage, cookies, аналитика, exception traces, access/debug логи |
| LogRedactor | Маскирует известные паттерны ключей до записи любого лога |
| Локальный single-user | Ключ через systemd EnvironmentFile, права 0600; .env не коммитится |
| UI | Ключ после ввода — только маска |
| README | Честное объяснение: ephemeral BYOK ≠ «сервер не видит ключ»; означает отсутствие записи на диск + автоудаление по TTL |

Корпоративный режим (этап 2): локальный connector в инфраструктуре клиента — ключ не покидает клиента.

---

## 12. Надёжность

- Health endpoints: `/health`, `/ready`.
- Graceful shutdown: стоп intake → завершение текущего сегмента → сброс SQLite WAL → сохранение job state.
- При старте: восстановление jobs `queued`/`running`; повтор только idempotent translate-запросов.
- Тесты обязательны для: schema, job recovery, валидация prompt-режимов, log redaction, экспорт SRT/VTT.

---

## 13. PipeWire

- Discovery источников: `wpctl` / `pw-dump` либо pactl-совместимый слой.
- UI: выбор node для meeting_audio и mic_audio.
- **Не обещать** автоматический универсальный захват системного звука: README — пошаговая инструкция создания virtual sink/source через PipeWire/WirePlumber для Zoom/Meet/Teams.
- Никакой mock-логики: если захват нельзя гарантировать без конкретного node — диагностика + manual setup, не скрывать ограничение.

---

## 14. Структура проекта

```
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
```

---

## 14a. Конфигурация (config.toml)

**Принцип: ноль хардкода.** Все настраиваемые параметры — только в конфиге. Конфиг минимальный, расширяемый; панель управления читает и пишет его (кроме секретов — ключи по правилам раздела 11).

```toml
[provider]
active = "gemini"              # gemini | claude | custom

[provider.gemini]
endpoint = "..."
model = "..."
# ключ: EnvironmentFile / BYOK, не в config

[provider.claude]
endpoint = "..."
model = "..."

[stt]
model = "ggml-base.bin"
fallback_model = "ggml-tiny.bin"
language_autodetect = true

[streams.meeting]
source_language = "en"
target_language = "ru"
pipewire_node = ""

[streams.microphone]
source_language = "ru"
target_language = "en"
pipewire_node = ""
enabled = false

[vad]
silence_close_ms = 600         # 500–700
segment_max_s = 20

[translation.context]
window_short = 4               # сегмент < short_chars
window_mid = 3                 # short_chars..long_chars
window_long = 2                # сегмент > long_chars
short_chars = 100
long_chars = 200

[retention]
audio = "after_stt"            # after_stt | 24h | 7d | keep

[memory]
high_mb = 1600
max_mb = 1750

[ui]
host = "127.0.0.1"
port = 8790
```

---

## 15. Критерии приёмки MVP

1. Запуск на Linux Mint Cinnamon без Docker, GPU и тяжёлого Python-ML стека.
2. Раздельный захват выбранного PipeWire-потока и микрофона.
3. Локальное распознавание тестовых EN, ES, RU, UA фрагментов через `ggml-base.bin`.
4. Raw-стенограмма с таймкодами сохраняется в SQLite.
5. В облако уходит только текст; перевод отображается в UI.
6. Недоступность API не останавливает локальную запись/транскрибацию.
7. В `live_literal` числа, даты, имена не изменяются редактором.
8. В `post_clean` создаётся отдельная версия текста с `edit_log`.
9. Экспорт TXT, SRT/VTT, JSON работает из завершённой сессии.
10. При memory pressure очередь пишется на диск; `MemoryMax` не превышается аварийно.
11. BYOK-ключ не сохраняется на диск и не попадает в логи.
12. Остановка/перезапуск не повреждают SQLite-сессию; незавершённые translate-jobs восстанавливаются.

---

## 16. Ограничения MVP и этап 2

| Ограничение MVP | Этап 2 |
| :-- | :-- |
| Односторонний режим (meeting_audio) по умолчанию | Полный двусторонний режим |
| Нет озвучки перевода | TTS (Text-to-Speech — синтез речи) |
| Нет разделения говорящих | Diarization (диаризация — определение «кто говорит») |
| BYOK ephemeral в RAM | Полноценное шифрованное credential vault |
| Только локальный запуск | Docker / production deployment |
| Manual setup PipeWire virtual sink | Автоматизация захвата |
| DOCX/PDF экспорт отсутствует | Добавить |
| Один активный провайдер одновременно (Gemini или Claude, из панели) | Параллельные провайдеры / A-B сравнение |
