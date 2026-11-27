import {fetchWatchlist, addWatch, deleteWatch, fetchLatestSummary, fetchSummaryHistory, generateSummary} from './api.js';

const byId = (id) => document.getElementById(id);
const states = {pending: '待生成', processing: '生成中', completed: '已完成', failed: '失败，可重试'};
let pollTimer;
let selectedDate = null;
let loading = false;

function node(tag, text) {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
}

function renderWatch(items) {
    const list = byId('watchList');
    list.replaceChildren();
    if (!items.length) list.append(node('li', '尚未配置关注股票（最多 20 只）。'));
    for (const item of items) {
        const row = node('li', `${item.name}（${item.code}） `);
        const remove = node('button', '删除');
        remove.type = 'button';
        remove.className = 'btn btn-ghost btn-sm';
        remove.setAttribute('aria-label', `删除关注 ${item.name}`);
        remove.addEventListener('click', async () => {
            remove.disabled = true;
            try {
                renderWatch((await deleteWatch(item.code)).items);
                byId('watchStatus').textContent = '已删除；已有摘要保留生成时的关注信息。';
            } catch (error) {
                byId('watchStatus').textContent = error.message;
            } finally { remove.disabled = false; }
        });
        row.append(remove);
        list.append(row);
    }
}

function showRecord(record, today) {
    const content = byId('summaryContent');
    content.replaceChildren();
    if (!record) {
        content.append(node('p', '尚无成功摘要。添加关注股票、导入个人策略后，可在收盘后生成。'));
        return;
    }
    content.append(node('p', `${record.date} · ${states[record.status] || record.status} · 第 ${record.attempts} 次尝试`));
    if (record.error) content.append(node('p', record.error));
    if (record.result) {
        const label = record.date < today ? '历史摘要，不能代表今日行情。' : '该日期最近成功摘要。';
        content.append(node('p', `${label}生成于 ${new Date(record.generated_at).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai'})}（Asia/Shanghai）。`));
        if (record.status !== 'completed') content.append(node('p', '本次尚未成功，以下保留上次成功内容。'));
        content.append(node('div', record.result.answer));
        for (const source of record.result.strategies || []) {
            content.append(node('p', `引用：个人策略 · ${source.title} · document_id=${source.document_id} · chunk_id=${source.chunk_id}`));
        }
    }
}

async function refresh() {
    if (loading) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(refresh, 2500);
        return;
    }
    loading = true;
    clearTimeout(pollTimer);
    try {
        const [latest, history, watches] = await Promise.all([fetchLatestSummary(), fetchSummaryHistory(), fetchWatchlist()]);
        if (!byId('summaryDate').value) byId('summaryDate').value = latest.today;
        byId('summaryDate').max = latest.today;
        renderWatch(watches.items);
        const current = latest.current;
        byId('summaryStatus').textContent = current
            ? `今日：${states[current.status]}。${current.error || ''}`
            : '今日尚无摘要；盘中、休市或行情延迟时不能生成当日正式摘要。';
        const selected = selectedDate && history.items.find((item) => item.date === selectedDate);
        showRecord(selected || latest.latest_success || current, latest.today);
        const list = byId('summaryHistory');
        list.replaceChildren();
        if (!history.items.length) list.append(node('li', '暂无历史记录。'));
        for (const record of history.items) {
            const row = document.createElement('li');
            const button = node('button', `${record.date} · ${states[record.status]} · 第 ${record.attempts} 次尝试`);
            button.type = 'button';
            button.className = 'btn btn-ghost btn-sm';
            button.addEventListener('click', () => {
                selectedDate = record.date;
                byId('summaryDate').value = record.date;
                showRecord(record, latest.today);
            });
            row.append(button);
            list.append(row);
        }
        if (history.items.some((item) => item.status === 'processing')) pollTimer = setTimeout(refresh, 2500);
    } catch (error) {
        byId('summaryStatus').textContent = `读取失败：${error.message}，可点击刷新重试。`;
    } finally { loading = false; }
}

async function generate(retry) {
    const day = byId('summaryDate').value;
    if (!day) { byId('summaryStatus').textContent = '请选择收盘日期。'; return; }
    byId('summaryGenerate').disabled = byId('summaryRetry').disabled = true;
    try {
        const response = await generateSummary(day, retry);
        selectedDate = day;
        byId('summaryStatus').textContent = response.started ? '生成已开始，可刷新查看。' : '该日已生成或正在处理。';
        await refresh();
    } catch (error) {
        byId('summaryStatus').textContent = error.message;
    } finally { byId('summaryGenerate').disabled = byId('summaryRetry').disabled = false; }
}

byId('watchForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = byId('watchQuery');
    byId('watchAdd').disabled = true;
    try {
        renderWatch((await addWatch(input.value)).items);
        input.value = '';
        byId('watchStatus').textContent = '已关注；新的摘要将使用更新后的列表。';
    } catch (error) { byId('watchStatus').textContent = error.message; }
    finally { byId('watchAdd').disabled = false; }
});
byId('summaryGenerate').addEventListener('click', () => generate(false));
byId('summaryRetry').addEventListener('click', () => generate(true));
byId('summaryRefresh').addEventListener('click', refresh);
refresh();
