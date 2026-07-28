# Все конфигурационные переменные speech-local v2.0

Источники: `.env.example`, `config.example.toml`, `app/config.py` (FLAT_DEFAULTS), `systemd/speech.env.example`, код.

---

## I. Переменные окружения (.env / systemd env)

| Переменная | Дефолт | Где читается | Назначение |
|---|---|---|---|
| `GEMINI_API_KEY` | — | BYOK (keystore) | Ключ Gemini |
| `CLAUDE_API_KEY` | — | BYOK (keystore) | Ключ Claude |
| `OPENAI_API_KEY` | — | BYOK (keystore) | Ключ OpenAI |
| `SPEECH_DATA_DIR` | `data/` | app (data_dir) | Путь к данным |
| `LOG_LEVEL` | `info` | app/logging_setup.py | Уровень логирования |
| `UI_HOST` | `127.0.0.1` | app/config.py → config.toml | Хост UI |
| `UI_PORT` | `8790` | app/config.py → config.toml | Порт UI |
| `BYOK_KEYSTORE_PATH` | — | app (BYOK) | Путь к мастер-ключу keystore |
| `WAYLAND_DISPLAY` | (системная) | clipboard.py | Детект Wayland |
| `DISPLAY` | (системная) | clipboard.py | Детект X11 |

---

## II. config.toml — полный список (из FLAT_DEFAULTS + валидации)

### `[privacy]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `default_profile` | string | `"open"` | `"open"`, `"confidential"` |
| `allow_switch_midsession` | bool | `true` | — |

### `[provider.translation]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `active` | string | `"gemini"` | `"gemini"`, `"claude"`, `"custom"` |
| `endpoint` | string | `""` | — |
| `model` | string | `""` | — |

### `[provider.realtime]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `active` | string | `"openai"` | `"openai"`, `"gemini"`, `"none"` |
| `model` | string | `"gpt-realtime-translate"` | — |
| `enabled_profiles` | string[] | `["open"]` | только `["open"]` |

### `[provider.draft]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `active` | string | `"gemini"` | `"gemini"` |
| `model` | string | `""` | — |
| `max_words` | int | `120` | > 0 |

### `[stt]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `model` | string | `"ggml-base.bin"` | — |
| `fallback_model` | string | `"ggml-tiny.bin"` | — |
| `mode` | string | `"file_per_segment"` | `"file_per_segment"` |
| `json_output` | bool | `true` | — |
| `language_autodetect` | bool | `true` | — |

### `[streams.microphone]` и `[streams.meeting]`

| Ключ | Тип | Дефолт (mic) | Дефолт (meeting) | Допустимые |
|---|---|---|---|---|
| `source_language` | string | `"ru"` | `"en"` | `"ru"`, `"en"`, `"es"`, `"pl"` |
| `target_language` | string | `"en"` | `"ru"` | `"ru"`, `"en"`, `"es"`, `"pl"` |
| `pipewire_node` | string | `""` | `""` | длина ≤ 255 |
| `enabled` | bool | `true` | `true` | — |
| `priority` | string | `"primary"` | `"secondary"` | `"primary"`, `"secondary"` |

### `[vad]`

| Ключ | Тип | Дефолт | Диапазон |
|---|---|---|---|
| `silence_close_ms` | int | `1000` | 800–1200 |
| `segment_min_s` | float | `0.8` | > 0 |
| `segment_max_s` | float | `15` | ≥ segment_min_s |
| `partial_interval_ms` | int | `600` | 100–5000 |

### `[latency]`

| Ключ | Тип | Дефолт |
|---|---|---|
| `target_ms` | int | `3000` |
| `degrade_above_ms` | int | `3000` |

### `[translation.context]`

| Ключ | Тип | Дефолт |
|---|---|---|
| `window_short` | int | `4` |
| `window_mid` | int | `3` |
| `window_long` | int | `2` |
| `short_chars` | int | `100` |
| `long_chars` | int | `200` |

### `[draft]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `auto_generate` | bool | `true` | — |
| `library_max_tokens` | int | `30000` | > 0 |
| `generate_language` | string | `"ru"` | только `"ru"` |
| `translate_mode` | string | `"live_literal"` | только `"live_literal"` |

### `[retention]`

| Ключ | Тип | Дефолт | Допустимые |
|---|---|---|---|
| `audio` | string | `"after_stt"` | `"after_stt"`, `"24h"`, `"7d"`, `"keep"` |

### `[memory]`

| Ключ | Тип | Дефолт |
|---|---|---|
| `high_mb` | int | `1750` |
| `max_mb` | int | `1900` |

### `[delivery]`

| Ключ | Тип | Дефолт |
|---|---|---|
| `clipboard_hotkey` | string | `"ctrl+alt+c"` |

### `[ui]`

| Ключ | Тип | Дефолт |
|---|---|---|
| `host` | string | `"127.0.0.1"` |
| `port` | int | `8790` |

---

## III. pyproject.toml — только для сборки (не runtime-конфиг)

| Ключ | Значение |
|---|---|
| `python` | `>=3.12` |
| зависимости | `aiohttp` |
| dev-зависимости | `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `httpx`, `syrupy` |
| ruff line-length | 100 |
| mypy strict | `true` |

---

## IV. systemd unit — параметры сервиса

| Параметр | Значение |
|---|---|
| `MemoryHigh` | `1750M` |
| `MemoryMax` | `2000M` |
| `Restart` | `on-failure` |
| `TimeoutStopSec` | `30` |
| `WorkingDirectory` | `/opt/speech-local` |
| `EnvironmentFile` | `/opt/speech-local/.env` |
