/* Chat page controller */

import {
    clearHistory,
    connectStream,
    fetchHistory,
    submitQuery,
} from './api.js';
import {renderMarkdown} from './markdown.js';
import {
    autoGrow,
    confirmDialog,
    copyToClipboard,
    errorMessage,
    formatClockTime,
    formatDayLabel,
    formatDuration,
    h,
    hydrateIcons,
    icon,
    isNearBottom,
    peerPageUrl,
    scrollToBottom,
    toast,
} from './ui.js';

const SESSION_KEY = 'kb_session_id';
const STREAM_KEY = 'kb_stream';

// Backend may send raw node names; keep progress chips readable client-side too.
const NODE_LABELS = {
    item_name_confirmed_node: '确认问题产品',
    vector_search_node: '切片搜索',
    hyde_search_node: '切片搜索(假设性文档)',
    web_search_node: '网络搜索',
    rrf_merge_node: '倒排融合',
    rerank_node: '重排序',
    reranker_node: '重排序',
    web_mcp_search_node: '网络搜索',
    hybrid_vector_search_node: '切片搜索',
    hyde_vector_search_node: '切片搜索(假设性文档)',
    answer_output_node: '生成答案',
};

function nodeLabel(name) {
    return NODE_LABELS[name] || name;
}

const thread = document.getElementById('thread');
const intro = document.getElementById('chatIntro');
const scroller = document.getElementById('scroller');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');
const stopBtn = document.getElementById('stopBtn');
const clearBtn = document.getElementById('btnClear');
const streamToggle = document.getElementById('streamToggle');
const scrollBottomBtn = document.getElementById('scrollBottom');
const statusLine = document.getElementById('statusLine');

let sessionId = localStorage.getItem(SESSION_KEY) || createSessionId();
localStorage.setItem(SESSION_KEY, sessionId);

let streamEnabled = localStorage.getItem(STREAM_KEY) !== '0';
let activeRun = null;
let pendingSend = false;
let resizeInput = () => {};

/* ---------- Init ---------- */

hydrateIcons();

document.querySelectorAll('[data-page-link="import"]').forEach((link) => {
    link.href = peerPageUrl('import.html');
});

streamToggle.checked = streamEnabled;
streamToggle.addEventListener('change', () => {
    streamEnabled = streamToggle.checked;
    localStorage.setItem(STREAM_KEY, streamEnabled ? '1' : '0');
    toast(streamEnabled ? '已开启流式输出' : '已关闭流式输出', {
        type: 'info',
        duration: 2000,
    });
});

resizeInput = autoGrow(input, 180);

document.querySelectorAll('.suggestion').forEach((button) => {
    button.addEventListener('click', () => {
        sendQuestion(button.dataset.question || button.textContent || '');
    });
});

sendBtn.addEventListener('click', () => sendQuestion(input.value));

input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendQuestion(input.value);
    }
});

stopBtn.addEventListener('click', () => {
    if (!activeRun) return;
    const bot = activeRun.bot;
    closeActiveRun();
    finalizeBot(bot, {answer: bot.raw, stopped: true});
    toast('已停止生成', {type: 'info', duration: 2200});
});

clearBtn.addEventListener('click', async () => {
    const confirmed = await confirmDialog({
        title: '清空当前对话',
        message: '当前会话的全部历史记录将被删除，且无法恢复。知识库中的文档不受影响。',
        confirmText: '清空对话',
        danger: true,
    });
    if (!confirmed) return;

    closeActiveRun();
    setBusy(false);

    try {
        await clearHistory(sessionId);
    } catch (error) {
        toast(`服务端清空失败：${errorMessage(error)}`, {type: 'warning'});
    }

    sessionId = createSessionId();
    localStorage.setItem(SESSION_KEY, sessionId);
    thread.replaceChildren(intro);
    intro.hidden = false;
    statusLine.textContent = '';
    toast('对话已清空', {type: 'success', duration: 2400});
    input.focus();
});

scroller.addEventListener('scroll', () => {
    scrollBottomBtn.hidden = isNearBottom(scroller, 240);
});

scrollBottomBtn.addEventListener('click', () => {
    scrollToBottom(scroller);
});

/* ---------- Session ---------- */

