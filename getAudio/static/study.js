// ========== 项目工作台：从来源生成（报告 / 闪卡 / 自测题 / 对照检查）+ 存下来的回答 ==========
// 后端在 study.py；从左栏勾选的来源生成，每条都带出处芯片，点开在左栏看原文（跟问答同一套）。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx / cxShowTab / citedHtml / sourcesHtml /
// wireCitations / citeLabel）、projects.js（srcState / srcScopeBody / srcCountText / openSourceReader /
// passageIndexOf / nbShowPane）、tools.js（toolOverlay / exportMenuHtml）。

// 跟工作台格子同一套线条图标（index.html 里那几个格子的 path 一样）
const ST_PATHS = {
    report: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h4"/>',
    flashcards: '<rect x="3" y="8" width="13" height="12" rx="2"/><path d="M8 4h11a2 2 0 0 1 2 2v10"/>',
    quiz: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.4 2.4c-.6.3-1 .8-1 1.5v.4"/><path d="M12 16.8h.01"/>',
    coverage: '<path d="M4 6.5l1.6 1.6L8.5 5"/><path d="M4 13.5l1.6 1.6 2.9-3.1"/><path d="M11.5 7h8.5M11.5 14h8.5M11.5 20h8.5"/><path d="M5.5 20h.01"/>',
    note: '<path d="M7 3.5h10a1 1 0 0 1 1 1V21l-6-4-6 4V4.5a1 1 0 0 1 1-1z"/>',
    read: '<path d="M3 5h6a3 3 0 0 1 3 3v12a2.5 2.5 0 0 0-2.5-2.5H3z"/><path d="M21 5h-6a3 3 0 0 0-3 3v12a2.5 2.5 0 0 1 2.5-2.5H21z"/>',
    topics: '<path d="M12 4v16"/><path d="M7 20h10"/><path d="M4 7h16"/><path d="M7 7l-3 6.5a3 3 0 0 0 6 0z"/><path d="M17 7l-3 6.5a3 3 0 0 0 6 0z"/>',
    predictions: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r=".8"/>',
    cards: '<path d="M5 6h14a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-8l-4 3v-3H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z"/><path d="M9 10.5h.01M12 10.5h.01M15 10.5h.01"/>',
    visual: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M21 16l-5-5-8.5 8.5"/>',
    podcast: '<path d="M4 15v-3a8 8 0 0 1 16 0v3"/><rect x="3" y="14" width="4.5" height="6.5" rx="1.6"/><rect x="16.5" y="14" width="4.5" height="6.5" rx="1.6"/>',
    slides: '<rect x="3" y="4" width="18" height="12" rx="1.5"/><path d="M12 16v4M8 20h8"/><path d="M7.5 8.5h6M7.5 11.5h9"/>',
    compare: '<circle cx="8" cy="8" r="3.2"/><circle cx="16" cy="8" r="3.2"/><path d="M2.5 19.5c.6-3 2.8-5 5.5-5s4.9 2 5.5 5"/><path d="M13.6 15.2c.7-.5 1.5-.7 2.4-.7 2.7 0 4.9 2 5.5 5"/>',
};
function stIcon(kind, size = 18) {
    return `<span class="nb-ic" data-t="${kind}"><svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor"
        stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ST_PATHS[kind] || ST_PATHS.note}</svg></span>`;
}
const ST_FORMATS = ['briefing', 'guide', 'faq', 'timeline', 'custom'];
let stState = { id: null, items: [], docs: [], lensBusy: new Set(), cur: null, timer: null, kind: null, fc: null, quiz: {}, filter: 'all' };

// 闪卡、自测题、对照是一段段分开画的：出处编号按整份产出统一排，别每段都从 1 开始
function stOrder() { return Object.keys((stState.cur && stState.cur.citations) || {}); }

