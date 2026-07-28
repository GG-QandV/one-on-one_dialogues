# D2 — текстовый провайдер Google Gemini

| | |
| :-- | :-- |
| Файл | `app/translation/providers/gemini_text.py` |
| Уровень | Middle |
| Оценка | 220 LOC, 12 тестов |
| Зависит от | D1 (`base.py`), D4 (`prompts.py`) |
| Блокирует | H2 (живые тесты и выбор финальной связки моделей) |
| Пункт спеки | §9, §10.1, §19 (`[provider.translation]`) |

## Задача

Реализовать текстовый перевод через Gemini поверх `BaseTranslationProvider`.
Gemini в MVP закрывает то, что не умеет realtime-модель OpenAI: украинский
и польский как цели перевода (тир 2), и все режимы с кастомными промптами —
`live_literal`, `live_safe`, `post_clean`.

Провайдер выбирается в панели управления; финальное решение Gemini или
Claude принимается по живым тестам (H2), поэтому оба обязаны быть
взаимозаменяемы без правок вызывающего кода.

## Интерфейс

```python
@dataclass(frozen=True, slots=True)
class GeminiConfig:
    endpoint: str = "https://generativelanguage.googleapis.com/v1beta"
    model: str = "gemini-flash-lite-latest"
    timeout_s: float = 15.0
    max_output_tokens: int = 1024
    temperature: float = 0.0        # перевод — не творческая задача

class GeminiTextProvider(BaseTranslationProvider):
    name = "gemini"
    def __init__(self, config: GeminiConfig, privacy: PrivacyController,
                 key_provider: Callable[[], str],
                 *, http_client: Any | None = None) -> None: ...
    async def _call(self, req: TranslationRequest, key: str) -> str: ...
    def _parse(self, req: TranslationRequest, raw: str) -> TranslationResult: ...
    async def close(self) -> None: ...
```

## Поведение

1. **Промпты берутся из `prompts.build(mode, req)` (D4)**, не пишутся здесь.
   Дублирование текстов промптов в провайдере — прямой путь к расхождению
   между Gemini и Claude, а значит к разным переводам одного текста.

2. **Формат запроса.** `POST {endpoint}/models/{model}:generateContent`,
   ключ в заголовке `x-goog-api-key` (не в query — там он попадает в логи
   прокси). Системный промпт — в `system_instruction`, пользовательский —
   в `contents[0].parts[0].text`.

3. **Контекст.** Предыдущие сегменты из `req.context` передаются как
   отдельный текстовый блок перед переводимым текстом, с явной пометкой,
   что это контекст и переводить его не нужно. Формат блока — из D4.

4. **`temperature=0`** и `candidateCount=1`. Перевод должен быть
   воспроизводимым: одинаковый вход — одинаковый выход. Это условие
   идемпотентности ретраев в `JobQueue`.

5. **`POST_CLEAN` требует строгого JSON.** Использовать
   `generationConfig.responseMimeType = "application/json"` и
   `responseSchema` — Gemini поддерживает структурированный вывод.
   Разбор ответа — через `prompts.validate_response`, не своим парсером.

6. **Разбор ответа.** Текст лежит в
   `candidates[0].content.parts[0].text`. Отсутствие `candidates`,
   пустой массив, `finishReason` не `STOP` → `ProviderResponseInvalid`
   с указанием `finishReason` (но без текста запроса).

7. **`provider_request_id`** заполнять из заголовка ответа
   `x-request-id` либо из `responseId` тела, если присутствует. Нужен для
   разбора инцидентов с провайдером.

8. **HTTP-клиент инжектируется** параметром `http_client`. По умолчанию
   создаётся `httpx.AsyncClient` с настроенным таймаутом и переиспользуется
   между вызовами; `close()` его закрывает. Инжекция обязательна для тестов.

9. **Блокировка безопасности Gemini.** Если ответ содержит
   `promptFeedback.blockReason` — это `ProviderResponseInvalid` с кодом
   причины. Переговорный контент изредка триггерит фильтры; задача обязана
   уйти в `failed` с понятной причиной, а не молча вернуть пустоту.

## Запрещено

- Писать тексты промптов в этом файле.
- Передавать ключ в query-строке URL.
- Ретраить внутри провайдера.
- Логировать `req.text`, `req.context` или ответ на уровне INFO и выше.
- Использовать `temperature > 0` или `candidateCount > 1`.
- Молча возвращать пустой перевод при блокировке фильтром.
- Обращаться к `privacy` напрямую: протокол реализован в базовом классе.

## Критерии приёмки

- [ ] Успешный ответ → `TranslationResult` с непустым `translation_raw`
- [ ] Ключ уходит в заголовке `x-goog-api-key`, в URL его нет
      (проверяется перехватывающим HTTP-клиентом)
- [ ] `LIVE_LITERAL`: числа, даты, суммы, имена и URL в ответе идентичны
      входным — на эталонном наборе `tests/fixtures/literal_20.json`
- [ ] `POST_CLEAN` возвращает валидный JSON, `changes` заполнен
- [ ] `finishReason: "MAX_TOKENS"` → `ProviderResponseInvalid`
- [ ] `promptFeedback.blockReason` → `ProviderResponseInvalid` с причиной
- [ ] 429 → `ProviderRateLimited` (retryable), 401 → `ProviderAuthError`
- [ ] Контекст из 3 сегментов присутствует в теле запроса и помечен как
      не подлежащий переводу
- [ ] Ключ `sk-CANARY` не встречается ни в одном исключении и в `repr`
- [ ] `close()` закрывает HTTP-клиент; повторный вызов безопасен

## Подсказки

Минимальное тело запроса:

```json
{
  "system_instruction": {"parts": [{"text": "<system из D4>"}]},
  "contents": [{"role": "user", "parts": [{"text": "<user из D4>"}]}],
  "generationConfig": {
    "temperature": 0,
    "candidateCount": 1,
    "maxOutputTokens": 1024
  }
}
```

Для `POST_CLEAN` добавляется:

```json
"generationConfig": {
  "responseMimeType": "application/json",
  "responseSchema": {
    "type": "object",
    "properties": {
      "clean_text": {"type": "string"},
      "changes": {"type": "array", "items": {
        "type": "object",
        "properties": {"type": {"type": "string"},
                       "original": {"type": "string"},
                       "replacement": {"type": "string"}}}}
    },
    "required": ["clean_text"]
  }
}
```

**Проверить перед реализацией:** конкретное имя модели и доступность
structured output могли измениться — сверить с актуальной документацией
Google. Имя модели берётся из конфига, в коде не хардкодится.

Ловушки:
- Gemini возвращает 200 с пустым `candidates` при срабатывании фильтра —
  проверять наличие, а не только код ответа;
- ведущие и хвостовые переводы строк в ответе встречаются регулярно,
  обрезать перед возвратом;
- модель иногда оборачивает JSON в ```json-блок даже при
  `responseMimeType: application/json` — снимать обёртку перед разбором.
