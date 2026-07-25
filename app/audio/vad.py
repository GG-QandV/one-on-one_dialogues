"""app/audio/vad.py — детектор речевой активности. Задача C5a.

Спека: раздел 5 «Сегментация и бюджет задержки».

Почему энергетический VAD, а не нейросетевой
--------------------------------------------
Спека (раздел 6) запрещает тяжёлый Python-ML runtime в MVP, а бюджет памяти
2.1 ГБ уже поделён. Silero VAD дал бы более точные границы, но тянет за собой
PyTorch. Энергетический детектор с адаптивным порогом занимает единицы
килобайт и работает за микросекунды на кадр.

Плата за это зафиксирована как риск R8 роадмапа (40-50%): на шумном канале
или при тихом говорящем границы будут хуже. Модуль спроектирован так, чтобы
замена реализации не задела остальной конвейер: сегментатор зависит от
интерфейса `VadDetector.process()`, а не от способа принятия решения.

Три механизма, без которых энергетический VAD непригоден
--------------------------------------------------------
1. **Адаптивный порог шума.** Фиксированный порог в dBFS не работает: уровень
   шума меняется от гарнитуры к гарнитуре и в пределах одного звонка. Порог
   считается относительно текущего уровня фона.

2. **Гистерезис.** Один порог на вход и выход даёт дребезг: на границе речи
   детектор переключается каждые 20 мс. Порог входа выше порога выхода.

3. **Дебаунс и hangover.** Щелчок клавиатуры не должен открывать сегмент,
   а пауза между словами внутри фразы — закрывать его. Речь считается
   начавшейся после N подряд активных кадров и закончившейся не раньше, чем
   через hangover после последнего активного.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.audio.pcm import FRAME_MS, Frame

log = logging.getLogger(__name__)


class VadState(str, Enum):
    SILENCE = "silence"
    #: Кандидат: активные кадры пошли, но дебаунс ещё не набран.
    RISING = "rising"
    SPEECH = "speech"
    #: Кандидат на закрытие: тишина пошла, но hangover ещё не истёк.
    FALLING = "falling"


class VadEventType(str, Enum):
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass(frozen=True, slots=True)
class VadEvent:
    type: VadEventType
    #: Для START — время начала речи с учётом дебаунса (откат назад).
    #: Для END — время последнего активного кадра, без hangover.
    at_ms: int
    noise_floor_db: float
    level_db: float


@dataclass(frozen=True, slots=True)
class VadConfig:
    """Все значения приходят из config.toml, секция [vad]."""

    #: Насколько кадр должен превышать фон, чтобы считаться речью.
    onset_offset_db: float = 9.0
    #: Порог выхода. Ниже порога входа — это и есть гистерезис.
    release_offset_db: float = 5.0
    #: Абсолютный минимум: ниже этого уровня речи не бывает даже в тишине.
    absolute_floor_db: float = -55.0
    #: Сколько подряд активных кадров нужно, чтобы признать начало речи.
    onset_debounce_ms: int = 100
    #: Сколько держать состояние SPEECH после последнего активного кадра.
    hangover_ms: int = 220
    #: Постоянная времени адаптации фона вверх (шум вырос) — медленно.
    noise_rise_per_s_db: float = 3.0
    #: Вниз (шум упал) — быстро, иначе детектор надолго «оглохнет».
    noise_fall_per_s_db: float = 24.0
    #: Длина окна для стартовой калибровки фона.
    calibration_ms: int = 600

    @property
    def onset_frames(self) -> int:
        return max(1, self.onset_debounce_ms // FRAME_MS)

    @property
    def hangover_frames(self) -> int:
        return max(1, self.hangover_ms // FRAME_MS)

    @property
    def calibration_frames(self) -> int:
        return max(1, self.calibration_ms // FRAME_MS)


@dataclass
class VadStats:
    """Для диагностического экрана (E5)."""

    state: VadState = VadState.SILENCE
    noise_floor_db: float = -60.0
    last_level_db: float = -100.0
    speech_frames: int = 0
    silence_frames: int = 0
    onsets: int = 0
    calibrated: bool = False
    _levels: deque[float] = field(default_factory=lambda: deque(maxlen=50), repr=False)

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "noise_floor_db": round(self.noise_floor_db, 1),
            "level_db": round(self.last_level_db, 1),
            "level_history_db": [round(x, 1) for x in self._levels],
            "speech_ratio": round(
                self.speech_frames / max(1, self.speech_frames + self.silence_frames), 3
            ),
            "onsets": self.onsets,
            "calibrated": self.calibrated,
        }


class VadDetector:
    """Потоковый детектор речи. Один экземпляр на аудиопоток.

    Использование::

        vad = VadDetector(VadConfig())
        for frame in splitter.push(chunk.pcm):
            for event in vad.process(frame):
                ...

    Детектор не хранит аудио и не решает, где границы сегмента, — только
    сообщает, где речь. Решение о сегменте принимает segmenter.py: там же
    учитываются минимальная длина, принудительная резка и разрывы записи.
    """

    def __init__(self, config: VadConfig | None = None) -> None:
        self._cfg = config or VadConfig()
        self._state = VadState.SILENCE
        self._noise_db = self._cfg.absolute_floor_db
        self._active_run = 0
        self._silent_run = 0
        self._calibration: list[float] = []
        self._speech_started_ms: int | None = None
        self._last_active_ms: int | None = None
        self.stats = VadStats(noise_floor_db=self._noise_db)

    # ------------------------------------------------------------ состояние

    @property
    def state(self) -> VadState:
        return self._state

    @property
    def in_speech(self) -> bool:
        """SPEECH и FALLING считаются речью: hangover ещё не истёк."""
        return self._state in (VadState.SPEECH, VadState.FALLING)

    @property
    def noise_floor_db(self) -> float:
        return self._noise_db

    @property
    def last_active_ms(self) -> int | None:
        """Конец последнего кадра, признанного речью.

        Сегментатор обрезает по нему хвостовую тишину: пауза, по которой
        закрылся сегмент, не должна попадать в файл для whisper — это
        лишняя секунда аудио в каждом запросе и прямой вычет из бюджета
        задержки (спека, раздел 5).
        """
        return self._last_active_ms

    def silence_duration_ms(self, now_ms: int) -> int:
        """Сколько длится тишина с последнего активного кадра.

        Сегментатор сравнивает это значение с порогом паузы из конфига.
        """
        if self._last_active_ms is None:
            return 0
        return max(0, now_ms - self._last_active_ms)

    # ---------------------------------------------------------------- работа

    def process(self, frame: Frame) -> list[VadEvent]:
        """Обработать один кадр. Возвращает 0 или 1 событие.

        Список, а не Optional — чтобы добавление новых типов событий
        (например, детекта клиппинга) не меняло сигнатуру.
        """
        level = frame.dbfs
        self.stats.last_level_db = level
        self.stats._levels.append(level)

        if not self.stats.calibrated:
            self._calibrate(level)

        threshold_on = max(
            self._noise_db + self._cfg.onset_offset_db, self._cfg.absolute_floor_db
        )
        threshold_off = max(
            self._noise_db + self._cfg.release_offset_db,
            self._cfg.absolute_floor_db - 5.0,
        )

        # Гистерезис: в тишине сравниваем с высоким порогом, в речи — с низким.
        active = level > (threshold_off if self.in_speech else threshold_on)

        if active:
            self._active_run += 1
            self._silent_run = 0
            self._last_active_ms = frame.t_end_ms
            self.stats.speech_frames += 1
        else:
            self._silent_run += 1
            self._active_run = 0
            self.stats.silence_frames += 1
            # Фон обновляется только по неречевым кадрам: иначе громкая речь
            # поднимет порог и детектор перестанет её слышать.
            self._adapt_noise(level)

        events = self._advance(frame, active)
        self.stats.state = self._state
        self.stats.noise_floor_db = self._noise_db
        return events

    def _advance(self, frame: Frame, active: bool) -> list[VadEvent]:
        cfg = self._cfg

        if self._state in (VadState.SILENCE, VadState.RISING):
            if not active:
                self._state = VadState.SILENCE
                return []
            self._state = VadState.RISING
            if self._active_run < cfg.onset_frames:
                return []
            # Речь подтверждена. Начало относим назад, на первый активный кадр:
            # иначе дебаунс срезает первые 100 мс — как правило, начало слова.
            start_ms = max(0, frame.t_end_ms - self._active_run * FRAME_MS)
            self._state = VadState.SPEECH
            self._speech_started_ms = start_ms
            self.stats.onsets += 1
            return [
                VadEvent(
                    VadEventType.SPEECH_START, start_ms, self._noise_db, frame.dbfs
                )
            ]

        # SPEECH или FALLING
        if active:
            self._state = VadState.SPEECH
            return []

        self._state = VadState.FALLING
        if self._silent_run < cfg.hangover_frames:
            return []

        end_ms = self._last_active_ms or frame.t_start_ms
        self._state = VadState.SILENCE
        self._speech_started_ms = None
        return [VadEvent(VadEventType.SPEECH_END, end_ms, self._noise_db, frame.dbfs)]

    # --------------------------------------------------------------- фон

    def _calibrate(self, level: float) -> None:
        """Стартовая калибровка по первым кадрам.

        Без неё первая фраза сессии почти всегда теряется: фон инициализирован
        абсолютным минимумом, порог входа задран, речь не проходит.
        """
        self._calibration.append(level)
        if len(self._calibration) < self._cfg.calibration_frames:
            return
        ordered = sorted(self._calibration)
        # Медиана устойчивее среднего к одиночным щелчкам при старте захвата.
        self._noise_db = max(
            ordered[len(ordered) // 2], self._cfg.absolute_floor_db
        )
        self.stats.calibrated = True
        self._calibration.clear()
        log.debug("VAD откалиброван, фон %.1f dBFS", self._noise_db)

    def _adapt_noise(self, level: float) -> None:
        """Асимметричная адаптация фона.

        Вверх медленно: иначе затяжная тихая речь будет принята за шум и
        поднимет порог. Вниз быстро: если шум исчез (выключили вентилятор),
        детектор должен вернуть чувствительность за доли секунды.
        """
        step_up = self._cfg.noise_rise_per_s_db * FRAME_MS / 1000
        step_down = self._cfg.noise_fall_per_s_db * FRAME_MS / 1000
        if level > self._noise_db:
            self._noise_db = min(level, self._noise_db + step_up)
        else:
            self._noise_db = max(
                self._cfg.absolute_floor_db, self._noise_db - step_down
            )

    # -------------------------------------------------------------- сброс

    def reset(self, *, recalibrate: bool = False) -> None:
        """Сброс после разрыва записи.

        Разрыв означает, что накопленное состояние не описывает новый поток:
        устройство могли сменить, уровень изменился. Пересчёт фона —
        по флагу: при коротком xrun это лишнее, при смене устройства нужно.
        """
        self._state = VadState.SILENCE
        self._active_run = 0
        self._silent_run = 0
        self._speech_started_ms = None
        self._last_active_ms = None
        if recalibrate:
            self._calibration.clear()
            self.stats.calibrated = False
            self._noise_db = self._cfg.absolute_floor_db
