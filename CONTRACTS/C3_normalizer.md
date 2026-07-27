# C3 — нормализатор PCM (Python fallback)

| | |
| :-- | :-- |
| Файл | `app/audio/normalizer.py` |
| Уровень | Middle |
| Оценка | 120 LOC, 8 тестов |
| Зависит от | `app/audio/pcm.py` (константы формата) |
| Блокирует | Нет (bpw-record и FFmpeg уже нормализуют сами) |
| Пункт спеки | §7 «Стек аудио», решение владельца №1 |

## Контекст

В проекте три звена нормализации, иерархия жёсткая:

1. **pw-record** (основной): сам приводит к 16k/mono/s16le через PipeWire-граф (`--rate 16000 --channels 1 --format s16`).
2. **FFmpeg** (первый fallback): те же параметры в `-ar`/`-ac`/`-f s16le` при захвате через `pipewire-pulse`.
3. **C3 `normalizer.py`** (второй fallback): когда FFmpeg недоступен, а аудио уже захвачено в другом формате.

C3 — НЕ обёртка FFmpeg. Это Python-ресемплинг без внешнего процесса. Если FFmpeg доступен и формат нецелевой — нормализацию делает FFmpeg, C3 не вызывается.

## Интерфейс

```python
@dataclass(frozen=True, slots=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_width: int       # байт на отсчёт, сейчас только 2 (s16le)

FORMAT_TARGET: AudioFormat = AudioFormat(16000, 1, 2)

def needs_normalization(fmt: AudioFormat) -> bool
    # True если fmt != FORMAT_TARGET

def normalize(pcm: bytes, src_fmt: AudioFormat) -> bytes
    # Привести к FORMAT_TARGET через audioop
    # raise ValueError если src_fmt неподдерживаемый
```

## Поведение

1. **`needs_normalization`** — тривиальное сравнение с `FORMAT_TARGET`. Быстрый путь: формат уже целевой → `False`, нормализатор не запускается.

2. **`normalize`** — ресемплинг + свёртка каналов. Только `audioop` из stdlib:
   - `audioop.ratecv` для ресемплинга (любой `src_rate → 16000`);
   - `audioop.tomono` для свёртки стерео/мультиканала в моно (фактор 0.5 на каждый канал);
   - `audioop.lin2lin` при несовпадении `sample_width` (сейчас 2 → 2, no-op, но интерфейс единообразен);
   - состояние ресемплера (state) не хранится между вызовами — каждый вызов самодостаточен.

3. **Формат на входе** — PCM s16le, любой sample_rate и channels. Другие sample_width (1, 4) не поддерживаются в MVP — `ValueError` с сообщением.

4. **Размер на выходе** — `len(pcm) * (16000/src_rate) * (1/src_channels)` (для s16le). При нецелом — округление вниз до чётного (чётность нужна для s16le alignment).

5. **Функция чистая.** Ни состояния, ни кэшей, ни I/O, ни внешних процессов. Два вызова с одинаковыми аргументами дают идентичный результат.

## Запрещено

- Вызывать FFmpeg, sox, whisper-cpp или любой внешний процесс.
- Иметь зависимость от `numpy`, `scipy`, `soundfile`, `pydub` и т.п.
- Хранить состояние ресемплера между вызовами (state из `ratecv` не переиспользуется).
- Принимать float32 PCM — только s16le.
- Делать I/O любого рода.
- Бросать исключения, кроме `ValueError` при неподдерживаемом формате.

## Критерии приёмки

- [ ] `needs_normalization(FORMAT_TARGET) == False`
- [ ] `needs_normalization(AudioFormat(48000, 2, 2)) == True`
- [ ] `normalize(pcm, FORMAT_TARGET) == pcm` (идентичные байты)
- [ ] Стерео 48 кГц → моно 16 кГц: длина выхода = `len(pcm) * 16000 // 48000 // 2` (деление на 2 для свёртки каналов) и округлено до чётного
- [ ] Эквивалентность FFmpeg vs C3: на одном и том же входе (стерео 48 кГц) оба дают PCM, чья RMS отличается не более чем на 10% (допуск на разную реализацию ресемплинга)
- [ ] `normalize(b"", AudioFormat(16000, 1, 2)) == b""`
- [ ] Неподдерживаемый sample_width → `ValueError`
- [ ] Нет вызовов `subprocess`, `os.system`, `shutil.which` (проверка import-инспекцией)
- [ ] Размер выхода при нецелом результате — чётное число

## Зависимость: audioop и Python 3.13+

`audioop` удалён в Python 3.13+ (PEP 594). Проект использует Python ≥3.12 (pyproject.toml). Если миграция на 3.13+ произойдёт до перехода C3 в поддержку — добавить `audioop-lts` в `[project.dependencies]`.

Зафиксировать версию Python в PR к C3.

## Подсказки

```python
import audioop

def normalize(pcm: bytes, src_fmt: AudioFormat) -> bytes:
    target = FORMAT_TARGET
    # 1. Конверсия sample_width (пока только 2→2, заглушка)
    if src_fmt.sample_width != target.sample_width:
        pcm = audioop.lin2lin(pcm, src_fmt.sample_width, target.sample_width)
    # 2. Свёртка каналов
    if src_fmt.channels != target.channels:
        pcm = audioop.tomono(pcm, target.sample_width, 0.5, 0.5)
    # 3. Ресемплинг
    if src_fmt.sample_rate != target.sample_rate:
        pcm, _state = audioop.ratecv(
            pcm, target.sample_width, target.channels,
            src_fmt.sample_rate, target.sample_rate, None
        )
    return pcm
```

Ловушка: `audioop.ratecv` после свёртки каналов работает уже с моно — `nchannels=1`.
