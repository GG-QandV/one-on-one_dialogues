/**
 * E7 — панель настроек: ключи, провайдер, языки, библиотека.
 * Зависит от: E2 (stream.js) — snapshot.
 * Команды — POST-обёртка над E1-роутами.
 */

function createSettings({ container, stream, commands }) {
  const state = {
    providers: [],
    keys: {},
    languages: { microphone: 'ru', meeting: 'en' },
    library: [],
    stt: {
      active: 'local_whisper',
      choices: ['local_whisper', 'openai_api', 'custom_api'],
      local: { model: '', fallback_model: '', device: 'auto' },
      cloud: { endpoint: '', model: '', language_hint: '', timeout_s: 15, key_present: false, key_masked: null },
    },
  };

  const STT_PROVIDER_LABELS = {
    local_whisper: 'Локальный whisper.cpp',
    openai_api: 'OpenAI Whisper API',
    custom_api: 'Свой API (совместимый с OpenAI)',
  };

  function render() {
    container.innerHTML = `
      <div class="settings-panel">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h2>Настройки</h2>
          <button class="btn settings-close">✕</button>
        </div>

        <div class="settings-section">
          <h3>Ключи провайдеров</h3>
          <div class="settings-field">
            <label>Gemini API ключ</label>
            ${state.keys.gemini
              ? `<div style="display:flex;gap:8px;align-items:center">
                   <code>${state.keys.gemini}</code>
                   <button class="btn small danger" data-revoke="gemini">Отозвать</button>
                 </div>`
              : `<div style="display:flex;gap:8px">
                   <input type="password" placeholder="sk-…" data-key="gemini">
                   <button class="btn primary small" data-save="gemini">Сохранить</button>
                 </div>`}
          </div>
          <div class="settings-field">
            <label>Claude API ключ</label>
            ${state.keys.claude
              ? `<div style="display:flex;gap:8px;align-items:center">
                   <code>${state.keys.claude}</code>
                   <button class="btn small danger" data-revoke="claude">Отозвать</button>
                 </div>`
              : `<div style="display:flex;gap:8px">
                   <input type="password" placeholder="sk-…" data-key="claude">
                   <button class="btn primary small" data-save="claude">Сохранить</button>
                 </div>`}
          </div>
        </div>

        <div class="settings-section">
          <h3>Модель транскрипции</h3>
          <div class="settings-field">
            <label>Транскрибатор</label>
            <select data-stt-active>
              ${state.stt.choices.map((c) =>
                `<option value="${c}" ${state.stt.active === c ? 'selected' : ''}>${STT_PROVIDER_LABELS[c] || c}</option>`
              ).join('')}
            </select>
          </div>

          <div class="settings-field" data-stt-local-fields style="${state.stt.active !== 'local_whisper' ? 'display:none' : ''}">
            <label>Локальная модель (whisper.cpp)</label>
            <input type="text" placeholder="ggml-base.bin" data-stt-local-model value="${escapeHtml(state.stt.local.model)}">
            <input type="text" placeholder="ggml-tiny.bin (fallback)" data-stt-local-fallback value="${escapeHtml(state.stt.local.fallback_model)}" style="margin-top:4px">
            <select data-stt-local-device style="margin-top:4px">
              ${['auto', 'cpu', 'cuda'].map((d) =>
                `<option value="${d}" ${state.stt.local.device === d ? 'selected' : ''}>${d}</option>`
              ).join('')}
            </select>
          </div>

          <div class="settings-field" data-stt-cloud-fields style="${state.stt.active !== 'local_whisper' ? '' : 'display:none'}">
            <label>API ключ транскрибатора</label>
            ${state.stt.cloud.key_present
              ? `<div style="display:flex;gap:8px;align-items:center">
                   <code>${escapeHtml(state.stt.cloud.key_masked || '…')}</code>
                   <button class="btn small danger" data-revoke="stt_cloud">Отозвать</button>
                 </div>`
              : `<div style="display:flex;gap:8px">
                   <input type="password" placeholder="sk-…" data-key="stt_cloud">
                   <button class="btn primary small" data-save="stt_cloud">Сохранить</button>
                 </div>`}
            <input type="text" placeholder="Endpoint (пусто = дефолт провайдера)" data-stt-cloud-endpoint value="${escapeHtml(state.stt.cloud.endpoint)}" style="margin-top:4px">
            <input type="text" placeholder="Модель, напр. whisper-1" data-stt-cloud-model value="${escapeHtml(state.stt.cloud.model)}" style="margin-top:4px">
            <input type="text" placeholder="Язык (пусто = автоопределение)" data-stt-cloud-lang value="${escapeHtml(state.stt.cloud.language_hint)}" style="margin-top:4px">
          </div>

          <button class="btn primary small" data-save-stt style="margin-top:8px">Сохранить транскрибатор</button>
        </div>

        <div class="settings-section">
          <h3>Языки</h3>
          <div class="settings-field">
            <label>Микрофон (исходящий)</label>
            <select data-lang="microphone">
              ${['ru','en','es','uk','pl','auto'].map(l =>
                `<option value="${l}" ${state.languages.microphone === l ? 'selected' : ''}>${l}</option>`
              ).join('')}
            </select>
          </div>
          <div class="settings-field">
            <label>Собеседник (входящий)</label>
            <select data-lang="meeting">
              ${['en','es','ru','uk','pl','auto'].map(l =>
                `<option value="${l}" ${state.languages.meeting === l ? 'selected' : ''}>${l}</option>`
              ).join('')}
            </select>
          </div>
        </div>

        <div class="settings-section">
          <h3>Библиотека фактов</h3>
          <div class="library-list">
            ${state.library.map(item => `
              <div style="display:flex;justify-content:space-between;padding:4px 0">
                <span>${escapeHtml(item.name)} <small>(${item.token_estimate} токенов)</small></span>
                <button class="btn small danger" data-delete-lib="${item.id}">Удалить</button>
              </div>
            `).join('')}
          </div>
          <div style="margin-top:8px">
            <textarea placeholder="Текст библиотеки…" data-lib-text style="min-height:80px"></textarea>
            <input type="text" placeholder="Название раздела" data-lib-name style="margin-top:4px">
            <button class="btn primary small" data-save-lib style="margin-top:4px">Сохранить раздел</button>
          </div>
        </div>
      </div>
    `;

    container.querySelector('.settings-close')?.addEventListener('click', close);
    container.querySelector('[data-save="gemini"]')?.addEventListener('click', () => saveKey('gemini'));
    container.querySelector('[data-save="claude"]')?.addEventListener('click', () => saveKey('claude'));
    container.querySelector('[data-revoke="gemini"]')?.addEventListener('click', () => revokeKey('gemini'));
    container.querySelector('[data-revoke="claude"]')?.addEventListener('click', () => revokeKey('claude'));
    container.querySelector('[data-save-lib]')?.addEventListener('click', saveLibrary);
    container.querySelector('[data-lang="microphone"]')?.addEventListener('change', (e) => setLang('microphone', e.target.value));
    container.querySelector('[data-lang="meeting"]')?.addEventListener('change', (e) => setLang('meeting', e.target.value));
    container.querySelector('[data-stt-active]')?.addEventListener('change', (e) => setSttActive(e.target.value));
    container.querySelector('[data-save-stt]')?.addEventListener('click', saveStt);
    container.querySelector('[data-save="stt_cloud"]')?.addEventListener('click', () => saveKey('stt_cloud'));
    container.querySelector('[data-revoke="stt_cloud"]')?.addEventListener('click', () => revokeKey('stt_cloud'));
    container.querySelectorAll('[data-delete-lib]').forEach(el => {
      el.addEventListener('click', () => deleteLibrary(el.dataset.deleteLib));
    });
  }

  async function saveKey(provider) {
    const input = container.querySelector(`[data-key="${provider}"]`);
    if (!input || !input.value.trim()) return;
    try {
      const res = await commands.putKey(provider, input.value);
      state.keys[provider] = res.masked || '…';
      input.value = '';
      if (provider === 'stt_cloud') await loadStt();
      render();
    } catch (err) {
      console.error('key save failed', err);
    }
  }

  async function revokeKey(provider) {
    try {
      await commands.revokeKey(provider);
      delete state.keys[provider];
      if (provider === 'stt_cloud') await loadStt();
      render();
    } catch (err) {
      console.error('key revoke failed', err);
    }
  }

  function setSttActive(value) {
    state.stt.active = value;
    render(); // немедленно переключает видимость local/cloud, без запроса
  }

  async function saveStt() {
    const payload = {
      active: state.stt.active,
      local: {
        model: container.querySelector('[data-stt-local-model]')?.value || state.stt.local.model,
        fallback_model: container.querySelector('[data-stt-local-fallback]')?.value || state.stt.local.fallback_model,
        device: container.querySelector('[data-stt-local-device]')?.value || state.stt.local.device,
      },
      cloud: {
        endpoint: container.querySelector('[data-stt-cloud-endpoint]')?.value || '',
        model: container.querySelector('[data-stt-cloud-model]')?.value || '',
        language_hint: container.querySelector('[data-stt-cloud-lang]')?.value || '',
        timeout_s: state.stt.cloud.timeout_s,
      },
    };
    try {
      await commands.setStt(payload);
      await loadStt();
    } catch (err) {
      console.error('stt save failed', err);
    }
  }

  async function loadStt() {
    try {
      state.stt = await commands.getStt();
      render();
    } catch (err) {
      console.error('stt load failed', err);
    }
  }

  async function setLang(stream, lang) {
    state.languages[stream] = lang;
    try {
      await commands.setLanguages(state.languages);
    } catch (err) {
      console.error('lang set failed', err);
    }
  }

  async function saveLibrary() {
    const name = container.querySelector('[data-lib-name]');
    const text = container.querySelector('[data-lib-text]');
    if (!name || !text || !name.value.trim() || !text.value.trim()) return;
    try {
      await commands.upsertLibrary(name.value.trim(), null, text.value);
      name.value = '';
      text.value = '';
      await loadLibrary();
    } catch (err) {
      console.error('library save failed', err);
    }
  }

  async function deleteLibrary(id) {
    try {
      await commands.deleteLibrary(id);
      await loadLibrary();
    } catch (err) {
      console.error('library delete failed', err);
    }
  }

  async function loadLibrary() {
    try {
      state.library = await commands.listLibrary();
      render();
    } catch (_) {}
  }

  function close() {
    container.classList.add('hidden');
  }

  loadLibrary();
  loadStt();

  return {
    mount() { loadLibrary(); loadStt(); render(); },
    unmount() { container.innerHTML = ''; },
  };
}
