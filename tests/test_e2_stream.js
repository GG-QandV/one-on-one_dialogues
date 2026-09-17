/**
 * E2 — тесты SSE-клиента stream.js, регрессия потери перевода.
 *
 * Запуск: node --test tests/test_e2_stream.js
 * Зависимости: нет.
 *
 * Покрывает BUGFIX_E2_translation_loss.md:
 * segment.translated может прийти раньше segment.final, при этом
 * segment_id != utterance_id (fast->accurate). _handleFinal не должен
 * затирать placeholder с уже пришедшим переводом.
 */

const assert = require('node:assert');
const { describe, it } = require('node:test');

const { Stream } = require('../app/ui/static/js/stream.js');

function fresh() {
  const s = new Stream();
  s.ready = true; // применяем события напрямую, без буферизации
  return s;
}

describe('E2 stream.js — порядок translated/final', () => {
  it('сохраняет перевод, когда translated пришёл раньше final (segment_id != utterance_id)', () => {
    const s = fresh();
    s._applyEvent('segment.partial', { utterance_id: 'U1', role: 'meeting', t_start_ms: 100 }, null);
    s._applyEvent('segment.translated', { segment_id: 'S1', translation: 'ok', mode: 'accurate' }, null);
    s._applyEvent('segment.final', {
      segment_id: 'S1', utterance_id: 'U1', raw_text: 'text',
      role: 'meeting', t_start_ms: 100, t_end_ms: 200,
    }, null);

    const seg = s.segments.get('S1');
    assert.strictEqual(seg.translation, 'ok', 'перевод S1 сохранён');
    assert.strictEqual(seg.rawText, 'text', 'rawText дозаполнен final');
    assert.strictEqual(seg.status.stt, 'done');
    assert.strictEqual(seg.status.translation, 'done');
  });

  it('помечает fast-track партиал superseded при смене ключа', () => {
    const s = fresh();
    s._applyEvent('segment.partial', { utterance_id: 'U1', role: 'meeting', t_start_ms: 100 }, null);
    s._applyEvent('segment.translated', { segment_id: 'S1', translation: 'ok', mode: 'accurate' }, null);
    s._applyEvent('segment.final', {
      segment_id: 'S1', utterance_id: 'U1', raw_text: 'text',
      role: 'meeting', t_start_ms: 100, t_end_ms: 200,
    }, null);

    assert.strictEqual(s.segments.get('U1').superseded, true);
  });

  it('регресс: final раньше translated продолжает работать', () => {
    const s = fresh();
    s._applyEvent('segment.final', {
      segment_id: 'S2', utterance_id: 'U2', raw_text: 'text2',
      role: 'microphone', t_start_ms: 10, t_end_ms: 20,
    }, null);
    s._applyEvent('segment.translated', { segment_id: 'S2', translation: 'ok2', mode: 'accurate' }, null);

    const seg = s.segments.get('S2');
    assert.strictEqual(seg.translation, 'ok2');
    assert.strictEqual(seg.status.translation, 'done');
  });

  it('регресс: final и partial с совпадающим ключом не помечаются superseded', () => {
    const s = fresh();
    s._applyEvent('segment.partial', { utterance_id: 'X', role: 'meeting', t_start_ms: 1 }, null);
    s._applyEvent('segment.final', {
      segment_id: 'X', utterance_id: 'X', raw_text: 'tx',
      role: 'meeting', t_start_ms: 1, t_end_ms: 2,
    }, null);

    const seg = s.segments.get('X');
    assert.strictEqual(seg.rawText, 'tx');
    assert.strictEqual(seg.superseded, false);
  });

  it('не падает, если найденная запись без status (снапшот)', () => {
    const s = fresh();
    s.segments.set('S4', { id: 'S4', translation: 'keep', track: 'accurate' });
    s._applyEvent('segment.final', {
      segment_id: 'S4', utterance_id: 'U4', raw_text: 't4',
      role: 'meeting', t_start_ms: 5, t_end_ms: 6,
    }, null);

    const seg = s.segments.get('S4');
    assert.strictEqual(seg.translation, 'keep');
    assert.strictEqual(seg.status.stt, 'done');
  });
});
