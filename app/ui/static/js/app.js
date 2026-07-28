/**
 * app.js — bootstrap: инициализация Stream, вкладок, topbar, команд.
 */

(function () {
  const stream = new Stream({ url: '/events', snapshotUrl: '/api/snapshot' });

  const commands = {
    setPrivacy: (profile) =>
      fetch('/api/privacy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ profile }) })
        .then(r => r.ok ? r.json().catch(() => ({})) : Promise.reject()),
    startSession: (title) =>
      fetch('/api/session/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ meeting_title: title || null }) })
        .then(r => r.json()),
    stopSession: () =>
      fetch('/api/session/stop', { method: 'POST' }).then(r => r.ok ? undefined : Promise.reject()),
    fetchSnapshot: () =>
      fetch('/api/snapshot').then(r => r.json()),
    putKey: (provider, key) =>
      fetch('/api/key', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider, key }) })
        .then(r => r.json()),
    revokeKey: (provider) =>
      fetch('/api/key/revoke', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: provider || null }) })
        .then(r => r.ok ? undefined : Promise.reject()),
    setLanguages: (langs) =>
      fetch('/api/languages', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(langs) })
        .then(r => r.ok ? undefined : Promise.reject()),
    upsertLibrary: (name, domain, text) =>
      fetch('/api/library', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, domain, content_text: text }) })
        .then(r => r.json()),
    deleteLibrary: (id) =>
      fetch(`/api/library/${id}`, { method: 'DELETE' }).then(r => r.ok ? undefined : Promise.reject()),
    listLibrary: () =>
      fetch('/api/library').then(r => r.json()),
    copyDraft: (draftId, text) =>
      fetch('/api/clipboard', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ draft_id: draftId, text }) })
        .then(r => r.ok),
    listSessions: () =>
      fetch('/api/sessions').then(r => r.json()),
    getSession: (id) =>
      fetch(`/api/sessions/${id}`).then(r => r.json()),
    exportSessionUrl: (id, fmt) =>
      `/api/sessions/${id}/export.${fmt}`,
  };

  window.commands = commands;

  let currentTab = 'translation';

  function switchTab(name) {
    currentTab = name;
    document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
    document.querySelectorAll('.tab-panel').forEach(el => el.classList.toggle('active', el.id === 'tab-' + name));
  }

  document.querySelectorAll('.tab').forEach(el => {
    el.addEventListener('click', () => switchTab(el.dataset.tab));
  });

  const components = {
    topbar: createTopbar({ container: document.getElementById('topbar'), stream, commands }),
    settings: createSettings({ container: document.getElementById('settings-panel'), stream, commands }),
    diagnostics: createDiagnostics({ container: document.getElementById('tab-diagnostics'), stream, commands }),
    drafts: createDrafts({ container: document.getElementById('tab-drafts'), stream, commands }),
    history: createHistory({ container: document.getElementById('tab-history'), commands }),
  };

  Object.values(components).forEach(c => c.mount && c.mount());

  stream.connect();
})();
