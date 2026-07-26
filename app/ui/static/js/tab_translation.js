/**
 * tab_translation.js — живая лента реплик вкладки «Перевод».
 * Задача E3 роадмапа.
 *
 * Зависит от: E2 (stream.js) — поставщик состояния сегментов.
 * Этот модуль ТОЛЬКО отрисовывает готовые данные, не имеет сети и состояния.
 *
 * Известные gaps на стороне E2 (stream.js), зафиксированные при приёмке E3:
 * 1. translationDelay не заполняется — E2._handleFinal/_handleTranslated
 *    не проставляют это поле. E3 показывает заглушку '?'. Контракт E3 п. 4
 *    требует отображения задержки проверенного перевода.
 * 2. langNote не заполняется — E2 не передаёт note из LanguageDecision.
 *    Контракт E3 п. 6 требует подсказки при конфликте языков.
 * 3. segment.translated, пришедший до segment.final для того же segment_id,
 *    теряет данные перевода (E2._handleTranslated делает return при
 *    отсутствии сегмента в Map). E3 корректно не показывает перевод,
 *    но перевод не придёт повторно — E3 останется без финального текста.
 */


const ROLE_LABELS = {
  meeting: 'Клиент',
  microphone: 'Вы',
};

const PROFILE_LABELS = {
  open: 'открытый профиль',
  confidential: 'закрытый профиль',
};

function formatTime(ms) {
  ms = Math.max(0, ms);
  const h = String(Math.floor(ms / 3600000)).padStart(2, '0');
  ms %= 3600000;
  const m = String(Math.floor(ms / 60000)).padStart(2, '0');
  ms %= 60000;
  const s = String(Math.floor(ms / 1000)).padStart(2, '0');
  return `${h}:${m}:${s}`;
}

