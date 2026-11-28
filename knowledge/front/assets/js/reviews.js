import {saveReview, fetchReviews, indexReview, editReview, fetchReview, createRequestId as requestId} from './api.js';

const form = document.getElementById('reviewForm');
const input = document.getElementById('reviewText');
const save = document.getElementById('reviewSave');
const refresh = document.getElementById('reviewRefresh');
const status = document.getElementById('reviewStatus');
const list = document.getElementById('reviewList');
let draftId = null;
let loaded = false;

const states = {executed: '已成交', planned: '计划 / 未成交', unknown: '成交状态未明确'};
const indexStates = {
    pending: '待建立索引，原文已保存。',
    processing: '正在建立索引，原文已保存；稍后刷新查看。',
    completed: '索引已完成，可在提问时检索这条复盘。',
    failed: '索引失败，原文已保存，可稍后重试。',
    interrupted: '索引中断，原文已保存，可重试。',
};

function node(tag, text) {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
}

function renderReview(review) {
    const article = document.createElement('article');
    article.append(node('h3', `复盘 · ${new Date(review.created_at).toLocaleString('zh-CN')}`));
    article.append(node('p', review.original_text));
    if (review.extraction_status === 'succeeded') {
        const extraction = review.extraction;
        const events = document.createElement('ul');
        for (const event of extraction.events) {
            const direction = {buy: '买入', sell: '卖出'}[event.direction] || '方向未明示';
            const price = event.price == null ? '价格未明示' : `${event.price} 元/股`;
            const name = [event.stock_name, event.mentioned_stock_code].filter(Boolean).join(' ') || '标的未明示';
            const item = node('li', `${states[event.status]}：${name}，${direction}，${price}`);
            if (event.identity_conflict) item.append(node('p', '名称与代码存在冲突，请核对原文；暂不关联个股。'));
            if (event.reason) item.append(node('p', `原因：${event.reason}`));
            item.append(node('p', `原文依据：${event.evidence}`));
            events.append(item);
        }
        article.append(events);
        if (!extraction.events.length) article.append(node('p', '未提取到具体买卖事件。'));
        article.append(node('p', `反思教训：${extraction.lesson || '未明示'}`));
        article.append(node('p', `情绪：${extraction.emotions.join('、') || '未明示'}`));
        article.append(node('p', '自动抽取结果请与原文核对。'));
    } else {
        article.append(node('p', review.extraction_status === 'failed'
            ? '抽取未成功，复盘文本已保存，可按原文核对。'
            : '复盘文本已保存，抽取尚未完成；稍后可刷新查看。'));
    }
    const indexing = document.createElement('div');
    const indexStatus = review.index_status || 'pending';
    indexing.append(node('p', `检索索引：${indexStates[indexStatus] || '状态暂不可用，原文已保存。'}`));
    if (!['completed', 'processing'].includes(indexStatus)) {
        const retry = node('button', indexStatus === 'pending' ? '建立索引' : '重试索引');
        retry.type = 'button';
        retry.className = 'btn btn-ghost btn-sm';
        retry.disabled = review.index_can_retry === false;
        if (retry.disabled && review.index_retry_after) {
            indexing.append(node('p', `可重试时间：${new Date(review.index_retry_after).toLocaleString('zh-CN')}，届时请刷新。`));
        }
        retry.addEventListener('click', async () => {
            if (retry.disabled || article.dataset.editing) return;
            article.dataset.indexing = 'true';
            edit.disabled = true;
            retry.disabled = true;
            retry.textContent = '正在建立索引…';
            indexing.setAttribute('aria-busy', 'true');
            status.textContent = '正在建立复盘索引，原文已保存。';
            try {
                const updated = await indexReview(review.id);
                if (!article.dataset.editing) article.replaceWith(renderReview(updated));
                status.textContent = updated.warning || indexStates[updated.index_status] || '请刷新查看索引状态。';
            } catch (error) {
                retry.textContent = '重试索引';
                retry.disabled = false;
                status.textContent = `${error.message || '索引暂不可用。'} 复盘原文已保存，请刷新查看状态。`;
            } finally {
                indexing.removeAttribute('aria-busy');
                delete article.dataset.indexing;
                edit.disabled = false;
            }
        });
        indexing.append(retry);
    }
    article.append(indexing, node('p', review.disclaimer));
    const edit = node('button', '编辑原文 / 纠错');
    edit.type = 'button';
    edit.className = 'btn btn-ghost btn-sm';
    edit.addEventListener('click', () => {
        if (article.dataset.editing || article.dataset.indexing) return;
        article.dataset.editing = 'true';
        edit.disabled = true;
        const indexButtons = Array.from(indexing.querySelectorAll('button'));
        const disabledStates = indexButtons.map((button) => button.disabled);
        indexButtons.forEach((button) => { button.disabled = true; });
        const editor = document.createElement('form');
        const label = node('label', '修改自然语言复盘，保存后重新抽取并更新检索索引');
        const text = document.createElement('textarea');
        text.id = `review-edit-${review.id}`;
        label.htmlFor = text.id;
        text.value = review.original_text;
        text.rows = 5;
        text.maxLength = 10000;
        text.required = true;
        let revision = review.revision || 1;
        let editId = requestId();
        text.addEventListener('input', () => { editId = requestId(); });
        const feedback = node('p', `正在编辑版本 ${revision}，输入内容在保存失败时保留。`);
        feedback.setAttribute('role', 'status');
        const submit = node('button', '保存纠错并重新抽取');
        submit.className = 'btn btn-primary btn-sm';
        const cancel = node('button', '取消编辑');
        cancel.type = 'button';
        cancel.className = 'btn btn-ghost btn-sm';
        cancel.addEventListener('click', () => {
            editor.remove();
            delete article.dataset.editing;
            edit.disabled = false;
            indexButtons.forEach((button, position) => { button.disabled = disabledStates[position]; });
            edit.focus();
        });
        const latest = node('button', '查看已保存版本');
        latest.type = 'button';
        latest.className = 'btn btn-ghost btn-sm';
        const comparison = document.createElement('div');
        latest.addEventListener('click', async () => {
            latest.disabled = true;
            try {
                const current = await fetchReview(review.id);
                comparison.replaceChildren(node('p', `已保存版本 ${current.revision}：${current.original_text}`));
                if (current.revision !== revision) {
                    const adopt = node('button', '已核对，以此版本继续保存我的修改');
                    adopt.type = 'button';
                    adopt.className = 'btn btn-ghost btn-sm';
                    adopt.addEventListener('click', () => {
                        revision = current.revision;
                        editId = requestId();
                        feedback.textContent = `将基于版本 ${revision} 保存；你的输入保持不变。`;
                        adopt.remove();
                    });
                    comparison.append(adopt);
                }
            } catch (error) { feedback.textContent = error.message; }
            finally { latest.disabled = false; }
        });
        editor.addEventListener('submit', async (event) => {
            event.preventDefault();
            if (submit.disabled || !text.value.trim()) return;
            submit.disabled = cancel.disabled = latest.disabled = text.disabled = true;
            editor.setAttribute('aria-busy', 'true');
            feedback.textContent = '正在保存新原文、重新抽取和更新索引…';
            try {
                const updated = await editReview(review.id, text.value, revision, editId);
                const replacement = renderReview(updated);
                article.replaceWith(replacement);
                status.textContent = updated.warning || `纠错已保存为版本 ${updated.revision}；请核对抽取与索引状态。`;
                replacement.querySelector('button')?.focus();
            } catch (error) { feedback.textContent = error.message; }
            finally {
                submit.disabled = cancel.disabled = latest.disabled = text.disabled = false;
                editor.removeAttribute('aria-busy');
            }
        });
        editor.append(label, text, submit, cancel, latest, feedback, comparison);
        article.append(editor);
        text.focus();
    });
    article.append(edit);
    return article;
}

