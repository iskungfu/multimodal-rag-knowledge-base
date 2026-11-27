/* API layer: base URL resolution, fetch wrapper, SSE helper */

const bodyDataset = document.body ? document.body.dataset : {};
const urlOverride = new URLSearchParams(location.search).get('api');
const configuredBase = (bodyDataset.apiBase || '').trim();

function originBase() {
    if (location.protocol === 'http:' || location.protocol === 'https:') {
        const targetPort = bodyDataset.apiPort;
        const currentPort = location.port || (location.protocol === 'https:' ? '443' : '80');

        // If the page is opened on the sibling service (8000/8001), point it
        // back to the service that owns this page. Other deployments stay same-origin.
        if (targetPort && /^800[01]$/.test(currentPort) && currentPort !== targetPort) {
            return `${location.protocol}//${location.hostname}:${targetPort}`;
        }

        return location.origin;
    }
    const port = bodyDataset.apiPort || '8000';
    return `http://127.0.0.1:${port}`;
}

function trimBase(value) {
    return String(value || '').replace(/\/+$/, '');
}

/**
 * Service base URL.
 * - Served over HTTP: same origin (no hard-coded host/port).
 * - Opened from disk: fall back to <body data-api-port>.
 * - Override: ?api=http://host:port or <body data-api-base="...">.
 */
export const API_BASE = trimBase(urlOverride || configuredBase || originBase());

/* ---------- 超时配置（毫秒） ---------- */

const DEFAULT_TIMEOUT = 30000;

// 流式提交只需立即拿到 task_id，真正的等待发生在 SSE 通道上，短超时即可
const STREAM_SUBMIT_TIMEOUT = 30000;

// 非流式要等整条检索主图（多路召回 + rerank + LLM 生成）跑完，
// 60s 经常不够，因此放宽上限，并允许用 <body data-query-timeout="秒"> 覆盖
const SYNC_SUBMIT_TIMEOUT = 600000;
const syncTimeoutSec = Number(bodyDataset.queryTimeout || 0);
const SYNC_QUERY_TIMEOUT =
    Number.isFinite(syncTimeoutSec) && syncTimeoutSec > 0 ? syncTimeoutSec * 1000 : SYNC_SUBMIT_TIMEOUT;

export class ApiError extends Error {
    constructor(message, status = 0, payload = null) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.payload = payload;
    }
}

function pickErrorMessage(data, status) {
    if (data && typeof data === 'object') {
        const detail = data.detail ?? data.message ?? data.error;
        if (typeof detail === 'string' && detail.trim()) return detail.trim();
        if (Array.isArray(detail) && detail.length) {
            const first = detail[0];
            if (first && typeof first.msg === 'string') return first.msg;
        }
        if (detail) return JSON.stringify(detail);
    }
    if (typeof data === 'string' && data.trim()) {
        return data.trim().slice(0, 300);
    }
    if (status === 422) return '请求参数不合法';
    if (status === 404) return '接口不存在，请确认服务地址';
    if (status >= 500) return '服务处理失败，请稍后重试';
    return `请求失败（HTTP ${status}）`;
}

async function request(path, options = {}) {
    const {
        timeout = DEFAULT_TIMEOUT,
        timeoutMessage = '请求超时，请检查服务是否繁忙',
        ...fetchOptions
    } = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);

    try {
        const response = await fetch(`${API_BASE}${path}`, {
            ...fetchOptions,
            signal: controller.signal,
        });

        const raw = await response.text();
        let data = null;
        if (raw) {
            try {
                data = JSON.parse(raw);
            } catch (_) {
                data = raw;
            }
        }

        if (!response.ok) {
            throw new ApiError(pickErrorMessage(data, response.status), response.status, data);
        }

        return data;
    } catch (error) {
        if (error instanceof ApiError) throw error;
        if (error && error.name === 'AbortError') {
            throw new ApiError(timeoutMessage, 0);
        }
        throw new ApiError('无法连接到服务，请确认后端已启动', 0, error);
    } finally {
        clearTimeout(timer);
    }
}

function postJSON(path, payload, options = {}) {
    return request(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
        ...options,
    });
}

/* ---------- Import service (:8000) ---------- */

export function checkImportHealth() {
    return request('/hello', {timeout: 5000});
}

