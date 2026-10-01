// ========== 博主分区的工具：导出 / 听原话 / 合集 / 话题雷达 / 预测排行 ==========
// 依赖 app.js（T / escapeHtml / safeUrl / navigate / showToast / fmtUsd / chainDisplayName / mergeCart）
// 和 explore.js（cx / cxStore / cxCiteCache / citedHtml / sourcesHtml / wireCitations / openShareCard）。

// ---------- 通用弹窗：点遮罩 / × / Esc 关 ----------
function toolOverlay(id) {
    const ov = document.getElementById(id);
    if (ov && !ov.dataset.wired) {
        ov.dataset.wired = '1';
        ov.addEventListener('click', e => {
            if (e.target === ov || e.target.closest('[data-close]')) ov.classList.add('hidden');
        });
    }
    return ov;
}
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    ['collection-overlay', 'radar-overlay', 'lb-overlay'].forEach(id => {
        const ov = document.getElementById(id);
        if (ov && !ov.classList.contains('hidden')) ov.classList.add('hidden');
    });
});

function downloadBlob(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 800);
}

function safeName(s) {
    return String(s || 'Verbatim').replace(/[\\/:*?"<>|\n]+/g, ' ').trim().slice(0, 70) || 'Verbatim';
}

// ================= 导出（PDF / Word / Markdown），出处变成编号脚注 =================
// 调用方给一份 { title, blocks: [{ heading?, md, citations? } ...] }；[#3-12] 按出现顺序编号，
// 文末列出处：原话 — 说话人 · 第几期 标题 · 日期 · [▶ 时间](原视频那一秒)
function buildExportMd(doc) {
    const order = [], refs = {};
    const lines = [`# ${doc.title}`, ''];
    if (doc.sub) lines.push(`_${doc.sub}_`, '');
    for (const b of doc.blocks) {
        if (b.heading) lines.push(`## ${b.heading}`, '');
        const cites = b.citations || {};
        Object.assign(refs, cites);
        const md = String(b.md || '').replace(CITE_RE, (m, cid) => {
            if (!cites[cid] && !refs[cid]) return '';
            let n = order.indexOf(cid);
            if (n < 0) { order.push(cid); n = order.length - 1; }
            return `[${n + 1}]`;
        });
        lines.push(md, '');
    }
    if (order.length) {
        lines.push(`## ${T('exp.sources')}`, '');
        order.forEach((cid, i) => {
            const c = refs[cid];
            const page = c.kind === 'doc' ? [c.page ? T('reader.page', { n: c.page }) : '', c.heading || ''].filter(Boolean).join(' · ') : '';
            const where = [c.creator, c.speaker, `${c.label || 'EP' + c.ep_no} ${c.episode || ''}`.trim(), page, c.date]
                .filter(Boolean).join(' · ');
            const link = c.video_url ? ` · [▶ ${c.ts || T('exp.video')}](${c.video_url})` : (c.ts ? ` · ${c.ts}` : '');
            lines.push(`${i + 1}. “${(c.quote || c.obs || '').replace(/\n/g, ' ')}” — ${where}${link}`);
        });
        lines.push('');
    }
    lines.push(`_${T('exp.footer', { date: new Date().toISOString().slice(0, 10) })}_`);
    return lines.join('\n');
}

async function runExport(fmt, doc) {
    const md = buildExportMd(doc);
    const name = safeName(doc.title);
    if (fmt === 'md') {
        downloadBlob(new Blob([md], { type: 'text/markdown;charset=utf-8' }), name + '.md');
        return;
    }
    // Word 本机生成；PDF 交给墨页排版打印（要几秒，墨页没开会提示）
    if (fmt === 'pdf') showToast(T('exp.pdfWorking'));
    try {
        const r = await fetch(`/api/export/${fmt === 'pdf' ? 'pdf' : 'docx'}`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: doc.title, markdown: md }) });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
        downloadBlob(await r.blob(), name + (fmt === 'pdf' ? '.pdf' : '.docx'));
    } catch (e) { showToast(T('exp.failed', { e: String(e.message || e) })); }
}

