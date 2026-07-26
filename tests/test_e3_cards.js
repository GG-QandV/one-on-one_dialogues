/**
 * E3 — тесты живых карточек перевода (tab_translation.js)
 *
 * Запуск: node --test tests/test_e3_cards.js
 * Зависимости: нет (минимальный DOM-шим)
 */

const assert = require('node:assert');
const { describe, it } = require('node:test');

// ====== Минимальный DOM-шим ======
let elId = 0;
function makeEl(tag) {
  const id = ++elId;
  const el = {
    _id: id,
    tagName: tag.toUpperCase(),
    className: '',
    dataset: {},
    children: [],
    parentNode: null,
    _innerHTML: '',
    _listeners: {},
    _scrollTop: 0, _scrollHeight: 100, _clientHeight: 80,
    get innerHTML() {
      // Возвращаем innerHTML, либо рендерим из children для escapeHtml
      if (this._innerHTML) return this._innerHTML;
      return this.children.map(c => {
        if (c.nodeType === 3) return c.textContent;
        return `<${c.tagName.toLowerCase()} class="${c.className}">${c.innerHTML}</${c.tagName.toLowerCase()}>`;
      }).join('');
    },
    set innerHTML(v) { this._innerHTML = v; },
    get textContent() { return this._textContent !== undefined ? this._textContent : this.children.map(c => c.textContent || '').join(''); },
    set textContent(v) { this._textContent = String(v); },
    addEventListener(ev, fn) { this._listeners[ev] = fn; },
    removeEventListener(ev) { delete this._listeners[ev]; },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      const idx = this.children.indexOf(child);
      if (idx !== -1) { this.children.splice(idx, 1); }
      child.parentNode = null;
      return child;
    },
    replaceChild(newChild, oldChild) {
      const idx = this.children.indexOf(oldChild);
      if (idx !== -1) {
        newChild.parentNode = this;
        this.children[idx] = newChild;
        oldChild.parentNode = null;
      }
      return oldChild;
    },
    get scrollTop() { return this._scrollTop; },
    set scrollTop(v) { this._scrollTop = v; },
    get scrollHeight() { return this._scrollHeight; },
    get clientHeight() { return this._clientHeight; },
    // Для querySelector-like поиска
    querySelector(sel) {
      if (sel.startsWith('.')) {
        const cls = sel.slice(1);
        return this.children.find(c => c.className === cls) || null;
      }
      return null;
    },
  };
  return el;
}

const dom = {
  createElement(tag) { return makeEl(tag); },
  createTextNode(text) { const n = { nodeType: 3, textContent: String(text) }; return n; },
};

global.document = dom;
global.window = {};
global.requestAnimationFrame = (fn) => fn();

// ====== Загружаем модуль ======
const fs = require('fs');
const srcPath = '/home/gg/projects/LENDINGS/speech-local/app/ui/static/js/tab_translation.js';
const code = fs.readFileSync(srcPath, 'utf8');

const exportCode = code + `
if (typeof module !== 'undefined') {
  module.exports = {
    segmentToCardData, createCardElement, createCards,
    formatTime, statusIcon, escapeHtml,
  };
}
`;

const m = { exports: {} };
const fn = new Function('module', 'exports', 'window', exportCode);
fn(m, m.exports, global.window);
const exported = m.exports;

// ====== TESTS ======