export function uploadDocument(file, strategy = false) {
    const form = new FormData();
    form.append('file', file, file.name);
    return request(strategy ? '/strategies/upload' : '/upload', {
        method: 'POST',
        body: form,
        timeout: 120000,
    });
}

export function fetchTaskStatus(taskId, strategy = false) {
    return request(`${strategy ? '/strategies' : '/status'}/${encodeURIComponent(taskId)}`, {timeout: 15000});
}

export function importStrategyText(title, text) {
    return postJSON('/strategies/text', {title, text});
}

export function fetchStrategies() {
    return request('/strategies');
}

/* ---------- Query service (:8001) ---------- */

export function saveReview(text, requestId) {
    return postJSON('/reviews', {text, request_id: requestId}, {
        timeout: 90000,
        timeoutMessage: '等待抽取超时，文本可能已保存。可查看最近复盘，或保留输入重试。',
    });
}

export function fetchReviews() {
    return request('/reviews?limit=20');
}

export function submitQuery({query, sessionId, isStream}) {
    const stream = Boolean(isStream);
    // 流式只等 task_id；非流式要等整条主图跑完，两者不能共用同一个超时
    const timeout = stream ? STREAM_SUBMIT_TIMEOUT : SYNC_QUERY_TIMEOUT;

    return postJSON('/query', {
        query,
        session_id: sessionId || null,
        is_stream: stream,
    }, {
        timeout,
        // 非流式超时后后端通常仍在生成，答案最终会写入历史记录
        timeoutMessage: stream
            ? '提交查询超时，请确认后端服务是否正常'
            : `等待回答超时（已等待 ${Math.round(timeout / 1000)}s）。后端可能仍在生成，稍后刷新页面可从历史记录查看结果`,
    });
}

export function fetchHistory(sessionId, limit = 50) {
    const params = new URLSearchParams({limit: String(limit)});
    return request(`/history/${encodeURIComponent(sessionId)}?${params.toString()}`, {
        timeout: 8000,
    });
}

export function clearHistory(sessionId) {
    return request(`/history/${encodeURIComponent(sessionId)}`, {method: 'DELETE'});
}

/**
 * Subscribe to a query task over SSE.
 * @param {string} taskId
 * @param {{onProgress?: Function, onDelta?: Function, onFinal?: Function, onError?: Function}} handlers
 * @returns {{close: () => void}}
 */
export function connectStream(taskId, handlers = {}) {
    const {onProgress, onDelta, onFinal, onError} = handlers;
    let closed = false;
    let source;

    try {
        source = new EventSource(`${API_BASE}/stream/${encodeURIComponent(taskId)}`);
    } catch (error) {
        onError?.(error);
        return {close() {}};
    }

    const parse = (event) => {
        try {
            return JSON.parse(event.data || '{}');
        } catch (_) {
            return {};
        }
    };

    source.addEventListener('progress', (event) => {
        if (!closed) onProgress?.(parse(event));
    });

    source.addEventListener('delta', (event) => {
        if (!closed) onDelta?.(parse(event));
    });

    const finish = (event) => {
        if (closed) return;
        closed = true;
        let data = {};
        try {
            data = JSON.parse(event.data || '{}');
        } catch (_) {
            data = {};
        }
        source.close();
        onFinal?.(data);
    };

    source.addEventListener('final', finish);
    source.addEventListener('final_answer', finish);

    source.addEventListener('error', (event) => {
        if (closed) return;
        closed = true;
        source.close();
        onError?.(event);
    });

    return {
        close() {
            if (closed) return;
            closed = true;
            source.close();
        },
    };
}

/* 每日投资摘要；生成请求立即返回，随后读取持久化状态。 */
export const fetchWatchlist = () => request('/watchlist');
export const addWatch = (query) => request('/watchlist', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({query}),
});
export const deleteWatch = (code) => request(`/watchlist/${encodeURIComponent(code)}`, {method: 'DELETE'});
export const fetchLatestSummary = () => request('/daily-summaries/latest');
export const fetchSummary = (date) => request(`/daily-summaries/${encodeURIComponent(date)}`);
export const fetchSummaryHistory = () => request('/daily-summaries?limit=30');
export const generateSummary = (date, retry = false) => request('/daily-summaries/generate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({date, retry}),
});
