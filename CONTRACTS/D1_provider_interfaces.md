# D1 — базовые интерфейсы провайдеров перевода

| | |
| :-- | :-- |
| Файл | `app/translation/base.py` |
| Уровень | Middle |
| Оценка | 215 LOC, 11 тестов |
| Зависит от | `app/privacy.py` (готов), `app/errors.py` (готов), D4 (`prompts.audit`, отложенный импорт) |
| Блокирует | D2 (Gemini), D3 (Claude), D4 (промпты), I4 (перевод черновиков) |
| Пункт спеки | §10.1 |

## Задача

Определить общий контракт текстовых переводчиков и базовый класс, который
берёт на себя то, что обязаны делать все провайдеры одинаково: протокол
приватности, работу с ключом, классификацию ошибок. Провайдер-наследник
пишет только формирование запроса и разбор ответа.

Ошибки в этом слое дороги: он единственный, кто стоит между конвейером и
интернетом. Пропущенная проверка профиля здесь означает утечку у всех
провайдеров сразу.

## Интерфейс

```python
class TranslationMode(str, Enum):
    LIVE_LITERAL = "live_literal"
    LIVE_SAFE = "live_safe"
    POST_CLEAN = "post_clean"

@dataclass(frozen=True, slots=True)
class TranslationRequest:
    text: str
    source_language: str
    target_language: str
    mode: TranslationMode
    context: tuple[str, ...] = ()      # предыдущие raw_text, окно 4/3/2
    segment_id: str | None = None

@dataclass(frozen=True, slots=True)
class Change:
    type: Literal["filler_removed", "punctuation", "lost_entity", "other"]
    original: str
    replacement: str

@dataclass(frozen=True, slots=True)
class TranslationResult:
    translation_raw: str
    translation_clean: str | None = None
    changes: tuple[Change, ...] = ()
    provider_request_id: str | None = None

class TranslationProvider(Protocol):
    name: str
    privacy: PrivacyController
    async def translate(self, req: TranslationRequest, *,
                        fence: Fence) -> TranslationResult: ...
    async def close(self) -> None: ...

class BaseTranslationProvider(ABC):
    """Общая обвязка. Наследники реализуют _call и _parse."""

    Auditor = Callable[[TranslationRequest, TranslationResult],
                       TranslationResult]

    def __init__(self, name: str, privacy: PrivacyController,
                 key_provider: Callable[[], str],
                 *, timeout_s: float = 15.0,
                 auditor: Auditor | None = None) -> None: ...

    async def translate(self, req: TranslationRequest) -> TranslationResult:
        """Финальный метод. Наследникам переопределять ЗАПРЕЩЕНО."""

    @abstractmethod
    async def _call(self, req: TranslationRequest, key: str) -> str:
        """Сетевой вызов. Возвращает сырой текст ответа."""

    @abstractmethod
    def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult:
        """Разбор ответа провайдера."""

    async def close(self) -> None: ...
```

## Поведение

1. **Протокол приватности реализован в `translate`, один раз для всех:**

   ```
   fence  = privacy.require(Capability.TEXT_TO_CLOUD)
   key    = key_provider()                   # ProviderAuthError если пусто
   raw    = await _call(req, key)            # с таймаутом
   privacy.validate(fence, TEXT_TO_CLOUD)    # ДО возврата результата
   result = _parse(req, raw)
   return auditor(req, result)               # аудит инвариантов, п. 8
   ```

   Наследник не видит fence и не может его пропустить — метод `translate`
   объявлен финальным по соглашению, переопределение ловится ревью.

2. **Ключ запрашивается в момент вызова**, не в конструкторе и не хранится
   в поле. Владелец времени жизни ключа — `KeyStore` (G2).

3. **Таймаут** на `_call` через `asyncio.wait_for`. Превышение →
   `ProviderUnavailable` с указанием таймаута, но **без** текста запроса
   в сообщении (в тексте может быть содержимое переговоров).

4. **Классификация ошибок** — в базовом классе, метод
   `_classify(status_code: int, body: str) -> ProviderError`:

   | Код | Исключение | retryable |
   | :-- | :-- | :-- |
   | 401, 403 | `ProviderAuthError` | нет |
   | 429 | `ProviderRateLimited` | да |
   | 500–599 | `ProviderUnavailable` | да |
   | 400, 422 | `ProviderResponseInvalid` | нет |
   | прочее | `ProviderError` | да |