function stTitle(o) {
    if (o.kind === 'doc') {
        const col = o.chain && o.chain !== cx.id ? !!o.col : cx.chain && ['collection', 'project'].includes(cx.chain.kind);
        return (o.lens ? T('lens.' + o.lens) : T(col ? 'rd.overview' : 'rd.portrait')) + (o.person ? ' · ' + o.person : '');
    }
    if (o.kind === 'note') return o.title || T('st.k.note');
    if (o.kind === 'view') {             // 谁的立场 / 预测 / 原话
        const who = (cx.people || []).length > 1 ? personName(cxPerson(o.chain)) || o.person : '';
        return T(o.view === 'cards' ? 'cx.tab.cards' : 'cx.tab.' + o.view) + (who ? ' · ' + who : '');
    }
    if (o.kind === 'compare') return T('st.k.compare') + ' · ' + String(o.question || '').slice(0, 40);
    if (o.kind === 'visual') return T('st.k.visual') + ' · ' + T('vs.nEps', { n: (o.tasks || []).length });
    if (o.kind === 'podcast') return podcastTitle(o);
    if (o.kind === 'slides') return slidesTitle(o);
    if (o.kind === 'report') {
        const name = o.format === 'custom' ? String(o.prompt || T('st.f.custom')).slice(0, 40) : T('st.f.' + (o.format || 'briefing'));
        return name + (o.focus ? ' · ' + o.focus : '');
    }
    return T('st.k.' + o.kind) + (o.focus ? ' · ' + o.focus : '');
}

// ----- 打开项目：复位，拉列表 -----
function stOpen(id) {
    clearTimeout(stState.timer);
    stState = { id, items: [], docs: [], lensBusy: new Set(), cur: null, timer: null, kind: null, fc: null, quiz: {}, filter: 'all' };
    const panel = document.getElementById('cx-studio');
    if (panel) panel.innerHTML = '';
    document.querySelectorAll('#chain-explore .st-tool').forEach(b => b.classList.toggle('hidden', !!window.VERBATIM_DEMO));
    stRenderList();
    stLoad();
}

async function stLoad() {
    const id = stState.id;
    if (!id) return;
    let d;
    try {
        const r = await fetch(`/api/chain/${id}/studio`);
        if (!r.ok) return;
        d = await r.json();
    } catch { return; }
    if (id !== stState.id) return;
    // 画像和镜头也是「生成出来的东西」：跟报告等排在一起（只属于这个频道 / 这些录音，不吃文档）。
    // 项目里有好几个博主：每人的都列出来，标题后面写是谁的（文件在各自的链条里）
    const ppl = (cx.people || []).length ? cx.people : [{ chain_id: id, self: true }];
    const multi = ppl.length > 1;
    const lists = await Promise.all(ppl.map(p => fetch(`/api/chain/${p.chain_id}/files?detail=1`)
        .then(r => r.json()).catch(() => [])));
    if (id !== stState.id) return;
    const ch = cx.chain || {};
    stState.docs = ppl.flatMap((p, i) => {
        const fd = p.chain_id === id ? (ch.final_doc || p.final_doc) : p.final_doc;
        return (Array.isArray(lists[i]) ? lists[i] : []).filter(f => f && (f.name === fd || /^镜头_\w+\.md$/.test(f.name)))
            .map(f => ({ id: `doc:${p.chain_id}:${f.name}`, kind: 'doc', file: f.name, created_at: f.mtime, status: 'done',
                chain: p.chain_id, person: multi ? personName(p) : '', col: !p.url && p.chain_id === id && !!p.kind && p.kind !== 'creator',
                lens: (f.name.match(/^镜头_(\w+)\.md$/) || [])[1] || '' }));
    });
    const wasRunning = new Set(stState.items.filter(o => o.status === 'running').map(o => o.id));
    stState.items = d.items || [];
    stRenderList();
    // 刚生成完的：如果中间正开着它，换成成品
    const cur = stState.cur;
    if (cur && wasRunning.has(cur.id) && stState.items.some(o => o.id === cur.id && o.status !== 'running')) stView(cur.id);
    clearTimeout(stState.timer);
    if (stState.items.some(o => o.status === 'running')) stState.timer = setTimeout(stLoad, 2500);
}