describe('E3 — tab_translation.js', () => {

  describe('segmentToCardData', () => {
    it('maps E2 segment fields to card data', () => {
      const seg = {
        id: 'seg_1', role: 'meeting', tStartMs: 332000,
        rawText: 'Bueno, necesitamos revisar el contrato.',
        translation: null, draftText: 'Нам нужно просмотреть контракт.',
        lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'done', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
        draftDelay: '0.9', translationDelay: '?',
      };
      const data = exported.segmentToCardData(seg);
      assert.strictEqual(data.segmentId, 'seg_1');
      assert.strictEqual(data.role, 'meeting');
      assert.strictEqual(data.tStartMs, 332000);
      assert.strictEqual(data.rawText, 'Bueno, necesitamos revisar el contrato.');
      assert.strictEqual(data.draftText, 'Нам нужно просмотреть контракт.');
      assert.strictEqual(data.lang, 'ES');
      assert.strictEqual(data.privacyProfile, 'open');
      assert.strictEqual(data.superseded, false);
    });

    it('provides fallbacks for missing fields', () => {
      const seg = { id: 'seg_2', role: 'microphone', tStartMs: 0 };
      const data = exported.segmentToCardData(seg);
      assert.strictEqual(data.lang, '??');
      assert.strictEqual(data.privacyProfile, 'open');
      assert.strictEqual(data.targetLang, '??');
      assert.strictEqual(data.sttStatus, 'pending');
      assert.strictEqual(data.translationDelay, '?');
      assert.strictEqual(data.superseded, false);
    });
  });

  describe('createCardElement', () => {
    it('renders header with timecode, role, lang, profile', () => {
      const data = exported.segmentToCardData({
        id: 'seg_1', role: 'meeting', tStartMs: 332000,
        rawText: 'Bueno, necesitamos revisar el contrato.',
        draftText: 'черновик', lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'done', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
        draftDelay: '0.9', translationDelay: '?',
      });
      const el = exported.createCardElement(data);
      assert.strictEqual(el.tagName, 'ARTICLE');
      assert.strictEqual(el.className, 'card');
      assert.strictEqual(el.dataset.segmentId, 'seg_1');
      const header = el.children[0];
      assert.strictEqual(header.tagName, 'HEADER');
      const hHtml = header.innerHTML;
      assert.ok(hHtml.includes('00:05:32'), 'timecode missing');
      assert.ok(hHtml.includes('Клиент'), 'role label missing');
      assert.ok(hHtml.includes('ES'), 'lang missing');
      assert.ok(hHtml.includes('открытый профиль'), 'profile missing');

      const rawP = el.children.find(c => c.className === 'raw');
      assert.ok(rawP, 'raw paragraph missing');
      assert.ok(rawP.textContent.includes('Bueno'));
    });

    it('renders draft with grey italic style', () => {
      const data = exported.segmentToCardData({
        id: 'seg_d', role: 'meeting', tStartMs: 1000,
        rawText: 'test', draftText: 'черновик',
        draftDelay: '0.9', lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'pending', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
      });
      const el = exported.createCardElement(data);
      const tp = el.children.find(c => c.className === 'translation translation--draft');
      assert.ok(tp);
      assert.ok(tp.textContent.includes('черновик'));
    });

    it('renders verified translation when translationText present', () => {
      const data = exported.segmentToCardData({
        id: 'seg_v', role: 'meeting', tStartMs: 1000,
        rawText: 'test', draftText: null,
        translation: 'Проверенный перевод',
        translationDelay: '2.8', lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'done', translation: 'done' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
      });
      const el = exported.createCardElement(data);
      const tp = el.children.find(c => c.className === 'translation translation--verified');
      assert.ok(tp);
      assert.ok(tp.textContent.includes('проверено'));
    });

    it('renders orphan card with grey note (orphan > draft priority)', () => {
      const data = exported.segmentToCardData({
        id: 'seg_o', role: 'meeting', tStartMs: 1000,
        rawText: 'test', draftText: 'черновик',
        lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'pending', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: true,
      });
      const el = exported.createCardElement(data);
      const tp = el.children.find(c => c.className === 'translation translation--orphan');
      assert.ok(tp, 'orphan card should have translation--orphan class');
      assert.ok(tp.textContent.includes('не подтверждён'));
    });

    it('translation replaces draft — not both visible simultaneously', () => {
      const data = exported.segmentToCardData({
        id: 'seg_m', role: 'meeting', tStartMs: 1000,
        rawText: 'test', draftText: 'черновик',
        translation: 'Проверенный перевод',
        draftDelay: '0.9', translationDelay: '2.8',
        lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'done', translation: 'done' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
      });
      const el = exported.createCardElement(data);
      const drafts = el.children.filter(c =>
        c.className && c.className.includes('translation--draft'));
      const verified = el.children.filter(c =>
        c.className && c.className.includes('translation--verified'));
      assert.strictEqual(drafts.length, 0, 'draft should not be present when translation exists');
      assert.strictEqual(verified.length, 1, 'verified translation should be present');
    });

    it('renders lang conflict indicator with note', () => {
      const data = exported.segmentToCardData({
        id: 'seg_c', role: 'meeting', tStartMs: 1000,
        rawText: 'test', draftText: 'черновик',
        lang: 'ES', langConflict: true,
        langNote: 'Язык не совпадает с настройками',
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'pending', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
      });
      const el = exported.createCardElement(data);
      const headerHtml = el.children[0].innerHTML;
      assert.ok(headerHtml.includes('data-conflict="true"'),
        'conflict should be set to true');
      assert.ok(headerHtml.includes('ES'), 'lang code should be visible');
      assert.ok(headerHtml.includes('Язык не совпадает'),
        'lang note should be in title/tooltip');
    });

    it('shows error and retry button when translationError present', () => {
      const data = exported.segmentToCardData({
        id: 'seg_e', role: 'meeting', tStartMs: 1000,
        rawText: 'test', lang: 'ES', langConflict: false,
        privacyProfile: 'open', targetLang: 'RU',
        status: { stt: 'done', translation: 'error' },
        sttError: null,
        translationError: 'timeout',
        superseded: false, orphan: false,
      });
      const el = exported.createCardElement(data);
      const footer = el.children.find(c => c.className === 'status');
      assert.ok(footer, 'footer with status class should exist');
      const fHtml = footer.innerHTML;
      assert.ok(fHtml.includes('timeout'), `expected 'timeout' in footer: ${fHtml}`);
      assert.ok(fHtml.includes('retry-btn'), `expected retry-btn in footer: ${fHtml}`);
    });
  });

  describe('createCards integration', () => {
    it('mount() renders initial segments from stream state', () => {
      const container = dom.createElement('div');
      const stream = {
        getState: () => ({ segments: [
          { id: 'seg_1', role: 'meeting', tStartMs: 1000,
            rawText: 'Hello', draftText: 'Привет',
            lang: 'EN', langConflict: false,
            privacyProfile: 'open', targetLang: 'RU',
            status: { stt: 'done', translation: 'pending' },
            sttError: null, translationError: null,
            superseded: false, orphan: false,
            draftDelay: '0.9', translationDelay: '?' },
        ]}),
        subscribe: () => () => {},
      };
      const cards = exported.createCards({ container, stream });
      cards.mount();
      assert.strictEqual(container.children.length, 1);
      const scrollDiv = container.children[0];
      assert.strictEqual(scrollDiv.className, 'cards-scroll-container');
      cards.unmount();
    });

    it('unmount() calls all unsubscribe functions', () => {
      let unsubCalled = false;
      const container = dom.createElement('div');
      const stream = {
        getState: () => ({ segments: [] }),
        subscribe: () => () => { unsubCalled = true; },
      };
      const cards = exported.createCards({ container, stream });
      cards.mount();
      cards.unmount();
      assert.strictEqual(unsubCalled, true);
    });

    it('unmount() is safe to call twice', () => {
      const container = dom.createElement('div');
      const stream = {
        getState: () => ({ segments: [] }),
        subscribe: () => () => {},
      };
      const cards = exported.createCards({ container, stream });
      cards.mount();
      cards.unmount();
      cards.unmount(); // second call should not throw
      assert.ok(true);
    });

    it('profile comes from segment field, not global state (isolation)', () => {
      const data = exported.segmentToCardData({
        id: 'seg_p', role: 'meeting', tStartMs: 1000,
        rawText: 'test', lang: 'EN', langConflict: false,
        privacyProfile: 'confidential', targetLang: 'RU',
        status: { stt: 'done', translation: 'pending' },
        sttError: null, translationError: null,
        superseded: false, orphan: false,
      });
      assert.strictEqual(data.privacyProfile, 'confidential');
      const el = exported.createCardElement(data);
      const header = el.children[0];
      assert.ok(header.innerHTML.includes('profile--confidential'));
      assert.ok(header.innerHTML.includes('закрытый профиль'));
    });

    it('superseded segment is removed from DOM', () => {
      const container = dom.createElement('div');
      const subs = {};
      const stream = {
        _segments: [
          { id: 'seg_1', role: 'meeting', tStartMs: 1000,
            rawText: 'Hello', draftText: 'Привет',
            lang: 'EN', langConflict: false,
            privacyProfile: 'open', targetLang: 'RU',
            status: { stt: 'done', translation: 'done' },
            sttError: null, translationError: null,
            superseded: false, orphan: false,
            translation: 'Привет',
            draftDelay: '?', translationDelay: '?',
          },
        ],
        getState() { return { segments: this._segments }; },
        subscribe(event, handler) {
          subs[event] = handler;
          return () => delete subs[event];
        },
      };
      const cards = exported.createCards({ container, stream });
      cards.mount();
      assert.strictEqual(container.children[0].children.length, 1,
        'card should be in DOM before supersede');

      stream._segments[0].superseded = true;
      subs['segment.final']({ segment_id: 'seg_1' });

      assert.strictEqual(container.children[0].children.length, 0,
        'superseded card should be removed from DOM');
      cards.unmount();
    });
  });

  describe('formatTime', () => {
    it('formats ms to HH:MM:SS', () => {
      assert.strictEqual(exported.formatTime(0), '00:00:00');
      assert.strictEqual(exported.formatTime(332000), '00:05:32');
      assert.strictEqual(exported.formatTime(3600000), '01:00:00');
      assert.strictEqual(exported.formatTime(3661000), '01:01:01');
      assert.strictEqual(exported.formatTime(-1000), '00:00:00');
    });
  });

  describe('setFilter', () => {
    it('filters cards by role without errors', () => {
      const container = dom.createElement('div');
      const stream = {
        getState: () => ({ segments: [
          { id: 'seg_1', role: 'meeting', tStartMs: 1000,
            rawText: 'Hello', lang: 'EN', langConflict: false,
            privacyProfile: 'open', targetLang: 'RU',
            status: { stt: 'done', translation: 'pending' },
            sttError: null, translationError: null,
            superseded: false, orphan: false },
          { id: 'seg_2', role: 'microphone', tStartMs: 2000,
            rawText: 'Hi', lang: 'EN', langConflict: false,
            privacyProfile: 'open', targetLang: 'RU',
            status: { stt: 'done', translation: 'pending' },
            sttError: null, translationError: null,
            superseded: false, orphan: false },
        ]}),
        subscribe: () => () => {},
      };
      const cards = exported.createCards({ container, stream });
      cards.mount();
      cards.setFilter('meeting');
      cards.unmount();
      assert.ok(true);
    });
  });
});
