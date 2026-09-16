# Zoom — подключение через PipeWire (speech-local)

> Источники: `SPEC v2 §18 PipeWire` (`docs/SPEC_speech_local_v2.md:552`), `CONTRACT H6` (`CONTRACTS/H6_readme.md`), `scripts/create_pipewire_sources.sh`, `config.example.toml:41`.

Этот гайд — **блок Zoom** из требуемой H6-инструкции (три блока: Zoom / Meet / Teams). Meet и Teams — по той же схеме, меняются только имена узлов и ловушки.

---

## 1. Как это работает (30 сек)

Zoom — **десктопное** приложение (в отличие от браузерного Meet). Даёт самый чистый сигнал:

- **Поток `meeting`** — звук участников Zoom (EN → RU). Идёт через виртуальный sink `speech-local.monitor`.
- **Поток `microphone`** — твой микрофон (RU → EN).

Оба выбираются в UI `http://127.0.0.1:8790` → `Diagnostics` (`streams.meeting.pipewire_node` из `config.toml:41-44`).

**Плюс Zoom:** узел `Zoom` изолирован — в отличии от браузерного Meet/Teams Web, где весь звук браузера смешивается. **Ловушка (H6:171):** узел `Zoom` появляется **только после входа в конференцию** — искать его до звонка бесполезно.

Доставка перевода — только `буфер обмена по горячей клавише` (`SPEC:428`). Вставка в чат Zoom невозможна: десктопный клиент — нативное приложение без DOM (`SPEC:431-433`), `Zoom Apps SDK — Никогда`.

---

## 2. Предварительно

```bash
systemctl --user status pipewire pipewire-pulse wireplumber | head
which wpctl pw-cli pw-link pavucontrol  # любой достаточно
```

---

## 3. Создание виртуального sink (B7)

Один sink на все три клиента — тот же, что для Meet/Teams:

```bash
bash scripts/create_pipewire_sources.sh
# Ожидаемый вывод:
# Creating PipeWire virtual sink: speech-local.monitor
# Virtual sink speech-local.monitor created

# Проверка (имена на твоей машине будут другими — H6:58):
pw-cli list-objects | grep -A2 speech-local.monitor
pactl list sinks short | grep speech-local
```

Если `pw-cli create-node` падает — ставь `pipewire-pulse` и `systemctl --user restart pipewire`.

---

## 4. Zoom — пошагово

### 4.1 Направить Zoom в виртуальный sink

1. Запусти Zoom, войди в тестовую конференцию (иначе узла нет).
2. В Zoom: `Settings → Audio → Output Speaker → speech-local monitor sink` (если Zoom не показывает sink — делай через систему ниже).
3. Через систему (`pavucontrol` → `Playback`):
   - Найди `Zoom` / `zoom` (имя на твоей машине будет другое — H6:59).
   - Переключи его `Output` на `speech-local monitor sink`.
4. Без GUI:
   ```bash
   # Имена на твоей машине будут другими
   wpctl status | grep -A2 -i zoom
   pw-link -o | grep -i -E "zoom|speech-local"
   # Должен показать Sink Input zoom → speech-local.monitor
   ```

**Важно:** остальной системный звук (музыка, уведомления) не должен идти в `speech-local.monitor` — иначе попадёт в транскрипт. Проверь в `pavucontrol → Playback`, что только `Zoom` направлен в sink.

### 4.2 В speech-local — выбрать узел встречи

```toml
# config.toml — руками или через UI
[streams.meeting]
pipewire_node = "speech-local.monitor"
source_language = "en"
target_language = "ru"
enabled = true

[streams.microphone]
pipewire_node = ""       # пусто → default alsa_input
source_language = "ru"
target_language = "en"
```

Или UI: `http://127.0.0.1:8790 → Settings → Audio → meeting → speech-local.monitor`.

### 4.3 Микрофон для Zoom (чтобы собеседники слышали)

`speech-local` только слушает микрофон, не маршрутизирует его. В Zoom: `Settings → Audio → Microphone → твой alsa_input` (тот же, что в `streams.microphone`).

---

## 5. Проверка до первого звонка (H6:68 — обязательно)

```bash
# Включи тестовую конференцию Zoom с любым звуком
xdg-open http://127.0.0.1:8790  # вкладка Diagnostics
# Ожидаешь: meeting > -50 dB, microphone > -40 dB, VAD: active
```

CLI:

```bash
wpctl status | grep -A2 Sinks
curl -s http://127.0.0.1:8790/api/snapshot | python3 -m json.tool | grep -A2 '"ui"'
```

Если `meeting` молчит — смотри таблицу.

---

## 6. Диагностика (H6 §9)

| Симптом | Вероятная причина | Проверка |
|---|---|---|
| `meeting` тишина, `microphone` есть | Zoom не в звонке (узла нет) или не направлен в sink | Войди в звонок → `pavucontrol → Playback → Zoom → speech-local.monitor`; `pw-link -o \| grep zoom` |
| `speech-local.monitor` не создаётся | Нестандартный PipeWire / нет `pw-cli` | `pw-cli list-objects` ошибка → `pipewire-pulse`, `systemctl --user restart pipewire` |
| В транскрипт попадает музыка/уведомления | Весь системный звук идёт в sink | В `pavucontrol` только `Zoom` должен быть на `speech-local.monitor` |
| Перевод не появляется | Ключ BYOK истёк (60 мин) | Индикатор провайдера в UI, `POST /api/key {"provider":"gemini"}` |
| Задержка выросла | Очередь STT, память у порога `memory.high_mb 1750` | Diagnostics → `backlog_ms`, `queue_depth` |
| `Копировать` не работает в Zoom чат | Попытка вставки — не поддерживается | Используй `Ctrl+Alt+C` → `Ctrl+V` в чат (SPEC:431 — буфер единственный с 100% покрытием) |

Быстрая диагностика: `bash scripts/diagnose_hardware.sh`

---

## 7. Приватность и профили

- **open** — `meeting_audio` уходит в облако.
- **confidential** — только текст. Переключи до сессии (`POST /api/privacy {"profile":"confidential"}`).

Ключ BYOK — RAM 60 мин, маскируется `...3RRc` (`app/security/byok.py:14`).

---

## 8. Ссылки

- `config.example.toml` — секции `streams.*`
- `docs/SPEC_speech_local_v2.md:552` — §18 PipeWire
- `CONTRACTS/H6_readme.md` — требования H6
- `docs/MEET_pipewire.md` — блок Google Meet (браузер)
- `docs/TEAMS_pipewire.md` — блок MS Teams (Desktop/Web)
- `scripts/create_pipewire_sources.sh` — B7
- `scripts/diagnose_hardware.sh` — диагностика

> Имена `alsa_input.*`, `Zoom`, `speech-local.monitor` — примеры. На твоей машине будут другими — это нормально (H6:59).
