import {fetchInvestorSettings, fetchStrategyCatalog, saveInvestorSettings, createRequestId} from './api.js';

const byId = (id) => document.getElementById(id);
let saved = null;
let catalog = [];
let loaded = false;
let dirty = false;

function option(value, text) {
    const element = document.createElement('option');
    element.value = value;
    element.textContent = text;
    return element;
}

function renderModes() {
    byId('activeStrategyMode').replaceChildren(option('', '全部个人策略'), ...saved.modes.map((mode) => option(mode.id, mode.name)));
    byId('activeStrategyMode').value = saved.active_mode_id || '';
    byId('editingMode').replaceChildren(option('', '新建模式'), ...saved.modes.map((mode) => option(mode.id, mode.name)));
    renderEditor();
}

function renderEditor() {
    const mode = saved.modes.find((item) => item.id === byId('editingMode').value);
    byId('modeName').value = mode?.name || '';
    byId('modeDelete').disabled = !mode;
    const list = byId('modeDocuments');
    list.replaceChildren();
    const documents = [...catalog];
    for (const id of mode?.document_ids || []) {
        if (!documents.some((item) => item.id === id)) documents.push({id, title: '原绑定文档暂不可用，请取消勾选或检查导入状态'});
    }
    for (const documentInfo of documents) {
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = documentInfo.id;
        checkbox.checked = mode?.document_ids.includes(documentInfo.id) || false;
        label.append(checkbox, document.createTextNode(documentInfo.title));
        list.append(label);
    }
    if (!documents.length) list.textContent = '暂无已完成导入的个人策略。可先去“文档导入”添加，再重新加载配置。';
}

function profileFromForm() {
    const sectors = byId('preferredSectors').value.split(/[,，、\n]/).map((item) => item.trim()).filter(Boolean);
    if (sectors.length > 10 || sectors.some((item) => item.length > 40)) throw new Error('板块最多 10 个，每个名称最多 40 字。');
    return {risk_preference: byId('riskPreference').value, horizon: byId('investmentHorizon').value,
        preferred_sectors: sectors, notes: byId('profileNotes').value};
}

function renderProfile() {
    byId('riskPreference').value = saved.profile.risk_preference;
    byId('investmentHorizon').value = saved.profile.horizon;
    byId('preferredSectors').value = saved.profile.preferred_sectors.join('、');
    byId('profileNotes').value = saved.profile.notes;
}

async function load() {
    byId('settingsReload').disabled = true;
    byId('settingsFields').disabled = true;
    byId('settingsStatus').textContent = '正在加载档案和策略文档…';
    try {
        const [settings, documents] = await Promise.all([fetchInvestorSettings(), fetchStrategyCatalog()]);
        if (dirty) {
            const region = byId('settingsStatus');
            region.textContent = '当前有未保存的输入，已保留。选择“替换表单”后才会重新载入已保存配置。';
            const replace = document.createElement('button');
            replace.type = 'button';
            replace.className = 'btn btn-ghost btn-sm';
            replace.textContent = '放弃本页修改，用已保存配置替换表单';
            replace.addEventListener('click', () => {
                saved = settings;
                catalog = documents.items;
                dirty = false;
                renderProfile();
                renderModes();
                region.textContent = '已重新加载保存的配置。';
            });
            region.append(replace);
            return;
        }
        saved = settings;
        catalog = documents.items;
        renderProfile();
        renderModes();
        loaded = true;
        byId('settingsStatus').textContent = documents.truncated ? '已加载配置；文档列表只显示前 500 条。' : '配置已加载。修改后请保存。';
    } catch (error) { byId('settingsStatus').textContent = error.message; }
    finally {
        byId('settingsReload').disabled = false;
        byId('settingsFields').disabled = !saved;
    }
}

async function persist(modes, activeModeId) {
    if (!saved || byId('settingsFields').disabled) return;
    try {
        const payload = {revision: saved.revision, profile: profileFromForm(), modes, active_mode_id: activeModeId || null};
        byId('settingsFields').disabled = true;
        byId('settingsReload').disabled = true;
        byId('settingsForm').setAttribute('aria-busy', 'true');
        byId('settingsStatus').textContent = '正在保存配置…';
        saved = await saveInvestorSettings(payload);
        dirty = false;
        renderProfile();
        renderModes();
        byId('settingsStatus').textContent = '配置已保存。后续问答和新生成摘要将使用此设置；已有摘要需要重新生成才会改变。';
    } catch (error) { byId('settingsStatus').textContent = `${error.message} 当前输入已保留。`; }
    finally {
        byId('settingsFields').disabled = false;
        byId('settingsReload').disabled = false;
        byId('settingsForm').removeAttribute('aria-busy');
    }
}

byId('settingsForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (saved) persist(saved.modes, byId('activeStrategyMode').value);
});
byId('editingMode').addEventListener('change', renderEditor);
byId('modeSave').addEventListener('click', () => {
    const name = byId('modeName').value.trim();
    if (!name) {
        byId('settingsStatus').textContent = '请填写策略模式名称。';
        byId('modeName').focus();
        return;
    }
    const id = byId('editingMode').value || createRequestId();
    const documentIds = Array.from(byId('modeDocuments').querySelectorAll('input:checked'), (input) => input.value);
    const modes = saved.modes.filter((mode) => mode.id !== id);
    modes.push({id, name, document_ids: documentIds});
    persist(modes, id);
});
byId('modeDelete').addEventListener('click', () => {
    const id = byId('editingMode').value;
    if (!id) return;
    const activeId = byId('activeStrategyMode').value;
    persist(saved.modes.filter((mode) => mode.id !== id), activeId === id ? null : activeId);
});
byId('settingsReload').addEventListener('click', load);
byId('settingsForm').addEventListener('input', () => { dirty = true; });
byId('settingsForm').addEventListener('change', () => { dirty = true; });
byId('settingsPanel').addEventListener('toggle', (event) => {
    if (event.target.open && !loaded && !byId('settingsReload').disabled) load();
});
