/**
 * E6 — верхняя панель (topbar): профиль, язык, соединение, сессия.
 * Зависит от: E2 (stream.js) — поставщик событий и состояния.
 * Команды — POST-обёртка над E1-роутами.
 */

function createTopbar({ container, stream, commands }) {
  const state = {
    profile: 'open',
    switching: false,
    connectionState: 'closed',
    degradationLevel: null,
    sessionActive: false,
    langInfo: { lang: '??', conflict: false, note: '' },
  };

  function render() {
    const isOpen = state.profile === 'open';
    container.innerHTML = `
      <div class="topbar">
        <div class="profile-switch" title="Профиль конфиденциальности">
          <button class="profile-toggle ${state.profile}"
                  data-profile="${state.profile}"
                  ${state.switching ? 'disabled' : ''}
                  aria-label="Переключить профиль">
          </button>
          <span class="profile-label ${state.profile}">
            ${isOpen ? 'Открытый' : 'Конфиденциальный'}
          </span>
          ${state.switching ? '<span class="badge badge-warn">переключение…</span>' : ''}
        </div>

        <div class="lang-indicator" data-conflict="${state.langInfo.conflict}"
             title="${state.langInfo.note || ''}">
          ${state.langInfo.lang}
          ${state.langInfo.conflict ? ' ⚠' : ''}
        </div>

        <span class="connection-state ${state.connectionState}">
          ${state.connectionState === 'open' ? 'соединение' :
            state.connectionState === 'connecting' ? 'подключение…' :
            state.connectionState === 'reconnecting' ? 'переподключение…' :
            'отключён'}
        </span>

        ${state.degradationLevel && state.degradationLevel !== 'NORMAL'
          ? `<span class="degradation-badge ${state.degradationLevel === 'MEMORY_HARD' ? 'hard' : ''}">
               ${state.degradationLevel}
             </span>`
          : ''}

        <div class="session-controls">
          ${!state.sessionActive
            ? '<button class="btn primary session-start">Старт</button>'
            : '<button class="btn danger session-stop">Стоп</button>'}
          <button class="btn settings-open" title="Настройки">⚙</button>
        </div>
      </div>
    `;

    container.querySelector('.profile-toggle')?.addEventListener('click', onToggleProfile);
    container.querySelector('.session-start')?.addEventListener('click', onStartSession);
    container.querySelector('.session-stop')?.addEventListener('click', onStopSession);
    container.querySelector('.settings-open')?.addEventListener('click', onOpenSettings);
  }

  async function onToggleProfile() {
    if (state.switching) return;
    const next = state.profile === 'open' ? 'confidential' : 'open';
    state.switching = true;
    render();
    try {
      await commands.setPrivacy(next);
    } catch (err) {
      state.switching = false;
      render();
    }
  }

  async function onStartSession() {
    try {
      await commands.startSession();
      state.sessionActive = true;
      render();
    } catch (err) {
      console.error('session start failed', err);
    }
  }

  async function onStopSession() {
    try {
      await commands.stopSession();
      state.sessionActive = false;
      render();
    } catch (err) {
      console.error('session stop failed', err);
    }
  }

  function onOpenSettings() {
    const panel = document.getElementById('settings-panel');
    if (panel) panel.classList.remove('hidden');
  }

  const unsubscribes = [
    stream.subscribe('privacy.changed', (data) => {
      state.profile = data.profile;
      state.switching = false;
      render();
    }),
    stream.subscribe('status', (data) => {
      state.degradationLevel = data.degradation_level || null;
      render();
    }),
    stream.subscribe('connection', (data) => {
      state.connectionState = data.state;
      render();
    }),
  ];

  const snap = stream.getState();
  if (snap.privacy) {
    state.profile = snap.privacy.profile || 'open';
  }

  render();

  return {
    mount() { render(); },
    unmount() { unsubscribes.forEach(fn => fn()); container.innerHTML = ''; },
  };
}
