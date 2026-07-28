/**
 * E8 — вкладка «История»: список сессий, просмотр реплик и черновиков, экспорт.
 * Данные из /api/sessions, /api/sessions/{id}, /api/sessions/{id}/export.{fmt}.
 * Ничего не вычисляет — только отображает и инициирует экспорт.
 */

function formatSessionTime(iso) {
  if (!iso) return '—';
  return iso.replace('T', ' ').replace('Z', '');
}

function createHistory({ container, commands }) {
  const state = {
    sessions: [],
    openId: null,
    detail: null,
    error: null,
  };

  const listEl = document.createElement('div');
  listEl.className = 'history-list';
  const detailEl = document.createElement('div');
  detailEl.className = 'history-detail';

  function renderList() {
    if (state.error) {
      listEl.innerHTML = `<div class="diag-item"><div class="diag-label">Ошибка загрузки истории</div></div>`;
      return;
    }
    if (!state.sessions.length) {
      listEl.innerHTML = `<div class="diag-item"><div class="diag-label">Сессий пока нет</div></div>`;
      return;
    }
    listEl.innerHTML = '';
    state.sessions.forEach((s) => {
      const row = document.createElement('div');
      row.className = 'history-item' + (s.id === state.openId ? ' active' : '');
      const statusBadge = s.status === 'finished' ? 'badge-ok'
        : s.status === 'aborted' ? 'badge-err' : 'badge-warn';
      row.innerHTML = `
        <div>
          <div style="font-weight:600">${escapeHtml(s.meeting_title || 'Без названия')}</div>
          <div class="diag-note">${formatSessionTime(s.started_at)} → ${formatSessionTime(s.ended_at)}</div>
        </div>
        <span class="badge ${statusBadge}">${escapeHtml(s.status || '?')}</span>
      `;
      row.addEventListener('click', () => openSession(s.id));
      listEl.appendChild(row);
    });
  }

  function renderDetail() {
    if (!state.openId) {
      detailEl.innerHTML = `<div class="diag-item"><div class="diag-label">Выберите сессию слева</div></div>`;
      return;
    }
    if (!state.detail) {
      detailEl.innerHTML = `<div class="diag-item"><div class="diag-label">Загрузка…</div></div>`;
      return;
    }
    const { session, segments, drafts } = state.detail;
    const exportButtons = ['txt', 'srt', 'vtt', 'json'].map((fmt) => `
      <a class="btn small" href="${commands.exportSessionUrl(session.id, fmt)}" download>${fmt.toUpperCase()}</a>
    `).join(' ');

    const segmentsHtml = segments.map((seg) => `
      <div class="card">
        <header>
          <span class="role">${escapeHtml(seg.role || seg.track || '?')}</span>
          <span class="lang">${escapeHtml(seg.detected_language || '??')}</span>
        </header>
        ${seg.raw_text ? `<p class="raw">${escapeHtml(seg.raw_text)}</p>` : ''}
        ${seg.translation_raw ? `<p class="translation">${escapeHtml(seg.translation_raw)}</p>` : ''}
      </div>
    `).join('');

    const draftsHtml = drafts.map((d) => `
      <div class="draft-card">
        <header><span style="font-weight:600">Черновик</span> <span class="badge ${d.status === 'copied' ? 'badge-ok' : 'badge-info'}">${escapeHtml(d.status || '?')}</span></header>
        <p class="draft-text">${escapeHtml(d.draft_ru || '')}</p>
      </div>
    `).join('');

    detailEl.innerHTML = `
      <h3 style="margin-bottom:8px">${escapeHtml(session.meeting_title || 'Без названия')}</h3>
      <div class="diag-note" style="margin-bottom:12px">
        ${formatSessionTime(session.started_at)} → ${formatSessionTime(session.ended_at)} ·
        профиль: ${escapeHtml(session.default_privacy_profile || '?')}
      </div>
      <div style="margin-bottom:16px">Экспорт: ${exportButtons}</div>
      <h4 style="margin-bottom:8px">Реплики (${segments.length})</h4>
      <div class="cards-scroll-container" style="max-height:300px;margin-bottom:16px">${segmentsHtml || '<div class="diag-note">нет данных</div>'}</div>
      <h4 style="margin-bottom:8px">Черновики (${drafts.length})</h4>
      <div class="cards-scroll-container" style="max-height:200px">${draftsHtml || '<div class="diag-note">нет данных</div>'}</div>
    `;
  }

  function render() {
    renderList();
    renderDetail();
  }

  async function refreshList() {
    try {
      const data = await commands.listSessions();
      state.sessions = data.sessions || [];
      state.error = null;
    } catch (_) {
      state.error = true;
    }
    render();
  }

  async function openSession(id) {
    state.openId = id;
    state.detail = null;
    render();
    try {
      state.detail = await commands.getSession(id);
    } catch (_) {
      state.detail = null;
    }
    render();
  }

  return {
    mount() {
      container.appendChild(listEl);
      container.appendChild(detailEl);
      refreshList();
      render();
    },
    unmount() {
      state.openId = null;
      state.detail = null;
      container.innerHTML = '';
    },
  };
}
