/* Shared UI helpers: icons, DOM builder, toast, dialog, formatting */

const ICON_PATHS = {
    check: '<path d="M20 6 9 17l-5-5"/>',
    'check-circle':
        '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    'alert-triangle':
        '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    upload:
        '<path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m16 16-4-4-4 4"/>',
    copy: '<rect x="8" y="8" width="14" height="14" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    retry:
        '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    stop: '<rect x="4" y="4" width="16" height="16" rx="2"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    inbox:
        '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    'file-text':
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    trash:
        '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>',
    'external-link':
        '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    message:
        '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    book: '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    image:
        '<rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    sparkles:
        '<path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/>',
    'arrow-down': '<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
};

/**
 * Create an SVG icon element.
 * @param {string} name
 * @param {{size?: number, className?: string, strokeWidth?: number}} options
 * @returns {SVGElement}
 */
export function icon(name, options = {}) {
    const {size = 18, className = '', strokeWidth = 1.8} = options;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', String(size));
    svg.setAttribute('height', String(size));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(strokeWidth));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    if (className) svg.setAttribute('class', className);
    // Paths are static, trusted strings defined above.
    svg.innerHTML = ICON_PATHS[name] || ICON_PATHS.info;
    return svg;
}

/**
 * Replace [data-icon] placeholders with SVG icons.
 * @param {ParentNode} root
 */
export function hydrateIcons(root = document) {
    root.querySelectorAll('[data-icon]').forEach((el) => {
        const name = el.getAttribute('data-icon') || 'info';
        const size = Number(el.getAttribute('data-icon-size') || 20);
        el.replaceChildren(icon(name, {size}));
        el.removeAttribute('data-icon');
        el.removeAttribute('data-icon-size');
    });
}

/**
 * Tiny hyperscript helper. Strings become text nodes.
 * @param {string} tag
 * @param {Record<string, any>} [attrs]
 * @param {...(Node|string|number|null|undefined|Array<any>)} children
 * @returns {HTMLElement}
 */
export function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);

    Object.entries(attrs || {}).forEach(([key, value]) => {
        if (value === null || value === undefined || value === false) return;

        if (key === 'class' || key === 'className') {
            el.className = String(value);
        } else if (key === 'text') {
            el.textContent = String(value);
        } else if (key === 'dataset') {
            Object.entries(value).forEach(([dataKey, dataValue]) => {
                if (dataValue !== null && dataValue !== undefined) {
                    el.dataset[dataKey] = String(dataValue);
                }
            });
        } else if (key === 'style' && typeof value === 'object') {
            Object.assign(el.style, value);
        } else if (key.startsWith('on') && typeof value === 'function') {
            el.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (value === true) {
            el.setAttribute(key, '');
        } else {
            el.setAttribute(key, String(value));
        }
    });

    const append = (child) => {
        if (child === null || child === undefined || child === false) return;
        if (Array.isArray(child)) {
            child.forEach(append);
        } else if (child instanceof Node) {
            el.appendChild(child);
        } else {
            el.appendChild(document.createTextNode(String(child)));
        }
    };

    children.forEach(append);
    return el;
}

/* ---------- Formatting ---------- */

export function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return '';
    if (value < 1) return `${Math.round(value * 1000)}ms`;
    if (value < 60) return `${value.toFixed(1)}s`;
    const minutes = Math.floor(value / 60);
    const rest = Math.round(value % 60);
    return `${minutes}m ${rest}s`;
}

export function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

function toDate(ts) {
    if (ts === null || ts === undefined || ts === '') return new Date();
    const value = Number(ts);
    if (!Number.isFinite(value)) return new Date();
    // Backend history uses seconds; tolerate milliseconds too.
    return new Date(value > 1e12 ? value : value * 1000);
}

export function formatClockTime(ts) {
    const date = toDate(ts);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'});
}

