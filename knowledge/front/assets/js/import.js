/* Import page controller */

import {
    checkImportHealth,
    fetchTaskStatus,
    uploadDocument,
    importStrategyText,
    fetchStrategies,
} from './api.js';
import {
    errorMessage,
    formatBytes,
    formatClockTime,
    formatDuration,
    h,
    hydrateIcons,
    icon,
    peerPageUrl,
    toast,
} from './ui.js';

const MAX_FILE_SIZE = 50 * 1024 * 1024;
const ACCEPT_EXT = /\.(pdf|md|markdown)$/i;

const FLOW_COMMON = [
    '上传文件',
    '检查文件',
    'Markdown图片处理',
    '文档切分',
    '主体名称识别',
    '向量生成',
    '导入向量数据库',
];

const FLOW_PDF = [
    '上传文件',
    '检查文件',
    'PDF转Markdown',
    'Markdown图片处理',
    '文档切分',
    '主体名称识别',
    '向量生成',
    '导入向量数据库',
];

const STATUS_META = {
    uploading: {label: '上传中', chip: 'chip-primary', tone: ''},
    processing: {label: '处理中', chip: 'chip-primary', tone: ''},
    completed: {label: '已完成', chip: 'chip-success', tone: 'success'},
    failed: {label: '失败', chip: 'chip-danger', tone: 'danger'},
};

const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const taskList = document.getElementById('taskList');
const queueEmpty = document.getElementById('queueEmpty');
const queueCount = document.getElementById('queueCount');
const clearCompletedBtn = document.getElementById('clearCompleted');
const connState = document.getElementById('connState');
const connText = document.getElementById('connText');

/** @type {Array<ReturnType<typeof createTask>>} */
let tasks = [];
let dragDepth = 0;

/* ---------- Static wiring ---------- */

hydrateIcons();

document.querySelectorAll('[data-page-link="chat"]').forEach((link) => {
    link.href = peerPageUrl('chat.html');
});

if (clearCompletedBtn) {
    clearCompletedBtn.addEventListener('click', () => {
        const removable = tasks.filter(
            (task) => task.status === 'completed' || task.status === 'failed'
        );
        removable.forEach((task) => removeTask(task, false));
        updateQueueState();
        if (removable.length) {
            toast(`已清除 ${removable.length} 个已结束任务`, {type: 'success'});
        }
    });
}

/* ---------- Dropzone ---------- */

dropzone.addEventListener('click', () => fileInput.click());

dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        fileInput.click();
    }
});

fileInput.addEventListener('change', (event) => {
    addFiles(event.target.files);
    fileInput.value = '';
});

['dragenter', 'dragover'].forEach((type) => {
    dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        if (type === 'dragenter') dragDepth += 1;
        dropzone.classList.add('is-dragging');
    });
});

dropzone.addEventListener('dragleave', (event) => {
    event.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dropzone.classList.remove('is-dragging');
});

dropzone.addEventListener('drop', (event) => {
    event.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove('is-dragging');
    if (event.dataTransfer && event.dataTransfer.files) {
        addFiles(event.dataTransfer.files);
    }
});

// Prevent the browser from navigating away when dropping files elsewhere.
['dragover', 'drop'].forEach((type) => {
    window.addEventListener(type, (event) => {
        if (event.target !== dropzone && !dropzone.contains(event.target)) {
            event.preventDefault();
        }
    });
});

/* ---------- Queue ---------- */

function addFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length) return;

    let accepted = 0;

    files.forEach((file) => {
        if (!ACCEPT_EXT.test(file.name)) {
            toast(`「${file.name}」格式不支持，仅支持 PDF 与 Markdown`, {type: 'error'});
            return;
        }
        if (file.size === 0) {
            toast(`「${file.name}」是空文件`, {type: 'error'});
            return;
        }
        if (file.size > MAX_FILE_SIZE) {
            toast(`「${file.name}」超过 50 MB 上限`, {type: 'error'});
            return;
        }

        const task = createTask(file);
        tasks.unshift(task);
        taskList.prepend(task.el);
        accepted += 1;
        startUpload(task);
    });

    if (accepted) {
        updateQueueState();
    }
}

