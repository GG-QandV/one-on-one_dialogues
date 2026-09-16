# MS Teams — подключение через PipeWire (speech-local)

> Источники: `SPEC v2 §18 PipeWire` (`docs/SPEC_speech_local_v2.md:552`), `CONTRACT H6` (`CONTRACTS/H6_readme.md`), `scripts/create_pipewire_sources.sh`, `config.example.toml:41`.

Этот гайд — **блок Teams** из требуемой H6-инструкции (три блока: Zoom / Meet / Teams). Zoom и Meet — по той же схеме, меняются только имена узлов и ловушки.

---

## 1. Как это работает (30 сек)

Teams — единственный из трёх, где есть **два разных узла**:

- **Teams Desktop** (Electron) — создаёт собственный PipeWire-клиент `teams` / `Microsoft Teams`.
- **Teams Web** (Chrome/Firefox) — идёт как `Chromium` / `Firefox`, как и Meet.

Остальное как у Meet: два независимых захвата `streams.meeting` + `streams.microphone` (`config.example.toml:41-44`), выбор узла в UI `http://127.0.0.1:8790` → `Diagnostics`.

**Ограничения (H6:74):** если проверялся только один вариант (например web), так и писать — не обобщать. Desktop-узел появляется только после входа в звонок.

---

## 2. Предварительно

```bash
systemctl --user status pipewire pipewire-pulse wireplumber | head
which wpctl pw-cli pw-link pavucontrol  # любой достаточно
```

---

## 3. Создание виртуального sink (B7)

Тот же sink, что и для Meet — один на все клиенты:

```bash
bash scripts/create_pipewire_sources.sh
# Ожидаемый вывод:
# Creating PipeWire virtual sink: speech-local.monitor
# Virtual sink speech-local.monitor created

# Проверка (имена на твоей машине будут другими — H6:58):
pw-cli list-objects | grep -A2 speech-local.monitor
pactl list sinks short | grep speech-local
```

Если `pw-cli create-node` падает — ставь `pipewire-pulse` и перезапусти `systemctl --user restart pipewire`.

---

## 4. MS Teams — пошагово

### 4.1 Вариант A — Teams Desktop (рекомендуется для чистого сигнала)

1. Установи Teams Desktop (`.deb` с сайта Microsoft) и войди в тестовый звонок — **до входа узла нет** (H6:171 — искать несуществующий узел нельзя, сначала войти в встречу).
2. Открой `pavucontrol` → `Playback`:
   - Найди `Microsoft Teams` / `teams` (имя зависит от версии, на твоей машине будет другое — H6:59).
   - Переключи его `Output` на `speech-local monitor sink`.
3. Альтернатива без GUI:
   ```bash
   # Имена на твоей машине будут другими
   wpctl status | grep -A2 -i teams
   pw-link -o | grep -i -E "teams|speech-local"
   # Должен показать Sink Input teams → speech-local.monitor
   ```

**Плюс Desktop:** в `Playback` виден именно Teams, можно изолировать его звук от остального браузера.

### 4.2 Вариант B — Teams Web (Chrome / Firefox)

Идентично Meet:

1. Открой Teams в браузере, войди в звонок.
2. `pavucontrol → Playback → Chromium / Firefox: AudioCallbackDriver → Output → speech-local monitor sink`.
3. Проверка:
   ```bash
   wpctl status
   # Ищи в Sink Inputs: Chromium / Firefox (а не teams)
   pw-link -o | grep -i -E "chromium|firefox|speech-local"
   ```

**Минус Web:** как и у Meet — **весь звук браузера** (вкладки, YouTube) попадёт в `meeting` (H6:64). Для чистого сигнала держи Teams Web в отдельном профиле `chromium --user-data-dir=/tmp/teams`.

### 4.3 В speech-local — выбрать узел встречи

```toml
# config.toml — руками или через UI
[streams.meeting]
pipewire_node = "speech-local.monitor"
source_language = "en"   # язык участников
target_language = "ru"
enabled = true

[streams.microphone]
pipewire_node = ""       # пусто → default alsa_input
source_language = "ru"
target_language = "en"
```

Или UI: `http://127.0.0.1:8790 → Settings → Audio → meeting → speech-local.monitor`.

Узел для `meeting` — один и тот же (`speech-local.monitor`) для обоих вариантов; меняется только что в него направляет звук (Teams Desktop vs браузер).

---

## 5. Проверка до первого звонка (H6:68 — обязательно)

```bash
# Включи любое видео со звуком (для Desktop — внутри Teams, для Web — в браузере)
xdg-open http://127.0.0.1:8790  # Diagnostics
# Ожидаешь: meeting и microphone > -50 dB, VAD: active
```

CLI:

```bash
wpctl status | grep -A2 Sinks
curl -s http://127.0.0.1:8790/api/snapshot | python3 -m json.tool | grep -A2 '"ui"'
```

Если `meeting` молчит — смотри таблицу ниже.

---

## 6. Диагностика (H6 §9)

| Симптом | Вероятная причина | Проверка |
|---|---|---|
| `meeting` тишина, `microphone` есть — Desktop | Узел `teams` не направлен в sink (появляется только в звонке) | Войди в звонок → `pavucontrol → Playback → teams → speech-local.monitor`; `pw-link -o \| grep teams` |
| `meeting` тишина — Web | Браузер не направлен в sink | `pavucontrol → Playback → Chromium → speech-local.monitor`; `wpctl status` |
| `speech-local.monitor` не создаётся | Нестандартный PipeWire / нет `pw-cli` | `pw-cli list-objects` ошибка → `pipewire-pulse`, `systemctl --user restart pipewire` |
| Весь звук вкладок в транскрипте (Web) | Ограничение браузерного клиента | Отдельный профиль браузера без других вкладок |
| Перевод не появляется | Ключ BYOK истёк (60 мин) или облако недоступно | Индикатор провайдера в UI, `POST /api/key {"provider":"gemini"}` заново |
| Задержка выросла | Очередь STT, память у порога `memory.high_mb 1750` | Diagnostics → `backlog_ms`, `queue_depth` |

Быстрая диагностика: `bash scripts/diagnose_hardware.sh`

---

## 7. Приватность и профили

- **open** — `meeting_audio` уходит в облако для перевода.
- **confidential** — в облако только текст. Переключи до старта сессии (`POST /api/privacy {"profile":"confidential"}`).

Ключ BYOK — RAM 60 мин, маскируется `...3RRc`, не в `speech.db` (`app/security/byok.py:14`).

---

## 8. Ссылки

- `config.example.toml` — секции `streams.*`
- `docs/SPEC_speech_local_v2.md:552` — §18 PipeWire
- `CONTRACTS/H6_readme.md` — требования H6 (три блока, честность BYOK, диагностика)
- `docs/MEET_pipewire.md` — аналогичный блок для Google Meet (браузерный захват)
- `scripts/create_pipewire_sources.sh` — B7
- `scripts/diagnose_hardware.sh` — уровни и узлы

> Имена `alsa_input.*`, `teams`, `Chromium`, `speech-local.monitor` — примеры. На твоей машине будут другие — это нормально (H6:59).