// 一个「导出 ▾」小菜单；getDoc 在点的时候才算，拿到的是最新内容
const exportSources = {};
let exportSeq = 0;
function exportMenuHtml(getDoc, cls = '') {
    const key = 'x' + (++exportSeq);
    exportSources[key] = getDoc;
    return `<details class="exp-menu ${cls}"><summary class="cx-link">${T('exp.export')} ▾</summary>
        <div class="exp-pop">
            <button type="button" data-exp="${key}" data-fmt="pdf">${T('exp.pdf')}</button>
            <button type="button" data-exp="${key}" data-fmt="docx">${T('exp.word')}</button>
            <button type="button" data-exp="${key}" data-fmt="md">${T('exp.markdown')}</button>
        </div></details>`;
}
document.addEventListener('click', e => {
    const b = e.target.closest('[data-exp]');
    if (b) {
        const get = exportSources[b.dataset.exp];
        b.closest('details').open = false;
        if (get) runExport(b.dataset.fmt, get());
        return;
    }
    document.querySelectorAll('details.exp-menu[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; });
});

// ================= 听这段原话 =================
function clipWindow(c) {
    const q = String(c.quote || '');
    const cjk = (q.match(/[㐀-鿿]/g) || []).length;
    const words = (q.replace(/[㐀-鿿]/g, ' ').match(/[A-Za-z0-9']+/g) || []).length;
    const speak = cjk / 4.2 + words / 2.6;                       // 大致语速：中文 4 字/秒，英文 2.6 词/秒
    const start = Math.max(0, (c.sec || 0) - 2);
    return { start, dur: Math.round(Math.min(60, Math.max(10, speak + 6))) };
}

function clipButtonHtml(c) {
    if (!c.task_id || c.sec == null || !c.video_url || window.VERBATIM_DEMO) return '';
    const w = clipWindow(c);
    return `<button type="button" class="cx-link" data-clip="${escapeHtml(c.task_id)}|${w.start}|${w.dur}">▶ ${T('clip.listen')}</button>`;
}

document.addEventListener('click', e => {
    const b = e.target.closest('[data-clip]');
    if (!b) return;
    const [tid, start, dur] = b.dataset.clip.split('|');
    const holder = b.closest('.cx-src') || b.parentElement;
    let box = holder.querySelector('.clip-box');
    if (box) { box.remove(); return; }                         // 再点一次收起
    box = document.createElement('div');
    box.className = 'clip-box';
    const url = `/api/clip?task_id=${encodeURIComponent(tid)}&start=${start}&dur=${dur}`;
    box.innerHTML = `<span class="cx-muted">${T('clip.loading')}</span>`;
    holder.appendChild(box);
    const audio = new Audio();
    audio.controls = true;
    audio.preload = 'auto';
    audio.src = url;
    audio.addEventListener('canplay', () => {
        box.innerHTML = '';
        box.appendChild(audio);
        const dl = document.createElement('a');
        dl.className = 'cx-link';
        dl.href = url + '&dl=1';
        dl.textContent = T('clip.download');
        box.appendChild(dl);
        audio.play().catch(() => {});
    }, { once: true });
    audio.addEventListener('error', async () => {
        let msg = T('clip.failed');
        try { const j = await (await fetch(url)).json(); if (j.error) msg += ' ' + j.error; } catch { /* 不是 JSON */ }
        box.innerHTML = `<span class="cx-err">${escapeHtml(msg)}</span>`;
    }, { once: true });
});

// ================= 合集：新建 / 往里加 =================
let colState = { mode: 'new', id: null, tab: 'transcripts', picked: new Set(), pickedChains: new Set(), items: [], q: '' };

function openCollectionModal(opts = {}) {
    const ov = toolOverlay('collection-overlay');
    colState = { mode: opts.addTo ? 'add' : 'new', id: opts.addTo || null, tab: 'transcripts',
                 picked: new Set(opts.taskIds || []), pickedChains: new Set(opts.chainIds || []), items: [], q: '' };
    ov.querySelector('#col-title').textContent = T(colState.mode === 'add' ? 'col.addTitle' : 'col.newTitle');
    ov.querySelector('#col-meta').classList.toggle('hidden', colState.mode === 'add');
    ov.querySelector('#col-name').value = opts.name || '';
    ov.querySelector('#col-msg').textContent = '';
    ov.classList.remove('hidden');
    colTab('transcripts');
    colLoad('');
    setTimeout(() => (colState.mode === 'add' ? ov.querySelector('#col-q') : ov.querySelector('#col-name')).focus(), 60);
}

function colTab(tab) {
    colState.tab = tab;
    document.querySelectorAll('#collection-overlay .col-tab').forEach(b => b.classList.toggle('on', b.dataset.t === tab));
    colRender();
}

async function colLoad(q) {
    colState.q = q;
    try {
        const r = await (await fetch('/api/transcripts/pick?q=' + encodeURIComponent(q))).json();
        if (colState.q !== q) return;
        colState.items = r.items || [];
        colState.total = r.total || 0;
    } catch { colState.items = []; }
    if (!colState.chains) {
        try {
            const chains = await (await fetch('/api/chains')).json();
            colState.chains = chains.filter(c => !['collection', 'project'].includes(c.kind) && (c.videos || []).some(v => v.status === 'done') && !c.hidden);
        } catch { colState.chains = []; }
    }
    colRender();
}

function colRender() {
    const box = document.getElementById('col-list');
    if (!box) return;
    if (colState.tab === 'transcripts') {
        box.innerHTML = colState.items.map(it => `<label class="col-item">
            <input type="checkbox" data-tid="${escapeHtml(it.task_id)}" ${colState.picked.has(it.task_id) ? 'checked' : ''}>
            <span class="col-t">${escapeHtml(it.title)}</span>
            <span class="cx-muted">${escapeHtml([it.creator, it.date, it.minutes ? it.minutes + ' min' : ''].filter(Boolean).join(' · '))}</span>
        </label>`).join('') || `<p class="cx-muted">${T('col.none')}</p>`;
    } else {
        const q = colState.q.toLowerCase();
        const list = (colState.chains || []).filter(c => !q || chainDisplayName(c).toLowerCase().includes(q));
        box.innerHTML = list.map(c => `<label class="col-item">
            <input type="checkbox" data-cid="${escapeHtml(c.id)}" ${colState.pickedChains.has(c.id) ? 'checked' : ''}>
            <span class="col-t">${escapeHtml(chainDisplayName(c))}</span>
            <span class="cx-muted">${T('creators.nEpisodes', { n: (c.videos || []).filter(v => v.status === 'done').length })}</span>
        </label>`).join('') || `<p class="cx-muted">${T('col.none')}</p>`;
    }
    box.querySelectorAll('input[data-tid]').forEach(i => i.onchange = () => {
        i.checked ? colState.picked.add(i.dataset.tid) : colState.picked.delete(i.dataset.tid); colCount();
    });
    box.querySelectorAll('input[data-cid]').forEach(i => i.onchange = () => {
        i.checked ? colState.pickedChains.add(i.dataset.cid) : colState.pickedChains.delete(i.dataset.cid); colCount();
    });
    colCount();
}

function colCount() {
    const el = document.getElementById('col-count');
    if (el) el.textContent = T('col.picked', { n: colState.picked.size, m: colState.pickedChains.size });
}

async function colSubmit(e) {
    e.preventDefault();
    const msg = document.getElementById('col-msg');
    const body = { task_ids: [...colState.picked], chain_ids: [...colState.pickedChains] };
    if (!body.task_ids.length && !body.chain_ids.length) { msg.textContent = T('col.pickSome'); return; }
    let url = `/api/chain/${colState.id}/sources/transcripts`;   // 往项目 / 合集里加（合集会顺带抽新加的那几期的卡）
    if (colState.mode === 'new') {
        body.name = document.getElementById('col-name').value.trim();
        body.kind = document.getElementById('col-kind').value;
        body.lang = currentLang === 'zh' ? 'zh' : 'auto';
        if (!body.name) { msg.textContent = T('col.nameIt'); return; }
        url = '/api/collections';
    }
    msg.textContent = T('col.creating');
    try {
        const r = await (await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body) })).json();
        if (r.error) { msg.textContent = r.error; return; }
        document.getElementById('collection-overlay').classList.add('hidden');
        if (typeof clearMergeCart === 'function' && colState.fromCart) clearMergeCart();
        const id = r.id || colState.id;
        cxStore('cx.tab', 'episodes');
        if (colState.mode === 'add' && typeof chainDetailId !== 'undefined' && chainDetailId === id) {
            refreshChainDetail();
            if (typeof srcLoad === 'function') srcLoad();
        } else {
            navigate('chain/' + id);
        }
        showToast(T('col.started'));
    } catch (err) { msg.textContent = String(err); }
}

(function wireCollection() {
    const ov = toolOverlay('collection-overlay');
    if (!ov) return;
    ov.querySelectorAll('.col-tab').forEach(b => b.onclick = () => colTab(b.dataset.t));
    let t = null;
    ov.querySelector('#col-q').addEventListener('input', e => {
        clearTimeout(t);
        t = setTimeout(() => colLoad(e.target.value.trim()), 200);
    });
    ov.querySelector('#col-form').addEventListener('submit', colSubmit);
    const btn = document.getElementById('collection-open');
    if (btn) {
        if (window.VERBATIM_DEMO) btn.remove();
        else btn.addEventListener('click', () => openCollectionModal());
    }
})();

// 分集页勾好的（可以跨博主）直接建合集
function collectionFromCart() {
    if (typeof mergeCart === 'undefined' || !mergeCart.size) return;
    openCollectionModal({ taskIds: [...mergeCart] });
    colState.fromCart = true;
}

// ================= 话题雷达 =================
function openRadar(q) {
    const ov = toolOverlay('radar-overlay');
    ov.classList.remove('hidden');
    const input = ov.querySelector('#radar-q');
    if (q) { input.value = q; runRadar(); }
    setTimeout(() => input.focus(), 60);
}

const STANCE_KEYS = ['pro', 'mixed', 'neutral', 'con', 'none'];
function netLabel(v) {
    if (v == null) return '<span class="cx-muted">—</span>';
    const cls = v > 0.2 ? 'st-pro' : v < -0.2 ? 'st-con' : 'st-mixed';
    const word = v > 0.2 ? T('radar.pos') : v < -0.2 ? T('radar.neg') : T('radar.mid');
    return `<span class="cx-stance ${cls}">${word} ${v > 0 ? '+' : ''}${v.toFixed(2)}</span>`;
}
function trendLabel(a, b) {
    if (a == null || b == null) return '<span class="cx-muted">—</span>';
    const d = b - a;
    if (Math.abs(d) < 0.25) return `<span class="cx-muted">→ ${T('radar.same')}</span>`;
    return d > 0 ? `<span class="st-pro rd-trend">↑ ${T('radar.warmer')}</span>` : `<span class="st-con rd-trend">↓ ${T('radar.cooler')}</span>`;
}

async function runRadar(e) {
    if (e) e.preventDefault();
    const q = document.getElementById('radar-q').value.trim();
    const out = document.getElementById('radar-out');
    if (!q) return;
    out.innerHTML = `<div class="cx-thinking">${T('radar.loading')}</div>`;
    let r;
    try { r = await (await fetch('/api/radar', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q }) })).json(); } catch (err) { r = { error: String(err) }; }
    if (r.error) { out.innerHTML = `<p class="cx-err">${escapeHtml(r.error)}</p>`; return; }
    const rows = r.rows || [];
    if (!rows.length) { out.innerHTML = `<p class="cx-muted">${T('radar.none', { q: escapeHtml(q) })}</p>`; return; }
    rows.forEach(row => (row.best || []).forEach(c => { c.creator = row.author; cxCiteCache[c.id + '@' + row.chain_id] = c; }));
    const top = rows.slice(0, 4).map(x => x.chain_id);
    out.innerHTML = `
        <p class="cx-muted">${T('radar.sub', { n: rows.length, q: escapeHtml(q) })}</p>
        <div class="radar-table">
            <div class="radar-row radar-hd"><span>${T('radar.who')}</span><span>${T('radar.count')}</span><span>${T('radar.stance')}</span>
                <span>${T('radar.overall')}</span><span>${T('radar.trend')}</span><span>${T('radar.span')}</span><span></span></div>
            ${rows.map(row => {
                const tot = Object.values(row.stances).reduce((a, b) => a + b, 0) || 1;
                const bar = STANCE_KEYS.filter(k => row.stances[k]).map(k =>
                    `<i class="st-bg-${k}" style="width:${(row.stances[k] / tot * 100).toFixed(1)}%" title="${T('stance.' + k)} ${row.stances[k]}"></i>`).join('');
                const best = row.best && row.best[0];
                return `<div class="radar-row">
                    <span class="radar-who"><b>${escapeHtml(row.author)}</b>
                        ${best ? `<span class="radar-q">“${escapeHtml(best.quote.slice(0, 90))}${best.quote.length > 90 ? '…' : ''}”</span>` : ''}</span>
                    <span class="tnum">${T('radar.cards', { n: row.cards, m: row.episodes })}</span>
                    <span class="tp-bar radar-bar">${bar}</span>
                    <span>${netLabel(row.net)}</span>
                    <span>${trendLabel(row.early, row.late)}</span>
                    <span class="cx-muted tnum">${escapeHtml(row.first === row.last ? row.first : row.first + ' – ' + row.last)}</span>
                    <span><button type="button" class="cx-link" data-radar-ask="${row.chain_id}">${T('radar.ask')}</button></span>
                </div>`;
            }).join('')}
        </div>
        <div class="radar-actions">
            ${rows.length >= 2 && !window.VERBATIM_DEMO ? `<button type="button" class="btn-secondary cx-send" id="radar-cmp">${T('radar.compare', { n: Math.min(4, rows.length) })}</button>` : ''}
            <span class="cx-muted">${T('radar.how')}</span>
        </div>`;
    out.querySelectorAll('[data-radar-ask]').forEach(b => b.onclick = () => {
        document.getElementById('radar-overlay').classList.add('hidden');
        cxStore('cx.tab', 'ask');
        cxStore('cx.pendingAsk', JSON.stringify({ id: b.dataset.radarAsk, q: T('radar.askQ', { q }) }));
        navigate('chain/' + b.dataset.radarAsk);
    });
    const cmp = out.querySelector('#radar-cmp');
    if (cmp) cmp.onclick = () => {
        document.getElementById('radar-overlay').classList.add('hidden');
        cxStore('cmp.pick', top.join(','));
        compareOpen();
        document.getElementById('cmp-q').value = T('radar.askQ', { q });
    };
}