5. **Ретраев внутри нет.** Повтор — прерогатива `JobQueue`, которая знает
   про идемпотентность и backoff. Провайдер, который ретраит сам, ломает
   учёт попыток и удваивает счёт за облако.

6. **`translation_clean` заполняется только для `POST_CLEAN`.** Для двух
   live-режимов — `None`. `changes` наследник **не заполняет вовсе**
   (кроме разбора JSON `POST_CLEAN`): содержимое `changes` — результат
   аудита, см. п. 7.

7. **Аудит инвариантов — в базовом классе, один раз для всех.** После
   разбора ответа результат проходит через `auditor` (по умолчанию —
   `prompts.audit` из D4), который дописывает в `changes`:
   - для `LIVE_SAFE` — слова-паразиты, предъявленные к удалению;
   - для всех режимов — сущности, пропавшие из перевода: числа, суммы,
     даты, URL, обвал длины (`type="lost_entity"`).

   Аудит **не бросает исключений** и не отменяет перевод: он фиксирует
   подозрение на смысловой дрейф в `edit_log_json`, решение принимает
   человек. Наследник аудит пропустить не может — вызов внутри финального
   `translate`.

   **Разрыв цикла импортов.** `prompts` (D4) импортирует типы из `base`,
   поэтому `base` не импортирует `prompts` на уровне модуля. Фабрика
   провайдеров передаёт `auditor` явно; при `auditor is None` `translate`
   делает отложенный импорт внутри метода. Это единственное допустимое
   отступление от правила абсолютных импортов верхнего уровня.

8. **Валидация результата.** Пустой `translation_raw` при непустом входном
   тексте → `ProviderResponseInvalid`. Это ловит частый отказ моделей
   («не могу перевести») до записи в БД.

## Запрещено

- Переопределять `translate` в наследниках.
- Хранить ключ в поле объекта, в логах, в сообщениях исключений.
- Ретраить внутри провайдера.
- Логировать текст сегмента на уровне INFO и выше: содержимое переговоров
  не должно оседать в логах. DEBUG допустим при явном включении.
- Импортировать конкретные HTTP-библиотеки в `base.py`: базовый класс
  транспортно-нейтрален, `_call` реализуют наследники.
- Отключать аудит, делать его опциональным флагом конфига или переносить
  в наследников: пропущенный аудит у одного провайдера означает, что
  дрейф в его переводах не виден никому.
- Превращать результат аудита в исключение: подозрение на дрейф — не
  отказ перевода. Сегмент сохраняется, пометка уходит в `edit_log_json`.

## Критерии приёмки

- [ ] `translate` в конфиденциальном профиле проходит (текст разрешён)
- [ ] `translate` после `switch()` посреди вызова → `StaleGenerationError`,
      результат не возвращается
- [ ] Пустой ключ → `ProviderAuthError`, сетевого вызова не было
- [ ] Таймаут → `ProviderUnavailable`, в сообщении нет текста запроса
- [ ] Коды 401/429/503/400 дают правильные типы с правильным `retryable`
- [ ] Пустой ответ при непустом входе → `ProviderResponseInvalid`
- [ ] `LIVE_LITERAL` без потерь сущностей → `translation_clean is None`,
      `changes == ()`
- [ ] `auditor` вызывается ровно один раз за `translate`, после
      `privacy.validate` (проверяется подменённым аудитором-счётчиком)
- [ ] Перевод с пропавшим числом → в `changes` элемент `lost_entity`,
      исключения нет, `translation_raw` не изменён
- [ ] `auditor is None` → отложенный импорт `prompts.audit` отрабатывает,
      цикла импортов нет (тест импортирует `app.translation.base` первым)
- [ ] Ключ не встречается в `repr()` объекта и в тексте любого исключения
      (проверяется тестом, перебирающим все возбуждаемые исключения)

## Подсказки

Тест приватности пишется по образцу `tests/test_privacy_isolation.py`:
транспорт инжектируется, `_call` подменяется, утверждения — о том, что
именно ушло «в сеть».

Для проверки «ключ не течёт» удобно взять заведомо узнаваемое значение
(`"sk-CANARY-12345"`) и грепать по `repr`, `str(exc)`, `traceback.format_exc()`.
