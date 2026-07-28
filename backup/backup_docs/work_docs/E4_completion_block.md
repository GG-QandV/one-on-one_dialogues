# Завершение E4 — служебные поля в карточке черновика

E4 (`tab_drafts.js`) и E2 (`stream.js`) готовы. Не хватает проброса и
показа полей, которые мы добавили: `confidence`, `lang_ok`, `gap_note`,
`suggested_clarification`. Плюс фикс snapshot-бага (snake↔camel).

UX-решения (владелец):
- `confidence` → бейдж «оценка N%» ТОЛЬКО при `confidence≠null`;
- `lang_ok=false` → бейдж «язык под вопросом»;
- `gap_note` + `suggested_clarification` → добавить в событие и показать.

Фронт vanilla JS — автотестов нет; проверка визуальная + backend-тест на
payload. Отмечено честно.

---

## 1. Backend — расширить событие DRAFT_CREATED

Событие сейчас несёт `confidence`, `lang_ok`, но НЕ `gap_note`/
`suggested_clarification`. Добавить.

### Патч `main.py`, в `_handle_draft`, publish(DRAFT_CREATED)

НАЙТИ:
```python
            self.ui_server.publish(
                EventType.DRAFT_CREATED,
                {
                    "draft_id": draft_id,
                    "trigger_segment_id": segment_id,
                    "draft_ru": candidate.draft_ru,
                    "sources": list(candidate.sources),
                    "has_gaps": candidate.has_gaps_claimed,
                    "confidence": candidate.confidence,
                    "lang_ok": candidate.lang_ok,
                },
            )
```
ЗАМЕНИТЬ НА:
```python
            self.ui_server.publish(
                EventType.DRAFT_CREATED,
                {
                    "draft_id": draft_id,
                    "trigger_segment_id": segment_id,
                    "draft_ru": candidate.draft_ru,
                    "sources": list(candidate.sources),
                    "has_gaps": candidate.has_gaps_claimed,
                    "gap_note": candidate.gap_note,
                    "confidence": candidate.confidence,
                    "lang_ok": candidate.lang_ok,
                    "suggested_clarification": candidate.suggested_clarification,
                },
            )
```

> §7.1 payload расширен — согласовано (это те же служебные поля, что мы
> добавляли в схему). E2 маппит их ниже.

---

## 2. E2 — маппинг новых полей

### Патч `app/ui/static/js/stream.js`, `_handleDraftCreated`

НАЙТИ:
```javascript
    _handleDraftCreated(data) {
        this.drafts.set(data.draft_id, {
            id: data.draft_id,
            triggerSegmentId: data.trigger_segment_id,
            draftRu: data.draft_ru,
            draftTranslated: null,
            sources: data.sources || [],
            hasGaps: data.has_gaps || false,
            copied: false,
        });
    }
```
ЗАМЕНИТЬ НА:
```javascript
    _handleDraftCreated(data) {
        this.drafts.set(data.draft_id, {
            id: data.draft_id,
            triggerSegmentId: data.trigger_segment_id,
            draftRu: data.draft_ru,
            draftTranslated: null,
            sources: data.sources || [],
            hasGaps: data.has_gaps || false,
            gapNote: data.gap_note ?? null,
            confidence: (typeof data.confidence === 'number') ? data.confidence : null,
            langOk: (data.lang_ok === undefined) ? true : Boolean(data.lang_ok),
            suggestedClarification: data.suggested_clarification ?? null,
            copied: false,
        });
    }
```

---

## 3. E4 — бейджи и блоки в карточке

### Патч `app/ui/static/js/tab_drafts.js`, `createDraftCardElement`

**3a. Бейджи confidence и lang_ok — в header.**

НАЙТИ:
```javascript
  const header = document.createElement('header');
  header.innerHTML = `
    <span style="font-weight:600;color:#333">Черновик</span>
    ${draft.hasGaps ? '<span class="badge badge-err" style="font-size:12px">есть пробелы в фактах</span>' : ''}
    ${draft.copied ? '<span class="badge badge-ok">скопировано</span>' : ''}
    ${draft.ignored ? '<span style="color:#999;font-size:12px">игнорирован</span>' : ''}
  `;
```
ЗАМЕНИТЬ НА:
```javascript
  const confBadge = (typeof draft.confidence === 'number')
    ? `<span class="badge badge-warn" style="font-size:12px">оценка ${Math.round(draft.confidence * 100)}%</span>`
    : '';
  const langBadge = (draft.langOk === false)
    ? '<span class="badge badge-err" style="font-size:12px">язык под вопросом</span>'
    : '';
  const header = document.createElement('header');
  header.innerHTML = `
    <span style="font-weight:600;color:#333">Черновик</span>
    ${draft.hasGaps ? '<span class="badge badge-err" style="font-size:12px">есть пробелы в фактах</span>' : ''}
    ${confBadge}
    ${langBadge}
    ${draft.copied ? '<span class="badge badge-ok">скопировано</span>' : ''}
    ${draft.ignored ? '<span style="color:#999;font-size:12px">игнорирован</span>' : ''}
  `;
```

**3b. Блок suggested_clarification — после gap-note, перед источниками.**

