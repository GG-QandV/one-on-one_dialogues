# speech-local v2.0

Offline-first speech translation & draft assistant для Zoom/Google Meet/MS Teams.

## Быстрый старт

```bash
# 1. Установка
pip install -e .
pip install -e ".[dev]"   # для разработки

# 2. Модель whisper
scripts/download_model.sh base

# 3. Настройка PipeWire (если не работает pw-record)
scripts/create_pipewire_sources.sh

# 4. Конфиг
cp config.example.toml config.toml
# отредактировать config.toml

# 5. Запуск
python -m app.main
# UI: http://127.0.0.1:8790
```

## Архитектура

Два трека обработки:

- **Точный трек** — локальный whisper.cpp → raw_text (неизменяем) → перевод через LLM
- **Быстрый трек** — частичные результаты → облачный realtime (только в открытом профиле)

Профили конфиденциальности: **открытый** (аудио и текст уходят в облако) / **конфиденциальный** (только текст).

Подробнее: [SPEC_speech_local_v2.md](docs/SPEC_speech_local_v2.md), [INTERFACES.md](INTERFACES.md).

## Системные требования

- Linux с PipeWire
- Python ≥ 3.12
- Рекомендуется: 16+ ГБ RAM, whisper.cpp (base model ~450 МБ RSS)

## Конфигурация

`config.toml` — все настройки в одном файле: языки, провайдеры, VAD, пороги памяти, hotkey.

Ключи API задаются через UI (BYOK, живут в RAM 60 минут, в лог и файлы не пишутся):

| Провайдер       | Перевод          | Черновики |
| --------------- | ---------------- | --------- |
| Gemini          | ✅                | ✅         |
| Claude          | ✅                | ❌         |
| OpenAI Realtime | ✅ (быстрый трек) | ❌         |

## Скрипты

| Скрипт                               | Назначение                         |
| ------------------------------------ | ---------------------------------- |
| `scripts/install_whispercpp.sh`      | Сборка whisper.cpp                 |
| `scripts/download_model.sh`          | Загрузка моделей (base/tiny/small) |
| `scripts/create_pipewire_sources.sh` | Virtual sink для захвата встречи   |
| `scripts/diagnose_hardware.sh`       | Диагностика аудио и системы        |

## Systemd

```bash
sudo cp systemd/speech-gateway.service /etc/systemd/system/
sudo cp systemd/speech.env.example /etc/speech-local.env
# отредактировать /etc/speech-local.env
sudo systemctl enable --now speech-gateway
```

## Разработка

```bash
# Тесты
pytest -v

# Линтер
ruff check app/
ruff format app/ --check

# Типы
mypy app/
```

## Лицензия

**Business Source License 1.1** (BSL 1.1). См. [LICENSE](LICENSE).

- **Коммерческое использование** — по отдельной лицензии (обращайтесь к лицензиару).
- **Личное/некоммерческое использование** — бесплатно (опенсорс-доступ к коду).

Аналогичная лицензия используется в [agent-connector](https://github.com/GG-QandV/agent-connector).
