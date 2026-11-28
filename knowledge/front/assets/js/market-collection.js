import {collectMarket, fetchMarketCollection} from './api.js';
import {errorMessage} from './ui.js';

const panel = document.getElementById('marketCollectionPanel');
const date = document.getElementById('marketCollectionDate');
const start = document.getElementById('marketCollectionStart');
const refresh = document.getElementById('marketCollectionRefresh');
const status = document.getElementById('marketCollectionStatus');
const error = document.getElementById('marketCollectionError');
const missing = document.getElementById('marketCollectionMissing');
let timer;
let revision = 0;

function render(record) {
    missing.replaceChildren();
    if (!record) {
        status.textContent = '此日期尚无采集记录。';
        return;
    }
    const labels = {running: '采集中', completed: '采集完成', partial: '部分完成', failed: '未完成'};
    status.textContent = record.status === 'running' && !record.total
        ? `${record.date}：正在核对全市场证券名单，完成后开始保存行情。`
        : `${record.date}：${labels[record.status] || record.status}。名单 ${record.total || 0} 只，已保存 ${record.succeeded || 0} 只，缺失 ${record.failed || 0} 只，待处理 ${record.pending || 0} 只。${record.warning || ''}`;
    if (record.excluded_codes?.length) status.textContent += ` 来源另含 ${record.excluded_codes.length} 个未识别代码，未计入股票行情。`;
    if (record.incomplete_fields) status.textContent += ` ${record.incomplete_fields} 只已保存行情缺少估值或其他可选字段。`;
    error.textContent = record.error || '';
    for (const item of (record.failures || []).slice(0, 20)) {
        const row = document.createElement('li');
        row.textContent = `${item.code}：${item.reason}`;
        missing.append(row);
    }
    if ((record.failures || []).length > 20) {
        const row = document.createElement('li');
        row.textContent = `另有 ${record.failures.length - 20} 只未列出；再次采集会补齐缺失部分。`;
        missing.append(row);
    }
    if (record.status === 'running' && panel.open) timer = setTimeout(() => load(), 3000);
}

async function load(latest = false) {
    clearTimeout(timer);
    const current = ++revision;
    error.textContent = '';
    try {
        const result = await fetchMarketCollection(latest ? '' : date.value);
        if (current !== revision) return;
        if (latest && result.record) date.value = result.record.date;
        render(result.record);
    } catch (failure) {
        if (current === revision) error.textContent = `${errorMessage(failure)}，可点击查看进度重试。`;
    }
}

start.addEventListener('click', async () => {
    if (!date.reportValidity()) return;
    clearTimeout(timer);
    const current = ++revision;
    start.disabled = true;
    error.textContent = '';
    try {
        const result = await collectMarket(date.value);
        if (current === revision) render(result.record);
    } catch (failure) {
        if (current === revision) error.textContent = `${errorMessage(failure)}，可查看进度后重试。`;
    } finally {
        start.disabled = false;
    }
});
refresh.addEventListener('click', () => date.reportValidity() && load());
date.addEventListener('change', () => load());
panel.addEventListener('toggle', () => {
    if (panel.open) load(!date.value);
    else clearTimeout(timer);
});
