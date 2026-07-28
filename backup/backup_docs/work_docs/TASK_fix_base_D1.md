# Задание Middle-агенту: исправить `app/translation/base.py` (D1)

| | |
| :-- | :-- |
| Файл | `app/translation/base.py` |
| Контракт | `CONTRACTS/D1_provider_interfaces.md` (в репо это имя; = D1) |
| Тип | Правка + дописать тесты. НЕ переписывание |
| Блокирует | D2, D3 — по контракту вызовут `_parse(req, raw)` и упадут на текущей сигнатуре |
| Приоритет | Вперёд волны 1: без этого волна не стартует чисто |

## Почему это задача, хотя «161 тест зелёный»

Тесты проходят, потому что проверяют **D4 (`prompts.py`)** —
`test_prompt_modes`, `test_drift_20`. Они не покрывают **D1
(`base.py`)**: порядок приватности, StaleGeneration, классификацию,
обязательность аудита, сигнатуру `_parse`. Код `base.py` реализован с
шестью отклонениями от контракта, и ни одно не поймано, потому что тестов
на 11 критериев приёмки D1 в наборе фактически нет.

Это ровно случай «зелёные тесты на не тот инвариант». Правишь код по
контракту **и** дописываешь недостающие тесты D1 — иначе следующий агент
снова примет дырявый код за готовый.

Все шесть дефектов установлены чтением запушенного кода, не по отчётам.
Проверь каждый сам, открыв файл.

## Дефект 1 — порядок приватности перевёрнут (BLOCKER приёмки §21.7)

Сейчас в `translate`:
```python
self._privacy.validate(fence, Capability.TEXT_TO_CLOUD)   # ДО _call
api_key = self._key_provider()
raw_result = await self._call(req, api_key)
# после _call валидации НЕТ
```

Контракт D1 п. 1 требует:
```
fence  = privacy.require(Capability.TEXT_TO_CLOUD)   # взять fence
key    = key_provider()
raw    = await _call(req, key)                       # сетевой вызов
privacy.validate(fence, TEXT_TO_CLOUD)               # валидация ПОСЛЕ, до возврата
result = _parse(req, raw)
return auditor(req, result)
```

Смысл: `validate` **после** `_call` ловит переключение профиля,
случившееся **во время** сетевого запроса. Сейчас между `_call` и
возвратом профиль может смениться, а результат уйдёт под старым
поколением — дыра приватности, критерий §21.7.

Дополнительно: используется `self._privacy.allows(...)` + голая проверка,
а контракт требует `privacy.require(Capability) -> fence`. Свериться с
фактической сигнатурой `PrivacyController` в `app/privacy.py` (готов) —
там есть `require`, `validate`, `Fence`. Использовать их, не `allows`.

## Дефект 2 — неверный класс исключения

Сейчас:
```python
raise PermissionError("Privacy does not allow TEXT_TO_CLOUD")
```

`PermissionError` — стандартное Python-исключение, вне иерархии проекта.
Контракт (правило INTERFACES §0: «исключения только из `app/errors.py`»)
и `errors.py` (готов) требуют:
- нарушение профиля → `PrivacyViolation(capability, profile)`;
- устаревшее поколение после `switch()` → `StaleGenerationError`.

Оба класса уже есть в `app/errors.py`, импортировать оттуда. `PrivacyViolation`
принимает `(capability, profile)` — передать строки капабилити и профиля.

## Дефект 3 — сигнатура `_parse` разошлась с контрактом (BLOCKER волны 1)

Сейчас:
```python
def _parse(self, raw_response: str) -> TranslationResult:
```

Контракт:
```python
def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult:
```

D2 и D3 пишутся по контракту и вызовут `_parse(req, raw)`. С текущей
сигнатурой они упадут с `TypeError`. Привести к `(self, req, raw)`.
Вызов в `translate` тоже: `result = self._parse(req, raw_result)`.

## Дефект 4 — аудит опционален, молча пропускается

Сейчас:
```python
if self._auditor is not None:
    result = self._auditor(req, result)
```

При `auditor=None` аудит **не выполняется** — `detect_drift` не зовётся,
потеря чисел в переводе не фиксируется. Контракт D1 п. 7: «наследник
аудит пропустить не может». Требуется: при `auditor is None` —
**отложенный импорт** `prompts.audit` и вызов, а не пропуск.

```python
auditor = self._auditor
if auditor is None:
    from app.translation.prompts import audit as auditor   # отложенный импорт
result = auditor(req, result)
```

Отложенный импорт — намеренный: `prompts` импортирует типы из `base`,
поэтому `base` не может импортировать `prompts` на уровне модуля (цикл).
Это единственное разрешённое отступление от правила абсолютных импортов
верхнего уровня (контракт D1, «Разрыв цикла импортов»).

## Дефект 5 — классификация ошибок отсутствует

Сейчас:
```python
try:
    raw_result = await self._call(req, api_key)
except Exception as e:
    raise
```

