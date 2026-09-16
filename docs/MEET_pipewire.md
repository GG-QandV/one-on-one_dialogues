# Google Meet — подключение через PipeWire (speech-local)

> Источники: `SPEC v2 §18 PipeWire` (`docs/SPEC_speech_local_v2.md:552`), `CONTRACT H6` (`CONTRACTS/H6_readme.md`), `scripts/create_pipewire_sources.sh`, `config.example.toml:41`.

Этот гайд — **блок Meet** из требуемой H6-инструкции (три блока: Zoom / Meet / Teams). Zoom и Teams — по той же схеме, меняются только имена узлов.

---

## 1. Как это работает (30 сек)

Meet в браузере — обычный PipeWire-клиент. Отдельного Meet-API нет.

- **Поток `meeting`** — звук конференции (EN → RU). Идёт через виртуальный sink `speech-local.monitor`.
- **Поток `microphone`** — твой микрофон (RU → EN). Обычный `alsa_input.*`.

Оба выбираются в UI `http://127.0.0.1:8790` → `Diagnostics` (`streams.meeting.pipewire_node`, `streams.microphone.pipewire_node` из `config.toml:41-44`).

**Ограничение браузера (H6:64):** узел принадлежит браузеру, в нём **весь звук браузера** — соседние вкладки, YouTube, уведомления тоже попадут в `meeting`. Это не баг настройки. Для чистого сигнала держи Meet в отдельном профиле/окне браузера без других вкладок со звуком.

---

## 2. Предварительно

```bash
# PipeWire должен быть активен (Linux Mint / Ubuntu 24.04 — по умолчанию)
systemctl --user status pipewire pipewire-pulse wireplumber | head

# Инструменты для диагностики (идут с системой)
which wpctl pw-cli pw-link pavucontrol  # любой из них достаточно
```

---

## 3. Создание виртуального sink (B7)

```bash
# Создаёт Audio/Sink speech-local.monitor — помощник, не замена ручной настройке (H6:73)
bash scripts/create_pipewire_sources.sh
# Ожидаемый вывод:
# Creating PipeWire virtual sink: speech-local.monitor
# Virtual sink speech-local.monitor created
# To monitor meeting audio, set in config.toml:
#   [streams.meeting]
#   pipewire_node = "speech-local.monitor"

# Проверка (имена на твоей машине будут другими — H6:58):
pw-cli list-objects | grep -A2 speech-local.monitor
# или
pactl list sinks short | grep speech-local
```

Если `pw-cli create-node` падает на нестандартной конфигурации — делай вручную через `pavucontrol` → `Output Devices` → виртуальный sink всё равно появится после перезапуска `pipewire`.

---

## 4. Google Meet — пошагово (Chrome / Firefox, Linux)

### 4.1 В системе — направить браузер в виртуальный sink

1. Запусти Meet и войди в тестовую встречу (узел браузера появляется только после старта звонка).
2. Открой `pavucontrol` → вкладка `Playback`:
   - Найди `Chromium` / `Firefox: AudioCallbackDriver` — это Meet.
   - Переключи его `Output` на `speech-local monitor sink` (описание `speech-local monitor sink` из `SINK_DESCRIPTION`).
3. Альтернатива без GUI:
   ```bash
   # Имена на твоей машине будут другими (H6:60 — пример без оговорки запрещён)
   wpctl status
   # Ищи в Sinks: speech-local.monitor
   # и в Sink Inputs: Chromium/ Firefox

   pw-link -o | grep -i -E "chromium|firefox|speech-local"
   # Должно показать связь Sink Input браузера → speech-local.monitor
   ```

### 4.2 В speech-local — выбрать узел встречи

```toml
# config.toml — можно править руками или через UI
[streams.meeting]
pipewire_node = "speech-local.monitor"
source_language = "en"
target_language = "ru"
enabled = true

[streams.microphone]
pipewire_node = ""          # оставь пустым → возьмётся default alsa_input
# или явно: "alsa_input.pci-0000_04_00.6.HiFi__hw_acp__source"
source_language = "ru"
target_language = "en"
```

Или в UI: `http://127.0.0.1:8790` → `Settings → Audio` → выбери `speech-local.monitor` для `meeting`.

### 4.3 Проверка до первого звонка (H6:68 — обязательно)

```bash
# Включи любое видео со звуком в браузере (YouTube) — он тоже пойдёт в speech-local.monitor
# Открой диагностику:
xdg-open http://127.0.0.1:8790  # вкладка Diagnostics
```

Ожидаешь: оба индикатора `meeting` и `microphone` показывают уровень `>-50 dB`, `VAD: active`. Если `meeting` молчит — узел не тот (см. диагностику ниже).

Быстрая CLI-проверка:

```bash
# Имена будут другими на твоей машине
wpctl status | grep -A2 Sinks
pw-link -o -i | head
curl -s http://127.0.0.1:8790/api/snapshot | python3 -m json.tool | grep -A2 '"meeting"\|"microphone"'
```

---

## 5. Диагностика (H6 §9 — 5 симптомов)

| Симптом | Вероятная причина | Проверка |
|---|---|---|
| Тишина в `meeting`, `microphone` есть | Выбран не тот PipeWire-узел | `pavucontrol → Playback` переключить браузер на `speech-local.monitor`; в Diagnostics выбрать другой `pipewire_node` |
| Перевод не появляется | Ключ истёк (60 мин BYOK) или облако недоступно | Индикатор провайдера в UI + `POST /api/key` заново (`{"provider":"gemini"}`) |
| Задержка выросла втрое | Очередь `STT` копится, память у порога `memory.high_mb 1750` | Diagnostics → `backlog_ms`, `queue_depth` |
| `speech-local.monitor` не создаётся | Нестандартный PipeWire / нет `pw-cli` | `pw-cli list-objects` ошибка → поставь `pipewire-pulse`, перезапусти `systemctl --user restart pipewire` |
| Весь звук вкладок попадает в транскрипт | Ограничение браузерного Meet | Вынеси Meet в отдельный профиль `chromium --user-data-dir=/tmp/meet` без других вкладок |

Полная диагностика: `bash scripts/diagnose_hardware.sh`

---

## 6. Приватность и профили

- **open** — `meeting_audio` уходит в облако (`Gemini`/`OpenAI Realtime`) для перевода. В README честно указано (`CONTRACTS/H6_readme.md:78-97`).
- **confidential** — в облако уходит только текст, аудио остаётся локально. Переключи в UI до старта сессии (`POST /api/privacy {"profile":"confidential"}`).

Ключ BYOK — в RAM 60 минут, маскируется в логах (`...3RRc`), не пишется в `speech.db` (`app/security/byok.py:14`).

---

## 7. Ссылки

- `config.example.toml` — все секции `streams.*`
- `docs/SPEC_speech_local_v2.md:552` — §18 PipeWire (почему нет автозахвата)
- `CONTRACTS/H6_readme.md` — требования к README/гайдам (три блока, BYOK-честность, диагностика)
- `scripts/create_pipewire_sources.sh` — создание sink (B7)
- `scripts/diagnose_hardware.sh` — проверка уровней и узлов

> Примечание: имена узлов `alsa_input.*`, `Chromium`, `speech-local.monitor` — примеры. На твоей машине будут другие — это нормально (H6:59).
