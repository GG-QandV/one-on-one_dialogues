# B3 — типы записей БД

| | |
| :-- | :-- |
| Файл | `app/models.py` |
| Уровень | Junior |
| Оценка | 220 LOC, 8 тестов |
| Зависит от | B2 (`migrations/`), `app/db.py` (готов) |
| Блокирует | B4 (writer), D6, D7, G4, E7 — всё, что читает строки |
| Пункт спеки | §8 (схема данных) |

## Задача

Типизированное представление строк пяти таблиц из §8 и преобразование
`sqlite3.Row` → датакласс.

Смысл модуля прикладной, а не эстетический: `sqlite3.Row` при обращении к
несуществующему столбцу бросает `IndexError`, а не отдаёт `None`. После
любой миграции схемы код, читающий строки по именам, начинает падать в
случайных местах. Одно место преобразования означает одно место правки.

Второе: константы статусов. Строки `'fast'`, `'accurate'`, `'pending'`,
`'copied'` сейчас разбросаны по контрактам D6, D7, G4, I5. Опечатка в
литерале не ловится ни типами, ни тестами — запрос просто возвращает
пустой результат.

## Интерфейс

```python
class Track(str, Enum):
    FAST = "fast"
    ACCURATE = "accurate"

class TranslationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

class DraftStatus(str, Enum):
    GENERATED = "generated"
    IGNORED = "ignored"
    COPIED = "copied"

class SessionStatus(str, Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    ABORTED = "aborted"

@dataclass(frozen=True, slots=True)
class SessionRow: ...
@dataclass(frozen=True, slots=True)
class AudioStreamRow: ...
@dataclass(frozen=True, slots=True)
class SegmentRow: ...
@dataclass(frozen=True, slots=True)
class DraftRow: ...
@dataclass(frozen=True, slots=True)
class LibraryContextRow: ...

def session_from_row(row: sqlite3.Row) -> SessionRow: ...
def segment_from_row(row: sqlite3.Row) -> SegmentRow: ...
def draft_from_row(row: sqlite3.Row) -> DraftRow: ...
def stream_from_row(row: sqlite3.Row) -> AudioStreamRow: ...
def library_from_row(row: sqlite3.Row) -> LibraryContextRow: ...

def row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any: ...
```

## Поведение

1. **Состав полей — ровно из §8**, имена совпадают со столбцами. Ни одного
   поля сверх схемы: вычисляемые значения (длительность, флаг «переведён»)
   — это `property`, а не столбцы.

2. **`row_get` — единственный способ чтения столбца.** Отсутствующий
   столбец возвращает `default`, а не бросает `IndexError`. Все
   `*_from_row` построены на нём.

3. **Частичные выборки поддерживаются.** `SELECT id, raw_text` даёт
   валидный `SegmentRow` с `None` в непрочитанных полях. Требовать
   `SELECT *` ради типизации — значит тянуть текст всех сегментов там,
   где нужен один столбец.

4. **Перечисления разбираются мягко.** Неизвестное значение в столбце
   статуса не бросает исключение, а сохраняется как есть в поле
   `*_raw` рядом с `None` в типизированном поле. Данные из БД старше
   кода — штатная ситуация при откате версии, и падать на чтении
   истории нельзя.

5. **Время.** Поля `t_start_ms`, `t_end_ms` — `int`, миллисекунды от
   старта потока. Поля `created_at`, `started_at`, `updated_at` — `str` в
   ISO-8601 UTC, **не** `datetime`: преобразование в объект даёт
   бесконечные вопросы часового пояса при обратной записи. Разбор в
   `datetime` — задача слоя отображения.

6. **JSON-столбцы разбираются:** `edit_log_json` → `dict | None`,
   `sources_json` → `tuple[str, ...]`. Битый JSON → пустое значение и
   поле `*_malformed: True`, без исключения: выгрузка истории не должна
   падать из-за одной повреждённой строки.