function stMeta(o) {
    const when = String(o.created_at || '').slice(5, 16);
    if (o.kind === 'doc' && o.lens && stState.lensBusy.has(o.chain + ':' + o.lens)) {
        const again = stState.docs.some(d => d.lens === o.lens && d.chain === o.chain);   // 已有一版 = 重新生成；没有 = 第一次生成
        return `<span class="st-spin" aria-hidden="true"></span>${T(again ? 'rd.regenerating' : 'rd.generating')}`;
    }
    if (o.status === 'running' && o.kind === 'podcast') return `<span class="st-spin" aria-hidden="true"></span>${escapeHtml(podcastMeta(o))}`;
    if (o.status === 'running' && o.progress && o.progress.total > 1) {     // 画面卡一期一期做：写做到第几期
        return `<span class="st-spin" aria-hidden="true"></span>${escapeHtml(T('vs.progress', { i: Math.min(o.progress.done + 1, o.progress.total), n: o.progress.total }))}`;
    }
    if (o.status === 'running') return `<span class="st-spin" aria-hidden="true"></span>${T('st.generating')}`;
    if (o.status === 'failed') return `<span class="cx-err">${escapeHtml(T('st.failed'))}</span>`;
    const parts = [when];
    if (o.kind === 'flashcards' || o.kind === 'quiz') parts.push(T('st.nItems.' + o.kind, { n: o.n || '' }));
    if (o.kind === 'visual') parts.push(T('vs.nCards', { n: o.n || 0 }));
    if (o.kind === 'podcast' && o.duration) parts.push(podClock(o.duration));
    if (o.kind === 'slides' && o.n_slides) parts.push(T('sl.nPages', { n: o.n_slides }));
    if (o.cost_usd) parts.push(fmtUsd(o.cost_usd));
    return escapeHtml(parts.join(' · '));
}

function stRenderList() {
    const box = document.getElementById('st-list');
    if (!box) return;
    // 生成的报告 / 闪卡等 + 画像 / 镜头，按时间新的在前
    // 第一次生成的镜头还没有文件：先占一行，转着圈
    const now = new Date().toISOString().replace('T', ' ').slice(0, 16);
    const multi = (cx.people || []).length > 1;
    const pending = [...stState.lensBusy].map(k => k.split(':')).filter(([c, l]) => !stState.docs.some(d => d.lens === l && d.chain === c))
        .map(([c, l]) => ({ id: `doc:${c}:镜头_${l}.md`, kind: 'doc', file: '镜头_' + l + '.md', lens: l, chain: c, created_at: now,
            status: 'running', person: multi ? personName(cxPerson(c)) : '' }));
    const all = [...stState.items, ...stState.docs, ...pending]
        .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    if (!all.length) {                   // 跟 Gemini 一样：空着时说清楚这里会放什么
        box.innerHTML = window.VERBATIM_DEMO ? '' : `<div class="st-empty"><svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 19L15 9"/><path d="M14 6l1-3 1 3 3 1-3 1-1 3-1-3-3-1z"/><path d="M19 13l.6 1.4L21 15l-1.4.6L19 17l-.6-1.4L17 15l1.4-.6z"/></svg>
            <b>${T('st.emptyT')}</b><span>${T('st.emptyD')}</span></div>`;
        return;
    }
    const regenSvg = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>';
    box.innerHTML = all.map(o => `
        <div class="st-item${stOpenId && stOpenId() === o.id ? ' on' : ''}" data-id="${escapeHtml(o.id)}" data-st="${escapeHtml(o.status)}">
            <button type="button" class="st-open" ${o.status === 'running' ? 'disabled' : ''}>
                ${stIcon(o.kind === 'doc' ? 'read' : o.kind === 'view' ? o.view : o.kind, 16)}
                <span class="st-txt"><span class="st-it">${escapeHtml(stTitle(o))}</span><span class="st-im">${stMeta(o)}</span></span>
            </button>
            ${o.status === 'failed' ? `<button type="button" class="cx-link st-retry" title="${escapeHtml(o.error || '')}">${T('st.retry')}</button>` : ''}
            ${o.kind === 'doc' ? (o.lens && !window.VERBATIM_DEMO && !stState.lensBusy.has(o.chain + ':' + o.lens)
                ? `<button type="button" class="st-del st-regen" aria-label="${escapeHtml(T('rd.regen'))}" title="${escapeHtml(T('rd.regen'))}">${regenSvg}</button>` : '')
            : o.status !== 'running' ? `<button type="button" class="st-del" aria-label="${escapeHtml(T('common.delete'))}" title="${escapeHtml(T('common.delete'))}">×</button>` : ''}
        </div>`).join('');
    box.querySelectorAll('.st-item').forEach(el => {
        const id = el.dataset.id;
        const o = all.find(x => x.id === id);
        if (o.kind === 'doc') {
            el.querySelector('.st-open').addEventListener('click', () => stOpenDoc(o.file, o.chain));
            const rg = el.querySelector('.st-regen');
            if (rg) rg.addEventListener('click', () => {
                if (!confirm(T('rd.regenConfirm', { name: stTitle(o) }))) return;
                if (o.chain) cxSetPerson(o.chain);
                readGenerate(o.lens, true);
            });
            return;
        }
        el.querySelector('.st-open').addEventListener('click', () => {
            if (o.status === 'failed') { showToast(T('st.failedWhy', { e: o.error || '?' })); return; }
            if (o.kind === 'view') stOpenView(o); else stView(id);
        });
        const retry = el.querySelector('.st-retry');
        if (retry && o.kind === 'compare') retry.addEventListener('click', () => comparePicker());
        else if (retry && o.kind === 'visual') retry.addEventListener('click', () => visualPicker());
        else if (retry && o.kind === 'podcast') retry.addEventListener('click', () => podcastPicker());
        else if (retry && o.kind === 'slides') retry.addEventListener('click', () => slidesPicker());
        else if (retry) retry.addEventListener('click', () => stGenerate({ kind: o.kind, focus: o.focus, n: o.n,
            scope: o.scope, outline: o.outline_src, format: o.format, prompt: o.prompt }, true));
        const del = el.querySelector('.st-del');
        if (del) del.addEventListener('click', async () => {
            if (!confirm(T('st.delConfirm', { t: stTitle(o) }))) return;
            await fetch(`/api/chain/${stState.id}/studio/${id}`, { method: 'DELETE' });
            if ((stState.cur && stState.cur.id === id) || stOpenId() === id) { stState.cur = null; cxShowTab('ask'); }
            stLoad();
        });
    });
}

