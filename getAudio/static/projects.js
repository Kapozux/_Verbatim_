// ========== 项目（笔记本式）：左「来源」/ 中「问答」/ 右「Studio」==========
// 一切都是项目：频道只是一种来源（加了频道的项目就是以前的「博主」）。
// 后端：app.py 的 /api/projects、/api/chain/<id>/sources*、/rename、/cards/build、/api/docs/<id>；
// 文档转换和切段在 sources.py；提问时原文段落跟证据卡一起检索（ask.py）。
// 依赖 app.js（T / escapeHtml / renderMarkdown / navigate / showToast / refreshChainDetail / loadChains /
// nfPreview / chainUrl）、explore.js（cx / cxShowTab / passageIndexOf 在本文件）、tools.js（toolOverlay / openCollectionModal）。

const DOC_EXT = ['pdf', 'doc', 'docx', 'ppt', 'pptx', 'md', 'markdown', 'txt', 'rtf', 'html', 'htm',
                 'png', 'jpg', 'jpeg', 'webp', 'heic'];
const MEDIA_EXT = ['mp3', 'm4a', 'wav', 'ogg', 'opus', 'flac', 'aac', 'wma', 'mp4', 'mov', 'mkv', 'webm', 'avi', 'm4v'];
let srcState = { id: null, data: null, timer: null, sel: null, open: {} };
let nfProject = null;                 // 「添加来源」里走频道预览时，频道要加进的项目（app.js 的 nfStart 读它）

// ================= 新建 / 改名 =================
let pjOpenAddFor = null;              // 新建完自动弹「添加来源」（跟 Gemini Notebook 一样）

(function wireNewProject() {
    const btn = document.getElementById('project-new');
    if (!btn) return;
    if (window.VERBATIM_DEMO) { btn.remove(); return; }
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
            const r = await (await fetch('/api/projects', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: T('pj.untitled'), lang: currentLang === 'zh' ? 'zh' : 'auto' }),
            })).json();
            if (!r.ok) { showToast(r.error || T('common.couldNotLoad')); return; }
            pjOpenAddFor = r.id;
            if (typeof loadChains === 'function') loadChains();
            navigate('chain/' + r.id);
        } catch (e) { showToast(String(e)); } finally { btn.disabled = false; }
    });
})();

// 点项目名改名（档案头每次重画，事件挂在外层）
document.addEventListener('click', e => {
    const h = e.target.closest('#chain-detail-info .cp-name');
    if (!h || window.VERBATIM_DEMO || h.isContentEditable) return;
    const id = cx.id;
    const old = h.textContent;
    h.contentEditable = 'true';
    h.classList.add('editing');
    h.focus();
    document.getSelection().selectAllChildren(h);
    const done = async save => {
        h.contentEditable = 'false';
        h.classList.remove('editing');
        const name = h.textContent.trim().slice(0, 60);
        if (!save || !name || name === old) { h.textContent = old; return; }
        try {
            const r = await (await fetch(`/api/chain/${id}/rename`, { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })).json();
            if (!r.ok) throw new Error(r.error);
            if (typeof refreshChainDetail === 'function') refreshChainDetail();
            if (typeof loadChains === 'function') loadChains();
        } catch (err) { h.textContent = old; showToast(String(err.message || err)); }
    };
    h.addEventListener('keydown', function k(ev) {
        if (ev.key === 'Enter') { ev.preventDefault(); h.removeEventListener('keydown', k); h.blur(); }
        if (ev.key === 'Escape') { h.removeEventListener('keydown', k); done(false); }
    });
    h.addEventListener('blur', () => done(true), { once: true });
});

// ================= 左栏：来源 =================
async function srcFetch(id) {
    const r = await fetch(`/api/chain/${id}/sources`);
    if (!r.ok) throw new Error('not found');
    return r.json();
}

// 全部来源的 id（勾选 = 提问范围）
function srcAllIds(d) {
    if (!d) return [];
    return [...(d.videos || []).filter(v => v.task_id && v.status === 'done').map(v => v.task_id),
            ...(d.channels || []).flatMap(ch => (ch.videos || []).filter(v => v.status === 'done').map(v => v.task_id)),
            ...(d.recordings || []).filter(r => r.status === 'done').map(r => r.task_id),
            ...(d.docs || []).filter(x => x.status === 'ready').map(x => x.doc_id),
            ...(d.repos || []).filter(r => r.cards > 0).map(r => r.repo_id)];
}
function srcIsOn(sid) { return !srcState.sel || srcState.sel.has(sid); }

// 给提问用：全选 = 不限范围
function srcScopeBody() {
    const all = srcAllIds(srcState.data);
    if (!srcState.sel || !all.length) return undefined;
    const picked = all.filter(x => srcState.sel.has(x));
    if (picked.length === all.length) return undefined;
    return { type: 'all', sources: picked };
}

function srcCountText() {
    const all = srcAllIds(srcState.data);
    const n = srcState.sel ? all.filter(x => srcState.sel.has(x)).length : all.length;
    return n === all.length ? T(n === 1 ? 'nb.nSourcesOne' : 'nb.nSources', { n }) : T('nb.nOfSources', { n, m: all.length });
}
function srcSyncCount() {
    const el = document.getElementById('cx-src-n');
    if (el) el.textContent = srcState.data ? srcCountText() : '';
    const li = document.getElementById('cx-hero-n');        // 对话开头「N 个来源」，加了来源要跟着变
    if (li && srcState.data) {
        const n = srcAllIds(srcState.data).length;
        li.textContent = T(n === 1 ? 'nb.nSourcesOne' : 'nb.nSources', { n });
    }
}

async function srcLoad() {
    const id = cx.id;
    if (!id) return;
    clearTimeout(srcState.timer);
    if (srcState.id !== id) {
        srcState = { id, data: null, timer: null, sel: null, open: {} };
        dscReset();
    }
    let d;
    try { d = await srcFetch(id); } catch { return; }
    if (cx.id !== id) return;
    srcState.data = d;
    srcRender();
    srcSyncCount();
    if (typeof vcLoad === 'function' && vcState.id !== id) vcLoad();        // 说话人那一行（换了项目才重新拉）
    // 来源数跟档案头对不上（刚加 / 刚删）：刷新档案头——「问」能不能用也是按它算的
    const ch = cx.chain || {};
    const n = srcAllIds(d).length;
    if (ch.n_sources !== undefined && ch.n_sources !== n && typeof refreshChainDetail === 'function') refreshChainDetail();
    const running = st => !['done', 'failed', 'cancelled'].includes(st);
    const busy = d.indexing || d.docs.some(x => x.status === 'converting')
        || d.recordings.some(r => r.status !== 'done') || (d.channel && running(d.channel.stage))
        || (d.channels || []).some(ch => running(ch.stage)) || (d.repos || []).some(r => r.status === 'reading');
    // 引用的博主跑完了 / 加了新博主：人名条、工作台列表、哪些格子能用都要跟着变
    const sig = (d.channels || []).map(ch => ch.chain_id + ch.stage).join();
    if (srcState.chSig !== undefined && srcState.chSig !== sig && typeof peopleLoad === 'function') peopleLoad();
    srcState.chSig = sig;
    if (busy) srcState.timer = setTimeout(() => { if (cx.id === id) srcLoad(); }, 4000);
    if (pjOpenAddFor === id) {
        pjOpenAddFor = null;
        if (!n) openAddSources();
    }
}

