/**
 * E2 — SSE клиент: подписка, reconnect, дедупликация, нормализованное состояние
 * Зависит от: E1 (ui/server.py) — endpoint /events и /api/snapshot
 * Блокирует: E3 (cards.js), E4 (панель), E6 (диагностика), E7 (история), H6 (приёмка)
 */

const DEFAULT_MAX_BACKOFF_MS = 30000;
const HEARTBEAT_MULTIPLIER = 3;
const ORPHAN_TIMEOUT_MS = 30000;
const JITTER_FACTOR = 0.2;

function safeParseArray(s) {
    try { const v = JSON.parse(s); return Array.isArray(v) ? v : []; }
    catch { return []; }
}

class Stream {
    constructor({ url = '/events', snapshotUrl = '/api/snapshot', maxBackoffMs = DEFAULT_MAX_BACKOFF_MS } = {}) {
        this.url = url;
        this.snapshotUrl = snapshotUrl;
        this.maxBackoffMs = maxBackoffMs;

        this.es = null;
        this.buffer = [];
        this.ready = false;
        this.reconnectAttempt = 0;
        this.reconnectTimer = null;
        this.heartbeatTimer = null;
        this.lastEventTime = Date.now();
        this.seenSequences = new Set(); // (type, id, sequence)
        this.subscribers = new Map(); // eventType -> Set(handler)

        // Нормализованное состояние
        this.segments = new Map(); // segmentId -> { id, role, tStartMs, tEndMs, rawText, translation, mode, track, superseded, status: {stt, translation} }
        this.drafts = new Map();   // draftId -> { id, triggerSegmentId, draftRu, draftTranslated, sources, hasGaps, copied }

        this.connectionState = 'closed'; // 'connecting' | 'open' | 'reconnecting' | 'closed'
    }

    // ========== Публичный API ==========

    /**
     * Подписаться на изменения состояния
     * @param {string} eventType - тип события: 'segment.partial', 'segment.final', 'segment.translated', 'draft.created', 'draft.translated', 'privacy.changed', 'status', 'connection'
     * @param {Function} handler - вызывается с (data) при изменении
     * @returns {Function} unsubscribe
     */
    subscribe(eventType, handler) {
        if (!this.subscribers.has(eventType)) {
            this.subscribers.set(eventType, new Set());
        }
        this.subscribers.get(eventType).add(handler);
        return () => this.subscribers.get(eventType).delete(handler);
    }

    /** Получить полное текущее состояние для первичной отрисовки */
    getState() {
        return {
            segments: Array.from(this.segments.values()).sort((a, b) => a.tStartMs - b.tStartMs),
            drafts: Array.from(this.drafts.values()),
            privacy: this.privacy,
            status: this.status,
        };
    }

    /** Открыть соединение */
    connect() {
        if (this.es) return;
        this._doConnect();
    }

    /** Закрыть соединение и остановить таймеры */
    close() {
        this._clearTimers();
        if (this.es) {
            this.es.close();
            this.es = null;
        }
        this._setConnectionState('closed');
    }

    /** Принудительный реконнект (например, после смены профиля) */
    reconnect() {
        this.close();
        this.reconnectAttempt = 0;
        this._doConnect();
    }

    // ========== Внутреннее ==========

    _doConnect() {
        this._setConnectionState('connecting');
        this.es = new EventSource(this.url);

        this.es.onopen = () => {
            this.reconnectAttempt = 0;
            this._setConnectionState('open');
            this._startHeartbeatWatchdog();
            this._fetchSnapshot();
        };

        this.es.onerror = (err) => {
            // EventSource не даёт статус ответа; различаем по тому, дошёл ли снапшот
            if (this.connectionState === 'connecting') {
                // Начальное подключение не удалось — будет ретрай через onerror/onclose
            }
            this._scheduleReconnect();
        };

        // Именованные события — через addEventListener
        const eventTypes = [
            'segment.partial',
            'segment.final',
            'segment.translated',
            'draft.created',
            'draft.translated',
            'privacy.changed',
            'status',
        ];
        for (const type of eventTypes) {
            this.es.addEventListener(type, (e) => this._onEvent(type, e));
        }

        // Обычные сообщения (без event:) — тоже обрабатываем
        this.es.onmessage = (e) => this._onEvent('message', e);
    }

    async _fetchSnapshot() {
        try {
            const resp = await fetch(this.snapshotUrl);
            if (!resp.ok) throw new Error(`snapshot ${resp.status}`);
            const snap = await resp.json();
            this._applySnapshot(snap);
        } catch (err) {
            console.warn('[Stream] snapshot fetch failed:', err);
            // Не фатально — будем работать только с событиями
        } finally {
            this.ready = true;
            // Применить накопленные события
            const buf = this.buffer.splice(0);
            for (const ev of buf) this._applyEvent(ev.type, ev.data, ev.sequence);
        }
    }