function stOpenDoc(file, chain) {
    if (chain && chain !== cxPid()) {                         // 项目里别的博主的：先换到他
        cxSetPerson(chain);
        readState.want = file;                                // 换人后 readLoad 读完文件列表就打开这一篇
        if (cx.tab !== 'read') cxShowTab('read');
        return;
    }
    if (cx.loaded.read && cx.tab === 'read') { readOpen(file); return; }
    readState.want = file;
    if (cx.loaded.read) { cxShowTab('read'); readOpen(file); } else cxShowTab('read');
}

// 镜头在后台重新生成：列表那一行转圈，好了刷新（时间跟着变）
function stLensBusy(lens, on) {
    if (on) stState.lensBusy.add(lens); else stState.lensBusy.delete(lens);
    if (on) stRenderList(); else stLoad();
}

// ----- 生成前的设置 -----
function stOutlineCandidates() {
    const d = (typeof srcState !== 'undefined' && srcState.data) || {};
    return [...(d.docs || []).filter(x => x.status === 'ready').map(x => ({ id: x.doc_id, title: x.title || x.filename || '' })),
            ...(d.recordings || []).filter(r => r.status === 'done').map(r => ({ id: r.task_id, title: r.title || '' }))];
}

function stOpenDialog(kind) {
    if (typeof srcState === 'undefined' || !srcState.data || !srcAllIds(srcState.data).length) {
        showToast(T('st.noSources'));
        return;
    }
    const ov = toolOverlay('st-overlay');
    stState.kind = kind;
    ov.querySelector('#st-dlg-t').innerHTML = `${stIcon(kind, 20)}<span>${escapeHtml(T('st.k.' + kind))}</span>`;
    ov.querySelector('#st-dlg-d').textContent = T('st.d.' + kind);
    ov.querySelector('#st-msg').textContent = '';
    ov.querySelector('#st-focus').value = '';
    const nf = ov.querySelector('#st-n-f');
    nf.classList.toggle('hidden', !(kind === 'flashcards' || kind === 'quiz'));
    ov.querySelector('#st-n').value = kind === 'flashcards' ? '15' : '10';
    // 报告：选格式（自定义就写要求）
    ov.querySelector('#st-fmt-f').classList.toggle('hidden', kind !== 'report');
    ov.querySelector('#st-prompt').value = '';
    stState.fmt = 'briefing';
    if (kind === 'report') stRenderFormats();
    const of = ov.querySelector('#st-outline-f');
    of.classList.toggle('hidden', kind !== 'coverage');
    if (kind === 'coverage') {
        const cands = stOutlineCandidates();
        // 名字里像清单的默认选中（提纲、要求、评分标准、问题列表……）
        const guess = cands.find(x => /提纲|大纲|考纲|纲要|要求|清单|评分|标准|题目|问题|outline|syllabus|checklist|rubric|requirement|brief|question/i.test(x.title));
        ov.querySelector('#st-outline').innerHTML = cands.map(x =>
            `<option value="${escapeHtml(x.id)}"${guess && guess.id === x.id ? ' selected' : ''}>${escapeHtml(x.title)}</option>`).join('');
    }
    ov.querySelector('#st-from').textContent = T(kind === 'coverage' ? 'st.fromList' : 'st.from', { s: srcCountText() });
    ov.classList.remove('hidden');
    setTimeout(() => ov.querySelector(kind === 'coverage' ? '#st-outline' : '#st-focus').focus(), 50);
}

