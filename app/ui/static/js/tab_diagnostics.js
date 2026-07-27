/**
 * E5 — вкладка «Диагностика»: память, деградация, провайдеры, аудио, задержки.
 * Данные из /api/snapshot + события status.
 * Ничего не вычисляет — только отображает.
 */

function createDiagnostics({ container, stream, commands }) {
  let refreshTimer = null;
  const state = {
    snapshot: null,
    status: {},
  };

  function render() {
    const snap = state.snapshot || {};
    const mem = snap.memory || {};
    const deg = snap.degradation || {};
    const prov = snap.providers || {};
    const capture = snap.capture || {};
    const ui = snap.ui || {};
    const db = snap.db_writer || {};
    const stt = snap.stt || {};

    const memOk = mem.memory_mb < (mem.high_mb || 1750);
    const memWarn = mem.memory_mb >= (mem.high_mb || 1750) && mem.memory_mb < (mem.max_mb || 1900);
    const memDanger = mem.memory_mb >= (mem.max_mb || 1900);

    container.innerHTML = `
      <h2 style="font-size:18px;margin-bottom:16px">Диагностика</h2>

      <div class="diag-section">
        <h3>Память <small style="font-weight:normal">(${mem.source || '?'})</small></h3>
        <div class="diag-grid">
          <div class="diag-item">
            <div class="diag-label">Текущее</div>
            <div class="diag-value ${memDanger ? 'danger' : memWarn ? 'warning' : ''}">
              ${mem.memory_mb != null ? Math.round(mem.memory_mb) + ' МБ' : '—'}
            </div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Пик</div>
            <div class="diag-value">${mem.peak_mb != null ? Math.round(mem.peak_mb) + ' МБ' : '—'}</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Порог high/max</div>
            <div class="diag-value">${mem.high_mb || 1750} / ${mem.max_mb || 1900} МБ</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Источник</div>
            <div class="diag-value" style="font-size:14px">
              ${mem.source || '—'}
              ${mem.degraded_source ? '<span class="badge badge-warn">деградирован</span>' : ''}
            </div>
            ${mem.degraded_source ? '<div class="diag-note">дочерний whisper не учитывается, значение занижено</div>' : ''}
          </div>
        </div>
      </div>

      <div class="diag-section">
        <h3>Деградация</h3>
        <div class="diag-grid">
          <div class="diag-item">
            <div class="diag-label">Уровень</div>
            <div class="diag-value ${deg.level && deg.level !== 'NORMAL' ? 'warning' : ''}">
              ${deg.level || 'NORMAL'}
            </div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Переходы</div>
            <div class="diag-value">${deg.transitions != null ? deg.transitions : '—'}</div>
          </div>
        </div>
      </div>

      <div class="diag-section">
        <h3>Провайдеры</h3>
        <div class="diag-grid">
          ${Object.keys(prov).length
            ? Object.entries(prov).map(([name, p]) => `
              <div class="diag-item">
                <div class="diag-label">${escapeHtml(name)}</div>
                <div>
                  <span class="badge ${p.state === 'available' ? 'badge-ok' : p.state === 'degraded' ? 'badge-warn' : 'badge-err'}">
                    ${p.state || '?'}
                  </span>
                </div>
                ${p.last_error_code ? `<div class="diag-note">${escapeHtml(p.last_error_code)}</div>` : ''}
              </div>
            `).join('')
            : '<div class="diag-item"><div class="diag-label">—</div></div>'}
        </div>
      </div>

      <div class="diag-section">
        <h3>Аудио-уровни</h3>
        <div class="diag-grid">
          ${['microphone', 'meeting'].filter(r => capture[r]).map(role => {
            const c = capture[role] || {};
            return `
              <div class="diag-item">
                <div class="diag-label">${role === 'microphone' ? 'Микрофон' : 'Собеседник'}</div>
                <div class="diag-value" style="font-size:14px">
                  ${c.last_rms != null ? (c.last_rms * 100).toFixed(0) + '%' : '—'}
                </div>
                <div class="diag-note">
                  ${c.state || '—'} · ${c.backend || '—'}
                  ${c.reconnects ? ' · реконнекты: ' + c.reconnects : ''}
                </div>
              </div>
            `;
          }).join('')}
        </div>
      </div>

      <div class="diag-section">
        <h3>Задержки</h3>
        <div class="diag-grid">
          <div class="diag-item">
            <div class="diag-label">Быстрый трек</div>
            <div class="diag-value">${state.status.latency_ms != null ? state.status.latency_ms + ' мс' : '—'}</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Точный трек</div>
            <div class="diag-value">${state.status.backlog_ms != null ? state.status.backlog_ms + ' мс' : '—'}</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">STT очередь</div>
            <div class="diag-value">${stt.backlog_ms != null ? (stt.backlog_ms / 1000).toFixed(1) + ' с' : '—'}</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">БД writer</div>
            <div class="diag-value">${db.queue_depth != null ? db.queue_depth : '—'}</div>
          </div>
        </div>
      </div>

      <div class="diag-section">
        <h3>UI</h3>
        <div class="diag-grid">
          <div class="diag-item">
            <div class="diag-label">Клиенты</div>
            <div class="diag-value">${ui.client_count != null ? ui.client_count : '—'}</div>
          </div>
          <div class="diag-item">
            <div class="diag-label">Потеряно событий</div>
            <div class="diag-value">${ui.lost_events != null ? ui.lost_events : '—'}</div>
          </div>
        </div>
      </div>
    `;
  }

  async function refresh() {
    try {
      state.snapshot = await commands.fetchSnapshot();
      render();
    } catch (_) {}
  }

  const unsub = stream.subscribe('status', (data) => {
    Object.assign(state.status, data);
    render();
  });

  return {
    mount() {
      refresh();
      refreshTimer = setInterval(refresh, 5000);
    },
    unmount() {
      unsub();
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = null;
      container.innerHTML = '';
    },
  };
}