НАЙТИ:
```javascript
  if (draft.hasGaps && draft.gapNote) {
    const note = document.createElement('p');
    note.className = 'draft-gap-note';
    note.innerHTML = '<span style="font-weight:600">Пробел в фактах:</span> ' + escapeHtml(draft.gapNote);
    article.appendChild(note);
  }
```
ЗАМЕНИТЬ НА:
```javascript
  if (draft.hasGaps && draft.gapNote) {
    const note = document.createElement('p');
    note.className = 'draft-gap-note';
    note.innerHTML = '<span style="font-weight:600">Пробел в фактах:</span> ' + escapeHtml(draft.gapNote);
    article.appendChild(note);
  }

  if (draft.suggestedClarification) {
    const sugg = document.createElement('p');
    sugg.className = 'draft-clarify';
    sugg.innerHTML = '<span style="font-weight:600">Уточнить у собеседника:</span> '
      + escapeHtml(draft.suggestedClarification);
    article.appendChild(sugg);
  }
```

> `confBadge` использует `badge-warn` (оценка — не ошибка, а «внимание,
> это вывод, не факт»). `langBadge` — `badge-err` (язык мимо — серьёзно).
> Классы уже есть в cards.css (используются has_gaps/sources).

---

## 4. Фикс snapshot-бага (E2) — snake→camel для черновиков

Найденный при сверке дефект: `_applySnapshot` кладёт черновики сырым
`{...d}` (snake_case), а карточка ждёт camelCase → после reconnect
восстановленные черновики рендерятся пусто. Чиним в том же заходе.

### Патч `stream.js`, `_applySnapshot`, ветка drafts

НАЙТИ:
```javascript
        // Черновики
        if (Array.isArray(snap.drafts)) {
            for (const d of snap.drafts) {
                const existing = this.drafts.get(d.id);
                if (!existing || (d.sequence != null && existing.sequence != null && d.sequence > existing.sequence)) {
                    this.drafts.set(d.id, { ...d, sequence: d.sequence });
                }
            }
        }
```
ЗАМЕНИТЬ НА:
```javascript
        // Черновики: снапшот приходит в snake_case (из БД), приводим к camelCase
        // как в _handleDraftCreated, иначе карточка не отрисуется.
        if (Array.isArray(snap.drafts)) {
            for (const d of snap.drafts) {
                const existing = this.drafts.get(d.id);
                if (!existing || (d.sequence != null && existing.sequence != null && d.sequence > existing.sequence)) {
                    this.drafts.set(d.id, {
                        id: d.id,
                        triggerSegmentId: d.trigger_segment_id,
                        draftRu: d.draft_ru,
                        draftTranslated: d.draft_translated ?? null,
                        sources: (typeof d.sources_json === 'string')
                            ? safeParseArray(d.sources_json) : (d.sources || []),
                        hasGaps: Boolean(d.has_gaps),
                        gapNote: d.gap_note ?? null,
                        confidence: (typeof d.confidence === 'number') ? d.confidence : null,
                        langOk: (d.lang_ok === undefined || d.lang_ok === null) ? true : Boolean(d.lang_ok),
                        suggestedClarification: d.suggested_clarification ?? null,
                        copied: d.status === 'copied',
                        ignored: d.status === 'ignored',
                        sequence: d.sequence,
                    });
                }
            }
        }
```

Добавить хелпер рядом с классом (или в начало файла):
```javascript
function safeParseArray(s) {
    try { const v = JSON.parse(s); return Array.isArray(v) ? v : []; }
    catch { return []; }
}
```

> `sources_json` в БД — это JSON-строка; событие же шлёт готовый массив
> `sources`. Хелпер разводит оба случая. Если снапшот шлёт `sources`
> массивом — ветка `d.sources` его возьмёт.

> Точка сверки: какие поля реально в `snap.drafts` (формат снапшота
> сервера). Если сервер уже camelCase-ит — этот фикс не нужен; проверить
> `_snapshot_handler`/сборку снапшота в server.py. По коду json_export
> поля snake_case — вероятно снапшот тоже.

---

## 5. Тесты

### Backend (`tests/test_draft_handler.py` — дополнить)
- событие DRAFT_CREATED содержит `gap_note`, `suggested_clarification`,
  `confidence`, `lang_ok` (проверить пойманный publish).

### Фронт
Автотестов JS в проекте нет — проверка визуальная:
- черновик с `confidence=0.75` → бейдж «оценка 75%»;
- черновик-факт (`confidence=null`) → бейджа оценки НЕТ;
- `lang_ok=false` → бейдж «язык под вопросом»;
- `has_gaps` + `gap_note` → блок пробела виден;
- `suggested_clarification` → блок «Уточнить у собеседника» виден;
- reconnect (снапшот) → карточки восстановлены с текстом (фикс §4).

> Если в проекте появится JS-тест-раннер — эти проверки первые кандидаты
> на автоматизацию. Пока честно: фронт проверяется руками.

---

## 6. Порядок
1. Backend payload (§1) + backend-тест.
2. E2 маппинг события (§2).
3. E4 бейджи/блоки (§3).
4. Snapshot-фикс (§4) — сверить формат снапшота.
5. Backend-тест зелёный (число), фронт — визуально.

## 7. Точки сверки
| Что | Где |
| :-- | :-- |
| Формат `snap.drafts` (snake vs camel) | server.py `_snapshot_handler` |
| Классы `badge-warn`/`badge-err` существуют | cards.css |
| `candidate.gap_note`/`suggested_clarification` доступны в `_handle_draft` | main.py (candidate от I2) |

После этого блока цепочка черновиков полностью видна пользователю:
генерация → перевод → карточка со всеми служебными полями. Фича черновиков
закрыта end-to-end.