function stRenderFormats() {
    const box = document.getElementById('st-fmts');
    box.innerHTML = ST_FORMATS.map(f => `<button type="button" class="st-fmt${stState.fmt === f ? ' on' : ''}" data-f="${f}"
        role="radio" aria-checked="${stState.fmt === f}"><b>${T('st.f.' + f)}</b><span>${T('st.fd.' + f)}</span></button>`).join('');
    box.querySelectorAll('.st-fmt').forEach(b => b.addEventListener('click', () => {
        stState.fmt = b.dataset.f;
        stRenderFormats();
        const custom = stState.fmt === 'custom';
        document.getElementById('st-prompt-f').classList.toggle('hidden', !custom);
        if (custom) document.getElementById('st-prompt').focus();
    }));
    document.getElementById('st-prompt-f').classList.toggle('hidden', stState.fmt !== 'custom');
}

async function stGenerate(body, quiet) {
    const r = await fetch(`/api/chain/${stState.id}/studio`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.status);
    if (!quiet) showToast(T('st.started'));
    stLoad();
    return j.item;
}

function stWire() {
    document.querySelectorAll('#chain-explore .st-tool').forEach(b =>
        b.addEventListener('click', () => b.dataset.st === 'visual' ? visualPicker() : b.dataset.st === 'podcast' ? podcastPicker() : b.dataset.st === 'slides' ? slidesPicker() : stOpenDialog(b.dataset.st)));
    document.getElementById('st-form').addEventListener('submit', async e => {
        e.preventDefault();
        const ov = document.getElementById('st-overlay');
        const go = ov.querySelector('#st-go');
        const kind = stState.kind;
        const body = { kind, focus: ov.querySelector('#st-focus').value.trim(), scope: srcScopeBody() };
        if (kind === 'flashcards' || kind === 'quiz') body.n = +ov.querySelector('#st-n').value;
        if (kind === 'report') {
            body.format = stState.fmt;
            body.prompt = ov.querySelector('#st-prompt').value.trim();
            if (body.format === 'custom' && !body.prompt) { ov.querySelector('#st-msg').textContent = T('st.promptNeed'); return; }
        }
        if (kind === 'coverage') {
            body.outline = ov.querySelector('#st-outline').value;
            if (!body.outline) { ov.querySelector('#st-msg').textContent = T('st.listNeed'); return; }
        }
        go.disabled = true;
        try {
            await stGenerate(body);
            ov.classList.add('hidden');
        } catch (err) {
            ov.querySelector('#st-msg').textContent = String(err.message || err);
        } finally { go.disabled = false; }
    });
    document.addEventListener('keydown', e => {
        const ov = document.getElementById('st-overlay');
        if (e.key === 'Escape' && ov && !ov.classList.contains('hidden')) { ov.classList.add('hidden'); return; }
        stKeys(e);
    });
}

// ================= 中间栏：看一份 =================
async function stView(id) {
    let o;
    try {
        const r = await fetch(`/api/chain/${stState.id}/studio/${id}`);
        if (!r.ok) throw new Error(r.status);
        o = await r.json();
    } catch { showToast(T('common.couldNotLoad')); return; }
    stState.cur = o;
    stState.fc = null;
    stState.filter = 'all';
    cxShowTab('studio');
    nbvKind(T('st.k.' + o.kind));
    nbvTitle(stTitle(o));
    stRenderList();
    stRender();
}

function stCoverage(o) {
    const c = o.coverage;
    if (o.kind === 'visual') return o.cost_usd ? fmtUsd(o.cost_usd) : '';
    if (!c) return '';
    const bits = [T('st.covSources', { n: c.sources })];
    if (c.thinned) bits.push(T(o.focus ? 'st.covFocus' : 'st.covThinned', { n: c.passages_used, m: c.passages_total }));
    if (o.cost_usd) bits.push(fmtUsd(o.cost_usd));
    return bits.join(' · ');
}