export function formatDayLabel(ts) {
    const date = toDate(ts);
    if (Number.isNaN(date.getTime())) return '';
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    const diffDays = Math.round((startOfToday - startOfDate) / 86400000);
    if (diffDays === 0) return '今天';
    if (diffDays === 1) return '昨天';
    if (date.getFullYear() === now.getFullYear()) {
        return `${date.getMonth() + 1}月${date.getDate()}日`;
    }
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`;
}

/* ---------- Clipboard ---------- */

export async function copyToClipboard(text) {
    const value = String(text ?? '');
    if (!value) return false;

    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(value);
            return true;
        }
    } catch (_) {
        // fall through to legacy path
    }

    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.top = '-1000px';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    let ok = false;
    try {
        ok = document.execCommand('copy');
    } catch (_) {
        ok = false;
    }
    textarea.remove();
    return ok;
}

/* ---------- Toast ---------- */

function ensureToastRoot() {
    let root = document.querySelector('.toast-root');
    if (!root) {
        root = h('div', {class: 'toast-root', 'aria-live': 'polite'});
        document.body.appendChild(root);
    }
    return root;
}

/**
 * @param {string} message
 * @param {{type?: 'info'|'success'|'error'|'warning', duration?: number}} [options]
 * @returns {() => void} dismiss
 */
export function toast(message, options = {}) {
    const {type = 'info', duration = 4200} = options;
    const root = ensureToastRoot();
    const iconName =
        type === 'success' ? 'check-circle' : type === 'info' ? 'info' : 'alert-triangle';

    let timer = null;

    const node = h(
        'div',
        {class: 'toast', dataset: {type}, role: 'status'},
        h('span', {class: 'toast-icon'}, icon(iconName, {size: 17})),
        h('div', {class: 'toast-body', text: message}),
        h(
            'button',
            {
                class: 'toast-close',
                type: 'button',
                'aria-label': '关闭提示',
                onclick: () => dismiss(),
            },
            icon('x', {size: 14})
        )
    );

    function dismiss() {
        if (!node.isConnected || node.classList.contains('is-leaving')) return;
        clearTimeout(timer);
        node.classList.add('is-leaving');
        node.addEventListener('animationend', () => node.remove(), {once: true});
        setTimeout(() => node.remove(), 320);
    }

    root.appendChild(node);
    timer = setTimeout(dismiss, duration);
    return dismiss;
}

/* ---------- Confirm dialog ---------- */

/**
 * @param {{title?: string, message?: string, confirmText?: string,
 * cancelText?: string, danger?: boolean}} options
 * @returns {Promise<boolean>}
 */
export function confirmDialog(options = {}) {
    const {
        title = '确认操作',
        message = '',
        confirmText = '确定',
        cancelText = '取消',
        danger = false,
    } = options;

    return new Promise((resolve) => {
        let settled = false;

        const confirmBtn = h('button', {
            class: danger ? 'btn btn-danger' : 'btn btn-primary',
            type: 'button',
            text: confirmText,
        });

        const dialog = h(
            'dialog',
            {class: 'dialog'},
            h(
                'div',
                {class: 'dialog-body'},
                h('h2', {class: 'dialog-title', text: title}),
                message ? h('p', {class: 'dialog-text', text: message}) : null
            ),
            h(
                'div',
                {class: 'dialog-actions'},
                h('button', {
                    class: 'btn btn-secondary',
                    type: 'button',
                    text: cancelText,
                    onclick: () => finish(false),
                }),
                confirmBtn
            )
        );

        function finish(value) {
            if (settled) return;
            settled = true;
            resolve(value);
            dialog.close();
            dialog.remove();
        }

        confirmBtn.addEventListener('click', () => finish(true));
        dialog.addEventListener('cancel', (event) => {
            event.preventDefault();
            finish(false);
        });
        dialog.addEventListener('click', (event) => {
            if (event.target === dialog) finish(false);
        });

        document.body.appendChild(dialog);
        dialog.showModal();
        confirmBtn.focus();
    });
}

/* ---------- Interaction helpers ---------- */

export function prefersReducedMotion() {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function autoGrow(textarea, maxHeight = 180) {
    const resize = () => {
        textarea.style.height = 'auto';
        const next = Math.min(textarea.scrollHeight, maxHeight);
        textarea.style.height = `${next}px`;
        textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
    };
    textarea.addEventListener('input', resize);
    resize();
    return resize;
}

export function isNearBottom(scroller, threshold = 140) {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < threshold;
}

export function scrollToBottom(scroller, smooth = true) {
    const behavior = smooth && !prefersReducedMotion() ? 'smooth' : 'auto';
    scroller.scrollTo({top: scroller.scrollHeight, behavior});
}

/**
 * Build a cross-service page URL (import :8000, chat :8001).
 * @param {'import.html'|'chat.html'} page
 */
export function peerPageUrl(page) {
    const targetPort = page === 'chat.html' ? '8001' : '8000';

    if (!/^https?:$/.test(location.protocol)) {
        return `http://127.0.0.1:${targetPort}/front/${page}`;
    }

    const url = new URL(location.href);
    const directory = /\/front\/[^/]*$/.test(url.pathname)
        ? url.pathname.replace(/[^/]*$/, '')
        : '/front/';
    url.pathname = `${directory}${page}`;
    if (url.port === '8000' || url.port === '8001') {
        url.port = String(targetPort);
    }
    url.search = '';
    url.hash = '';
    return url.toString();
}

/* Last-resort error text */
export function errorMessage(error) {
    if (!error) return '未知错误';
    if (typeof error === 'string') return error;
    return error.message || String(error);
}