function createTask(file, strategy = document.getElementById('importKind').value === 'personal') {
    const isPdf = /\.pdf$/i.test(file.name);
    const flow = strategy
        ? ['上传文件', '检查文件', ...(isPdf ? ['PDF转Markdown'] : []), '策略脱敏', '文档切分', '向量生成', '导入向量数据库']
        : (isPdf ? FLOW_PDF : FLOW_COMMON);
    const uid = `task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;

    const task = {
        id: uid,
        file,
        strategy,
        name: file.name,
        size: file.size,
        isPdf,
        flow,
        taskId: '',
        status: 'uploading',
        done: [],
        running: [],
        durations: {},
        error: '',
        startedAt: Date.now(),
        stopped: false,
        pollTimer: null,
        consecutiveErrors: 0,
        stepsExpanded: true,
        el: null,
    };

    const statusChip = h('span', {class: 'chip chip-primary'});

    const progressFill = h('div', {class: 'progress-fill'});
    const progressValue = h('span', {class: 'progress-value', text: '0%'});

    const stepsList = h('ol', {class: 'task-steps', id: `${uid}-steps`});
    const stepsSummary = h('span', {class: 'steps-summary', text: '处理节点'});
    const stepsToggle = h(
        'button',
        {
            class: 'steps-toggle',
            type: 'button',
            'aria-expanded': 'true',
            'aria-controls': `${uid}-steps`,
        },
        stepsSummary,
        icon('chevron-down', {size: 14, className: 'chev'})
    );

    stepsToggle.addEventListener('click', () => {
        task.stepsExpanded = !task.stepsExpanded;
        stepsToggle.setAttribute('aria-expanded', String(task.stepsExpanded));
        stepsList.hidden = !task.stepsExpanded;
    });

    const errorText = h('span');
    const errorBox = h(
        'div',
        {class: 'alert task-error', hidden: true},
        icon('alert-triangle', {size: 16}),
        errorText
    );

    const timeEl = h('span', {class: 'task-time', text: '等待处理'});
    const chatLink = h(
        'a',
        {
            class: 'btn btn-sm btn-secondary',
            href: peerPageUrl('chat.html'),
            target: '_blank',
            rel: 'noopener noreferrer',
            hidden: true,
        },
        icon('message', {size: 14}),
        h('span', {text: '去提问'})
    );
    const retryBtn = h(
        'button',
        {class: 'btn btn-sm btn-secondary', type: 'button', hidden: true},
        icon('retry', {size: 14}),
        h('span', {text: '重试'})
    );
    retryBtn.addEventListener('click', () => retryTask(task));

    const removeBtn = h(
        'button',
        {
            class: 'icon-btn task-remove',
            type: 'button',
            'aria-label': `从列表中移除 ${file.name}`,
            title: '从列表中移除（不中断服务端处理）',
        },
        icon('x', {size: 16})
    );
    removeBtn.addEventListener('click', () => removeTask(task, true));

    const card = h(
        'article',
        {class: 'task-card', dataset: {status: 'uploading'}},
        h(
            'div',
            {class: 'task-head'},
            h('span', {class: 'file-badge'}, h('span', {class: 'file-ext', text: isPdf ? 'PDF' : 'MD'})),
            h(
                'div',
                {class: 'task-info'},
                h(
                    'div',
                    {class: 'task-title-row'},
                    h('h3', {class: 'task-name', text: file.name}),
                    statusChip
                ),
                h('p', {
                    class: 'task-sub',
                    text: `${formatBytes(file.size)} · ${isPdf ? 'PDF' : 'Markdown'} · ${formatClockTime(Date.now())}`,
                })
            ),
            removeBtn
        ),
        h(
            'div',
            {class: 'progress task-progress'},
            h('div', {class: 'progress-track'}, progressFill),
            progressValue
        ),
        h(
            'div',
            {class: 'task-steps-wrap'},
            stepsToggle,
            stepsList
        ),
        errorBox,
        h('div', {class: 'task-foot'}, timeEl, h('div', {class: 'task-foot-actions'}, chatLink, retryBtn))
    );

    Object.assign(task, {
        el: card,
        statusChip,
        progressFill,
        progressValue,
        stepsList,
        stepsSummary,
        stepsToggle,
        errorBox,
        errorText,
        timeEl,
        chatLink,
        retryBtn,
    });

    renderTask(task);
    return task;
}

function renderTask(task) {
    const meta = STATUS_META[task.status] || STATUS_META.processing;
    const card = task.el;

    card.dataset.status = task.status;

    task.statusChip.className = `chip ${meta.chip}`;
    task.statusChip.replaceChildren(h('span', {class: 'dot'}), document.createTextNode(meta.label));

    const total = task.flow.length;
    let doneCount = task.flow.filter((name) => task.done.includes(name)).length;

    if (task.status === 'completed') doneCount = total;

    let percent = 0;
    if (task.status === 'uploading') {
        percent = 4;
    } else if (task.status === 'completed') {
        percent = 100;
    } else if (task.status === 'failed') {
        percent = Math.max(4, (doneCount / total) * 100);
    } else {
        const runningBonus = task.running.length ? 0.5 : 0;
        percent = Math.min(96, ((doneCount + runningBonus) / total) * 100);
    }

    task.progressFill.style.width = `${Math.max(2, percent)}%`;
    task.progressFill.dataset.tone = meta.tone;
    task.progressValue.textContent = `${Math.round(percent)}%`;

    task.stepsSummary.textContent = `处理节点 ${doneCount} / ${total}`;

    const doneSet = new Set(task.done);
    const runningSet = new Set(task.running);

    task.stepsList.replaceChildren(
        ...task.flow.map((name) => {
            const done = doneSet.has(name);
            const running = !done && runningSet.has(name);
            const duration = task.durations[name];

            const dot = h(
                'span',
                {class: 'step-dot'},
                done
                    ? icon('check', {size: 11, strokeWidth: 2.6})
                    : running
                        ? h('span', {class: 'spinner'})
                        : null
            );

            return h(
                'li',
                {class: `step${done ? ' is-done' : ''}${running ? ' is-running' : ''}`},
                dot,
                h('span', {class: 'step-name', text: name}),
                h('span', {
                    class: 'step-time',
                    text: duration ? formatDuration(duration) : '',
                })
            );
        })
    );

    task.errorBox.hidden = !task.error;
    task.errorText.textContent = task.error || '';

    task.chatLink.hidden = task.status !== 'completed' || task.strategy;
    task.retryBtn.hidden = task.status !== 'failed';

    const totalDuration = Object.values(task.durations).reduce(
        (sum, value) => sum + (Number(value) || 0),
        0
    );
    const elapsed = (Date.now() - task.startedAt) / 1000;

    if (task.status === 'uploading') {
        task.timeEl.textContent = '正在上传文件';
    } else if (task.status === 'processing') {
        task.timeEl.textContent = totalDuration > 0
            ? `已用时 ${formatDuration(totalDuration)}`
            : `已用时 ${formatDuration(elapsed)}`;
    } else if (task.status === 'completed') {
        task.timeEl.textContent = `总耗时 ${formatDuration(totalDuration || elapsed)}`;
    } else {
        task.timeEl.textContent = '处理失败';
    }
}

/* ---------- Lifecycle ---------- */

async function startUpload(task) {
    clearTimeout(task.pollTimer);
    task.stopped = false;
    task.consecutiveErrors = 0;
    task.status = 'uploading';
    task.taskId = '';
    task.done = [];
    task.running = ['上传文件'];
    task.durations = {};
    task.error = '';
    task.startedAt = Date.now();
    task.stepsExpanded = true;
    task.stepsToggle.setAttribute('aria-expanded', 'true');
    task.stepsList.hidden = false;
    renderTask(task);

    try {
        const result = task.textInput
            ? await importStrategyText(task.textInput.title, task.textInput.text)
            : await uploadDocument(task.file, task.strategy);
        if (task.stopped || !task.el.isConnected) return;
        if (!result || !result.task_id) {
            throw new Error('服务未返回任务 ID');
        }
        task.taskId = result.task_id;
        task.status = 'processing';
        task.done = ['上传文件'];
        task.running = [];
        renderTask(task);
        pollTask(task);
    } catch (error) {
        if (task.stopped || !task.el.isConnected) return;
        task.status = 'failed';
        task.error = errorMessage(error);
        task.running = [];
        task.stopped = true;
        renderTask(task);
        toast(`「${task.name}」上传失败：${task.error}`, {type: 'error'});
    }
}

async function pollTask(task) {
    if (task.stopped) return;

    try {
        const data = await fetchTaskStatus(task.taskId, task.strategy);
        if (task.stopped || !task.el.isConnected) return;
        task.consecutiveErrors = 0;
        applyStatus(task, data || {});
    } catch (error) {
        if (task.stopped) return;
        task.consecutiveErrors += 1;

        if (task.consecutiveErrors >= 5) {
            task.status = 'failed';
            task.error = `无法获取任务状态：${errorMessage(error)}`;
            task.running = [];
            task.stopped = true;
            renderTask(task);
            return;
        }

        if (task.consecutiveErrors === 2) {
            toast('任务状态获取异常，正在重试…', {type: 'warning'});
        }
    }

    if (!task.stopped && task.status === 'processing') {
        task.pollTimer = setTimeout(() => pollTask(task), 1400);
    }
}

function applyStatus(task, data) {
    if (Array.isArray(data.done_list)) task.done = data.done_list;
    if (Array.isArray(data.running_list)) task.running = data.running_list;
    if (data.durations && typeof data.durations === 'object') task.durations = data.durations;

    const status = data.status || 'processing';

    if (status === 'completed') {
        task.status = 'completed';
        task.done = [...task.flow];
        task.running = [];
        task.stopped = true;
        toast(`「${task.name}」已完成导入`, {type: 'success'});
        renderTask(task);
        collapseStepsLater(task);
        if (task.strategy) refreshStrategies();
        return;
    }

    if (status === 'failed') {
        task.status = 'failed';
        task.running = [];
        task.stopped = true;
        task.error = data.error || '工作流执行失败，请检查后端日志后重试';
        renderTask(task);
        return;
    }

    task.status = 'processing';
    renderTask(task);
}

function collapseStepsLater(task) {
    setTimeout(() => {
        if (task.status !== 'completed' || !task.stepsExpanded) return;
        task.stepsExpanded = false;
        task.stepsToggle.setAttribute('aria-expanded', 'false');
        task.stepsList.hidden = true;
    }, 1200);
}

function retryTask(task) {
    if (task.status === 'uploading' || task.status === 'processing') return;
    toast(`正在重新上传「${task.name}」`, {type: 'info'});
    startUpload(task);
}

function removeTask(task, notify) {
    clearTimeout(task.pollTimer);
    task.stopped = true;
    task.el.remove();
    tasks = tasks.filter((item) => item !== task);
    updateQueueState();
    if (notify) {
        toast(`已从列表移除「${task.name}」`, {type: 'info', duration: 2600});
    }
}

function updateQueueState() {
    queueEmpty.hidden = tasks.length > 0;
    queueCount.textContent = String(tasks.length);

    const hasFinished = tasks.some(
        (task) => task.status === 'completed' || task.status === 'failed'
    );
    clearCompletedBtn.hidden = !hasFinished;
    clearCompletedBtn.disabled = !hasFinished;
}

/* ---------- Health ---------- */

async function refreshHealth() {
    if (!connState || !connText) return;

    connState.dataset.state = 'pending';
    connText.textContent = '检查服务';

    try {
        await checkImportHealth();
        connState.dataset.state = 'ok';
        connText.textContent = '服务正常';
    } catch (_) {
        connState.dataset.state = 'error';
        connText.textContent = '服务未连接';
    }
}

refreshHealth();
setInterval(refreshHealth, 30000);

updateQueueState();

document.getElementById('strategyTextForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const title = document.getElementById('strategyTitle').value.trim();
    const text = document.getElementById('strategyText').value.trim();
    if (!title || !text) return;
    const task = createTask(new File([text], `${title}.md`, {type: 'text/markdown'}), true);
    task.textInput = {title, text};
    tasks.unshift(task);
    taskList.prepend(task.el);
    updateQueueState();
    startUpload(task);
});

async function refreshStrategies() {
    const status = document.getElementById('strategyListStatus');
    try {
        const result = await fetchStrategies();
        document.getElementById('strategyList').replaceChildren(...result.items.map((item) =>
            h('li', {text: `${item.title} · ${STATUS_META[item.status]?.label || item.status}${item.error ? ` · ${item.error}` : ''}`})
        ));
        status.textContent = result.items.length ? '' : '还没有保存个人策略';
    } catch (error) {
        status.textContent = errorMessage(error);
    }
}
document.getElementById('refreshStrategies').addEventListener('click', refreshStrategies);
refreshStrategies();