    _onEvent(type, event) {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            return; // игнорируем не-JSON
        }
        const sequence = event.lastEventId || data.sequence;
        if (!this.ready) {
            this.buffer.push({ type, data, sequence });
            return;
        }
        this._applyEvent(type, data, sequence);
    }

    _applyEvent(type, data, sequence) {
        // Дедупликация по (type, id, sequence)
        const id = data.segment_id || data.utterance_id || data.draft_id || data.id;
        if (id && sequence != null) {
            const key = `${type}:${id}:${sequence}`;
            if (this.seenSequences.has(key)) return;
            this.seenSequences.add(key);
        }

        this.lastEventTime = Date.now();

        switch (type) {
            case 'segment.partial':
                this._handlePartial(data);
                break;
            case 'segment.final':
                this._handleFinal(data);
                break;
            case 'segment.translated':
                this._handleTranslated(data);
                break;
            case 'draft.created':
                this._handleDraftCreated(data);
                break;
            case 'draft.translated':
                this._handleDraftTranslated(data);
                break;
            case 'privacy.changed':
                this._handlePrivacyChange(data);
                break;
            case 'status':
                this._handleStatus(data);
                break;
            case 'connection':
                // Не ожидаем от сервера, но на случай
                break;
        }
        this._notify(type, data);
    }

    _handlePartial(data) {
        const seg = this.segments.get(data.utterance_id) || {
            id: data.utterance_id,
            role: data.role,
            tStartMs: data.t_start_ms,
            tEndMs: data.t_start_ms, // будет обновлён при final
            rawText: null,
            translation: null,
            mode: null,
            track: 'fast',
            superseded: false,
            status: { stt: 'pending', translation: 'pending' },
            draftText: data.text || '',
            draftDelay: '?',
            lang: '??',
            langConflict: false,
            privacyProfile: 'open',
            targetLang: '??',
            sttError: null,
            translationError: null,
        };
        seg.draftText = data.text || '';
        this.segments.set(data.utterance_id, seg);
    }

    _handleFinal(data) {
        const existing = this.segments.get(data.utterance_id);
        const seg = existing || {
            id: data.segment_id,
            role: data.role,
            tStartMs: data.t_start_ms,
            tEndMs: data.t_end_ms,
            rawText: data.raw_text,
            translation: null,
            mode: null,
            track: 'accurate',
            superseded: false,
            status: { stt: 'done', translation: 'pending' },
            draftText: '',
            draftDelay: '?',
            lang: '??',
            langConflict: false,
            privacyProfile: 'open',
            targetLang: '??',
            sttError: null,
            translationError: null,
        };
        seg.id = data.segment_id;
        seg.tStartMs = data.t_start_ms;
        seg.tEndMs = data.t_end_ms;
        seg.rawText = data.raw_text;
        seg.track = 'accurate';
        seg.status.stt = 'done';
        this.segments.set(data.segment_id, seg);

        // Если был partial с тем же utterance_id — помечаем его superseded
        if (existing && existing.id !== data.segment_id) {
            existing.superseded = true;
        }
    }

    _handleTranslated(data) {
        const seg = this.segments.get(data.segment_id);
        if (!seg) return;
        seg.translation = data.translation;
        seg.mode = data.mode;
        seg.status.translation = 'done';

        // Вытеснение fast-track сегментов
        if (Array.isArray(data.superseded_ids)) {
            for (const fastId of data.superseded_ids) {
                const fastSeg = this.segments.get(fastId);
                if (fastSeg) {
                    fastSeg.superseded = true;
                }
            }
        }
    }

    _handleDraftCreated(data) {
        this.drafts.set(data.draft_id, {
            id: data.draft_id,
            triggerSegmentId: data.trigger_segment_id,
            draftRu: data.draft_ru,
            draftTranslated: null,
            sources: data.sources || [],
            hasGaps: data.has_gaps || false,
            gapNote: data.gap_note ?? null,
            confidence: (typeof data.confidence === 'number') ? data.confidence : null,
            langOk: (data.lang_ok === undefined) ? true : Boolean(data.lang_ok),
            suggestedClarification: data.suggested_clarification ?? null,
            copied: false,
        });
    }

    _handleDraftTranslated(data) {
        const draft = this.drafts.get(data.draft_id);
        if (draft) {
            draft.draftTranslated = data.draft_translated;
        }
    }

    _handlePrivacyChange(data) {
        this.privacy = data;
        this._notify('privacy.changed', data);
    }

    _handleStatus(data) {
        this.status = data;
        this._notify('status', data);
    }

    _applySnapshot(snap) {
        if (!snap) return;
        // Сегменты
        if (Array.isArray(snap.segments)) {
            for (const s of snap.segments) {
                const existing = this.segments.get(s.id);
                // Снапшот не затирает более новое состояние (сравниваем sequence если есть)
                if (!existing || (s.sequence != null && existing.sequence != null && s.sequence > existing.sequence)) {
                    this.segments.set(s.id, { ...s, sequence: s.sequence });
                }
            }
        }
        // Черновики: снапшот приходит в snake_case (из БД), приводим к camelCase
        // как в _handleDraftCreated, иначе карточка не отрисуется.
        if (Array.isArray(snap.drafts)) {
            for (const d of snap.drafts) {
                const existing = this.drafts.get(d.id);
                if (!existing || (d.sequence != null && existing.sequence != null && d.sequence > existing.sequence)) {
                    this.drafts.set(d.id, {
                        id: d.id,
                        triggerSegmentId: d.trigger_segment_id,
                        draftRu: d.draft_ru,
                        draftTranslated: d.draft_translated ?? null,
                        sources: (typeof d.sources_json === 'string')
                            ? safeParseArray(d.sources_json) : (d.sources || []),
                        hasGaps: Boolean(d.has_gaps),
                        gapNote: d.gap_note ?? null,
                        confidence: (typeof d.confidence === 'number') ? d.confidence : null,
                        langOk: (d.lang_ok === undefined || d.lang_ok === null) ? true : Boolean(d.lang_ok),
                        suggestedClarification: d.suggested_clarification ?? null,
                        copied: d.status === 'copied',
                        ignored: d.status === 'ignored',
                        sequence: d.sequence,
                    });
                }
            }
        }
        if (snap.privacy) this.privacy = snap.privacy;
        if (snap.status) this.status = snap.status;
    }

    _notify(eventType, data) {
        const handlers = this.subscribers.get(eventType);
        if (handlers) {
            for (const h of handlers) {
                try { h(data); } catch (err) { console.error('[Stream] handler error:', err); }
            }
        }
    }

    _setConnectionState(state) {
        if (this.connectionState === state) return;
        this.connectionState = state;
        this._notify('connection', { state, attempt: this.reconnectAttempt });
    }

    _scheduleReconnect() {
        if (this.reconnectTimer) return;
        this._setConnectionState('reconnecting');
        this._clearTimers();

        const base = Math.min(1000 * Math.pow(2, this.reconnectAttempt), this.maxBackoffMs);
        const jitter = base * JITTER_FACTOR * (Math.random() * 2 - 1); // ±20%
        const delay = Math.max(100, Math.round(base + jitter));
        this.reconnectAttempt++;

        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            if (this.connectionState !== 'closed') {
                this._doConnect();
            }
        }, delay);
    }

    _startHeartbeatWatchdog() {
        this._clearHeartbeat();
        this.heartbeatTimer = setInterval(() => {
            const elapsed = Date.now() - this.lastEventTime;
            // Heartbeat интервал на сервере ~15с (см. E1), умножаем на 3
            if (elapsed > 45000) {
                console.warn('[Stream] heartbeat timeout, forcing reconnect');
                this.es.close(); // вызовет onerror -> reconnect
            }
        }, 10000);
    }

    _clearTimers() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this._clearHeartbeat();
    }

    _clearHeartbeat() {
        if (this.heartbeatTimer) {
            clearInterval(this.heartbeatTimer);
            this.heartbeatTimer = null;
        }
    }

    // ========== Orphan detection (вызывается периодически извне или по таймеру) ==========
    /** Помечает partial-сегменты без final за 30с как orphan */
    checkOrphans() {
        const now = Date.now();
        for (const seg of this.segments.values()) {
            if (seg.track === 'fast' && !seg.superseded && seg.tStartMs) {
                const age = now - seg.tStartMs;
                if (age > ORPHAN_TIMEOUT_MS && !seg.orphan) {
                    seg.orphan = true;
                    this._notify('segment.orphan', seg);
                }
            }
        }
    }
}

/** Фабрика для удобства */
function createStream(options) {
    return new Stream(options);
}

// Экспорт для модульных систем и глобального использования
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { Stream, createStream };
} else if (typeof window !== 'undefined') {
    window.Stream = Stream;
    window.createStream = createStream;
}