function stExportDoc(o) {
    const r = o.result || {};
    const cites = o.citations || {};
    const title = stTitle(o);
    if (o.kind === 'report' || o.kind === 'note') return { title, blocks: [{ md: r.md, citations: cites }] };
    if (o.kind === 'visual') return visualExportDoc(o, title);
    if (o.kind === 'podcast') return podcastExportDoc(o, title);
    if (o.kind === 'slides') return slidesExportDoc(o, title);
    if (o.kind === 'compare') return { title: `${T('st.k.compare')}：${o.question || ''}`.slice(0, 80), blocks: [{ md: r.answer, citations: cites }] };
    if (o.kind === 'flashcards') {
        return { title, blocks: (r.cards || []).map((c, i) => ({ heading: `${i + 1}. ${c.front}`, md: c.back, citations: cites })) };
    }
    if (o.kind === 'quiz') {
        return { title, blocks: (r.questions || []).map((q, i) => ({
            heading: `${i + 1}. ${q.q.replace(CITE_RE, '')}`,
            md: q.options.map((x, j) => `${'ABCDEF'[j]}. ${x}`).join('  \n')
                + `\n\n**${T('st.answer')}: ${'ABCDEF'[q.answer]}** ${q.explain}`,
            citations: cites })) };
    }
    if (o.kind === 'coverage') {
        return { title: `${title} · ${r.outline_title || ''}`, blocks: [{ citations: cites,
            md: (r.items || []).map(it => `- ${ST_MARK[it.status]} **${it.item}** — ${T('st.s.' + it.status)}${it.note ? '：' + it.note : ''}`).join('\n') }] };
    }
    return { title, blocks: [] };
}

function stRender() {
    const o = stState.cur;
    const box = document.getElementById('cx-studio');
    if (!o || !box) return;
    const exp = typeof exportMenuHtml === 'function' ? exportMenuHtml(() => stExportDoc(stState.cur)) : '';
    // 对比的结果自己带导出和「各几张卡」那一行
    const head = o.kind === 'compare' ? '' : `<div class="st-head"><span class="cx-muted">${escapeHtml(stCoverage(o))}</span>${exp}</div>`;
    const r = o.result || {};
    const cites = o.citations || {};
    let body = '';
    if (o.kind === 'report' || o.kind === 'note') {
        body = `<div class="cx-digest"><div class="md-body st-md">${citedHtml(r.md, cites)}</div>${sourcesHtml(cites, r.md)}</div>`;
    } else if (o.kind === 'flashcards') {
        body = stFlashHtml(r.cards || []);
    } else if (o.kind === 'quiz') {
        body = stQuizHtml(r.questions || []);
    } else if (o.kind === 'coverage') {
        body = stOutlineHtml(r);
    } else if (o.kind === 'compare') {
        body = `<h3 class="pc-q">${escapeHtml(o.question || '')}</h3>` + cmpResultHtml({ answer: r.answer, citations: cites, creators: r.creators }, o.question || '');
    } else if (o.kind === 'visual') {
        body = visualHtml(o);
    } else if (o.kind === 'podcast') {
        body = podcastHtml(o);
    } else if (o.kind === 'slides') {
        body = slidesHtml(o);
    }
    box.innerHTML = head + body;
    wireCitations(box, cites);
    if (o.kind === 'flashcards') stFlashWire(box);
    if (o.kind === 'quiz') stQuizWire(box);
    if (o.kind === 'coverage') stOutlineWire(box);
    if (o.kind === 'visual') visualWire(box);
    if (o.kind === 'podcast') podcastWire(box);
    if (o.kind === 'slides') slidesWire(box);
}