7. **`SegmentRow.raw_text` — только чтение.** Датакласс `frozen`, и это
   не формальность: неизменяемость `raw_text` (§8.1) обеспечивается
   триггером БД (B4), а `frozen=True` убирает целый класс ошибок ещё до
   попытки записи.

8. **Полезные свойства (не столбцы):**
   `SegmentRow.duration_ms`, `SegmentRow.is_translated`,
   `SegmentRow.is_superseded`, `DraftRow.was_used`,
   `SessionRow.is_active`.

9. **Обратного преобразования нет.** `to_row()` / `to_dict()` для записи
   в БД не делается: запись идёт через явные `INSERT`/`UPDATE` в
   модулях-владельцах (`DraftGuard.store`, writer). Универсальный
   сериализатор в БД — прямой путь к обходу инвариантов §8.

10. **Модуль без зависимостей.** Только стандартная библиотека. Ни `db`,
    ни `errors`, ни сети, ни логирования.

## Запрещено

- Читать столбцы напрямую (`row["x"]`) в обход `row_get`.
- Бросать исключения при отсутствующем столбце, неизвестном статусе или
  битом JSON.
- Добавлять поля, которых нет в §8.
- Преобразовывать даты в `datetime` внутри модуля.
- Делать датаклассы мутабельными.
- Реализовывать запись в БД или сериализацию для записи.
- Дублировать строковые литералы статусов в других модулях: после
  появления этого файла `'accurate'`, `'pending'`, `'copied'` пишутся
  только как `Track.ACCURATE`, `TranslationStatus.PENDING`,
  `DraftStatus.COPIED`.

## Критерии приёмки

- [ ] Полная строка `segments` разбирается во все поля §8
- [ ] Частичная выборка (`id`, `raw_text`) даёт валидный объект с `None`
      в остальных полях
- [ ] Отсутствующий столбец → `default`, исключения нет
- [ ] Неизвестное значение `track` → типизированное поле `None`,
      `track_raw` содержит исходную строку
- [ ] `edit_log_json` с валидным JSON → `dict`; с битым → `None` и
      `edit_log_malformed is True`
- [ ] `sources_json` разбирается в кортеж строк
- [ ] `duration_ms` считается корректно; при `t_end_ms is None` → `None`
- [ ] `is_superseded` истинно при непустом `superseded_by_segment_id`
- [ ] Попытка присвоить поле датакласса → `FrozenInstanceError`
- [ ] Модуль импортирует только стандартную библиотеку (проверяется
      грепом)
- [ ] Значения всех перечислений совпадают со строками, используемыми в
      миграциях B2 (тест сверяет с DDL)

## Подсказки

Безопасное чтение:

```python
def row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
```

`sqlite3.Row` не поддерживает `.get()` и не является `dict` — обёртка
обязательна. `row.keys()` работает, но вызывать её на каждое поле
дороже, чем перехватить исключение один раз на отсутствующий столбец.

Мягкий разбор перечисления:

```python
def _enum(cls, value):
    if value is None:
        return None, None
    try:
        return cls(value), None
    except ValueError:
        return None, value          # (типизированное, сырое)
```

Ловушки:
- `bool` в SQLite хранится как `0`/`1`: поле `enabled` в `audio_streams`
  приводить явно, иначе `if row.enabled` истинно для строки `"0"`;
- `stt_confidence` — отрицательное число (усреднённый `avg_logprob`,
  контракт C6), и `None` при отсутствии данных; проверка `if confidence:`
  ошибочна для валидного нуля — сравнивать с `None` явно;
- перечисления наследуют `str`, поэтому `Track.ACCURATE == "accurate"`
  истинно и в SQL-параметры их можно передавать напрямую — это
  сознательное решение ради совместимости с существующими запросами;
- при добавлении столбца в B2 поле здесь добавляется тем же PR:
  расхождение схемы и типов обнаруживается только на чтении старой
  сессии, то есть у пользователя.