async function loadReviews() {
    if (list.querySelector('[data-editing]')) {
        status.textContent = '正在编辑的内容已保留，请使用该记录的“查看已保存版本”，或结束编辑后刷新列表。';
        return;
    }
    refresh.disabled = true;
    try {
        const response = await fetchReviews();
        if (list.querySelector('[data-editing]')) {
            status.textContent = '刷新结果已到达，正在编辑的内容已保留；结束编辑后可再次刷新。';
            return;
        }
        list.replaceChildren(...response.items.map(renderReview));
        if (!response.items.length) list.append(node('p', '还没有复盘记录。'));
        loaded = true;
    } finally {
        refresh.disabled = false;
    }
}

input.addEventListener('input', () => { draftId = null; });
form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (save.disabled || !input.value.trim()) return;
    if (list.querySelector('[data-editing]')) {
        status.textContent = '请先保存或取消正在编辑的复盘，再提交新记录；两处输入均已保留。';
        return;
    }
    draftId ||= requestId();
    save.disabled = true;
    input.disabled = true;
    status.textContent = '正在保存并抽取复盘…';
    try {
        const review = await saveReview(input.value, draftId);
        input.value = '';
        draftId = null;
        status.textContent = review.warning || (review.extraction_status === 'succeeded'
            ? '复盘已保存，抽取结果请与原文核对。'
            : '复盘文本已保存，抽取未完成，可按原文核对。');
        list.replaceChildren(renderReview(review));
        loaded = true;
    } catch (error) {
        status.textContent = error.message || '保存失败，请保留输入后重试。';
    } finally {
        save.disabled = false;
        input.disabled = false;
    }
});

refresh.addEventListener('click', () => {
    loadReviews().catch(() => { status.textContent = '暂时无法读取复盘，请稍后重试。'; });
});
document.getElementById('reviewPanel').addEventListener('toggle', (event) => {
    if (event.target.open && !loaded) {
        loadReviews().catch(() => { status.textContent = '暂时无法读取复盘，请稍后重试。'; });
    }
});