// ----- 闪卡：一张一张翻；下面还能展开看全部 -----
function stFlashHtml(cards) {
    if (!stState.fc) stState.fc = { order: cards.map((_, i) => i), at: 0, flipped: false };
    const f = stState.fc;
    const c = cards[f.order[f.at]] || {};
    const cites = stState.cur.citations || {};
    return `<div class="fc">
        <div class="fc-bar"><span class="cx-muted">${T('st.fcPos', { i: f.at + 1, n: cards.length })}</span>
            <button type="button" class="cx-link" id="fc-shuffle">${T('st.shuffle')}</button></div>
        <div class="fc-card${f.flipped ? ' flipped' : ''}" id="fc-card" role="button" tabindex="0"
            aria-label="${escapeHtml(T('st.flip'))}">
            <div class="fc-face fc-front md-body">${citedHtml(c.front, cites, stOrder())}</div>
            ${f.flipped ? `<div class="fc-face fc-back md-body">${citedHtml(c.back, cites, stOrder())}</div>` : `<span class="fc-hint">${T('st.flipHint')}</span>`}
        </div>
        <div class="fc-nav">
            <button type="button" class="btn-secondary" id="fc-prev" ${f.at === 0 ? 'disabled' : ''}>← ${T('st.prev')}</button>
            <button type="button" class="btn-primary" id="fc-flip">${T(f.flipped ? 'st.hide' : 'st.show')}</button>
            <button type="button" class="btn-secondary" id="fc-next" ${f.at >= cards.length - 1 ? 'disabled' : ''}>${T('st.next')} →</button>
        </div>
        <details class="fc-all"><summary>${T('st.fcAll', { n: cards.length })}</summary>
            <ol>${cards.map(x => `<li><div class="fc-q">${citedHtml(x.front, cites, stOrder())}</div><div class="md-body">${citedHtml(x.back, cites, stOrder())}</div></li>`).join('')}</ol>
        </details>
    </div>`;
}

function stFlashMove(d) {
    const f = stState.fc;
    const n = ((stState.cur.result || {}).cards || []).length;
    if (!f || f.at + d < 0 || f.at + d >= n) return;
    f.at += d;
    f.flipped = false;
    stRender();
}

function stFlashFlip() {
    if (!stState.fc) return;
    stState.fc.flipped = !stState.fc.flipped;
    stRender();
    const card = document.getElementById('fc-card');
    if (card) card.focus({ preventScroll: true });
}

function stFlashWire(box) {
    const card = box.querySelector('#fc-card');
    card.addEventListener('click', e => { if (!e.target.closest('.cx-cite')) stFlashFlip(); });
    box.querySelector('#fc-flip').addEventListener('click', stFlashFlip);
    box.querySelector('#fc-prev').addEventListener('click', () => stFlashMove(-1));
    box.querySelector('#fc-next').addEventListener('click', () => stFlashMove(1));
    box.querySelector('#fc-shuffle').addEventListener('click', () => {
        const o = stState.fc.order;
        for (let i = o.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [o[i], o[j]] = [o[j], o[i]]; }
        stState.fc.at = 0;
        stState.fc.flipped = false;
        stRender();
    });
}

// 闪卡开着时：空格 / 回车翻面，左右键换卡（焦点在输入框里不管）
function stKeys(e) {
    const o = stState.cur;
    const panel = document.getElementById('cx-studio');
    if (!o || o.kind !== 'flashcards' || !panel || panel.classList.contains('hidden') || !panel.offsetParent) return;
    if (e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (!document.getElementById('st-overlay').classList.contains('hidden')) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); stFlashMove(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); stFlashMove(-1); }
    else if ((e.key === ' ' || e.key === 'Enter') && e.target.closest('#fc-card, body') && !e.target.closest('button, a')) {
        e.preventDefault();
        stFlashFlip();
    }
}

// ----- 自测题：点一个选项立刻判对错、给解释和出处 -----
function stQuizHtml(qs) {
    const ans = stState.quiz[stState.cur.id] || (stState.quiz[stState.cur.id] = {});
    const cites = stState.cur.citations || {};
    const done = Object.keys(ans).length;
    const right = Object.entries(ans).filter(([i, j]) => qs[i] && qs[i].answer === j).length;
    return `<div class="qz">
        <div class="qz-score"><b>${T('st.score', { r: right, d: done, n: qs.length })}</b>
            ${done ? `<button type="button" class="cx-link" id="qz-reset">${T('st.retake')}</button>` : ''}</div>
        <ol class="qz-list">${qs.map((q, i) => {
            const picked = ans[i];
            const answered = picked !== undefined;
            return `<li class="qz-q${answered ? (picked === q.answer ? ' ok' : ' bad') : ''}" data-i="${i}">
                <div class="qz-text md-body">${citedHtml(q.q, cites, stOrder())}</div>
                <div class="qz-opts">${q.options.map((x, j) => `<button type="button" class="qz-opt${answered && j === q.answer ? ' right' : ''}${answered && j === picked && j !== q.answer ? ' wrong' : ''}"
                    data-j="${j}" ${answered ? 'disabled' : ''}><b>${'ABCDEF'[j]}</b><span>${citedHtml(x, cites, stOrder())}</span></button>`).join('')}</div>
                ${answered ? `<div class="qz-exp"><b>${T(picked === q.answer ? 'st.correct' : 'st.wrongIs', { a: 'ABCDEF'[q.answer] })}</b>
                    <div class="md-body">${citedHtml(q.explain, cites, stOrder())}</div></div>` : ''}
            </li>`;
        }).join('')}</ol>
    </div>`;
}