`_classify(status_code, body) -> ProviderError` из контракта D1 п. 4 не
реализован. Нужен метод, отображающий HTTP-код в исключение:

| Код | Исключение | retryable |
| :-- | :-- | :-- |
| 401, 403 | `ProviderAuthError` | нет |
| 429 | `ProviderRateLimited` | да |
| 500–599 | `ProviderUnavailable` | да |
| 400, 422 | `ProviderResponseInvalid` | нет |
| прочее | `ProviderError` | да |

Все пять классов есть в `app/errors.py`. `_classify` вызывают наследники
(D2/D3) при ненормальном коде ответа — метод живёт в базовом классе,
чтобы классификация была одна на всех. Реализовать здесь, в `base.py`.

## Дефект 6 — таймаут на `_call` отсутствует

Контракт D1 п. 3: таймаут через `asyncio.wait_for`, превышение →
`ProviderUnavailable` **без текста запроса** в сообщении. Сейчас `_call`
вызывается без обёртки таймаута. Добавить:

```python
try:
    raw_result = await asyncio.wait_for(
        self._call(req, api_key), timeout=self._timeout_s)
except asyncio.TimeoutError:
    raise ProviderUnavailable(f"timeout {self._timeout_s}s")  # без req.text
```

## Итоговый вид `translate` (ориентир, свериться с privacy.py)

```python
async def translate(self, req, *, fence):
    self._privacy.validate(fence, Capability.TEXT_TO_CLOUD)  # см. ниже*
    key = self._key_provider()                    # пусто -> ProviderAuthError
    try:
        raw = await asyncio.wait_for(self._call(req, key), self._timeout_s)
    except asyncio.TimeoutError:
        raise ProviderUnavailable(f"timeout {self._timeout_s}s")
    self._privacy.validate(fence, Capability.TEXT_TO_CLOUD)  # ПОСЛЕ _call
    result = self._parse(req, raw)
    auditor = self._auditor or _lazy_audit()
    return auditor(req, result)
```

*Уточнить по `privacy.py`: `fence` приходит параметром (как в
`TranslationProvider.translate(..., *, fence)`) или берётся внутри через
`require()`. Контракт D1 показывает `require` внутри, но протокол
`translate` принимает `fence` снаружи. **Свериться с privacy.py и с тем,
как fence прокидывается в готовом `openai_realtime.py` (D5, ✅)** — там
этот паттерн уже реализован и принят. Повторить его, не изобретать.

## Пустой ключ

`key_provider()` вернул пустую строку → `ProviderAuthError`, **до**
сетевого вызова (критерий приёмки D1). Проверить перед `_call`.

## Тесты — дописать (сейчас их нет)

11 критериев приёмки D1 из контракта, минимум:

```
[ ] translate в конфиденциальном профиле для текста проходит
[ ] switch() посреди вызова -> StaleGenerationError, результат не возвращён
[ ] пустой ключ -> ProviderAuthError, _call не вызывался
[ ] таймаут -> ProviderUnavailable, в сообщении нет req.text
[ ] коды 401/429/503/400 -> правильные типы и retryable (через _classify)
[ ] пустой ответ при непустом входе -> ProviderResponseInvalid
[ ] LIVE_LITERAL без потерь -> translation_clean is None, changes == ()
[ ] auditor вызывается ровно один раз, ПОСЛЕ второго validate
[ ] перевод с пропавшим числом -> lost_entity в changes, без исключения
[ ] auditor is None -> отложенный импорт prompts.audit отрабатывает
[ ] ключ sk-CANARY не в repr, str(exc), traceback.format_exc()
```

Тест на дефект 1 (порядок) — ключевой: подменить `_call` так, чтобы во
время его выполнения сменилось поколение профиля, и убедиться, что
результат **не** возвращается (StaleGenerationError). Нормальный порядок
без переключения этот дефект не ловит — нужен именно switch посреди
`_call`.

Тест приватности писать по образцу `tests/test_privacy_isolation.py`
(готов, 7.7K) — там транспорт инжектируется, утверждения о том, что ушло
«в сеть».

## Границы

- Не переписывать файл — шесть точечных правок + тесты.
- Не менять `errors.py`, `privacy.py` — они готовы, использовать как есть.
- Не трогать `prompts.py` (D4 сдан) — только отложенный импорт из него.
- Сигнатуры и типы — из контракта D1 и `INTERFACES.md §4.1`; при
  расхождении прав `INTERFACES.md`.
- Fence-паттерн повторить из `openai_realtime.py`, не изобретать.

## Если не сходится — спросить

- `privacy.py` не даёт `require`/`validate`/`Fence` в ожидаемой форме;
- `fence` в готовом `openai_realtime.py` прокидывается иначе, чем
  показывает контракт D1;
- `_classify` должен вызываться наследником, но наследник (D2/D3) ещё не
  написан — тогда протестировать `_classify` изолированно, по кодам.

После правки: `pytest` (ожидается >161, +тесты D1), `ruff`, `mypy --strict`.
В PR — какие из 6 дефектов закрыты, какие тесты добавлены, результат
прогона.
