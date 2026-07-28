Приоритет сработал (s10 первым), ошибка в самой проверке — исправляю assert:

Выявил ошибку синтаксиса SQL и спланировал исправление

Выявил ошибку синтаксиса SQL и спланировал исправление

Smoke нашёл реальный баг: `NOT IN ('skipped',)` — хвостовая запятая невалидна в SQL. Исправляю:

Edited a file, ran a command, read a file

Edited a file, ran a command, read a file

## Партия 1 из 2 готова — вертикальный срез замкнут

| Файл           | Путь в репо        | Задача          | Проверка                          |
| -------------- | ------------------ | --------------- | --------------------------------- |
| `runner.py`    | `app/stt/`         | C4a             | синтаксис                         |
| `fallback.py`  | `app/stt/`         | C7              | синтаксис + через smoke scheduler |
| `scheduler.py` | `app/stt/`         | **C4b**         | **smoke**                         |
| `supersede.py` | `app/translation/` | D8              | smoke на реальной БД              |
| `main.py`      | `app/`             | **F3 + сборка** | **smoke**                         |

### Результаты smoke

| Сценарий                                                                              | Итог                                       |
| ------------------------------------------------------------------------------------- | ------------------------------------------ |
| Приоритет microphone над meeting в очереди STT                                        | mic0 обработан первым                      |
| Backpressure: hard-порог 12 с отклонил 5-й сегмент, soft-флаг поднят                  | OK                                         |
| Fallback base→tiny по медиане rtf после 2 медленных вызовов                           | модели: base, base, tiny, tiny             |
| Supersede: перекрытие 100% и 60% замещены, 10% — нет; идемпотентность; сирота помечен | OK                                         |
| **Полный срез: сегмент → БД → STT → `raw_text` → supersede → translate-job**          | **«привет мир» в БД, translate в очереди** |
| Переключение профиля пишет аудит, отменяет облачные аудио-задачи                      | OK                                         |

Smoke оправдал компромисс дважды: нашёл SQL-баг `NOT IN ('skipped',)` в supersede (компиляция это не ловит — ошибка всплыла бы только в рантайме на проде) и подтвердил порядок остановки lifecycle.

### Осталось в партии 2 (senior финал)

`openai_realtime.py` (D5), `drafts/guardrails.py` (I5), `watchdog/degradation.py` (F2a/F2b), `tests/test_privacy_isolation.py` (J4), `tests/test_latency_budget.py` (H3) — ~1 090 LOC. Smoke только для realtime и degradation.
