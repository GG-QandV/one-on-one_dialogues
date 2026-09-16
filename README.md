# speech-local v2.0

Offline-first speech translation & draft assistant for Zoom/Google Meet/MS Teams.

## Quick Start

```bash
# 1. Install
pip install -e .
pip install -e ".[dev]"   # for development

# 2. Whisper model
scripts/download_model.sh base

# 3. PipeWire setup (only if pw-record is not working)
scripts/create_pipewire_sources.sh

# 4. Config
cp config.example.toml config.toml
# edit config.toml

# 5. Run
python -m app.main
# UI: http://127.0.0.1:8790
```

## Architecture

Two processing tracks:

- **Accurate track** — local whisper.cpp → raw_text (immutable) → LLM translation
- **Fast track** — partial results → cloud realtime (open profile only)

Privacy profiles: **open** (audio and text go to the cloud) / **confidential** (text only).

See: [SPEC_speech_local_v2.md](docs/SPEC_speech_local_v2.md), [INTERFACES.md](INTERFACES.md).

## System Requirements

- Linux with PipeWire
- Python ≥ 3.12
- Recommended: 16+ GB RAM, whisper.cpp (base model ~450 MB RSS)

## Configuration

`config.toml` — all settings in one file: languages, providers, VAD, memory thresholds, hotkey.

API keys are entered via the UI (BYOK, kept in RAM for 60 minutes, never written to logs or files):

| Provider         | Translation | Drafts |
| ---------------- | ----------- | ------ |
| Gemini           | ✅           | ✅      |
| Claude           | ✅           | ❌      |
| OpenAI Realtime  | ✅ (fast track) | ❌   |

## PipeWire — Zoom / Google Meet / MS Teams

Все три клиента подключаются **через PipeWire**, без внешних API. Детальные гайды (H6-совместимо):

* **Zoom** — [docs/ZOOM_pipewire.md](docs/ZOOM_pipewire.md) (десктоп, изолированный узел, ловушка `вход в звонок → узел появляется`)
* **Google Meet** — [docs/MEET_pipewire.md](docs/MEET_pipewire.md) (браузерный захват, `speech-local.monitor`)
* **MS Teams** — [docs/TEAMS_pipewire.md](docs/TEAMS_pipewire.md) (Desktop vs Web, разные узлы, ловушка `вход в звонок → только тогда появляется узел`)

Коротко (одинаково для всех): `bash scripts/create_pipewire_sources.sh` → `speech-local.monitor` → в `pavucontrol → Playback: <Chromium|teams> → speech-local monitor sink` → `config.toml: [streams.meeting] pipewire_node = "speech-local.monitor"` → проверка `wpctl status` + `http://127.0.0.1:8790` Diagnostics.

> Браузерный Meet/Teams Web захватывают **весь звук браузера** (соседние вкладки) — это ограничение, не баг. Teams Desktop — изолированный узел `teams`.

## Scripts

| Script                              | Purpose                                   |
| ----------------------------------- | ----------------------------------------- |
| `scripts/install_whispercpp.sh`     | Build whisper.cpp                         |
| `scripts/download_model.sh`         | Download models (base/tiny/small)         |
| `scripts/create_pipewire_sources.sh`| Virtual sink for meeting capture          |
| `scripts/diagnose_hardware.sh`      | Audio & system diagnostics                |

## Systemd

```bash
sudo cp systemd/speech-gateway.service /etc/systemd/system/
sudo cp systemd/speech.env.example /etc/speech-local.env
# edit /etc/speech-local.env
sudo systemctl enable --now speech-gateway
```

## Development

```bash
# Tests
pytest -v

# Linter
ruff check app/
ruff format app/ --check

# Types
mypy app/
```

## License

**Business Source License 1.1** (BSL 1.1). See [LICENSE](LICENSE).

- **Commercial use** — under a separate license (contact the licensor).
- **Personal/non-commercial use** — free (open-source access to the code).
