"""app/privacy.py — профили конфиденциальности. Задача J1 роадмапа.

Спека: раздел 2 «Профили конфиденциальности», критерий приёмки 7.
Роадмап, правило 8: любой код, отправляющий данные наружу, обязан проверять
текущий профиль; отсутствие проверки — блокирующий дефект.

Задача модуля
-------------
Профиль переключается посреди разговора. В этот момент в полёте могут быть:
  * открытая WebSocket-сессия облачного realtime-провайдера;
  * несколько HTTP-запросов на перевод текста;
  * куски аудио в буферах отправки.

Наивная проверка «if profile == open» в момент отправки недостаточна: между
проверкой и фактической отправкой проходит время, и переключение может
случиться внутри этого окна. Классическая гонка TOCTOU
(time-of-check to time-of-use — проверка и использование разнесены во времени).

Решение — fencing token (ограждающий токен): каждое переключение профиля
увеличивает счётчик поколений. Операция захватывает поколение в момент
старта и обязана предъявить его при завершении. Результат, полученный под
устаревшим поколением, отбрасывается, а не записывается.

Гарантия по времени
-------------------
Критерий 7 требует прекращения исходящего аудиотрафика в течение 500 мс.
Обеспечивается двумя механизмами:
  1. Поколение увеличивается синхронно, до любого ожидания — новые отправки
     отсекаются немедленно.
  2. Зарегистрированные teardown-хуки закрываются с жёстким дедлайном;
     не уложившийся хук отменяется, транспорт закрывается принудительно.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from app.errors import PrivacyViolation, StaleGenerationError

log = logging.getLogger(__name__)

#: Жёсткий дедлайн закрытия облачных аудио-сессий, мс. Критерий приёмки 7.
TEARDOWN_DEADLINE_MS = 500


class PrivacyProfile(str, Enum):
    OPEN = "open"
    CONFIDENTIAL = "confidential"


class Capability(str, Enum):
    """Что компонент собирается сделать. Проверяется до действия."""

    LOCAL_STT = "local_stt"
    LOCAL_STORAGE = "local_storage"
    #: Отправка текстового сегмента облачному переводчику.
    TEXT_TO_CLOUD = "text_to_cloud"
    #: Отправка аудио облачному провайдеру (быстрый трек).
    AUDIO_TO_CLOUD = "audio_to_cloud"
    #: Генерация черновика ответа (текст + библиотека фактов в облако).
    DRAFT_GENERATION = "draft_generation"
    LOCAL_EXPORT = "local_export"


#: Матрица изоляции. Формальный источник для тестов (задача A6, J4).
#: Изменение матрицы = изменение приватностной модели = ревью владельца.
CAPABILITY_MATRIX: dict[PrivacyProfile, frozenset[Capability]] = {
    PrivacyProfile.OPEN: frozenset(
        {
            Capability.LOCAL_STT,
            Capability.LOCAL_STORAGE,
            Capability.TEXT_TO_CLOUD,
            Capability.AUDIO_TO_CLOUD,
            Capability.DRAFT_GENERATION,
            Capability.LOCAL_EXPORT,
        }
    ),
    PrivacyProfile.CONFIDENTIAL: frozenset(
        {
            Capability.LOCAL_STT,
            Capability.LOCAL_STORAGE,
            Capability.TEXT_TO_CLOUD,
            Capability.DRAFT_GENERATION,
            Capability.LOCAL_EXPORT,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class Fence:
    """Снимок состояния профиля на момент начала операции.

    Захватывается перед отправкой, предъявляется при получении результата.
    """

    profile: PrivacyProfile
    generation: int

    def __str__(self) -> str:  # для логов
        return f"{self.profile.value}#{self.generation}"


class TeardownHook(Protocol):
    """Контракт компонента, держащего облачное аудио-соединение.

    Реализуется app/translation/providers/openai_realtime.py (задача D5).
    """

    name: str

    async def teardown(self) -> None:
        """Закрыть соединение и сбросить буферы отправки.

        Обязан быть отменяемым: при превышении дедлайна вызывающий выполнит
        cancel(), после чего компонент должен закрыть транспорт жёстко.
        """
        ...

    def force_close(self) -> None:
        """Синхронное аварийное закрытие. Вызывается после отмены teardown."""
        ...


AuditWriter = Callable[[dict[str, Any]], Awaitable[None]]


class PrivacyController:
    """Единственный владелец текущего профиля.

    Ни один компонент не хранит профиль у себя. Хранение копии — источник
    рассинхронизации: копия не обновится при переключении.
    """

    def __init__(
        self,
        initial: PrivacyProfile,
        *,
        audit_writer: AuditWriter | None = None,
        teardown_deadline_ms: int = TEARDOWN_DEADLINE_MS,
    ) -> None:
        self._profile = initial
        self._generation = 0
        self._hooks: dict[str, TeardownHook] = {}
        self._switch_lock = asyncio.Lock()
        self._audit = audit_writer
        self._deadline_s = teardown_deadline_ms / 1000
        self._listeners: list[Callable[[PrivacyProfile, int], None]] = []
        self._last_teardown_ms: int | None = None

    # ------------------------------------------------------------ состояние

    @property
    def profile(self) -> PrivacyProfile:
        return self._profile

    @property
    def generation(self) -> int:
        return self._generation

    def fence(self) -> Fence:
        """Захватить текущее состояние перед началом операции."""
        return Fence(self._profile, self._generation)

    def snapshot(self) -> dict[str, Any]:
        """Для диагностического экрана (E5)."""
        return {
            "profile": self._profile.value,
            "generation": self._generation,
            "open_cloud_sessions": sorted(self._hooks),
            "last_teardown_ms": self._last_teardown_ms,
        }

    # -------------------------------------------------------------- проверки

    def allows(self, capability: Capability) -> bool:
        return capability in CAPABILITY_MATRIX[self._profile]

    def require(self, capability: Capability) -> Fence:
        """Проверить право и захватить fence одним действием.

        Возвращает fence, который вызывающий обязан предъявить в validate()
        перед использованием результата. Раздельные вызовы allows() и fence()
        оставляют окно гонки — этот метод его закрывает.
        """
        if capability not in CAPABILITY_MATRIX[self._profile]:
            raise PrivacyViolation(capability.value, self._profile.value)
        return Fence(self._profile, self._generation)

    def validate(self, fence: Fence, capability: Capability) -> None:
        """Проверить, что операция всё ещё легитимна.

        Вызывается перед записью результата облачной операции в БД или UI.
        Расхождение поколений означает, что профиль переключили, пока запрос
        был в полёте: результат отбрасывается.
        """
        if fence.generation != self._generation:
            raise StaleGenerationError(
                f"результат под {fence} отброшен, текущее поколение "
                f"{self._generation}"
            )
        if capability not in CAPABILITY_MATRIX[self._profile]:
            raise PrivacyViolation(capability.value, self._profile.value)

    def is_stale(self, fence: Fence) -> bool:
        """Мягкая проверка без исключения — для фильтрации в циклах."""
        return fence.generation != self._generation

    # ------------------------------------------------------- teardown-хуки

    def register_hook(self, hook: TeardownHook) -> None:
        if hook.name in self._hooks:
            raise ValueError(f"хук '{hook.name}' уже зарегистрирован")
        self._hooks[hook.name] = hook
        log.debug("зарегистрирован teardown-хук: %s", hook.name)

    def unregister_hook(self, name: str) -> None:
        self._hooks.pop(name, None)

    def add_listener(self, fn: Callable[[PrivacyProfile, int], None]) -> None:
        """Слушатель переключения: UI, метрики. Синхронный, не блокирующий."""
        self._listeners.append(fn)

    # ---------------------------------------------------------- переключение

    async def switch(
        self,
        target: PrivacyProfile,
        *,
        session_id: str | None = None,
        reason: str = "user",
    ) -> int:
        """Переключить профиль. Возвращает фактическое время teardown в мс.

        Порядок операций критичен:
          1. Инкремент поколения — синхронно, до любого await. С этого момента
             все новые отправки под старым fence отсекаются validate().
          2. Смена профиля — новые require() уже видят новые правила.
          3. Закрытие облачных сессий с жёстким дедлайном.
          4. Запись в аудит.

        Шаги 1-2 не могут быть прерваны: между ними нет точек await.
        """
        async with self._switch_lock:
            if target == self._profile:
                return 0

            previous = self._profile
            started = time.perf_counter()

            # --- шаги 1-2: атомарны относительно event loop, без await ---
            self._generation += 1
            self._profile = target
            generation = self._generation
            # -------------------------------------------------------------

            log.warning(
                "профиль переключён: %s -> %s (поколение %d, причина: %s)",
                previous.value,
                target.value,
                generation,
                reason,
            )
            self._notify(target, generation)

            # Шаг 3: закрываем всё, что больше не разрешено новым профилем.
            forbidden_cloud_audio = not self.allows(Capability.AUDIO_TO_CLOUD)
            if forbidden_cloud_audio and self._hooks:
                await self._teardown_all()

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._last_teardown_ms = elapsed_ms

            if forbidden_cloud_audio and elapsed_ms > self._deadline_s * 1000:
                # Не подавляем: превышение дедлайна — дефект, а не мелочь.
                log.error(
                    "teardown занял %d мс при дедлайне %d мс",
                    elapsed_ms,
                    int(self._deadline_s * 1000),
                )

            await self._write_audit(
                session_id=session_id,
                from_profile=previous,
                to_profile=target,
                generation=generation,
                teardown_ms=elapsed_ms,
                reason=reason,
            )
            return elapsed_ms

    async def _teardown_all(self) -> None:
        """Закрыть все облачные аудио-сессии с жёстким дедлайном."""
        hooks = list(self._hooks.values())
        tasks = {
            asyncio.create_task(hook.teardown(), name=f"teardown:{hook.name}"): hook
            for hook in hooks
        }
        done, pending = await asyncio.wait(tasks.keys(), timeout=self._deadline_s)

        for task in pending:
            hook = tasks[task]
            log.error(
                "хук '%s' не уложился в %d мс, принудительное закрытие",
                hook.name,
                int(self._deadline_s * 1000),
            )
            task.cancel()
            try:
                hook.force_close()
            except Exception:  # noqa: BLE001 — аварийный путь не должен падать
                log.exception("force_close хука '%s' завершился ошибкой", hook.name)

        for task in done:
            exc = task.exception()
            if exc is not None:
                hook = tasks[task]
                log.exception(
                    "teardown хука '%s' завершился ошибкой", hook.name, exc_info=exc
                )
                try:
                    hook.force_close()
                except Exception:  # noqa: BLE001
                    log.exception("force_close хука '%s' не удался", hook.name)

        self._hooks.clear()

    def _notify(self, profile: PrivacyProfile, generation: int) -> None:
        for fn in self._listeners:
            try:
                fn(profile, generation)
            except Exception:  # noqa: BLE001 — слушатель не ломает переключение
                log.exception("слушатель переключения профиля упал")

    async def _write_audit(self, **fields: Any) -> None:
        if self._audit is None:
            return
        try:
            await self._audit(fields)
        except Exception:  # noqa: BLE001
            # Профиль уже переключён; потеря записи аудита не должна
            # откатывать переключение, но обязана быть замечена.
            log.exception("запись в privacy_audit_log не удалась: %s", fields)


# ---------------------------------------------------------------- декоратор

def guarded(capability: Capability):
    """Обвязка для методов, отправляющих данные наружу.

    Проверяет право до вызова и валидность fence после. Метод обязан
    принимать именованный аргумент `fence`::

        class GeminiText:
            @guarded(Capability.TEXT_TO_CLOUD)
            async def translate(self, segment, *, fence: Fence): ...

    Декоратор не заменяет явную проверку в местах, где между захватом fence
    и отправкой есть собственная асинхронная логика — там validate() надо
    вызывать вручную непосредственно перед отправкой.
    """

    def decorator(fn):
        async def wrapper(self, *args, **kwargs):
            controller: PrivacyController = getattr(self, "privacy")
            fence = controller.require(capability)
            kwargs["fence"] = fence
            result = await fn(self, *args, **kwargs)
            controller.validate(fence, capability)
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        wrapper.__wrapped__ = fn
        return wrapper

    return decorator