function stQuizWire(box) {
    const ans = stState.quiz[stState.cur.id];
    box.querySelectorAll('.qz-q').forEach(li => li.querySelectorAll('.qz-opt').forEach(b => b.addEventListener('click', e => {
        if (e.target.closest('.cx-cite')) return;
        ans[+li.dataset.i] = +b.dataset.j;
        const y = li.getBoundingClientRect().top;
        stRender();
        const again = document.querySelector(`#cx-studio .qz-q[data-i="${li.dataset.i}"]`);   // 重画后别跳走
        if (again) window.scrollBy(0, again.getBoundingClientRect().top - y);
    })));
    const reset = box.querySelector('#qz-reset');
    if (reset) reset.addEventListener('click', () => { stState.quiz[stState.cur.id] = {}; stRender(); });
}

// ----- 提纲对照：每条提纲讲没讲过 -----
const ST_MARK = { covered: '✓', partial: '◐', missing: '✗' };

function stOutlineHtml(r) {
    const cites = stState.cur.citations || {};
    const items = r.items || [];
    const cnt = r.counts || {};
    const f = stState.filter;
    const chip = (k, label) => `<button type="button" class="ol-chip${f === k ? ' on' : ''}" data-f="${k}">${label}</button>`;
    return `<div class="ol">
        <p class="cx-muted">${T('st.listAgainst', { t: escapeHtml(r.outline_title || '') })}</p>
        <div class="ol-chips">${chip('all', T('st.all', { n: items.length }))}
            ${['missing', 'partial', 'covered'].map(k => chip(k, `${ST_MARK[k]} ${T('st.s.' + k)} ${cnt[k] || 0}`)).join('')}</div>
        <ul class="ol-list">${items.filter(it => f === 'all' || it.status === f).map(it => {
            const oc = it.outline && cites[it.outline];
            return `<li class="ol-it" data-st="${it.status}">
                <span class="ol-st" title="${escapeHtml(T('st.s.' + it.status))}">${ST_MARK[it.status]}</span>
                <div class="ol-body"><div class="ol-item">${escapeHtml(it.item)}
                    ${oc ? `<button type="button" class="cx-cite" data-cid="${escapeHtml(it.outline)}" title="${escapeHtml(oc.quote.slice(0, 200))}">${citeLabel(oc)}</button>` : ''}</div>
                    ${it.note ? `<div class="ol-note md-body">${citedHtml(it.note, cites, stOrder())}</div>` : ''}</div>
            </li>`;
        }).join('') || `<li class="cx-muted">${T('st.olNone')}</li>`}</ul>
    </div>`;
}

function stOutlineWire(box) {
    box.querySelectorAll('.ol-chip').forEach(b => b.addEventListener('click', () => {
        stState.filter = b.dataset.f;
        stRender();
    }));
}

// ----- 问答里的回答存进工作台 -----
async function stSaveNote(m, btn) {
    btn.disabled = true;
    try {
        // 服务端按时间 + 开头几个字从聊天记录里找这条回答（内容和出处以记录为准）
        const r = await fetch(`/api/chain/${stState.id}/studio/note`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ at: m.at, head: String(m.content || '').slice(0, 80) }) });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || r.status);
        btn.innerHTML = `<span>✓ ${escapeHtml(T('st.saved1'))}</span>`;
        showToast(T('st.noteSaved'));
        stLoad();
    } catch (e) {
        btn.disabled = false;
        showToast(String(e.message || e));
    }
}

stWire();
// 直接打开 #/chain/<id> 时 explore.js 先跑过 exploreOpen、本文件后加载：补一次
if (typeof cx !== 'undefined' && cx.id && !stState.id) stOpen(cx.id);