function statusIcon(status) {
  if (status === 'done') return '<span class="ok">\u2713</span>';
  if (status === 'failed' || status === 'error') return '<span class="err">\u2717</span>';
  return '<span class="pending">\u2026</span>';
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

function segmentToCardData(seg) {
  return {
    segmentId: seg.id,
    role: seg.role,
    tStartMs: seg.tStartMs,
    rawText: seg.rawText || '',
    translationText: seg.translation || null,
    draftText: seg.draftText || null,
    draftDelay: seg.draftDelay || '?',
    translationDelay: seg.translationDelay || '?',
    lang: seg.lang || '??',
    langConflict: seg.langConflict || false,
    langNote: seg.langNote || '',
    privacyProfile: seg.privacyProfile || 'open',
    targetLang: seg.targetLang || '??',
    sttStatus: seg.status ? seg.status.stt : 'pending',
    sttError: seg.sttError || null,
    translationStatus: seg.status ? seg.status.translation : 'pending',
    translationError: seg.translationError || null,
    orphan: seg.orphan || false,
    superseded: seg.superseded || false,
  };
}

function createCardElement(data) {
  const article = document.createElement('article');
  article.className = 'card';
  article.dataset.segmentId = data.segmentId;

  const roleLabel = ROLE_LABELS[data.role] || data.role;
  const profileLabel = PROFILE_LABELS[data.privacyProfile] || data.privacyProfile;
  const profileMod = data.privacyProfile === 'confidential' ? 'profile--confidential' : 'profile--open';

  const header = document.createElement('header');
  header.innerHTML = `
    <time>${formatTime(data.tStartMs)}</time>
    <span class="role">${escapeHtml(roleLabel)}</span>
    <span class="lang" data-conflict="${data.langConflict ? 'true' : 'false'}"
          title="${escapeHtml(data.langNote || '')}">${escapeHtml(data.lang || '??')}</span>
    <span class="profile ${profileMod}">${profileLabel}</span>
  `;
  article.appendChild(header);

  if (data.rawText) {
    const p = document.createElement('p');
    p.className = 'raw';
    p.textContent = data.rawText;
    article.appendChild(p);
  }

  if (data.orphan && !data.translationText) {
    const p = document.createElement('p');
    p.className = 'translation translation--orphan';
    p.textContent = data.translationText || '\u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d \u0442\u043e\u0447\u043d\u044b\u043c \u0442\u0440\u0435\u043a\u043e\u043c';
    article.appendChild(p);
  } else if (data.draftText && !data.translationText) {
    const p = document.createElement('p');
    p.className = 'translation translation--draft';
    p.textContent = `${data.targetLang || '??'} \u00b7 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a (${data.draftDelay}s)`;
    article.appendChild(p);
  } else if (data.translationText) {
    const p = document.createElement('p');
    p.className = 'translation translation--verified';
    p.textContent = `${data.targetLang || '??'} \u00b7 \u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043e (${data.translationDelay}s)`;
    article.appendChild(p);
  }

  const footer = document.createElement('footer');
  footer.className = 'status';
  const sttStatus = data.sttError
    ? `<span class="err">STT \u2717 ${escapeHtml(data.sttError)}</span>`
    : `<span class="ok">STT ${statusIcon('done')}</span>`;
  const trStatus = data.translationError
    ? `<span class="err">\u041f\u0435\u0440\u0435\u0432\u043e\u0434 \u2717 ${escapeHtml(data.translationError)} <button class="retry-btn" data-segment-id="${data.segmentId}">\u043f\u043e\u0432\u0442\u043e\u0440</button></span>`
    : data.translationStatus === 'pending'
      ? `<span class="pending">\u041f\u0435\u0440\u0435\u0432\u043e\u0434 ${statusIcon('pending')}</span>`
      : `<span class="ok">\u041f\u0435\u0440\u0435\u0432\u043e\u0434 ${statusIcon('done')}</span>`;
  footer.innerHTML = `${sttStatus} | ${trStatus}`;
  article.appendChild(footer);

  return article;
}

function createCards({ container, stream, formatTime: ft }) {
  const scrollContainer = document.createElement('div');
  scrollContainer.className = 'cards-scroll-container';

  const renderFn = ft || formatTime;
  let currentFilter = 'all';
  let isAtBottom = true;
  let newCount = 0;
  let cards = [];
  let _unsubs = [];

  function mount() {
    container.appendChild(scrollContainer);
    scrollContainer.addEventListener('scroll', onScroll);

    if (stream && stream.getState) {
      const state = stream.getState();
      if (state && state.segments) {
        state.segments.forEach(seg => addCardFromData(segmentToCardData(seg)));
      }

      _unsubs = [
        stream.subscribe('segment.partial', handleEvent),
        stream.subscribe('segment.final', handleEvent),
        stream.subscribe('segment.translated', handleEvent),
        stream.subscribe('privacy.changed', handlePrivacyChange),
      ];
    }
  }

  function unmount() {
    _unsubs.forEach(fn => fn());
    _unsubs = [];
    scrollContainer.removeEventListener('scroll', onScroll);
    if (scrollContainer.parentNode) {
      scrollContainer.parentNode.removeChild(scrollContainer);
    }
  }

  function onScroll() {
    const el = scrollContainer;
    const threshold = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (threshold < 40) {
      isAtBottom = true;
      newCount = 0;
    } else {
      isAtBottom = false;
    }
  }

  function scrollToLatest() {
    scrollContainer.scrollTop = scrollContainer.scrollHeight;
    isAtBottom = true;
    newCount = 0;
  }

  function handleEvent(data) {
    const id = data.segment_id || data.utterance_id;
    if (id) _sync(id);
    if (Array.isArray(data.superseded_ids)) {
      data.superseded_ids.forEach(sid => _sync(sid));
    }
  }

  function handlePrivacyChange() {
    const state = stream.getState();
    if (!state || !state.segments) return;
    for (const seg of state.segments) {
      const existing = cards.find(c => c.data.segmentId === seg.id);
      if (existing) {
        existing.data.privacyProfile = seg.privacyProfile || 'open';
        const newEl = createCardElement(existing.data);
        scrollContainer.replaceChild(newEl, existing.el);
        existing.el = newEl;
      }
    }
  }

  function _sync(segmentId) {
    const state = stream.getState();
    if (!state || !state.segments) return;
    const seg = state.segments.find(s => s.id === segmentId);
    if (!seg) return;

    if (seg.superseded) {
      const idx = cards.findIndex(c => c.data.segmentId === segmentId);
      if (idx !== -1) {
        const entry = cards.splice(idx, 1)[0];
        if (entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
      }
      return;
    }

    const data = segmentToCardData(seg);
    const existing = cards.find(c => c.data.segmentId === segmentId);

    if (existing) {
      Object.assign(existing.data, data);
      const newEl = createCardElement(existing.data);
      scrollContainer.replaceChild(newEl, existing.el);
      existing.el = newEl;
    } else {
      addCardFromData(data);
    }
  }

  function addCardFromData(data) {
    if (data.superseded) return;
    if (currentFilter !== 'all' && data.role !== currentFilter) return;

    const el = createCardElement(data);
    scrollContainer.appendChild(el);
    cards.push({ data, el });

    if (isAtBottom) {
      requestAnimationFrame(() => {
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
      });
    } else {
      newCount++;
    }
  }

  function setFilter(filter) {
    currentFilter = filter;
    scrollContainer.innerHTML = '';
    cards = [];
    if (stream && stream.getState) {
      const state = stream.getState();
      if (state && state.segments) {
        state.segments.forEach(seg => addCardFromData(segmentToCardData(seg)));
      }
    }
    scrollToLatest();
  }

  return { mount, unmount, setFilter, scrollToLatest };
}