function createSessionId() {
    if (window.crypto && typeof crypto.randomUUID === 'function') {
        return `sess-${crypto.randomUUID().replace(/-/g, '').slice(0, 16)}`;
    }
    return `sess-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

/* ---------- Rendering ---------- */

function hideIntro() {
    if (intro) intro.hidden = true;
}

function addUserMessage(text, ts, smooth = true) {
    hideIntro();
    const el = h(
        'div',
        {class: 'msg msg-user'},
        h(
            'div',
            {class: 'msg-user-main'},
            h('div', {class: 'bubble', text}),
            h('time', {class: 'msg-time', text: formatClockTime(ts)})
        )
    );
    thread.appendChild(el);
    scrollToBottom(scroller, smooth);
    return el;
}

function addDayLine(label) {
    thread.appendChild(h('div', {class: 'dayline', text: label}));
}

function createProgressCard() {
    const iconBox = h('span', {class: 'progress-icon'}, h('span', {class: 'spinner'}));
    const title = h('span', {class: 'progress-title', text: '准备中'});
    const meta = h('span', {class: 'progress-meta', text: ''});
    const steps = h('div', {class: 'progress-steps'});

    const toggle = h(
        'button',
        {
            class: 'icon-btn progress-toggle',
            type: 'button',
            'aria-expanded': 'true',
            'aria-label': '收起处理过程',
        },
        icon('chevron-down', {size: 15, className: 'chev'})
    );

    const card = h(
        'div',
        {class: 'progress-card', dataset: {state: 'running'}},
        h('div', {class: 'progress-head'}, iconBox, title, meta, toggle),
        steps
    );

    const ui = {
        card,
        iconBox,
        title,
        meta,
        steps,
        toggle,
        expanded: true,
        touched: false,
    };

    toggle.addEventListener('click', () => {
        ui.expanded = !ui.expanded;
        ui.touched = true;
        toggle.setAttribute('aria-expanded', String(ui.expanded));
        toggle.setAttribute('aria-label', ui.expanded ? '收起处理过程' : '展开处理过程');
        card.classList.toggle('is-collapsed', !ui.expanded);
    });

    return ui;
}

function updateProgress(ui, {status, done, running, elapsed}) {
    const doneLabels = dedupe(
        (Array.isArray(done) ? done : []).map((name) => nodeLabel(name))
    );
    const runningLabels = dedupe(
        (Array.isArray(running) ? running : []).map((name) => nodeLabel(name))
    ).filter((name) => !doneLabels.includes(name));
    const state = status === 'completed' ? 'completed' : status === 'failed' ? 'failed' : 'running';

    ui.card.dataset.state = state;

    if (state === 'completed') {
        ui.iconBox.replaceChildren(icon('check-circle', {size: 16}));
        ui.title.textContent = '处理完成';
        ui.meta.textContent = `${doneLabels.length} 个步骤${elapsed ? ` · ${formatDuration(elapsed)}` : ''}`;
    } else if (state === 'failed') {
        ui.iconBox.replaceChildren(icon('alert-triangle', {size: 16}));
        ui.title.textContent = '处理失败';
        ui.meta.textContent = '';
    } else {
        ui.iconBox.replaceChildren(h('span', {class: 'spinner'}));
        ui.title.textContent = '正在处理';
        ui.meta.textContent = elapsed ? formatDuration(elapsed) : '';
    }

    const chips = [
        ...doneLabels.map((name) =>
            h(
                'span',
                {class: 'step-chip is-done'},
                icon('check', {size: 11, strokeWidth: 2.6}),
                h('span', {text: name})
            )
        ),
        ...runningLabels.map((name) =>
            h(
                'span',
                {class: 'step-chip is-running'},
                h('span', {class: 'spinner'}),
                h('span', {text: name})
            )
        ),
    ];

    ui.steps.replaceChildren(...chips);
    ui.steps.hidden = chips.length === 0;

    ui.toggle.hidden = chips.length === 0;

    if (state === 'completed' && !ui.touched) {
        setTimeout(() => {
            if (ui.card.dataset.state !== 'completed' || ui.touched) return;
            ui.expanded = false;
            ui.toggle.setAttribute('aria-expanded', 'false');
            ui.card.classList.add('is-collapsed');
        }, 900);
    }
}

function createBotMessage(ts, options = {}) {
    const {history = false, question = ''} = options;

    const answerEl = h('div', {class: 'md'});
    const imagesEl = h('div', {class: 'image-grid', hidden: true});
    const loadingEl = h(
        'div',
        {class: 'answer-loading'},
        h('div', {class: 'skeleton skeleton-line'}),
        h('div', {class: 'skeleton skeleton-line'}),
        h('div', {class: 'skeleton skeleton-line'})
    );

    const progressUi = createProgressCard();
    if (history) progressUi.card.hidden = true;

    const copyBtn = h(
        'button',
        {class: 'action-btn', type: 'button'},
        icon('copy', {size: 13}),
        h('span', {text: '复制'})
    );
    const retryBtn = h(
        'button',
        {class: 'action-btn', type: 'button'},
        icon('retry', {size: 13}),
        h('span', {text: '重试'})
    );
    const actionsEl = h('div', {class: 'msg-actions'}, copyBtn, retryBtn);
    actionsEl.hidden = !history;

    const contentEl = h(
        'div',
        {class: 'msg-content'},
        progressUi.card,
        loadingEl,
        answerEl,
        imagesEl,
        actionsEl
    );

    if (history) loadingEl.hidden = true;

    const el = h(
        'div',
        {class: 'msg msg-bot'},
        h(
            'div',
            {class: 'msg-avatar', 'aria-hidden': 'true'},
            icon('sparkles', {size: 17})
        ),
        h(
            'div',
            {class: 'msg-main'},
            h(
                'div',
                {class: 'msg-head'},
                h('strong', {text: '多模态RAG智能知识库'}),
                h('time', {text: formatClockTime(ts)})
            ),
            contentEl
        )
    );

    thread.appendChild(el);

    const bot = {
        el,
        progressUi,
        answerEl,
        imagesEl,
        loadingEl,
        actionsEl,
        copyBtn,
        retryBtn,
        raw: '',
        question,
        startedAt: Date.now(),
        finished: false,
        done: [],
        running: [],
        renderTimer: null,
        waitTimer: null,
    };

    copyBtn.addEventListener('click', async () => {
        const text = bot.raw || answerEl.textContent || '';
        if (!text) {
            toast('暂无可复制的内容', {type: 'warning', duration: 2000});
            return;
        }
        const ok = await copyToClipboard(text);
        toast(ok ? '回答已复制' : '复制失败，请手动选择文本', {
            type: ok ? 'success' : 'error',
            duration: 2200,
        });
    });

    retryBtn.addEventListener('click', () => {
        if (activeRun || !bot.question) return;
        resetBot(bot);
        runQuery(bot.question, bot);
    });

    return bot;
}

function resetBot(bot) {
    stopWaitTicker(bot);
    clearTimeout(bot.renderTimer);
    bot.raw = '';
    bot.finished = false;
    bot.done = [];
    bot.running = [];
    bot.startedAt = Date.now();
    bot.answerEl.replaceChildren();
    bot.imagesEl.replaceChildren();
    bot.imagesEl.hidden = true;
    bot.loadingEl.hidden = false;
    bot.actionsEl.hidden = true;
    bot.progressUi.card.hidden = false;
    bot.progressUi.card.classList.remove('is-collapsed');
    bot.progressUi.expanded = true;
    bot.progressUi.touched = false;
    bot.progressUi.toggle.setAttribute('aria-expanded', 'true');
    bot.progressUi.toggle.hidden = true;
    bot.progressUi.steps.replaceChildren();
    bot.progressUi.title.textContent = '准备中';
    bot.progressUi.meta.textContent = '';
    bot.progressUi.iconBox.replaceChildren(h('span', {class: 'spinner'}));
    bot.progressUi.card.dataset.state = 'running';
}

function renderAnswer(bot, text, imageUrls = [], options = {}) {
    const {streaming = false} = options;
    const parsed = streaming ? {text: text, images: []} : parseAnswerAndImages(text);
    const displayText = parsed.text || '';

    // Keep the copyable text in sync with what is displayed (image block removed).
    bot.raw = displayText;

    if (!displayText) {
        bot.answerEl.replaceChildren();
    } else {
        bot.answerEl.replaceChildren(renderMarkdown(displayText));
    }

    if (streaming) return;

    const images = dedupe([
        ...parsed.images,
        ...(Array.isArray(imageUrls) ? imageUrls : []),
    ]).filter(isImageUrl);

    renderImageGallery(bot, images);
}

function renderImageGallery(bot, urls) {
    bot.imagesEl.replaceChildren();

    if (!urls.length) {
        bot.imagesEl.hidden = true;
        return;
    }

    bot.imagesEl.hidden = false;

    urls.forEach((url, index) => {
        const img = h('img', {
            src: url,
            alt: `参考图片 ${index + 1}`,
            loading: 'lazy',
            decoding: 'async',
            referrerpolicy: 'no-referrer',
        });

        const thumb = h(
            'button',
            {
                class: 'image-thumb',
                type: 'button',
                'aria-label': `查看大图 ${index + 1}`,
            },
            img
        );

        img.addEventListener('error', () => {
            thumb.remove();
            if (!bot.imagesEl.children.length) {
                bot.imagesEl.hidden = true;
            }
        });
        thumb.addEventListener('click', () => openLightbox(url, img.alt));

        bot.imagesEl.appendChild(thumb);
    });
}

function openLightbox(url, alt) {
    const previousFocus = document.activeElement;
    const closeBtn = h(
        'button',
        {class: 'lightbox-close', type: 'button', 'aria-label': '关闭大图'},
        icon('x', {size: 20})
    );

    const box = h(
        'div',
        {
            class: 'lightbox',
            role: 'dialog',
            'aria-modal': 'true',
            'aria-label': '图片预览',
        },
        closeBtn,
        h('img', {src: url, alt: alt || '参考图片'}),
        h('div', {class: 'lightbox-caption', text: url})
    );

    const onKeyDown = (event) => {
        if (event.key === 'Escape') close();
    };

    function close() {
        box.remove();
        document.removeEventListener('keydown', onKeyDown);
        previousFocus?.focus?.();
    }

    closeBtn.addEventListener('click', close);
    box.addEventListener('click', (event) => {
        if (event.target === box) close();
    });
    document.addEventListener('keydown', onKeyDown);

    document.body.appendChild(box);
    closeBtn.focus();
}

/* ---------- Answer parsing ---------- */

function isImageUrl(raw) {
    const value = String(raw || '').trim();
    if (!value) return false;
    try {
        const url = new URL(value, location.href);
        return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(url.pathname);
    } catch (_) {
        return /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/i.test(value);
    }
}

function dedupe(list) {
    const seen = new Set();
    const out = [];
    list.forEach((item) => {
        const value = String(item || '').trim();
        if (!value || seen.has(value)) return;
        seen.add(value);
        out.push(value);
    });
    return out;
}

function parseAnswerAndImages(text) {
    const raw = String(text || '');
    const marker = /【\s*图片\s*】|\[\s*图片\s*\]/g;
    let lastMatch = null;
    let match;

    while ((match = marker.exec(raw)) !== null) {
        lastMatch = match;
    }

    if (!lastMatch) {
        return {text: raw, images: []};
    }

    const before = raw.slice(0, lastMatch.index).trimEnd();
    const after = raw.slice(lastMatch.index + lastMatch[0].length);
    const urls = [];

    after.split(/\r?\n/).forEach((line) => {
        const value = line.trim();
        if (!value) return;
        if (/^https?:\/\//i.test(value)) {
            urls.push(value);
        } else {
            const found = value.match(/https?:\/\/[^\s<>"'）】。，；]+/gi) || [];
            urls.push(...found);
        }
    });

    return {
        text: before,
        images: dedupe(urls).filter(isImageUrl),
    };
}

/* ---------- Run query ---------- */

function setBusy(busy, canStop = false) {
    sendBtn.disabled = busy;
    input.setAttribute('aria-busy', String(busy));
    stopBtn.hidden = !(busy && canStop);
}

function closeActiveRun() {
    if (!activeRun) return;
    const {close} = activeRun;
    activeRun = null;
    try {
        close?.();
    } catch (_) {
        // ignore
    }
}

async function sendQuestion(rawText) {
    const text = String(rawText || '').trim();
    if (!text || activeRun || pendingSend) return;

    // Wait for history to land first so the transcript stays in order.
    pendingSend = true;
    try {
        await historyReady;
    } finally {
        pendingSend = false;
    }

    if (activeRun) return;

    addUserMessage(text);
    input.value = '';
    resizeInput();

    const bot = createBotMessage(Date.now(), {question: text});
    scrollToBottom(scroller);
    runQuery(text, bot);
}

const WAIT_HINT = '正在检索并生成回答';

function startWaitTicker(bot) {
    stopWaitTicker(bot);
    const tick = () => {
        if (bot.finished) return;
        const sec = Math.max(0, Math.round((Date.now() - bot.startedAt) / 1000));
        statusLine.textContent = `${WAIT_HINT}…已等待 ${sec}s`;
    };
    tick();
    bot.waitTimer = setInterval(tick, 1000);
}

function stopWaitTicker(bot) {
    if (!bot.waitTimer) return;
    clearInterval(bot.waitTimer);
    bot.waitTimer = null;
}

async function runQuery(question, bot) {
    const useStream = streamEnabled;

    activeRun = {bot, close: null};
    bot.question = question;
    setBusy(true, useStream);

    // 非流式要等整条主图跑完，用计时器让用户看到进度仍在推进，避免误判为卡死
    if (useStream) {
        statusLine.textContent = '正在检索知识库…';
    } else {
        startWaitTicker(bot);
    }

    try {
        const data = await submitQuery({
            query: question,
            sessionId,
            isStream: useStream,
        });

        if (!activeRun || activeRun.bot !== bot) return;

        if (!useStream) {
            const doneList = Array.isArray(data?.done_list) ? data.done_list : [];
            bot.done = doneList;
            bot.running = [];

            updateProgress(bot.progressUi, {
                status: data?.error ? 'failed' : 'completed',
                done: bot.done,
                running: [],
                elapsed: (Date.now() - bot.startedAt) / 1000,
            });

            finalizeBot(bot, {
                answer: data?.answer || '',
                error: data?.error || '',
                imageUrls: data?.image_urls || [],
                doneList,
            });
            return;
        }

        if (!data?.task_id) {
            throw new Error('服务未返回任务 ID');
        }

        const stream = connectStream(data.task_id, {
            onProgress: (payload) => {
                if (bot.finished) return;

                if (Array.isArray(payload.done_list)) bot.done = payload.done_list;
                if (Array.isArray(payload.running_list)) bot.running = payload.running_list;

                const status = payload.status || 'processing';

                if (status === 'completed') {
                    bot.done = dedupe([...bot.done, ...bot.running]);
                    bot.running = [];
                    statusLine.textContent = '正在生成回答…';
                }

                updateProgress(bot.progressUi, {
                    status,
                    done: bot.done,
                    running: bot.running,
                    elapsed: (Date.now() - bot.startedAt) / 1000,
                });

                if (status === 'failed') {
                    finalizeBot(bot, {answer: bot.raw, error: '查询流程执行失败'});
                    return;
                }

                if (status === 'completed' && !bot.finishTimer) {
                    // Backstop: some generators close without emitting `final`.
                    bot.finishTimer = setTimeout(() => {
                        if (!bot.finished) {
                            finalizeBot(bot, {answer: bot.raw, doneList: bot.done});
                        }
                    }, 3000);
                }
            },
            onDelta: (payload) => {
                // Backend currently sends {"answer": "<chunk>"}; also accept
                // {"delta": ...} and {"content": ...} for compatibility.
                const delta =
                    typeof payload?.delta === 'string'
                        ? payload.delta
                        : typeof payload?.answer === 'string'
                            ? payload.answer
                            : typeof payload?.content === 'string'
                                ? payload.content
                                : '';

                if (bot.finished || !delta) return;
                bot.raw += delta;
                statusLine.textContent = '正在生成回答…';

                // A late delta means generation is still alive; postpone the backstop.
                if (bot.finishTimer) {
                    clearTimeout(bot.finishTimer);
                    bot.finishTimer = null;
                }

                scheduleAnswerRender(bot);
            },
            onFinal: (payload) => {
                const finalText =
                    typeof payload?.answer === 'string' && payload.answer.trim()
                        ? payload.answer
                        : bot.raw;
                finalizeBot(bot, {
                    answer: finalText,
                    error: payload?.error || '',
                    imageUrls: payload?.image_urls || [],
                    doneList: bot.done,
                });
            },
            onError: () => {
                if (bot.finished) return;
                if (bot.raw.trim()) {
                    finalizeBot(bot, {answer: bot.raw, doneList: bot.done});
                    toast('连接中断，已保留已生成内容', {type: 'warning'});
                } else {
                    finalizeBot(bot, {
                        answer: '',
                        error: 'SSE 连接中断或任务不存在，请稍后重试',
                        doneList: bot.done,
                    });
                }
            },
        });

        activeRun.close = stream.close;
    } catch (error) {
        if (!activeRun || activeRun.bot !== bot) return;
        finalizeBot(bot, {answer: '', error: errorMessage(error)});
    }
}

function scheduleAnswerRender(bot) {
    if (bot.renderTimer) return;
    bot.renderTimer = setTimeout(() => {
        bot.renderTimer = null;
        if (bot.finished) return;
        const nearBottom = isNearBottom(scroller, 220);
        renderAnswer(bot, bot.raw, [], {streaming: true});
        if (nearBottom) scrollToBottom(scroller, false);
    }, 90);
}

function finalizeBot(bot, {answer = '', error = '', imageUrls = [], stopped = false, doneList = null}) {
    if (bot.finished) return;
    bot.finished = true;

    stopWaitTicker(bot);

    clearTimeout(bot.renderTimer);
    clearTimeout(bot.finishTimer);
    bot.renderTimer = null;
    bot.finishTimer = null;

    const wasActive = activeRun && activeRun.bot === bot;
    if (wasActive) {
        closeActiveRun();
    }

    bot.loadingEl.hidden = true;

    if (Array.isArray(doneList)) bot.done = doneList;
    bot.done = dedupe([...bot.done, ...bot.running]);
    bot.running = [];

    const elapsed = (Date.now() - bot.startedAt) / 1000;
    const failed = Boolean(error);

    updateProgress(bot.progressUi, {
        status: failed ? 'failed' : 'completed',
        done: bot.done,
        running: [],
        elapsed,
    });

    const finalText = String(answer || bot.raw || '');
    const nearBottom = isNearBottom(scroller, 240);

    if (failed) {
        renderError(bot, error, finalText);
        statusLine.textContent = '处理失败';
    } else if (stopped && !finalText.trim()) {
        bot.answerEl.replaceChildren(
            h('p', {class: 'answer-note', text: '已停止生成，未产生内容。'})
        );
        statusLine.textContent = '已停止';
    } else if (!finalText.trim()) {
        // Guard against a silent empty final event instead of leaving a blank bubble.
        renderError(bot, '服务未返回答案内容，请重试', '');
        statusLine.textContent = '未返回内容';
    } else {
        renderAnswer(bot, finalText, imageUrls);
        statusLine.textContent = stopped ? '已停止生成' : '回答完成';
    }

    bot.actionsEl.hidden = false;
    bot.retryBtn.hidden = !bot.question;

    setBusy(false);
    if (nearBottom) scrollToBottom(scroller);

    if (failed) {
        toast(error, {type: 'error'});
    }
}

function renderError(bot, error, partialText) {
    bot.answerEl.replaceChildren();

    const cleanText = parseAnswerAndImages(partialText).text;
    if (cleanText.trim()) {
        bot.answerEl.appendChild(renderMarkdown(cleanText));
    }

    bot.answerEl.appendChild(
        h(
            'div',
            {class: 'alert', style: {marginTop: partialText.trim() ? '12px' : '0'}},
            icon('alert-triangle', {size: 16}),
            h('span', {text: error || '本次处理失败，请稍后重试'})
        )
    );
}

/* ---------- History ---------- */

async function loadHistory() {
    statusLine.textContent = '正在加载历史记录…';

    try {
        const data = await fetchHistory(sessionId, 50);
        const items = Array.isArray(data?.items) ? data.items : [];
        if (!items.length) return;

        hideIntro();

        let lastDay = '';
        let pendingQuestion = '';

        items.forEach((item) => {
            const day = formatDayLabel(item.ts);
            if (day && day !== lastDay) {
                addDayLine(day);
                lastDay = day;
            }

            if (item.role === 'user') {
                pendingQuestion = item.text || '';
                addUserMessage(item.text || '', item.ts, false);
                return;
            }

            const bot = createBotMessage(item.ts, {
                history: true,
                question: pendingQuestion,
            });
            renderAnswer(bot, item.text || '', item.image_urls || []);
            bot.actionsEl.hidden = false;
            bot.retryBtn.hidden = !pendingQuestion;
        });

        // Add a day divider for the first message group if history lacked ts.
        scrollToBottom(scroller, false);
    } catch (error) {
        statusLine.textContent = '历史记录加载失败';
        console.warn('[chat] history load failed', error);
    } finally {
        if (statusLine.textContent === '正在加载历史记录…') {
            statusLine.textContent = '';
        }
    }
}

const historyReady = loadHistory();

input.focus();
