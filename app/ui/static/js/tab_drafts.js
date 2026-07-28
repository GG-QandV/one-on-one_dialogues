/**
 * E4 — вкладка «Черновики ответов»: карточки, источники, копирование.
 * Зависит от: E2 (stream.js) — поставщик состояния черновиков.
 * Этот модуль ТОЛЬКО отрисовывает, не ведёт своего состояния.
 */

function escapeHtml(text) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

function formatTime(ms) {
  ms = Math.max(0, ms);
  const h = String(Math.floor(ms / 3600000)).padStart(2, '0');
  ms %= 3600000;
  const m = String(Math.floor(ms / 60000)).padStart(2, '0');
  ms %= 60000;
  const s = String(Math.floor(ms / 1000)).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function createDraftCardElement(draft, stream) {
  const article = document.createElement('article');
  article.className = 'draft-card';
  article.dataset.draftId = draft.id;

  const hasTranslated = Boolean(draft.draftTranslated);

  const triggerSeg = stream.getState().segments.find(s => s.id === draft.triggerSegmentId);
  const triggerText = triggerSeg ? triggerSeg.rawText || triggerSeg.draftText : null;

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
  article.appendChild(header);

  if (triggerText) {
    const p = document.createElement('p');
    p.style.cssText = 'font-size:12px;color:#888;margin:2px 0 6px;font-style:italic';
    p.textContent = 'Вопрос: ' + triggerText;
    article.appendChild(p);
  }

  const bodyRu = document.createElement('p');
  bodyRu.className = 'draft-text';
  bodyRu.textContent = draft.draftRu || '';
  article.appendChild(bodyRu);

  if (hasTranslated) {
    const bodyTr = document.createElement('p');
    bodyTr.className = 'draft-translated';
    bodyTr.textContent = draft.draftTranslated;
    article.appendChild(bodyTr);
  }

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

  const sourcesEl = document.createElement('p');
  sourcesEl.className = 'draft-sources';
  if (draft.sources && draft.sources.length > 0) {
    sourcesEl.innerHTML = '<span style="font-weight:600">Источники:</span> ' + escapeHtml(draft.sources.join(', '));
  } else if (!draft.hasGaps) {
    sourcesEl.innerHTML = '<span class="badge badge-warn">нет источников при отсутствии пробелов</span>';
  }
  article.appendChild(sourcesEl);

  const actions = document.createElement('div');
  actions.className = 'draft-actions';

  const copyRuBtn = document.createElement('button');
  copyRuBtn.className = 'btn small';
  copyRuBtn.textContent = draft.copied ? 'Скопировано' : 'Копировать RU';
  if (draft.copied) copyRuBtn.disabled = true;
  copyRuBtn.addEventListener('click', async () => {
    if (window.commands && window.commands.copyDraft) {
      const ok = await window.commands.copyDraft(draft.id, draft.draftRu);
      if (ok) {
        draft.copied = true;
        article.querySelector('header').innerHTML += '<span class="badge badge-ok">скопировано</span>';
        copyRuBtn.textContent = 'Скопировано';
        copyRuBtn.disabled = true;
      } else {
        const err = document.createElement('div');
        err.style.cssText = 'color:#c62828;font-size:12px;margin-top:4px';
        err.textContent = 'Не удалось скопировать, выделите текст вручную';
        actions.appendChild(err);
      }
    }
  });
  actions.appendChild(copyRuBtn);

  if (hasTranslated) {
    const copyTrBtn = document.createElement('button');
    copyTrBtn.className = 'btn small';
    copyTrBtn.textContent = 'Копировать перевод';
    copyTrBtn.addEventListener('click', async () => {
      if (window.commands && window.commands.copyDraft) {
        const ok = await window.commands.copyDraft(draft.id, draft.draftTranslated);
        if (ok) {
          draft.copied = true;
          copyTrBtn.textContent = 'Скопировано';
          copyTrBtn.disabled = true;
        } else {
          const err = document.createElement('div');
          err.style.cssText = 'color:#c62828;font-size:12px;margin-top:4px';
          err.textContent = 'Не удалось скопировать, выделите текст вручную';
          actions.appendChild(err);
        }
      }
    });
    actions.appendChild(copyTrBtn);
  }

  article.appendChild(actions);
  return article;
}

function createDrafts({ container, stream, commands }) {
  const scrollContainer = document.createElement('div');
  scrollContainer.className = 'cards-scroll-container';

  let _unsubs = [];
  let draftCards = [];

  function mount() {
    container.appendChild(scrollContainer);
    if (stream && stream.getState) {
      const state = stream.getState();
      if (state && state.drafts) {
        const sorted = Array.from(state.drafts.values()).sort((a, b) => (a.tStartMs || 0) - (b.tStartMs || 0));
        sorted.forEach(d => addCard(d));
      }
      _unsubs = [
        stream.subscribe('draft.created', (data) => {
          const draft = stream.drafts.get(data.draft_id);
          if (draft) addCard(draft);
        }),
        stream.subscribe('draft.translated', (data) => {
          const draft = stream.drafts.get(data.draft_id);
          if (draft) {
            draft.draftTranslated = data.draft_translated;
            refreshCard(draft);
          }
        }),
      ];
    }
  }

  function unmount() {
    _unsubs.forEach(fn => fn());
    _unsubs = [];
    draftCards = [];
    if (scrollContainer.parentNode) {
      scrollContainer.parentNode.removeChild(scrollContainer);
    }
  }

  function addCard(draft) {
    if (draftCards.find(c => c.id === draft.id)) return;
    const el = createDraftCardElement(draft, stream);
    scrollContainer.appendChild(el);
    draftCards.push({ id: draft.id, el, draft });
  }

  function refreshCard(draft) {
    const entry = draftCards.find(c => c.id === draft.id);
    if (!entry) return;
    const newEl = createDraftCardElement(draft, stream);
    scrollContainer.replaceChild(newEl, entry.el);
    entry.el = newEl;
    Object.assign(entry.draft, draft);
  }

  return { mount, unmount };
}