// 来源图标：跟侧栏、工作台同一套线条图标（不用表情符号）
const SRC_PATHS = {
    doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    slides: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M12 16v4M8 20h8"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-9 9"/>',
    note: '<path d="M5 4h14v11l-5 5H5z"/><path d="M14 20v-5h5"/>',
    web: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.4 2.4 3.6 5.2 3.6 8.5s-1.2 6.1-3.6 8.5c-2.4-2.4-3.6-5.2-3.6-8.5s1.2-6.1 3.6-8.5z"/>',
    audio: '<path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="14" width="4" height="6" rx="1.5"/><rect x="17" y="14" width="4" height="6" rx="1.5"/>',
    video: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10.5 9.3v5.4l4.5-2.7z"/>',
    channel: '<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M8 3l4 3 4-3"/>',
};
function srcSvg(kind) {
    return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7"
        stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SRC_PATHS[kind] || SRC_PATHS.doc}</svg>`;
}
function srcIcon(ext) {
    const k = ext === 'web' ? 'web' : ['ppt', 'pptx'].includes(ext) ? 'slides'
        : ['png', 'jpg', 'jpeg', 'webp', 'heic'].includes(ext) ? 'image' : ext === 'text' ? 'note' : 'doc';
    return `<span class="sr-svg" data-k="${k}">${srcSvg(k)}</span>`;
}

function srcHost(url) {
    try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
}

function srcRow(kind, sid, icon, title, sub, opts = {}) {
    const can = !!opts.ready;
    return `<div class="sr-row${opts.child ? ' sr-child' : ''}" data-kind="${kind}" data-sid="${escapeHtml(sid)}">
        <span class="sr-ic">${icon}</span>
        <button type="button" class="sr-title" ${can ? '' : 'disabled'} title="${escapeHtml(title)}">${escapeHtml(title)}</button>
        ${sub ? `<span class="sr-sub ${opts.subCls || ''}">${escapeHtml(sub)}</span>` : ''}
        ${opts.menu && !window.VERBATIM_DEMO ? `<details class="sr-menu"><summary aria-label="⋯">⋯</summary><div>${opts.menu}</div></details>` : ''}
        <input type="checkbox" class="sr-check" ${can ? '' : 'disabled'} ${can && srcIsOn(sid) ? 'checked' : ''}
            aria-label="${escapeHtml(title)}">
    </div>`;
}

function srcRender() {
    const box = document.getElementById('cx-sources');
    const d = srcState.data;
    if (!box || !d) return;
    const ro = !!window.VERBATIM_DEMO;
    const all = srcAllIds(d);
    const allOn = all.length > 0 && all.every(srcIsOn);
    const rows = [];
    // 一个博主一组：项目自己的频道（key = channel）和引用的博主（key = 他的链条 id，能从项目里拿掉）
    const group = (ch, vids, key, ref) => {
        const eps = (vids || []).filter(v => v.task_id);
        const done = eps.filter(v => v.status === 'done');
        const on = done.length && done.every(v => srcIsOn(v.task_id));
        const open = !!srcState.open[key];
        const face = ch.avatar
            ? `<img src="${escapeHtml(ch.avatar)}" alt="" referrerpolicy="no-referrer" onerror="this.remove()">` : '';
        const running = !['done', 'failed', 'cancelled'].includes(ch.stage);
        rows.push(`<div class="sr-row sr-group" data-kind="channel" data-ckey="${escapeHtml(key)}">
            <span class="sr-ic sr-face">${face || `<span class="sr-svg" data-k="channel">${srcSvg('channel')}</span>`}</span>
            <button type="button" class="sr-title sr-fold" aria-expanded="${open}" ${ref ? `title="${escapeHtml(T('src.sharedHint'))}"` : ''}>${escapeHtml(ch.name || ch.url)}</button>
            <span class="sr-sub${running ? ' run' : ''}">${running && !done.length ? T('src.transcribing') : T('src.nEpisodes', { n: done.length })}</span>
            <details class="sr-menu"><summary aria-label="⋯">⋯</summary><div>
                ${ref ? `<button type="button" data-act="open-creator">${T('src.openCreator')}</button>`
                    : `<button type="button" data-act="episodes">${T('nb.episodes')}</button>`}
                ${ref && !ro ? `<button type="button" data-act="remove-creator">${T('src.removeCreator')}</button>` : ''}</div></details>
            <button type="button" class="sr-caret" aria-label="toggle">${open ? '▾' : '▸'}</button>
            <input type="checkbox" class="sr-check sr-check-group" ${done.length ? '' : 'disabled'} ${on ? 'checked' : ''}>
        </div>`);
        if (open) {
            eps.forEach(v => rows.push(srcRow('video', v.task_id, `<span class="sr-svg" data-k="video">${srcSvg('video')}</span>`, v.title || v.task_id,
                v.status === 'done' ? '' : T('src.st.converting'), { ready: v.status === 'done', child: true })));
        }
    };
    if (d.channel) group(d.channel, d.videos, 'channel', false);
    (d.channels || []).forEach(ch => group(ch, ch.videos, ch.chain_id, true));
    if (!d.channel) {
        (d.videos || []).filter(v => v.task_id).forEach(v => rows.push(srcRow('video', v.task_id, `<span class="sr-svg" data-k="audio">${srcSvg('audio')}</span>`, v.title || v.task_id,
            v.status === 'done' ? '' : T('src.st.converting'), { ready: v.status === 'done',
                menu: `<button type="button" data-act="remove">${T('src.remove')}</button>` })));
    }
    (d.recordings || []).forEach(r => rows.push(srcRow('recording', r.task_id, `<span class="sr-svg" data-k="audio">${srcSvg('audio')}</span>`, r.title,
        r.status === 'done' ? '' : T('src.transcribing'), { ready: r.status === 'done',
            menu: `<button type="button" data-act="remove">${T('src.remove')}</button>` })));
    (d.docs || []).forEach(x => {
        // 网页的域名不占行内位置（左栏窄，标题会被挤没），放在阅读器标题旁边
        const sub = x.status === 'ready' ? (x.pages ? T('src.pages', { n: x.pages }) : '')
            : T('src.st.' + (x.status || 'missing'));
        rows.push(srcRow('doc', x.doc_id, srcIcon(x.ext), x.title, sub, {
            ready: x.status === 'ready', subCls: x.status === 'failed' ? 'bad' : x.status === 'converting' ? 'run' : '',
            menu: (x.status === 'failed' ? `<button type="button" data-act="retry">${T('src.retry')}</button>` : '')
                + `<button type="button" data-act="remove">${T('src.remove')}</button>` }));
        if (x.status === 'failed' && x.error) rows.push(`<div class="sr-err">${escapeHtml(x.error.slice(0, 180))}</div>`);
    });
    if (typeof repoSrcRows === 'function') rows.push(...repoSrcRows(d));          // 代码库（repos.js）
    document.getElementById('nb-src-top').classList.toggle('ro', ro);
    box.innerHTML = `
        ${d.indexing ? `<p class="src-note cx-thinking">${T('src.indexing')}</p>` : ''}
        ${rows.length ? `<label class="sr-all"><span>${T('src.selectAll')}</span>
            <input type="checkbox" id="sr-all" ${allOn ? 'checked' : ''} ${all.length ? '' : 'disabled'}></label>
            <div class="sr-list">${rows.join('')}${typeof vcSrcRowHtml === 'function' ? vcSrcRowHtml() : ''}</div>`
            : `<div class="sr-empty"><div class="sr-empty-ic">${srcSvg('doc')}</div><b>${T('nb.srcEmptyT')}</b>
                <span>${T('nb.srcEmptyD')}</span></div>`}`;
    srcWire(box);
    if (typeof vcWireSrc === 'function') vcWireSrc(box);
    srcRenderRail(d, ro);
    srcRenderCta();
}

// 来源栏收起后剩一条竖栏：展开键（在栏头）、＋、每个来源一个图标；点图标 = 展开并打开那条原文
function srcRenderRail(d, ro) {
    const rail = document.getElementById('nb-src-rail');
    if (!rail) return;
    const it = (kind, sid, icon, title, ready) => `<button type="button" class="nb-rail-it${ready && srcIsOn(sid) ? '' : ' off'}"
        data-kind="${kind}" data-sid="${escapeHtml(sid)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}">${icon}</button>`;
    const items = [];
    if (d.channel) {
        const face = d.channel.avatar
            ? `<img src="${escapeHtml(d.channel.avatar)}" alt="" referrerpolicy="no-referrer" onerror="this.replaceWith(document.createRange().createContextualFragment(srcSvg('channel')))">`
            : srcSvg('channel');
        items.push(`<button type="button" class="nb-rail-it nb-rail-face" data-kind="channel" data-sid="channel" title="${escapeHtml(d.channel.name || '')}"
            aria-label="${escapeHtml(d.channel.name || '')}">${face}</button>`);
    }
    (d.channels || []).forEach(ch => items.push(`<button type="button" class="nb-rail-it nb-rail-face" data-kind="channel"
        data-sid="${escapeHtml(ch.chain_id)}" title="${escapeHtml(ch.name || '')}" aria-label="${escapeHtml(ch.name || '')}">${ch.avatar
            ? `<img src="${escapeHtml(ch.avatar)}" alt="" referrerpolicy="no-referrer" onerror="this.replaceWith(document.createRange().createContextualFragment(srcSvg('channel')))">`
            : srcSvg('channel')}</button>`));
    if (!d.channel) {
        (d.videos || []).filter(v => v.task_id).forEach(v => items.push(it('video', v.task_id, srcSvg('audio'), v.title || v.task_id, v.status === 'done')));
    }
    (d.recordings || []).forEach(r => items.push(it('recording', r.task_id, srcSvg('audio'), r.title, r.status === 'done')));
    (d.docs || []).forEach(x => {
        const k = x.ext === 'web' ? 'web' : ['ppt', 'pptx'].includes(x.ext) ? 'slides'
            : ['png', 'jpg', 'jpeg', 'webp', 'heic'].includes(x.ext) ? 'image' : x.ext === 'text' ? 'note' : 'doc';
        items.push(it('doc', x.doc_id, srcSvg(k), x.title, x.status === 'ready'));
    });
    if (typeof repoRailItems === 'function') items.push(...repoRailItems(d, it));
    rail.innerHTML = (ro ? '' : `<button type="button" class="nb-rail-it nb-rail-add" title="${escapeHtml(T('nb.srcRailAdd'))}"
        aria-label="${escapeHtml(T('nb.srcRailAdd'))}"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"
        stroke-width="1.9" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>`)
        + (items.length ? `<span class="nb-rail-sep"></span>${items.join('')}` : '');
    const add = rail.querySelector('.nb-rail-add');
    if (add) add.addEventListener('click', e => { e.stopPropagation(); openAddSources(); });
    rail.querySelectorAll('.nb-rail-it[data-kind]').forEach(b => b.addEventListener('click', () => {
        // 点击会冒泡到整条栏，那边负责展开、记住状态；这里只管打开哪一条
        const { kind, sid } = b.dataset;
        if (kind === 'channel') { srcState.open[sid] = true; srcRender(); return; }
        if (kind === 'doc') { if ((d.docs || []).some(x => x.doc_id === sid && x.status === 'ready')) openSourceReader(sid, null); return; }
        if (kind === 'repo') { if ((d.repos || []).some(r => r.repo_id === sid && r.cards > 0)) openRepoReader(sid); return; }
        const v = [...(d.videos || []), ...(d.recordings || [])].find(x => x.task_id === sid);
        if (v && v.status === 'done') openTranscriptViewer(sid, null);
    }));
}

function srcSetSel(ids, on) {
    const all = srcAllIds(srcState.data);
    const sel = srcState.sel ? new Set(srcState.sel) : new Set(all);
    ids.forEach(i => (on ? sel.add(i) : sel.delete(i)));
    srcState.sel = all.every(i => sel.has(i)) ? null : sel;
    srcRender();
    srcSyncCount();
}

function srcWire(box) {
    const allBox = box.querySelector('#sr-all');
    if (allBox) allBox.addEventListener('change', () => srcSetSel(srcAllIds(srcState.data), allBox.checked));
    box.querySelectorAll('.sr-row:not(.vc-src)').forEach(row => {          // 「说话人」那一行自己接线（voices.js）
        const kind = row.dataset.kind;
        const sid = row.dataset.sid;
        const chk = row.querySelector('.sr-check');
        if (kind === 'channel') {
            const key = row.dataset.ckey;
            const d = srcState.data;
            const vids = key === 'channel' ? d.videos : ((d.channels || []).find(ch => ch.chain_id === key) || {}).videos;
            const eps = (vids || []).filter(v => v.task_id && v.status === 'done').map(v => v.task_id);
            chk.addEventListener('change', () => srcSetSel(eps, chk.checked));
            const fold = () => { srcState.open[key] = !srcState.open[key]; srcRender(); };
            row.querySelector('.sr-fold').addEventListener('click', fold);
            row.querySelector('.sr-caret').addEventListener('click', fold);
            const rm = row.querySelector('[data-act="remove-creator"]');
            if (rm) rm.addEventListener('click', () => srcAction('creator', key, 'remove'));
            const close = () => { const m = row.querySelector('.sr-menu'); if (m) m.open = false; };
            const all = row.querySelector('[data-act="episodes"]');          // 全部录音：频道的期、转写状态、合并
            if (all) all.addEventListener('click', () => { close(); cxShowTab('episodes'); });
            const oc = row.querySelector('[data-act="open-creator"]');        // 引用的博主：去他自己的页面（订阅、重新同步在那里）
            if (oc) oc.addEventListener('click', () => { close(); navigate('chain/' + key); });
            return;
        }
        if (chk) chk.addEventListener('change', () => srcSetSel([sid], chk.checked));
        const t = row.querySelector('.sr-title');
        if (t) t.addEventListener('click', () => (kind === 'doc' ? openSourceReader(sid, null)
            : kind === 'repo' ? openRepoReader(sid) : openTranscriptViewer(sid, null)));
        row.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => srcAction(kind, sid, b.dataset.act)));
    });
}

async function srcPost(url, opts) {
    try {
        const r = await (await fetch(url, opts)).json();
        if (r.error) { showToast(r.error); return null; }
        (r.errors || []).forEach(x => showToast(`${x.filename}: ${x.error}`));
        return r;
    } catch (e) { showToast(String(e)); return null; }
}

async function srcAction(kind, sid, act) {
    const id = srcState.id;
    const d = srcState.data || {};
    const title = ((d.docs || []).find(x => x.doc_id === sid) || (d.recordings || []).find(x => x.task_id === sid)
        || (d.videos || []).find(x => x.task_id === sid) || {}).title
        || ((d.channels || []).find(ch => ch.chain_id === sid) || {}).name
        || ((d.repos || []).find(r => r.repo_id === sid) || {}).title || '';
    if (act === 'read') {                    // 代码库：让 Daemon 读（repos.js 的弹框，估价写在按钮上）
        const m = document.querySelector(`.sr-row[data-sid="${CSS.escape(sid)}"] .sr-menu`);
        if (m) m.open = false;
        repoReadPicker(sid);
        return;
    }
    if (act === 'retry') {
        if (await srcPost(`/api/chain/${id}/sources/docs/${sid}/retry`, { method: 'POST' })) srcLoad();
        return;
    }
    if (act === 'remove') {
        if (!confirm(T(kind === 'creator' ? 'src.removeCreatorConfirm' : 'src.removeConfirm', { t: title }))) return;
        if (await srcPost(`/api/chain/${id}/sources/${sid}`, { method: 'DELETE' })) {
            if (srcState.sel) srcState.sel.delete(sid);
            srcLoad();
            if (kind === 'creator' && typeof peopleLoad === 'function') peopleLoad();
            if (typeof refreshChainDetail === 'function') refreshChainDetail();
        }
    }
}

// ---- Studio 顶上：没有频道的项目要立场 / 预测 / 综述，先得抽证据卡（按需，花钱前说清楚）----
function srcRenderCta() {
    const box = document.getElementById('nb-cards-cta');
    const d = srcState.data;
    if (!box || !d) return;
    if (d.channel || window.VERBATIM_DEMO || !d.cards_missing) { box.innerHTML = ''; return; }
    const n = d.cards_missing;
    const cost = fmtUsd(Math.max(0.01, n * 0.03 + 0.05));
    const running = d.indexing && d.analyze;
    box.innerHTML = `<div class="nb-cta">
        <div>${d.has_cards ? T('cards.ctaUpdate', { n }) : T('cards.cta', { n, cost })}</div>
        <button type="button" class="btn-primary cx-send" id="nb-cards-go" ${running ? 'disabled' : ''}>
            ${running ? T('cards.building') : T('cards.ctaBtn')}</button></div>`;
    const go = box.querySelector('#nb-cards-go');
    if (go) go.addEventListener('click', async () => {
        go.disabled = true;
        go.textContent = T('cards.building');
        if (await srcPost(`/api/chain/${srcState.id}/cards/build`, { method: 'POST' })) srcLoad();
    });
}

// ================= 添加来源 =================
// 能转写的视频 / 音频链接；其它的当网页抓
function isVideoLink(u) {
    return /(youtube\.com|youtu\.be|bilibili\.com|b23\.tv|vimeo\.com|douyin\.com|tiktok\.com|xiaoyuzhoufm\.com|podcasts\.apple\.com|soundcloud\.com|twitch\.tv|ixigua\.com|weibo\.com\/tv|v\.qq\.com)/i.test(u)
        || /\.(mp3|m4a|wav|aac|flac|ogg|opus|mp4|mov|mkv|webm|m4v)(\?|#|$)/i.test(u);
}

function isChannelLink(u) {
    return /youtube\.com\/(@|channel\/|c\/|user\/)/i.test(u) || /space\.bilibili\.com\//i.test(u);
}

function openAddSources() {
    const ov = toolOverlay('addsrc-overlay');
    if (!ov || !cx.id) return;
    nfProject = cx.id;
    ov.querySelector('#as-link').value = '';
    ov.querySelector('#as-msg').textContent = '';
    ov.querySelector('#as-text').classList.add('hidden');
    const repoForm = ov.querySelector('#as-repo');
    if (repoForm) repoForm.classList.add('hidden');
    document.getElementById('nf-wrap').classList.add('hidden');
    ov.classList.remove('hidden');
}

function closeAddSources() {
    const ov = document.getElementById('addsrc-overlay');
    if (ov) ov.classList.add('hidden');
}

async function asAddFiles(files) {
    const id = cx.id;
    const ext = f => (f.name.split('.').pop() || '').toLowerCase();
    const docs = files.filter(f => DOC_EXT.includes(ext(f)));
    const media = files.filter(f => MEDIA_EXT.includes(ext(f)) || /^(audio|video)\//.test(f.type));
    const skipped = files.length - docs.length - media.length;
    const msg = document.getElementById('as-msg');
    msg.textContent = T('src.uploading');
    let ok = 0;
    if (docs.length) {
        const fd = new FormData();
        docs.forEach(f => fd.append('files', f));
        const r = await srcPost(`/api/chain/${id}/sources/docs`, { method: 'POST', body: fd });
        if (r) ok += (r.docs || []).length;
    }
    if (media.length) {
        const fd = new FormData();
        media.forEach(f => fd.append('audios', f));
        fd.append('engine', (document.querySelector('input[name="engine"]:checked') || {}).value || 'gemini35');
        const r = await srcPost(`/api/chain/${id}/sources/upload`, { method: 'POST', body: fd });
        if (r) ok += (r.tasks || []).filter(t => t.task_id).length;
    }
    msg.textContent = '';
    if (skipped) showToast(T('as.skipped', { n: skipped }));
    if (ok) { closeAddSources(); srcLoad(); }
}

async function asAddLink() {
    const ov = document.getElementById('addsrc-overlay');
    const url = ov.querySelector('#as-link').value.trim();
    const msg = ov.querySelector('#as-msg');
    if (!/^https?:\/\//.test(url)) { msg.textContent = T('nf.badUrl'); return; }
    if (isChannelLink(url)) {
        // 整个频道：用原来那套「预览 → 挑期 → 选目的」，频道加进当前这个项目
        const wrap = document.getElementById('nf-wrap');
        wrap.classList.remove('hidden');
        chainUrl.value = url;
        msg.textContent = '';
        nfPreview(false);
        return;
    }
    msg.textContent = T('src.uploading');
    if (!isVideoLink(url)) {                 // 文章、网页、PDF 链接：抓下来当文档
        const w = await srcPost(`/api/chain/${cx.id}/sources/web`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls: [url] }) });
        msg.textContent = '';
        if (w) { ov.querySelector('#as-link').value = ''; closeAddSources(); srcLoad(); }
        return;
    }
    const r = await srcPost('/api/transcribe_urls', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: url, project_id: cx.id,
            engine: (document.querySelector('input[name="engine"]:checked') || {}).value || 'gemini35' }) });
    msg.textContent = '';
    if (r) {
        showToast(T('as.queued', { n: (r.tasks || []).length }));
        closeAddSources();
        srcLoad();
    }
}

// app.js 的 nfStart 建完频道后调这个
function nfDoneInProject(id) {
    nfProject = null;
    closeAddSources();
    if (cx.id === id) {
        if (typeof refreshChainDetail === 'function') refreshChainDetail();
        srcLoad();
        if (typeof peopleLoad === 'function') peopleLoad();
    } else {
        navigate('chain/' + id);
    }
}

(function wireAddSources() {
    const ov = toolOverlay('addsrc-overlay');
    if (!ov) return;
    const files = ov.querySelector('#as-files');
    const drop = ov.querySelector('#as-drop');
    drop.addEventListener('click', () => files.click());
    files.addEventListener('change', () => { asAddFiles([...files.files]); files.value = ''; });
    ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => asAddFiles([...(e.dataTransfer.files || [])]));
    ov.querySelector('#as-link-go').addEventListener('click', asAddLink);
    ov.querySelector('#as-link').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); asAddLink(); } });
    ov.querySelector('#as-text-btn').addEventListener('click', () => {
        const f = ov.querySelector('#as-text');
        f.classList.toggle('hidden');
        if (!f.classList.contains('hidden')) ov.querySelector('#as-text-body').focus();
    });
    ov.querySelector('#as-lib-btn').addEventListener('click', () => { closeAddSources(); openCollectionModal({ addTo: cx.id }); });
    ov.querySelector('#as-text').addEventListener('submit', async e => {
        e.preventDefault();
        const text = ov.querySelector('#as-text-body').value.trim();
        if (!text) return;
        const r = await srcPost(`/api/chain/${cx.id}/sources/docs`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, title: ov.querySelector('#as-text-title').value.trim() }) });
        if (r) {
            ov.querySelector('#as-text-body').value = '';
            ov.querySelector('#as-text-title').value = '';
            closeAddSources();
            srcLoad();
        }
    });
    // 整个项目页都能拖文件进来（跟 Gemini 一样）
    const view = document.getElementById('chain-detail-view');
    if (view && !window.VERBATIM_DEMO) {
        view.addEventListener('dragover', e => { if ([...(e.dataTransfer.types || [])].includes('Files')) e.preventDefault(); });
        view.addEventListener('drop', e => {
            if (!(e.dataTransfer.files || []).length) return;
            e.preventDefault();
            openAddSources();
            asAddFiles([...e.dataTransfer.files]);
        });
    }
})();

// ================= 看原文（在左栏里打开，跟 Gemini 一样）=================
const srcReaderCache = {};

function nbShowPane(p) {
    const ex = document.getElementById('chain-explore');
    if (ex) ex.dataset.nbActive = p;
    document.querySelectorAll('.nb-switch [data-nb]').forEach(b => b.classList.toggle('on', b.dataset.nb === p));
}

function readerOpen(title, sub, origHref) {
    const box = document.getElementById('src-reader');
    box.querySelector('#src-reader-name').textContent = title || '';
    box.querySelector('#src-reader-sub').textContent = sub ? ' · ' + sub : '';
    const orig = box.querySelector('#src-reader-orig');
    orig.classList.toggle('hidden', !origHref);
    if (origHref) orig.href = origHref;
    box.classList.remove('hidden');
    document.getElementById('chain-explore').classList.remove('src-min');   // 收起着也得展开，不然原文看不见
    document.getElementById('cx-sources').classList.add('hidden');
    document.querySelector('#chain-explore .nb-src').classList.add('reading');
    if (window.innerWidth < 1080) nbShowPane('sources');
    return box.querySelector('#src-reader-body');
}

function readerFocus(body, sel) {
    const el = sel && body.querySelector(sel);
    if (el) {
        el.classList.add('hl');
        setTimeout(() => el.scrollIntoView({ block: 'center' }), 30);
    } else {
        body.scrollTop = 0;
    }
}

async function openSourceReader(docId, passageIndex) {
    let d = srcReaderCache[docId];
    if (!d) {
        try { d = await (await fetch(`/api/docs/${docId}`)).json(); } catch { return; }
        if (d.error) { showToast(d.error); return; }
        srcReaderCache[docId] = d;
    }
    const m = d.meta;
    const body = readerOpen(m.title, m.pages ? T('src.pages', { n: m.pages }) : (m.url ? srcHost(m.url) : ''),
        m.url && m.ext === 'web' ? m.url : m.filename ? `/api/docs/${docId}/original` : '');
    let lastPage = null;
    let lastHead = null;
    body.innerHTML = (d.passages || []).map(p => {
        let pre = '';
        if (p.page != null && p.page !== lastPage) {
            pre += `<div class="rd-page">${T('reader.page', { n: p.page })}</div>`;
            lastPage = p.page;
        }
        if (p.heading && p.heading !== lastHead) {
            pre += `<h3 class="rd-h">${escapeHtml(p.heading)}</h3>`;
            lastHead = p.heading;
        }
        return `${pre}<div class="rd-par md-body" data-pi="${p.i}">${renderMarkdown(p.text)}</div>`;
    }).join('') || `<div class="md-body">${renderMarkdown(d.markdown || '')}</div>`;
    readerFocus(body, passageIndex != null ? `.rd-par[data-pi="${passageIndex}"]` : null);
}

async function openTranscriptViewer(taskId, sec) {
    let d;
    try { d = await (await fetch(`/api/history/${taskId}`)).json(); } catch { return; }
    if (!d || d.error) { showToast((d && d.error) || T('common.couldNotLoad')); return; }
    const segs = d.segments || [];
    const body = readerOpen(d.ai_title || d.filename || '', d.duration_seconds ? formatDuration(d.duration_seconds) : '', '');
    let hit = null;
    if (sec != null) {
        segs.forEach((s, i) => { if (parseTimestampToSeconds(s.timestamp || '0:00') <= sec + 0.5) hit = i; });
    }
    body.innerHTML = `<button type="button" class="cx-link rd-open" data-go="detail/${escapeHtml(taskId)}${sec != null ? '/t/' + sec : ''}">${T('nb.openFull')}</button>`
        + segs.map((s, i) => `<div class="rd-seg" data-si="${i}"><span class="rd-ts">${escapeHtml(s.timestamp || '')}</span>
            <span>${escapeHtml(s.text || '')}</span></div>`).join('');
    body.querySelector('.rd-open').addEventListener('click', e => navigate(e.target.dataset.go));
    readerFocus(body, hit != null ? `.rd-seg[data-si="${hit}"]` : null);
    if (typeof vcDecorateTranscript === 'function') vcDecorateTranscript(body, taskId, segs);   // 每句是谁说的、▶ 听
}

function closeSourceReader() {
    const box = document.getElementById('src-reader');
    if (!box) return;
    box.classList.add('hidden');
    document.getElementById('cx-sources').classList.remove('hidden');
    const pane = document.querySelector('#chain-explore .nb-src');
    if (pane) pane.classList.remove('reading');
}

(function wireReader() {
    const x = document.getElementById('src-reader-x');
    if (x) x.addEventListener('click', closeSourceReader);
    document.querySelectorAll('.nb-switch [data-nb]').forEach(b => b.addEventListener('click', () => nbShowPane(b.dataset.nb)));
    const back = document.getElementById('nb-back-chat');
    if (back) back.addEventListener('click', () => cxShowTab('ask'));
})();

// 出处 id 里的段落号：d10319147-3 → 3
function passageIndexOf(cid) {
    const m = String(cid || '').match(/-(\d+)$/);
    return m ? +m[1] : null;
}

// ================= 项目首页：卡片右上角 ⋮（改名 / 换表情 / 加入分组 / 置顶 / 隐藏 / 删除）=================
const PJM_ICONS = {
    rename: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M14 6l4 4"/>',
    emoji: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 14a4 4 0 0 0 7 0"/><path d="M9 9.5h.01M15 9.5h.01"/>',
    col: '<path d="M12 3.5l4 7H8z"/><circle cx="7.5" cy="16.5" r="3.5"/><rect x="13.5" y="13" width="7" height="7" rx="1.2"/>',
    pin: '<path d="M9 3h6l-1 6 4 4H6l4-4z"/><path d="M12 13v8"/>',
    hide: '<path d="M3 3l18 18"/><path d="M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 9 6 9 6a17 17 0 0 1-3.2 3.7M6.6 6.6C4.3 8.1 3 12 3 12s4 6 9 6a9 9 0 0 0 4.4-1.1"/>',
    show: '<path d="M3 12s4-6 9-6 9 6 9 6-4 6-9 6-9-6-9-6z"/><circle cx="12" cy="12" r="2.5"/>',
    del: '<path d="M4 7h16"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 13h9l1-13"/>',
};
const pjIcon = k => `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PJM_ICONS[k]}</svg>`;
const PJ_EMOJIS = ('📓 📔 📒 📕 📗 📘 📙 📚 📖 🗂️ 📁 🗃️ 📝 ✍️ 📌 🔖 🧠 💡 🔍 🔬 🧪 📊 📈 📉 💼 🏛️ ⚖️ 🌍 🗺️ 🏔️ 🌊 🌱 🌸 🔥 ⭐ ✨ '
    + '🎯 🎓 🎙️ 🎧 🎬 📺 📻 📷 🎵 🎨 💻 📱 🤖 🧩 ⚙️ 🛠️ 🚀 ✈️ 🏠 🏙️ 💰 🏦 📰 💬 🗣️ 👥 ❤️ 🧘 🏃 🍳 ☕ 🐱 🐶 🦊 🐼 🌙 ☀️ 🤔 😂 🥰 🇨🇳 🇺🇸').split(' ');
let pjCols = [];

function pjChain(id) { return (typeof lastChains !== 'undefined' ? lastChains : []).find(c => c.id === id) || (lastChainDetail && lastChainDetail.id === id ? lastChainDetail : null); }

function pjPop(anchor, html, cls = '') {
    pjPopClose();
    const pop = document.createElement('div');
    pop.id = 'pj-pop';
    pop.className = 'pj-pop ' + cls;
    pop.innerHTML = html;
    document.body.appendChild(pop);
    const r = anchor.getBoundingClientRect();
    const w = pop.offsetWidth, h = pop.offsetHeight;
    let left = Math.min(r.right - w, window.innerWidth - w - 12);
    if (left < 12) left = Math.min(r.left, window.innerWidth - w - 12);
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 6);
    pop.style.left = Math.max(12, left) + 'px';
    pop.style.top = top + 'px';
    return pop;
}
function pjPopClose() { const p = document.getElementById('pj-pop'); if (p) p.remove(); }
document.addEventListener('click', e => {
    const p = document.getElementById('pj-pop');
    if (p && !p.contains(e.target) && !e.target.closest('.pj-more, .cp-face-btn')) pjPopClose();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') pjPopClose(); });
window.addEventListener('scroll', pjPopClose, { passive: true });

function pjMenu(btn, id) {
    const c = pjChain(id);
    if (!c) return;
    if (document.getElementById('pj-pop') && document.getElementById('pj-pop').dataset.for === id) { pjPopClose(); return; }
    const active = !['done', 'failed', 'cancelled'].includes(c.stage);
    const item = (act, icon, label, cls = '') => `<button type="button" class="pj-mi ${cls}" data-act="${act}">${pjIcon(icon)}<span>${escapeHtml(label)}</span></button>`;
    const pop = pjPop(btn, `
        ${item('rename', 'rename', T('pjm.rename'))}
        ${item('emoji', 'emoji', T('pjm.emoji'))}
        ${item('col', 'col', T('pjm.addCol'))}
        ${item('pin', 'pin', T(c.pin != null ? 'pjm.unpin' : 'pjm.pin'))}
        ${active ? '' : item('hide', c.hidden ? 'show' : 'hide', T(c.hidden ? 'pjm.unhide' : 'pjm.hide'))}
        ${active ? '' : item('del', 'del', T('pjm.delete'), 'bad')}`, 'pj-menu');
    pop.dataset.for = id;
    pop.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', e => {
        e.stopPropagation();
        const act = b.dataset.act;
        if (act === 'rename') { pjPopClose(); pjRename(id); }
        else if (act === 'emoji') pjEmojiPicker(btn, id);
        else if (act === 'col') pjColPicker(btn, id);
        else if (act === 'pin') { pjPopClose(); pjSetPref(id, { pinned: c.pin == null }); }
        else if (act === 'hide') { pjPopClose(); hideChain(id, !c.hidden); }
        else if (act === 'del') { pjPopClose(); deleteChain(id, false); }
    }));
}

async function pjSetPref(id, body) {
    try {
        const r = await fetch('/api/chains/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, ...body }) });
        if (!r.ok) throw new Error((await r.json()).error || r.status);
    } catch (e) { showToast(String(e.message || e)); return; }
    if (typeof loadChains === 'function') loadChains();
    if (typeof chainDetailId !== 'undefined' && chainDetailId === id && typeof refreshChainDetail === 'function') refreshChainDetail();
}

// 卡片上就地改名：回车 / 失焦保存，Esc 放弃
function pjRename(id) {
    const card = document.querySelector(`.pj-card[data-id="${id}"]`);
    const name = card && card.querySelector('.pj-name');
    if (!name) return;
    const old = name.textContent;
    const input = document.createElement('input');
    input.className = 'pj-title-edit';
    input.value = old;
    input.maxLength = 60;
    name.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async save => {
        if (done) return;
        done = true;
        const v = input.value.trim();
        if (save && v && v !== old) {
            try {
                const r = await (await fetch(`/api/chain/${id}/rename`, { method: 'POST',
                    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: v }) })).json();
                if (!r.ok) throw new Error(r.error);
            } catch (e) { showToast(String(e.message || e)); }
        }
        loadChains();
    };
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('click', e => e.stopPropagation());
}

// 换表情：常用的一格一格点，也能自己输入任意一个（macOS 上 ⌃⌘空格 叫出系统表情面板）
function pjEmojiPicker(anchor, id) {
    const c = pjChain(id) || {};
    const pop = pjPop(anchor, `
        <div class="pj-emoji-grid">${PJ_EMOJIS.map(e => `<button type="button" class="pj-em${c.emoji === e ? ' on' : ''}" data-e="${e}">${e}</button>`).join('')}</div>
        <div class="pj-emoji-foot">
            <input class="search-input" id="pj-emoji-in" maxlength="16" placeholder="${escapeHtml(T('pjm.emojiPh'))}">
            ${c.emoji ? `<button type="button" class="cx-link" id="pj-emoji-reset">${T('pjm.emojiReset')}</button>` : ''}
        </div>`, 'pj-emoji-pop');
    pop.dataset.for = id;
    const pick = e => { pjPopClose(); pjSetPref(id, { emoji: e }); };
    pop.querySelectorAll('[data-e]').forEach(b => b.addEventListener('click', ev => { ev.stopPropagation(); pick(b.dataset.e); }));
    const input = pop.querySelector('#pj-emoji-in');
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && input.value.trim()) { e.preventDefault(); pick(input.value.trim()); } });
    input.addEventListener('input', () => {        // 系统表情面板插进来的就直接用
        const v = input.value.trim();
        if (v && /\p{Extended_Pictographic}/u.test(v) && !/[A-Za-z0-9一-鿿]/.test(v)) pick(v);
    });
    const reset = pop.querySelector('#pj-emoji-reset');
    if (reset) reset.addEventListener('click', ev => { ev.stopPropagation(); pick(''); });
    setTimeout(() => input.focus(), 30);
}

// 加入分组：勾选放进 / 拿出，也能新建
async function pjLoadCols() {
    try { pjCols = (await (await fetch('/api/groups')).json()).items || []; } catch { pjCols = []; }
    return pjCols;
}
async function pjColPicker(anchor, id) {
    const c = pjChain(id) || {};
    await pjLoadCols();
    const pop = pjPop(anchor, `
        <div class="pj-col-h">${T('pjm.colTitle')}</div>
        <div class="pj-col-list">${pjCols.map(col => `<label class="pj-col-it"><input type="checkbox" data-col="${col.id}"
            ${(c.collections || []).includes(col.id) ? 'checked' : ''}><span>${escapeHtml(col.name)}</span></label>`).join('')
            || `<p class="cx-muted pj-col-empty">${T('pjm.colEmpty')}</p>`}</div>
        <form class="pj-col-new"><input class="search-input" maxlength="60" placeholder="${escapeHtml(T('pjm.colNewPh'))}">
            <button type="submit" class="btn-secondary">${T('pjm.colNew')}</button></form>`, 'pj-col-pop');
    pop.dataset.for = id;
    pop.querySelectorAll('[data-col]').forEach(cb => cb.addEventListener('change', async () => {
        await fetch(`/api/groups/${cb.dataset.col}`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, add: cb.checked }) });
        loadChains();
    }));
    pop.querySelector('form').addEventListener('submit', async e => {
        e.preventDefault();
        const name = e.target.querySelector('input').value.trim();
        if (!name) return;
        await fetch('/api/groups', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, add: id }) });
        pjPopClose();
        await pjLoadCols();
        loadChains();
        showToast(T('pjm.colAdded', { name }));
    });
    pop.addEventListener('click', e => e.stopPropagation());
}

// 首页标题下的分组筛选：全部 / 各个分组
function pjRenderFilters(list) {
    const box = document.getElementById('pj-filters');
    if (!box) return;
    if (!pjCols.length) { box.innerHTML = ''; box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    const cnt = colId => list.filter(c => (c.collections || []).includes(colId)).length;
    const chip = (id, label, n) => `<button type="button" class="pj-chip${pjFilter === id ? ' on' : ''}" data-f="${id || ''}">${escapeHtml(label)}<span>${n}</span></button>`;
    const cur = pjCols.find(c => c.id === pjFilter);
    box.innerHTML = chip(null, T('pjm.all'), list.length) + pjCols.map(c => chip(c.id, c.name, cnt(c.id))).join('')
        + (cur ? `<button type="button" class="cx-link pj-col-del">${T('pjm.colDelete')}</button>` : '');
    box.querySelectorAll('[data-f]').forEach(b => b.addEventListener('click', () => {
        pjFilter = b.dataset.f || null;
        renderChains(lastChains);
    }));
    const del = box.querySelector('.pj-col-del');
    if (del) del.addEventListener('click', async () => {
        if (!confirm(T('pjm.colDeleteConfirm', { name: cur.name }))) return;
        await fetch(`/api/groups/${cur.id}`, { method: 'DELETE' });
        pjFilter = null;
        await pjLoadCols();
        renderChains(lastChains);
    });
}
pjLoadCols().then(() => { if (typeof lastChains !== 'undefined' && lastChains.length) renderChains(lastChains); });

// ================= 左右两栏可以收起（跟 Gemini 一样），收起的状态记住 =================
(function wireFold() {
    const nb = document.getElementById('chain-explore');
    if (!nb) return;
    const key = side => 'nbFold.' + side;
    const sync = () => nb.querySelectorAll('.nb-fold').forEach(b => {
        const folded = nb.classList.contains(b.dataset.fold + '-min');
        b.title = T(folded ? 'nb.unfold' : 'nb.fold');
        b.setAttribute('aria-label', b.title);
        b.setAttribute('aria-expanded', folded ? 'false' : 'true');
    });
    ['src', 'studio'].forEach(side => {
        try { if (localStorage.getItem(key(side)) === '1') nb.classList.add(side + '-min'); } catch { /* 无所谓 */ }
    });
    nb.querySelectorAll('.nb-fold').forEach(b => b.addEventListener('click', () => {
        const side = b.dataset.fold;
        const on = nb.classList.toggle(side + '-min');
        try { localStorage.setItem(key(side), on ? '1' : '0'); } catch { /* 无所谓 */ }
        sync();
    }));
    // 收起的栏整条都能点开
    nb.querySelectorAll('.nb-src, .nb-studio').forEach(p => p.addEventListener('click', e => {
        const side = p.classList.contains('nb-src') ? 'src' : 'studio';
        if (!nb.classList.contains(side + '-min') || e.target.closest('.nb-fold')) return;
        nb.classList.remove(side + '-min');
        try { localStorage.setItem(key(side), '0'); } catch { /* 无所谓 */ }
        sync();
    }));
    sync();
    document.addEventListener('langchange', sync);
})();

// ================= 找来源（discover.py）：搜网页 / YouTube / B 站，结果摆在左栏，勾了再加 =================
let dscState = { kind: 'web', items: [], picked: new Set(), busy: false, query: '', cost: 0 };

function dscReset() {
    dscState = { kind: dscState.kind, items: [], picked: new Set(), busy: false, query: '', cost: 0 };
    const res = document.getElementById('dsc-res');
    if (res) { res.innerHTML = ''; res.classList.add('hidden'); }
    const q = document.getElementById('dsc-q');
    if (q) q.value = '';
}

function dscFmtViews(n) {
    n = Number(n) || 0;
    if (!n) return '';
    return T('dsc.views', { n: n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? Math.round(n / 1e3) + 'K' : n });
}

function dscDurSec(d) {
    return String(d || '').split(':').reduce((a, x) => a * 60 + (parseInt(x, 10) || 0), 0);
}

function dscRender() {
    const res = document.getElementById('dsc-res');
    if (!res) return;
    res.classList.toggle('hidden', !dscState.busy && !dscState.items.length && !dscState.error && !dscState.query);
    if (dscState.busy) {
        res.innerHTML = `<div class="dsc-busy"><span class="st-spin" aria-hidden="true"></span>${T('dsc.searching', { q: escapeHtml(dscState.query) })}</div>`;
        return;
    }
    if (dscState.error) {
        res.innerHTML = `<div class="dsc-head"><span class="cx-err">${escapeHtml(dscState.error)}</span>
            <button type="button" class="dsc-x" aria-label="${escapeHtml(T('common.close'))}">×</button></div>`;
        res.querySelector('.dsc-x').addEventListener('click', dscReset);
        return;
    }
    if (!dscState.query) { res.innerHTML = ''; return; }
    const items = dscState.items;
    const free = items.filter(it => !it.have);
    const picked = items.filter(it => dscState.picked.has(it.url));
    const vids = picked.filter(it => it.type === 'video');
    const secs = vids.reduce((a, it) => a + dscDurSec(it.duration), 0);
    const dur = secs ? T('dsc.toTranscribe', { d: secs >= 3600 ? `${Math.floor(secs / 3600)}h ${Math.round(secs % 3600 / 60)}m` : `${Math.max(1, Math.round(secs / 60))} min` }) : '';
    res.innerHTML = `
        <div class="dsc-head"><b>${T(items.length ? 'dsc.found' : 'dsc.none', { n: items.length, q: escapeHtml(dscState.query) })}</b>
            <button type="button" class="dsc-x" aria-label="${escapeHtml(T('common.close'))}">×</button></div>
        ${items.length ? `<label class="sr-all"><span>${T('src.selectAll')}</span>
            <input type="checkbox" id="dsc-all" ${free.length && free.every(it => dscState.picked.has(it.url)) ? 'checked' : ''} ${free.length ? '' : 'disabled'}></label>` : ''}
        <ul class="dsc-list">${items.map((it, i) => {
            const meta = (it.type === 'video' ? [it.site, it.channel, it.duration, dscFmtViews(it.views)] : [it.site]).filter(Boolean).join(' · ');
            return `<li class="dsc-it${it.have ? ' have' : ''}">
                <span class="sr-svg" data-k="${it.type === 'video' ? 'video' : 'web'}">${srcSvg(it.type === 'video' ? 'video' : 'web')}</span>
                <div class="dsc-body">
                    <a class="dsc-title" href="${safeUrl(it.url)}" target="_blank" rel="noopener" title="${escapeHtml(it.url)}">${escapeHtml(it.title || it.url)}</a>
                    <div class="dsc-meta">${escapeHtml(meta)}${it.have ? ` · <b>${T('dsc.have')}</b>` : ''}</div>
                    ${it.snippet ? `<div class="dsc-snip">${escapeHtml(it.snippet)}</div>` : ''}
                </div>
                <input type="checkbox" class="sr-check" data-i="${i}" ${it.have ? 'disabled' : ''}
                    ${dscState.picked.has(it.url) ? 'checked' : ''} aria-label="${escapeHtml(it.title || it.url)}">
            </li>`;
        }).join('')}</ul>
        ${items.length ? `<div class="dsc-foot">
            <span class="cx-muted">${escapeHtml([dur, dscState.cost ? T('dsc.cost', { c: fmtUsd(dscState.cost) }) : ''].filter(Boolean).join(' · '))}</span>
            <button type="button" class="dsc-add" id="dsc-add" ${picked.length ? '' : 'disabled'}>${T('dsc.add', { n: picked.length })}</button></div>` : ''}`;
    res.querySelector('.dsc-x').addEventListener('click', dscReset);
    const all = res.querySelector('#dsc-all');
    if (all) all.addEventListener('change', () => {
        free.forEach(it => (all.checked ? dscState.picked.add(it.url) : dscState.picked.delete(it.url)));
        dscRender();
    });
    res.querySelectorAll('.dsc-list .sr-check').forEach(c => c.addEventListener('change', () => {
        const it = items[+c.dataset.i];
        if (c.checked) dscState.picked.add(it.url); else dscState.picked.delete(it.url);
        dscRender();
    }));
    const add = res.querySelector('#dsc-add');
    if (add) add.addEventListener('click', dscAdd);
}

async function dscSearch(e) {
    e.preventDefault();
    const q = document.getElementById('dsc-q').value.trim();
    if (!q || dscState.busy || !cx.id) return;
    const id = cx.id;
    Object.assign(dscState, { busy: true, query: q, items: [], picked: new Set(), error: '', cost: 0 });
    dscRender();
    try {
        const r = await fetch(`/api/chain/${id}/discover`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: q, kind: dscState.kind }) });
        const j = await r.json();
        if (cx.id !== id) return;
        if (!r.ok) throw new Error(j.error || r.status);
        dscState.items = j.items || [];
        dscState.cost = j.cost_usd || 0;
    } catch (err) {
        dscState.error = String(err.message || err);
    } finally {
        dscState.busy = false;
    }
    dscRender();
}

async function dscAdd() {
    const picked = dscState.items.filter(it => dscState.picked.has(it.url));
    const web = picked.filter(it => it.type !== 'video').map(it => it.url);
    const vids = picked.filter(it => it.type === 'video').map(it => it.url);
    const btn = document.getElementById('dsc-add');
    if (btn) btn.disabled = true;
    let added = 0;
    if (web.length) {
        const r = await srcPost(`/api/chain/${cx.id}/sources/web`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls: web }) });
        if (r) added += r.added || 0;
    }
    if (vids.length) {
        const r = await srcPost('/api/transcribe_urls', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ urls: vids.join('\n'), project_id: cx.id,
                engine: (document.querySelector('input[name="engine"]:checked') || {}).value || 'gemini35' }) });
        if (r) added += (r.tasks || []).length;
    }
    if (added) showToast(T('dsc.added', { n: added }));
    dscReset();
    srcLoad();
}

(function wireDiscover() {
    const form = document.getElementById('dsc-form');
    if (!form) return;
    form.addEventListener('submit', dscSearch);
    form.querySelectorAll('[data-kind]').forEach(b => b.addEventListener('click', () => {
        dscState.kind = b.dataset.kind;
        form.querySelectorAll('[data-kind]').forEach(x => {
            x.classList.toggle('on', x === b);
            x.setAttribute('aria-checked', x === b ? 'true' : 'false');
        });
        document.getElementById('dsc-q').focus();
    }));
    const add = document.getElementById('nb-add');
    if (add) add.addEventListener('click', openAddSources);
})();

// 直接打开 #/chain/<id> 时 app.js 先路由、本文件后加载：左栏还没人来填，这里补一次
if (typeof cx !== 'undefined' && cx.id) srcLoad();
