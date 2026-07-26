/**
 * tab_translation.js — живая лента реплик вкладки «Перевод».
 * Задача E3 роадмапа.
 *
 * Зависит от: E2 (sse_client.js) — поставщик событий сегментов.
 * Этот модуль ТОЛЬКО отрисовывает готовые данные, не имеет сети и состояния.
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

  if (data.translationText || data.draftText) {
    const p = document.createElement('p');

    if (data.draftText && !data.translationText) {
      p.className = 'translation translation--draft';
      p.textContent = `${data.targetLang || '??'} \u00b7 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a (${data.draftDelay}s)`;
    } else if (data.orphan) {
      p.className = 'translation translation--orphan';
      p.textContent = data.translationText || '\u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d \u0442\u043e\u0447\u043d\u044b\u043c \u0442\u0440\u0435\u043a\u043e\u043c';
    } else {
      p.className = 'translation translation--verified';
      p.textContent = `${data.targetLang || '??'} \u00b7 \u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d\u043e (${data.translationDelay}s)`;
    }
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

  function mount() {
    container.appendChild(scrollContainer);

    if (stream && stream.getState) {
      const state = stream.getState();
      if (state && state.segments) {
        state.segments.forEach(addCardFromData);
      }
    }

    scrollContainer.addEventListener('scroll', onScroll);

    if (stream && stream.on) {
      stream.on('segment.partial', handlePartial);
      stream.on('segment.final', handleFinal);
      stream.on('segment.translated', handleTranslated);
      stream.on('privacy.changed', handlePrivacyChange);
    }
  }

  function unmount() {
    if (stream && stream.off) {
      stream.off('segment.partial', handlePartial);
      stream.off('segment.final', handleFinal);
      stream.off('segment.translated', handleTranslated);
      stream.off('privacy.changed', handlePrivacyChange);
    }
    scrollContainer.removeEventListener('scroll', onScroll);
    container.removeChild(scrollContainer);
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

  function updateCard(segmentId, updateFn) {
    const existing = cards.find(c => c.data.segmentId === segmentId);
    if (!existing) return;
    updateFn(existing.data);
    const newEl = createCardElement(existing.data);
    scrollContainer.replaceChild(newEl, existing.el);
    existing.el = newEl;
  }

  function handlePartial(data) {
    addCardFromData({
      segmentId: data.utterance_id,
      role: data.role,
      tStartMs: data.t_start_ms,
      rawText: null,
      draftText: data.text || '',
      draftDelay: '?',
      lang: '??',
      langConflict: false,
      privacyProfile: 'open',
      targetLang: '??',
      sttStatus: 'pending',
      sttError: null,
      translationStatus: 'pending',
      translationError: null,
    });
  }

  function handleFinal(data) {
    updateCard(data.segment_id, d => {
      Object.assign(d, {
        role: data.role,
        tStartMs: data.t_start_ms,
        rawText: data.raw_text,
        track: data.track,
      });
    });
  }

  function handleTranslated(data) {
    updateCard(data.segment_id, d => {
      Object.assign(d, {
        translationText: data.translation,
        translationDelay: '?',
        translationStatus: 'done',
      });
      if (data.superseded_ids) {
        data.superseded_ids.forEach(id => removeCard(id));
      }
    });
  }

  function removeCard(segmentId) {
    const idx = cards.findIndex(c => c.data.segmentId === segmentId);
    if (idx !== -1) {
      const [entry] = cards.splice(idx, 1);
      if (entry.el.parentNode) {
        entry.el.parentNode.removeChild(entry.el);
      }
    }
  }

  function handlePrivacyChange(data) {
  }

  function setFilter(filter) {
    currentFilter = filter;
    scrollContainer.innerHTML = '';
    cards = [];
    if (stream && stream.getState) {
      const state = stream.getState();
      if (state && state.segments) {
        state.segments.forEach(addCardFromData);
      }
    }
    scrollToLatest();
  }

  return { mount, unmount, setFilter, scrollToLatest };
}