(function wireRadar() {
    const ov = toolOverlay('radar-overlay');
    if (!ov) return;
    ov.querySelector('#radar-form').addEventListener('submit', runRadar);
    const btn = document.getElementById('radar-open');
    if (btn) {
        if (window.VERBATIM_DEMO && !window.VERBATIM_DEMO_ASK) btn.remove();
        else btn.addEventListener('click', () => openRadar());
    }
})();

// ================= 预测排行 =================
async function openLeaderboard() {
    const ov = toolOverlay('lb-overlay');
    ov.classList.remove('hidden');
    const out = ov.querySelector('#lb-out');
    out.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
    let r;
    try { r = await (await fetch('/api/leaderboard')).json(); } catch { r = { rows: [] }; }
    const rows = r.rows || [];
    if (!rows.length) { out.innerHTML = `<p class="cx-muted">${T('lb.none')}</p>`; return; }
    let rank = 0;
    out.innerHTML = `<div class="lb-table">
        <div class="lb-row lb-hd"><span>#</span><span>${T('radar.who')}</span><span>${T('lb.rate')}</span>
            <span>${T('pr.v.true')}</span><span>${T('pr.v.false')}</span><span>${T('pr.v.pending')}</span><span>${T('lb.total')}</span></div>
        ${rows.map(x => `<div class="lb-row${x.ranked ? '' : ' lb-unranked'}" data-go="chain/${x.chain_id}">
            <span class="tnum">${x.ranked ? ++rank : '—'}</span>
            <span><b>${escapeHtml(x.author)}</b></span>
            <span>${x.ranked ? `<span class="lb-bar"><i style="width:${Math.round(x.hit_rate * 100)}%"></i></span>
                <b class="tnum">${Math.round(x.hit_rate * 100)}%</b> <span class="cx-muted">${T('lb.of', { a: x.true, b: x.resolved })}</span>`
                : `<span class="cx-muted">${T('lb.tooFew', { n: x.resolved, m: r.min_resolved })}</span>`}</span>
            <span class="tnum">${x.true}</span><span class="tnum">${x.false}</span><span class="tnum">${x.pending}</span>
            <span class="tnum">${x.predictions}</span>
        </div>`).join('')}
    </div><p class="cx-muted lb-note">${T('lb.note')}</p>`;
    out.querySelectorAll('[data-go]').forEach(el => el.onclick = () => {
        ov.classList.add('hidden');
        cxStore('cx.tab', 'predictions');
        navigate(el.dataset.go);
    });
}
(function wireLb() {
    toolOverlay('lb-overlay');
    const btn = document.getElementById('lb-open');
    if (btn) btn.addEventListener('click', openLeaderboard);
})();
