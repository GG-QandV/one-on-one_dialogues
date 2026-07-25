"""app/errors.py — общие типы исключений.

Единая иерархия нужна по двум причинам:
1. Обработчики верхнего уровня различают «ошибка среды» (можно повторить)
   и «нарушение инварианта» (повторять бессмысленно, нужен разбор).
2. LogRedactor (app/security/redactor.py) обрабатывает трейсы исключений;
   исключения не должны нести секретов в аргументах — см. SecretFreeError.
"""

from __future__ import annotations


class SpeechLocalError(Exception):
    """Базовое исключение проекта."""

    #: Машиночитаемый код, попадает в jobs.error_code. Переопределяется потомком.
    code: str = "UNKNOWN"

    #: Разрешён ли автоматический повтор операции.
    retryable: bool = False


class SecretFreeError(SpeechLocalError):
    """Исключение, в аргументы которого запрещено помещать секреты.

    Все исключения, пересекающие границу с облачными провайдерами,
    наследуются отсюда. Проверяется тестом test_log_redaction.py.
    """


# ------------------------------------------------------------ инварианты

class InvariantViolation(SpeechLocalError):
    """Нарушение инварианта целостности. Повтор бессмысленен."""

    code = "INVARIANT_VIOLATION"
    retryable = False


class ImmutableFieldError(InvariantViolation):
    code = "IMMUTABLE_FIELD"

    def __init__(self, table: str, field: str, row_id: str) -> None:
        super().__init__(f"{table}.{field} неизменяем (row_id={row_id})")
        self.table = table
        self.field = field
        self.row_id = row_id


class PrivacyViolation(InvariantViolation):
    """Попытка выполнить операцию, запрещённую текущим профилем.

    Блокирующий дефект по разделу 17 роадмапа. Никогда не подавляется,
    никогда не преобразуется в предупреждение.
    """

    code = "PRIVACY_VIOLATION"

    def __init__(self, capability: str, profile: str) -> None:
        super().__init__(
            f"операция '{capability}' запрещена в профиле '{profile}'"
        )
        self.capability = capability
        self.profile = profile


class StaleGenerationError(SpeechLocalError):
    """Результат получен под устаревшим поколением профиля и отброшен.

    Не ошибка в строгом смысле: штатный исход при переключении профиля
    в момент, когда облачный запрос был в полёте.
    """

    code = "STALE_GENERATION"
    retryable = False


# ------------------------------------------------------------- хранилище

class StorageError(SpeechLocalError):
    code = "STORAGE_ERROR"
    retryable = True


class WriterQueueFull(StorageError):
    code = "WRITER_QUEUE_FULL"
    retryable = True


class DatabaseClosed(StorageError):
    code = "DB_CLOSED"
    retryable = False


class MigrationError(StorageError):
    code = "MIGRATION_ERROR"
    retryable = False


# ---------------------------------------------------------------- очередь

class JobError(SpeechLocalError):
    code = "JOB_ERROR"


class JobNotFound(JobError):
    code = "JOB_NOT_FOUND"
    retryable = False


class LeaseLost(JobError):
    """Аренда истекла или перехвачена: результат воркера не принимается."""

    code = "LEASE_LOST"
    retryable = False


class NonRetryableJob(JobError):
    """Задача упала и не является идемпотентной — автоповтор запрещён."""

    code = "NON_RETRYABLE"
    retryable = False


# ------------------------------------------------------------------ аудио

class AudioError(SpeechLocalError):
    code = "AUDIO_ERROR"
    retryable = True


class NodeNotFound(AudioError):
    code = "PIPEWIRE_NODE_NOT_FOUND"
    retryable = False


class CaptureInterrupted(AudioError):
    code = "CAPTURE_INTERRUPTED"
    retryable = True


# -------------------------------------------------------------------- STT

class SttError(SpeechLocalError):
    code = "STT_ERROR"
    retryable = True


class ModelNotAvailable(SttError):
    code = "STT_MODEL_NOT_AVAILABLE"
    retryable = False


class SttOutputMalformed(SttError):
    code = "STT_OUTPUT_MALFORMED"
    retryable = False


# ------------------------------------------------------------- провайдеры

class ProviderError(SecretFreeError):
    code = "PROVIDER_ERROR"
    retryable = True


class ProviderAuthError(ProviderError):
    code = "PROVIDER_AUTH"
    retryable = False


class ProviderRateLimited(ProviderError):
    code = "PROVIDER_RATE_LIMITED"
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "PROVIDER_UNAVAILABLE"
    retryable = True


class ProviderResponseInvalid(ProviderError):
    code = "PROVIDER_RESPONSE_INVALID"
    retryable = True


# --------------------------------------------------------------- деградация

class DegradationRequired(SpeechLocalError):
    """Сигнал watchdog: превышен порог, требуется снижение нагрузки."""

    code = "DEGRADATION_REQUIRED"
    retryable = False

    def __init__(self, level: int, reason: str) -> None:
        super().__init__(f"уровень {level}: {reason}")
        self.level = level
        self.reason = reason
