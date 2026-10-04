// 所有操作先编辑草稿；只有确认保存按钮发送写入请求。
let _aliasDraft = null;
let _aliasBusy = false;
let _aliasRequest = 0;

async function aliasRequest(url, body) {
    const response = await fetch(url, body === undefined ? {cache: 'no-store'} : {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) {
        const error = new Error(result.error || '请求失败');
        error.result = result;
        throw error;
    }
    return result;
}

function showAliasSources(sources, message = '') {
    const container = document.getElementById('aliasSources');
    container.replaceChildren();
    if (message) {
        const notice = document.createElement('p');
        notice.textContent = message;
        container.append(notice);
    }
    for (const source of sources || []) {
        const row = document.createElement('div');
        row.textContent = `${source.source}：${source.names?.join('、') || '无演员信息'}${source.error ? ' — ' + source.error : ''}`;
        container.append(row);
    }
}

async function queryActressAliases() {
    if (_aliasBusy) return;
    const dvdid = document.getElementById('aliasDvdid').value.trim();
    if (!dvdid) return showToast('请填写番号', 'error');
    if (_aliasDraft && !confirm('当前未保存草稿将被替换，继续查询？')) return;
    cancelActressAliasDraft();
    const requestId = ++_aliasRequest;
    const button = document.getElementById('aliasQueryBtn');
    button.disabled = true;
    showAliasSources([], '查询中，最多等待 180 秒…');
    try {
        const data = await aliasRequest('/api/actress-aliases/query', {dvdid});
        if (requestId !== _aliasRequest) return;
        showAliasSources(data.sources, '查询完成，请核对并排除错误名字。保存前不会修改词库。');
        startAliasDraft(data.name, data.existing_aliases, data.original_name, data.revision, 'append', data.candidates);
    } catch (error) {
        if (requestId !== _aliasRequest) return;
        showAliasSources(error.result?.sources, error.message);
        showToast(error.message, 'error');
    } finally {
        button.disabled = false;
    }
}

let _aliasSearchRequest = 0;
async function searchActressAliases() {
    const q = document.getElementById('aliasSearch').value.trim();
    const container = document.getElementById('aliasSearchResults');
    const requestId = ++_aliasSearchRequest;
    container.replaceChildren();
    if (!q) return;
    try {
        const data = await aliasRequest('/api/actress-aliases?q=' + encodeURIComponent(q));
        if (requestId !== _aliasSearchRequest || document.getElementById('aliasSearch').value.trim() !== q) return;
        if (!data.records.length || data.total > data.records.length) {
            const notice = document.createElement('p');
            notice.className = 'task-muted';
            notice.textContent = !data.records.length ? '未找到匹配记录，可点击“新增女优”手动录入。' : '匹配较多，请输入更完整的名字缩小范围。';
            container.append(notice);
        }
        for (const record of data.records) {
            const button = document.createElement('button');
            button.className = 'alias-record';
            button.textContent = `${record.name} · ${record.aliases.join('、')}`;
            button.onclick = () => editActressAlias(record.name);
            container.append(button);
        }
    } catch (error) { showToast(error.message, 'error'); }
}

async function editActressAlias(name) {
    if (_aliasBusy || (_aliasDraft && !confirm('放弃当前未保存草稿，编辑这条记录？'))) return;
    const requestId = ++_aliasRequest;
    try {
        const data = await aliasRequest('/api/actress-aliases?name=' + encodeURIComponent(name));
        if (requestId !== _aliasRequest) return;
        startAliasDraft(name, data.aliases, name, data.revision, 'edit', []);
    } catch (error) { showToast(error.message, 'error'); }
}

async function newActressAlias() {
    if (_aliasBusy || (_aliasDraft && !confirm('放弃当前未保存草稿，新增女优？'))) return;
    const requestId = ++_aliasRequest;
    try {
        const data = await aliasRequest('/api/actress-aliases?revision_only=1');
        if (requestId !== _aliasRequest) return;
        startAliasDraft('', [], null, data.revision, 'edit', []);
    } catch (error) { showToast(error.message, 'error'); }
}

function startAliasDraft(name, existing, original, revision, mode, candidates) {
    const choices = new Map();
    for (const n of existing || []) choices.set(n, {name: n, selected: true, existing: true, sources: []});
    for (const candidate of candidates) {
        const old = choices.get(candidate.name);
        choices.set(candidate.name, {name: candidate.name, selected: true, existing: !!old, sources: candidate.sources});
    }
    if (name && !choices.has(name)) choices.set(name, {name, selected: true, existing: !!original, sources: []});
    _aliasDraft = {original_name: original, revision, mode, existing: [...(existing || [])], choices};
    document.getElementById('aliasFixedName').value = name;
    document.getElementById('aliasNewNames').value = '';
    document.getElementById('aliasEditor').disabled = false;
    document.getElementById('aliasEditorTitle').textContent = mode === 'append' ? '番号查询草稿' : '手动维护草稿';
    document.getElementById('aliasEditorHint').textContent = mode === 'append'
        ? '取消勾选仅排除本次候选，已有别名会保留。点击名字可设为统一名字。'
        : '取消勾选已有别名表示删除；修改统一名字后，原统一名字会保留为别名。';
    renderAliasChoices();
}

function renderAliasChoices() {
    const container = document.getElementById('aliasNameChoices');
    container.replaceChildren();
    for (const choice of _aliasDraft.choices.values()) {
        const row = document.createElement('div');
        row.className = 'alias-choice';
        const label = document.createElement('label');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.checked = choice.selected;
        // 查询模式下已有记录只读，避免把排除候选误认为删除已有别名。
        checkbox.disabled = _aliasDraft.mode === 'append' && choice.existing;
        checkbox.onchange = () => { choice.selected = checkbox.checked; renderAliasPreview(); };
        label.append(checkbox, document.createTextNode(choice.name));
        const sources = document.createElement('small');
        sources.textContent = [choice.existing ? '已有别名' : '', ...choice.sources].filter(Boolean).join(' · ');
        const choose = document.createElement('button');
        choose.textContent = '设为统一名字';
        choose.onclick = () => {
            document.getElementById('aliasFixedName').value = choice.name;
            choice.selected = true;
            checkbox.checked = true;
            renderAliasPreview();
        };
        row.append(label, sources, choose);
        container.append(row);
    }
    renderAliasPreview();
}

function addActressAliasNames() {
    if (!_aliasDraft || _aliasBusy) return;
    const input = document.getElementById('aliasNewNames');
    for (const value of input.value.split(/[\n,，]/)) {
        const name = value.trim();
        if (!name) continue;
        const existing = _aliasDraft.choices.get(name);
        if (existing) existing.selected = true;
        else _aliasDraft.choices.set(name, {name, selected: true, existing: false, sources: []});
    }
    input.value = '';
    renderAliasChoices();
}

function aliasDraftValues() {
    const name = document.getElementById('aliasFixedName').value.trim();
    const aliases = [..._aliasDraft.choices.values()].filter(c => c.selected).map(c => c.name);
    const final = new Set([name, ...aliases]);
    if (_aliasDraft.mode === 'append') _aliasDraft.existing.forEach(n => final.add(n));
    if (_aliasDraft.original_name && name !== _aliasDraft.original_name) final.add(_aliasDraft.original_name);
    return {name, aliases, final: [...final].filter(Boolean)};
}

function renderAliasPreview() {
    if (!_aliasDraft) return;
    const {name, final} = aliasDraftValues();
    const previous = new Set([_aliasDraft.original_name, ..._aliasDraft.existing].filter(Boolean));
    const added = final.filter(n => !previous.has(n));
    const removed = [...previous].filter(n => !final.includes(n));
    const lines = [!_aliasDraft.original_name ? `新增女优：${name || '请填写统一名字'}` :
        `统一名字：${_aliasDraft.original_name}${name !== _aliasDraft.original_name ? ' → ' + (name || '请填写') : '（保留）'}`,
        `新增名字：${added.join('、') || '无'}`, `删除别名：${removed.join('、') || '无'}`,
        `最终名字：${final.join('、') || '无'}`, '点击“确认保存”后才会修改词库。'];
    document.getElementById('aliasPreview').textContent = lines.join('\n');
    document.getElementById('aliasSaveBtn').disabled = !name || _aliasBusy;
}

async function saveActressAliases() {
    if (!_aliasDraft || _aliasBusy) return;
    if (document.getElementById('aliasNewNames').value.trim()) return showToast('补充名字尚未加入草稿，请先点击“添加到草稿”', 'error');
    const {name, aliases} = aliasDraftValues();
    if (!name) return;
    const body = {name, aliases, mode: _aliasDraft.mode, original_name: _aliasDraft.original_name,
        revision: _aliasDraft.revision, confirmed: true};
    ++_aliasRequest;
    _aliasBusy = true;
    document.getElementById('aliasEditor').disabled = true;
    try {
        const data = await aliasRequest('/api/actress-aliases', body);
        _aliasBusy = false;
        cancelActressAliasDraft();
        showToast(data.message);
        searchActressAliases();
    } catch (error) {
        document.getElementById('aliasEditor').disabled = false;
        showToast(error.message, 'error');
    } finally { _aliasBusy = false; renderAliasPreview(); }
}

function cancelActressAliasDraft() {
    if (_aliasBusy) return;
    ++_aliasRequest;
    _aliasDraft = null;
    document.getElementById('aliasEditor').disabled = true;
    document.getElementById('aliasFixedName').value = '';
    document.getElementById('aliasNameChoices').replaceChildren();
    document.getElementById('aliasPreview').textContent = '';
    document.getElementById('aliasEditorHint').textContent = '查询或选择已有记录开始编辑。勾选操作不会修改文件。';
}
