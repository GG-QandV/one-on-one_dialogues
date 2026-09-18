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
      mode: 'file_per_segment',
      choices: ['local_whisper', 'openai_api', 'custom_api'],
      chain: [],
    },
  };

  const STT_PROVIDER_LABELS = {
    local_whisper: 'Локальный whisper.cpp',
    openai_api: 'OpenAI Whisper API',
    custom_api: 'Свой API (совместимый с OpenAI)',
  };

  const CLOUD_CHOICES = ['openai_api', 'custom_api'];

  function esc(v) {
    return escapeHtml(v == null ? '' : String(v));
  }

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
          <p class="hint" style="font-size:12px;opacity:.7">
            Порядок = порядок попыток. Последняя строка (локальный whisper) обязательна
            и не может быть удалена или перемещена.
          </p>
          <div class="stt-chain">
            ${state.stt.chain.map((entry, i) => sttRow(entry, i)).join('')}
          </div>
          <button class="btn small" data-stt-add style="margin-top:8px">+ Добавить облачный провайдер</button>
          <div style="margin-top:8px">
            <button class="btn primary small" data-save-stt>Сохранить цепочку</button>
          </div>
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
    container.querySelector('[data-save-stt]')?.addEventListener('click', saveStt);
    container.querySelector('[data-stt-add]')?.addEventListener('click', addCloudEntry);
    container.querySelectorAll('[data-delete-lib]').forEach(el => {
      el.addEventListener('click', () => deleteLibrary(el.dataset.deleteLib));
    });
    container.querySelectorAll('[data-stt-move]').forEach(el => {
      el.addEventListener('click', () => moveEntry(Number(el.dataset.i), el.dataset.sttMove));
    });
    container.querySelectorAll('[data-stt-remove]').forEach(el => {
      el.addEventListener('click', () => removeEntry(Number(el.dataset.sttRemove)));
    });
    container.querySelectorAll('[data-stt-key-save]').forEach(el => {
      el.addEventListener('click', () => saveSttKey(el.dataset.sttKeySave));
    });
    container.querySelectorAll('[data-stt-key-revoke]').forEach(el => {
      el.addEventListener('click', () => revokeSttKey(el.dataset.sttKeyRevoke));
    });
  }

  function sttRow(entry, i) {
    const isLocal = entry.provider === 'local_whisper';
    const last = i === state.stt.chain.length - 1;
    const label = `${i + 1}. ${STT_PROVIDER_LABELS[entry.provider] || entry.provider}`;

    const fields = isLocal
      ? `
        <input type="text" data-stt-field="model" value="${esc(entry.model)}" placeholder="ggml-base.bin">
        <input type="text" data-stt-field="fallback_model" value="${esc(entry.fallback_model)}" placeholder="ggml-tiny.bin" style="margin-top:4px">
        <select data-stt-field="device" style="margin-top:4px">
          ${['auto', 'cpu', 'cuda'].map(d => `<option value="${d}" ${entry.device === d ? 'selected' : ''}>${d}</option>`).join('')}
        </select>`
      : `
        <select data-stt-field="provider" style="margin-top:4px">
          ${CLOUD_CHOICES.map(p => `<option value="${p}" ${entry.provider === p ? 'selected' : ''}>${STT_PROVIDER_LABELS[p]}</option>`).join('')}
        </select>
        <input type="text" data-stt-field="model" value="${esc(entry.model)}" placeholder="whisper-1" style="margin-top:4px">
        <input type="text" data-stt-field="endpoint" value="${esc(entry.endpoint)}" placeholder="Endpoint (пусто = дефолт)" style="margin-top:4px">
        <input type="text" data-stt-field="key_name" value="${esc(entry.key_name)}" placeholder="имя ключа в KeyStore" style="margin-top:4px">
        <input type="number" data-stt-field="timeout_s" value="${entry.timeout_s}" placeholder="timeout, с" style="margin-top:4px">
        <input type="number" data-stt-field="cooldown_s" value="${entry.cooldown_s}" placeholder="cooldown, с" style="margin-top:4px">
        ${entry.key_present
          ? `<div style="display:flex;gap:8px;align-items:center;margin-top:4px">
               <code>${esc(entry.key_masked || '…')}</code>
               <button class="btn small danger" data-stt-key-revoke="${esc(entry.key_name)}">Отозвать</button>
             </div>`
          : `<div style="display:flex;gap:8px;margin-top:4px">
               <input type="password" placeholder="API ключ" data-stt-newkey>
               <button class="btn small" data-stt-key-save="${esc(entry.key_name)}">Ключ</button>
             </div>`}`;

    const controls = isLocal
      ? `<span class="hint" style="font-size:12px;opacity:.7">обязательное звено</span>`
      : `<button class="btn small" data-stt-move="up" data-i="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
         <button class="btn small" data-stt-move="down" data-i="${i}" ${last || state.stt.chain[i + 1].provider === 'local_whisper' ? 'disabled' : ''}>↓</button>
         <button class="btn small danger" data-stt-remove="${i}">✕</button>`;

    return `
      <div class="stt-row" data-stt-row data-provider="${entry.provider}" style="border:1px solid #333;border-radius:6px;padding:8px;margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <strong>${esc(label)}</strong>
          <span style="display:flex;gap:4px">${controls}</span>
        </div>
        ${fields}
      </div>`;
  }

  function collectChain() {
    const chain = [];
    container.querySelectorAll('[data-stt-row]').forEach(row => {
      const provider = row.dataset.provider;
      const get = (f) => row.querySelector(`[data-stt-field="${f}"]`)?.value ?? '';
      if (provider === 'local_whisper') {
        chain.push({
          provider,
          model: get('model'),
          fallback_model: get('fallback_model'),
          device: get('device'),
          cooldown_s: 0,
        });
      } else {
        chain.push({
          provider: get('provider') || provider,
          model: get('model'),
          endpoint: get('endpoint'),
          key_name: get('key_name'),
          timeout_s: Number(get('timeout_s')) || 15,
          cooldown_s: Number(get('cooldown_s')) || 60,
        });
      }
    });
    return chain;
  }

  // Перерисовка списка не должна терять несохранённые правки других строк.
  function syncFromDom() {
    const chain = collectChain();
    if (chain.length) state.stt.chain = chain;
  }

  function addCloudEntry() {
    syncFromDom();
    const n = state.stt.chain.filter(e => e.provider !== 'local_whisper').length + 1;
    const local = state.stt.chain.find(e => e.provider === 'local_whisper') || {
      provider: 'local_whisper', model: 'ggml-base.bin',
      fallback_model: 'ggml-tiny.bin', device: 'auto', cooldown_s: 0,
    };
    const clouds = state.stt.chain.filter(e => e.provider !== 'local_whisper');
    clouds.push({
      provider: 'openai_api', model: '', endpoint: '',
      key_name: `stt_cloud_${n}`, timeout_s: 15, cooldown_s: 60,
    });
    state.stt.chain = [...clouds, local];
    render();
  }

  function moveEntry(i, dir) {
    syncFromDom();
    const chain = state.stt.chain;
    const j = dir === 'up' ? i - 1 : i + 1;
    // local_whisper зафиксирован последним — не участвует в перестановках.
    if (j < 0 || j >= chain.length) return;
    if (chain[i].provider === 'local_whisper' || chain[j].provider === 'local_whisper') return;
    [chain[i], chain[j]] = [chain[j], chain[i]];
    render();
  }

  function removeEntry(i) {
    syncFromDom();
    if (state.stt.chain[i]?.provider === 'local_whisper') return;
    state.stt.chain.splice(i, 1);
    render();
  }

  async function saveSttKey(keyName) {
    const input = findNewKeyInput(keyName);
    if (!input || !input.value.trim()) return;
    try {
      await commands.putKey(keyName, input.value.trim());
      await loadStt();
    } catch (err) {
      console.error('stt key save failed', err);
    }
  }

  function findNewKeyInput(keyName) {
    for (const row of container.querySelectorAll('[data-stt-row]')) {
      const kn = row.querySelector('[data-stt-field="key_name"]');
      if (kn && kn.value.trim() === keyName) return row.querySelector('[data-stt-newkey]');
    }
    return null;
  }

  async function revokeSttKey(keyName) {
    try {
      await commands.revokeKey(keyName);
      await loadStt();
    } catch (err) {
      console.error('stt key revoke failed', err);
    }
  }

  async function saveStt() {
    const chain = collectChain();
    const keys = {};
    container.querySelectorAll('[data-stt-row]').forEach(row => {
      if (row.dataset.provider === 'local_whisper') return;
      const kn = row.querySelector('[data-stt-field="key_name"]')?.value?.trim();
      const inp = row.querySelector('[data-stt-newkey]');
      if (kn && inp && inp.value.trim()) keys[kn] = inp.value.trim();
    });
    try {
      await commands.setStt({ chain, keys });
      await loadStt();
    } catch (err) {
      console.error('stt save failed', err);
    }
  }

  async function saveKey(provider) {
    const input = container.querySelector(`[data-key="${provider}"]`);
    if (!input || !input.value.trim()) return;
    try {
      const res = await commands.putKey(provider, input.value);
      state.keys[provider] = res.masked || '…';
      input.value = '';
      render();
    } catch (err) {
      console.error('key save failed', err);
    }
  }

  async function revokeKey(provider) {
    try {
      await commands.revokeKey(provider);
      delete state.keys[provider];
      render();
    } catch (err) {
      console.error('key revoke failed', err);
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

  async function loadStt() {
    try {
      const data = await commands.getStt();
      state.stt = data;
      render();
    } catch (err) {
      console.error('stt load failed', err);
    }
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
