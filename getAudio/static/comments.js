// ========== B 站评论区（后端 comments.py，ADR 0019）==========
// 两处：① 来源栏阅读器里这一期的「评论区」块（来源那一行 ⋯「抓评论区」）；
// ② 工作台格子「评论区整理」→ 中间弹框选期、篇幅、侧重 → 后台（没抓的先抓）→ 列表里一条，点开是概述 + 带出处的原话。
// 出处圆点点了：左栏打开那一期，评论区展开到那一条。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd / renderMarkdown）、explore.js（cx / stPickOverlay）、
// study.js（stIcon / stLoad / stState）、projects.js（srcState / srcPost / openTranscriptViewer）。

const CM_PAGE = 30;                       // 阅读器里一次画多少楼
const CM_LENGTHS = ['short', 'medium', 'long'];

function cmDate(sec) {
    if (!sec) return '';
    const d = new Date(sec * 1000);
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function cmMeta(r) {
    return [r.user, T('cm.likes', { n: r.like }), cmDate(r.ctime)].filter(Boolean).join(' · ');
}

// 抓的进度：先「第 230 / 500 楼」，再「楼中楼：第 12 / 87 楼」，后面小字写 B 站报的总数（有上限，不会都抓）
function cmFetchText(job) {
    const total = job.reported ? ' · ' + T('cm.biliTotal', { n: job.reported.toLocaleString() }) : '';
    if (job.stage === 'top') return T('cm.fetchTop', { i: job.i.toLocaleString(), n: job.n.toLocaleString() }) + total;
    if (job.stage === 'replies') return T('cm.fetchReplies', { i: job.i.toLocaleString(), n: job.n.toLocaleString() }) + total;
    return T('cm.fetchingStart');
}

// ---------------- 来源栏阅读器：评论区 ----------------
async function commentsFetch(tid) {
    const r = await srcPost(`/api/comments/${encodeURIComponent(tid)}/fetch`, { method: 'POST' });
    if (!r) return;
    showToast(T('cm.fetchStarted'));
    // 正开着这个视频的地方（阅读器、转写页、贴链接的弹窗）都刷新
    document.querySelectorAll('#src-reader-comments, .cm-list-box').forEach(box => {
        if (box.dataset.key === tid) commentsBlock(box, tid);
    });
}

function readerComments(tid, focusId) {
    return commentsBlock(document.getElementById('src-reader-comments'), tid, focusId);
}

// 一期的评论区（阅读器和转写页共用）：没抓 → 一行「抓」；在抓 → 进度；抓好 → 折叠的楼列表。返回接口数据。
async function commentsBlock(box, tid, focusId) {
    if (!box) return null;
    if (box.dataset.key !== tid) { box.dataset.shown = String(CM_PAGE); box.dataset.open = ''; }
    box.dataset.key = tid;
    clearTimeout(box._poll);
    const seq = (box._seq = (box._seq || 0) + 1);      // 同一期连着打开两次（点出处时）：只画最后一次的
    let d;
    try { d = await (await fetch(`/api/comments/${encodeURIComponent(tid)}`)).json(); } catch { return; }
    if (box.dataset.key !== tid || seq !== box._seq) return;
    if (!d || !d.bili) { box.innerHTML = ''; box.classList.add('hidden'); return d; }
    box.classList.remove('hidden');
    const job = d.job;
    if (job && job.running) {
        box.innerHTML = `<div class="cm-line"><span class="st-spin" aria-hidden="true"></span>${escapeHtml(cmFetchText(job))}</div>`;
        box._poll = setTimeout(() => commentsBlock(box, tid, focusId), 2000);
        return d;
    }
    const err = job && job.error ? `<div class="cm-line cx-err">${escapeHtml(job.error === 'bili_login' ? T('cm.noLogin') : T('cm.failed'))}</div>` : '';
    if (!d.has) {
        box.innerHTML = `${err}<div class="cm-line">${escapeHtml(T('cm.notYet'))}
            <button type="button" class="cx-link" data-cm="fetch">${escapeHtml(T('cm.fetch'))}</button></div>`;
        box.querySelector('[data-cm="fetch"]').addEventListener('click', () => commentsFetch(tid));
        return d;
    }
    const threads = d.threads || [];
    let shown = Math.max(CM_PAGE, +box.dataset.shown || CM_PAGE);
    if (focusId) {                           // 从出处点过来：画到那一楼为止，并展开它的回复
        const at = threads.findIndex(th => th.top.id === focusId || th.replies.some(r => r.id === focusId));
        if (at >= shown) shown = at + 1;
        box.dataset.open = at >= 0 ? threads[at].top.id : '';
    }
    box.dataset.shown = String(shown);
    const info = d.info || {};
    const reply = r => `<div class="cm-c cm-r" data-cmid="${escapeHtml(r.id)}"><div class="cm-m">${escapeHtml(cmMeta(r))}</div>
        <div class="cm-t">${escapeHtml(r.text)}</div></div>`;
    const thread = th => {
        const open = box.dataset.open === th.top.id;
        return `<div class="cm-th"><div class="cm-c" data-cmid="${escapeHtml(th.top.id)}"><div class="cm-m">${escapeHtml(cmMeta(th.top))}</div>
            <div class="cm-t">${escapeHtml(th.top.text)}</div></div>
            ${th.replies.length ? `<details class="cm-rs" data-root="${escapeHtml(th.top.id)}"${open ? ' open' : ''}>
                <summary>${escapeHtml(T('cm.nReplies', { n: th.replies.length }))}</summary>${open ? th.replies.map(reply).join('') : ''}</details>` : ''}</div>`;
    };
    if (box.dataset.wasOpen == null && box.closest('.cm-modal')) box.dataset.wasOpen = '1';   // 贴链接的弹窗：点的就是「下载评论区」，默认展开
    box.innerHTML = `${err}<details class="cm-box"${focusId || box.dataset.wasOpen ? ' open' : ''}>
        <summary>${escapeHtml(T('cm.inReader', { n: (info.count || 0).toLocaleString(), t: info.fetched_at || '' }))}</summary>
        ${info.capped && info.limits ? `<div class="cx-muted cm-capnote">${escapeHtml(T('cm.capped', { top: info.limits.top, rep: info.limits.replies }))}</div>` : ''}
        <div class="cm-tools"><span class="cx-muted">${escapeHtml(T('cm.sorted'))}</span>
            <button type="button" class="cx-link" data-cm="fetch">${escapeHtml(T('cm.refetch'))}</button></div>
        <div class="cm-list">${threads.slice(0, shown).map(thread).join('')}</div>
        ${threads.length > shown ? `<button type="button" class="cx-link cm-more" data-cm="more">${escapeHtml(T('cm.more', { n: threads.length - shown }))}</button>` : ''}
    </details>`;
    const det = box.querySelector('.cm-box');
    det.addEventListener('toggle', () => { box.dataset.wasOpen = det.open ? '1' : ''; });
    box.querySelector('[data-cm="fetch"]').addEventListener('click', () => commentsFetch(tid));
    const more = box.querySelector('[data-cm="more"]');
    if (more) more.addEventListener('click', () => { box.dataset.shown = String(shown + CM_PAGE); commentsBlock(box, tid); });
    box.querySelectorAll('.cm-rs').forEach(rs => rs.addEventListener('toggle', () => {     // 回复点开才画（一楼可能几百条）
        if (rs.open && !rs.querySelector('.cm-r')) {
            const th = threads.find(x => x.top.id === rs.dataset.root);
            if (th) rs.insertAdjacentHTML('beforeend', th.replies.map(reply).join(''));
        }
    }));
    if (focusId) {
        const el = box.querySelector(`[data-cmid="${CSS.escape(focusId)}"]`);
        if (el) {
            el.classList.add('hl');
            setTimeout(() => el.scrollIntoView({ block: 'center', behavior: 'smooth' }), 60);
        }
    }
    return d;
}

// ---------------- 工作台：评论区整理 ----------------
async function commentsPicker() {
    const ov = stPickOverlay();
    const head = `<button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="cm-t">${stIcon('comments', 20)}<span>${escapeHtml(T('st.k.comments'))}</span></h2>
        <p class="cx-muted">${escapeHtml(T('cm.desc'))}</p>`;
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick vs-pick pod-pick cm-pick" role="dialog" aria-modal="true" aria-labelledby="cm-t">${head}
        <div class="vp-foot"><span class="rd-pick-s"><span class="st-spin" aria-hidden="true"></span>${T('cx.loading')}</span></div></div>`;
    ov.classList.remove('hidden');
    let eps = [];
    try { eps = (await (await fetch(`/api/chain/${cx.id}/comments/episodes`)).json()).episodes || []; } catch { /* 按空的处理 */ }
    if (ov.classList.contains('hidden')) return;
    const modal = ov.querySelector('.cm-pick');
    if (!eps.length) {
        modal.innerHTML = `${head}<div class="vp-foot"><span class="rd-pick-s">${escapeHtml(T('cm.none'))}</span></div>`;
        return;
    }
    // 默认勾：左栏勾了的那几期；左栏全选（或没法判断）时只有一期就勾它，多期全勾
    const sel = typeof srcState !== 'undefined' && srcState.sel;
    const pre = new Set(eps.map(e => e.task_id).filter(t => !sel || sel.has(t)));
    const st = { length: 'medium' };
    modal.innerHTML = `${head}
        <form id="cm-form">
            ${eps.length > 1 ? `<label class="vs-ep vs-all"><input type="checkbox" id="cm-all"><span class="vs-ep-t">${escapeHtml(T('vs.all', { n: eps.length }))}</span></label>` : ''}
            <div class="vs-eps">${eps.map(e => `<label class="vs-ep"><input type="checkbox" value="${escapeHtml(e.task_id)}"${pre.has(e.task_id) ? ' checked' : ''}>
                <span class="vs-ep-t">${escapeHtml(e.title)}</span>
                <span class="vs-ep-m">${escapeHtml([e.who, e.count == null ? T('cm.willFetch') : T('cm.nComments', { n: e.count.toLocaleString() })].filter(Boolean).join(' · '))}</span>
            </label>`).join('')}</div>
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('cm.length'))}</span><div class="pod-lens" id="cm-lens" role="radiogroup"></div></div>
            <label class="pod-f"><span class="pod-l">${escapeHtml(T('st.focus'))}</span>
                <textarea id="cm-focus" rows="2" maxlength="300" placeholder="${escapeHtml(T('cm.focusPh'))}"></textarea></label>
            <div class="vp-foot"><span class="rd-pick-s" id="cm-msg"></span>
                <button type="submit" class="rd-pick-go primary" id="cm-go" disabled>${T('rd.generate')}</button></div>
        </form>`;
    const boxes = [...modal.querySelectorAll('.vs-eps input')];
    const all = modal.querySelector('#cm-all');
    const picked = () => boxes.filter(b => b.checked).map(b => b.value);
    let estSeq = 0;
    const sync = async () => {
        const on = picked();
        if (all) { all.checked = on.length === boxes.length; all.indeterminate = on.length > 0 && on.length < boxes.length; }
        const go = modal.querySelector('#cm-go');
        go.disabled = !on.length;
        const todo = eps.filter(e => on.includes(e.task_id) && e.count == null).length;
        modal.querySelector('#cm-msg').textContent = !on.length ? T('vs.pickSome') : todo ? T('cm.fetchFirst', { n: todo }) : T('vs.picked', { n: on.length });
        if (!on.length) { go.textContent = T('rd.generate'); return; }
        const seq = ++estSeq;
        let est = null;
        try { est = await (await fetch(`/api/chain/${cx.id}/studio/comments/estimate?tids=${on.map(encodeURIComponent).join(',')}`)).json(); } catch { /* 没估价就不写 */ }
        if (seq !== estSeq || !est) return;
        go.textContent = est.subscription ? T('cm.goSub') : est.usd ? T('vs.goCost', { cost: fmtUsd(Math.max(0.01, est.usd)) }) : T('rd.generate');
    };
    const renderLens = () => {
        const box = modal.querySelector('#cm-lens');
        box.innerHTML = CM_LENGTHS.map(l => `<button type="button" class="pod-len${st.length === l ? ' on' : ''}" data-l="${l}"
            role="radio" aria-checked="${st.length === l}">${escapeHtml(T('cm.len.' + l))}</button>`).join('');
        box.querySelectorAll('.pod-len').forEach(b => b.addEventListener('click', () => { st.length = b.dataset.l; renderLens(); }));
    };
    renderLens();
    boxes.forEach(b => b.addEventListener('change', sync));
    if (all) all.addEventListener('change', () => { boxes.forEach(b => { b.checked = all.checked; }); sync(); });
    sync();
    modal.querySelector('#cm-form').addEventListener('submit', async e => {
        e.preventDefault();
        const go = modal.querySelector('#cm-go');
        go.disabled = true;
        let r;
        try {
            r = await (await fetch(`/api/chain/${cx.id}/studio/comments`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_ids: picked(), length: st.length, focus: modal.querySelector('#cm-focus').value.trim(), lang: currentLang }) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) {
            modal.querySelector('#cm-msg').textContent = r.error === 'bili_login' ? T('cm.noLogin') : r.error;
            go.disabled = false;
            return;
        }
        ov.classList.add('hidden');
        showToast(T('cm.started'));
        stLoad();
    });
}

function commentsTitle(o) {
    return T('st.k.comments') + ' · ' + ((o.title || (o.titles || []).filter(Boolean).join(' / ')) || '').slice(0, 60);
}

function commentsProgress(o) {
    const p = o.progress || {};
    return p.stage === 'fetch' ? T('cm.stFetch', { i: Math.min((p.done || 0) + 1, p.total || 1), n: p.total || 1 })
        : p.stage === 'queue' ? T('cm.stQueue') : T('cm.stWrite');
}

function commentsCoverage(o) {
    return [T('cm.nComments', { n: (o.n || 0).toLocaleString() }), T('cm.len.' + (o.length || 'medium')),
        o.cost_usd ? fmtUsd(o.cost_usd) : ''].filter(Boolean).join(' · ');
}

// 正文里的 〔n〕 画成数字圆点；悬停看「谁 · 几赞 · 日期 — 原话」
function commentsHtml(o) {
    const cites = o.citations || [];
    const md = renderMarkdown(((o.result || {}).md) || '').replace(/〔(\d+)〕/g, (m, n) => {
        const c = cites[+n - 1];
        return c ? `<button type="button" class="cx-cite" data-cmn="${n}" title="${escapeHtml(cmMeta(c) + ' — ' + c.text.slice(0, 200))}">${n}</button>` : '';
    });
    const list = cites.map(c => `<div class="cx-src" data-cmn="${c.n}"><span class="cx-src-num">${c.n}</span>
        <div class="cm-t">${escapeHtml(c.text)}</div>
        <div class="cx-src-foot"><span>${escapeHtml(cmMeta(c))}</span>
            <button type="button" class="cx-link" data-cm-open="${c.n}">${escapeHtml(T('cm.openThread'))}</button></div></div>`).join('');
    return `<div class="cx-digest"><div class="md-body st-md">${md}</div>
        ${cites.length ? `<details class="cx-sources"><summary>${escapeHtml(T('cx.sources', { n: cites.length }))}</summary>${list}</details>` : ''}</div>`;
}

function commentsWire(box, o, openAt) {
    const cites = o.citations || [];
    openAt = openAt || (c => openTranscriptViewer(c.tid).then(() => readerComments(c.tid, c.id)));
    const open = n => {
        const c = cites[+n - 1];
        if (!c) return;
        box.querySelectorAll('.cx-src.hl').forEach(x => x.classList.remove('hl'));
        const it = box.querySelector(`.cx-src[data-cmn="${n}"]`);
        if (it) { it.classList.add('hl'); it.closest('details').open = true; }
        openAt(c);
    };
    box.querySelectorAll('.cx-cite[data-cmn]').forEach(b => b.addEventListener('click', () => open(b.dataset.cmn)));
    box.querySelectorAll('[data-cm-open]').forEach(b => b.addEventListener('click', () => open(b.dataset.cmOpen)));
}

function commentsExportDoc(o, title) {
    const cites = o.citations || [];
    const md = String((o.result || {}).md || '').replace(/〔(\d+)〕/g, '[$1]');
    const refs = cites.map(c => `${c.n}. ${c.text.replace(/\s+/g, ' ')} —— ${cmMeta(c)}`).join('\n');
    return { title, blocks: [{ md: md + (refs ? `\n\n## ${T('cx.sources', { n: cites.length })}\n\n${refs}` : ''), citations: {} }] };
}

// ---------------- 一个视频的评论区卡片（转写页贴链接的弹窗、转写详情页共用）----------------
// 上面一行「总结」：篇幅 + 侧重 + 按钮（价钱写在按钮上）；下面是总结结果；再下面是全部评论（commentsBlock）。
// key 是期的 task_id 或 BV 号；评论和总结都按视频存，在哪看都是同一份。
async function commentsCard(root, key) {
    if (!root) return;
    if (root.dataset.key !== key) {
        root.dataset.key = key;
        root.dataset.len = 'medium';
        root.dataset.cost = '';
        root.dataset.redo = '';
        root.innerHTML = `<div class="cm-head hidden">
                <span class="cm-dg-meta cx-muted"></span>
                <span class="cm-acts"><button type="button" class="btn-secondary cm-redo">${escapeHtml(T('cm.redigest'))}</button>
                    <a class="btn-secondary cm-csv" download>${escapeHtml(T('cm.csv'))}</a></span>
            </div>
            <div class="cm-ctl hidden">
                <div class="pod-lens cm-lens" role="radiogroup">${CM_LENGTHS.map(l => `<button type="button" class="pod-len" data-l="${l}" role="radio">${escapeHtml(T('cm.lenS.' + l))}</button>`).join('')}</div>
                <input type="text" class="search-input cm-focus" maxlength="300" placeholder="${escapeHtml(T('cm.focusPhS'))}">
                <span class="cm-ctl-go"><button type="button" class="btn-secondary cm-cancel hidden">${escapeHtml(T('common.cancel'))}</button>
                    <button type="button" class="rd-pick-go primary cm-go">${escapeHtml(T('cm.digest'))}</button></span>
            </div>
            <div class="cm-dg"></div>
            <div class="cm-reader cm-list-box"></div>`;
        const lens = () => root.querySelectorAll('.cm-lens .pod-len').forEach(b => {
            b.classList.toggle('on', b.dataset.l === root.dataset.len);
            b.setAttribute('aria-checked', b.dataset.l === root.dataset.len);
        });
        root.querySelectorAll('.cm-lens .pod-len').forEach(b => b.addEventListener('click', () => { root.dataset.len = b.dataset.l; lens(); }));
        lens();
        root.querySelector('.cm-go').addEventListener('click', () => commentsDigest(root, key));
        root.querySelector('.cm-redo').addEventListener('click', () => { root.dataset.redo = '1'; commentsCard(root, key); });
        root.querySelector('.cm-cancel').addEventListener('click', () => { root.dataset.redo = ''; commentsCard(root, key); });
        fetch(`/api/comments/${encodeURIComponent(key)}/estimate`).then(r => r.json()).then(est => {
            if (root.dataset.key !== key) return;
            root.dataset.cost = est.subscription ? T('cm.digestSub') : est.usd ? T('cm.digestCost', { cost: fmtUsd(Math.max(0.01, est.usd)) }) : '';
            if (root.dataset.cost) root.querySelector('.cm-go').textContent = root.dataset.cost;
        }).catch(() => {});
    }
    clearTimeout(root._poll);
    const list = root.querySelector('.cm-list-box');
    const d = await commentsBlock(list, key);
    if (root.dataset.key !== key || !d) return d;
    const dg = d.digest;
    const fetching = !!(d.job && d.job.running);
    const running = !!(dg && dg.status === 'running');
    const done = !!(dg && dg.status === 'done');
    const ro = !!window.VERBATIM_DEMO;
    // 上面一行：有总结时写它是什么时候、谁写的；右边「重新总结」「导出 CSV」
    const head = root.querySelector('.cm-head');
    head.classList.toggle('hidden', !d.has && !done);
    const csv = root.querySelector('.cm-csv');
    csv.classList.toggle('hidden', !d.has);
    csv.href = `/api/comments/${encodeURIComponent(key)}/csv`;
    root.querySelector('.cm-redo').classList.toggle('hidden', !done || ro || root.dataset.redo === '1');
    root.querySelector('.cm-dg-meta').textContent = done
        ? [String(dg.created_at || '').slice(5, 16), commentsCoverage(dg), cmModelLabel(dg.model), dg.auto ? T('cm.autoTag') : ''].filter(Boolean).join(' · ')
        : d.has ? T('cm.nComments', { n: ((d.info || {}).count || 0).toLocaleString() }) : '';
    // 选项（篇幅 / 侧重 / 按钮）：还没总结过、或点了「重新总结」才出现；在抓、在写的时候不出现
    const showCtl = !ro && !fetching && !running && (!done || root.dataset.redo === '1');
    root.querySelector('.cm-ctl').classList.toggle('hidden', !showCtl);
    root.querySelector('.cm-cancel').classList.toggle('hidden', !(done && root.dataset.redo === '1'));
    root.querySelector('.cm-go').textContent = root.dataset.cost || T('cm.digest');
    const box = root.querySelector('.cm-dg');
    if (fetching) {
        box.innerHTML = root.dataset.auto ? `<div class="cm-line cx-muted">${escapeHtml(T('cm.willAuto'))}</div>` : '';
        root._poll = setTimeout(() => { if (root.dataset.key === key && root.isConnected) commentsCard(root, key); }, 3000);
    } else if (running) {
        box.innerHTML = `<div class="cm-line"><span class="st-spin" aria-hidden="true"></span>${escapeHtml(commentsProgress(dg))}</div>`;
        root._poll = setTimeout(() => { if (root.dataset.key === key && root.isConnected) commentsCard(root, key); }, 2500);
    } else if (dg && dg.status === 'failed') {
        box.innerHTML = `<div class="cm-line cx-err">${escapeHtml(T(dg.error === 'interrupted' ? 'cm.interrupted' : dg.error === 'empty' ? 'cm.digestEmpty' : 'cm.digestFailed'))}</div>`;
    } else if (done) {
        const now = (d.info || {}).count || 0;
        const stale = dg.n && now && now !== dg.n ? `<div class="cm-line cm-stale">${escapeHtml(T('cm.stale', { n: now.toLocaleString(), m: dg.n.toLocaleString() }))}</div>` : '';
        box.innerHTML = stale + commentsHtml(dg);
        commentsWire(box, dg, c => commentsBlock(list, key, c.id));
    } else {
        box.innerHTML = '';
    }
    return d;
}

async function commentsDigest(root, key) {
    const go = root.querySelector('.cm-go');
    go.disabled = true;
    let r;
    try {
        r = await (await fetch(`/api/comments/${encodeURIComponent(key)}/digest`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ length: root.dataset.len, focus: root.querySelector('.cm-focus').value.trim(), lang: currentLang }) })).json();
    } catch (err) { r = { error: String(err) }; }
    go.disabled = false;
    if (r.error) {
        showToast(r.error === 'bili_login' ? T('cm.noLogin') : r.error);
        return;
    }
    root.dataset.redo = '';
    commentsCard(root, key);
}

// 转写详情页（资料库里点开的一期）：摘要下面一张评论区卡片，只有 B 站的期才出现
async function detailComments(rec) {
    const sec = document.getElementById('detail-comments-section');
    if (!sec) return;
    const tid = rec && rec.id;
    if (!tid || !/bilibili\.com\/video\/|b23\.tv\/|\bBV[0-9A-Za-z]{10}\b/.test(rec.source_url || '')) { sec.classList.add('hidden'); return; }
    sec.classList.remove('hidden');
    const d = await commentsCard(document.getElementById('detail-cm-card'), tid);
    if (d && !d.bili) sec.classList.add('hidden');
}

function cmModelLabel(m) {
    m = String(m || '');
    if (!m) return '';
    if (m.includes('opus')) return 'Claude Opus';
    if (m.includes('sonnet')) return 'Claude Sonnet';
    if (m.includes('haiku')) return 'Claude Haiku';
    return m.replace(/^gemini-/, 'Gemini ');
}

// 只下载评论区（不转写）：没抓过 → 抓，抓完后台自动总结；抓过但还没总结 → 直接自动总结。已有总结不重做。
async function cmStartAuto(r) {
    const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(x => x.json());
    let x;
    try {
        x = r.has ? await post(`/api/comments/${r.bvid}/digest`, { auto: true, lang: currentLang })
            : await post(`/api/comments/${r.bvid}/fetch`, { auto_digest: true, lang: currentLang });
    } catch { x = {}; }
    if (x.error) showToast(x.error === 'bili_login' ? T('cm.noLogin') : x.error);
    return x;
}

// 「转写」页贴的 B 站链接：那一行的「下载评论区」→ 中间弹窗（不转写视频，只抓评论；抓好能总结、导出 CSV）
async function commentsLinkModal(url) {
    let r;
    try { r = await (await fetch('/api/comments/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) })).json(); }
    catch { r = { error: 'net' }; }
    if (!r.bvid) { showToast(T('cm.notBiliLink')); return; }
    let ov = document.getElementById('cm-overlay');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'cm-overlay';
        ov.className = 'settings-overlay hidden';
        document.body.appendChild(ov);
    }
    toolOverlay('cm-overlay');
    ov.innerHTML = `<div class="cx-modal rd-pick pod-pick cm-modal" role="dialog" aria-modal="true" aria-labelledby="cm-mt">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="cm-mt">${stIcon('comments', 20)}<span>${escapeHtml(T('cm.title'))}</span></h2>
        <p class="cx-muted cm-modal-sub">${escapeHtml(r.title || r.bvid)}</p>
        <div class="cm-card-body"></div></div>`;
    ov.classList.remove('hidden');
    if (!window.VERBATIM_DEMO) await cmStartAuto(r);      // 点的就是「下载评论区」：没抓过就抓，抓完（或已经抓过）自动总结一份
    const body = ov.querySelector('.cm-card-body');
    body.dataset.auto = '1';                    // 弹窗里抓的会自动总结：抓的时候提示一句
    const title = async () => {                 // 抓完才知道标题
        const d = await commentsCard(body, r.bvid);
        if (d && d.info && d.info.title) ov.querySelector('.cm-modal-sub').textContent = d.info.title;
        else if (d && d.job && d.job.running && !ov.classList.contains('hidden')) setTimeout(title, 3000);
    };
    title();
}

// ---------------- 「转写」页：只要评论区（不转写）----------------
// 勾了「只要评论区」：大按钮变成「下载 N 个视频的评论区」，B 站链接一个接一个抓（同时抓容易被风控），
// 右边一张「评论区」卡片列出每个视频：排队 / 在抓 x / y / 几条评论；点一行打开那个视频的评论区弹窗。不是 B 站的标「跳过」。
const CM_BILI_RE = /bilibili\.com\/video\/|b23\.tv\//i;

function cmOnlyOn() {
    const box = document.getElementById('cm-only');
    return !!(box && box.checked && !box.closest('.hidden'));
}

function cmOnlySync(links) {
    const wrap = document.getElementById('cm-only-wrap');
    if (!wrap) return 0;
    setTimeout(() => {                           // 勾了「只要评论区」时把转写才用的设置藏起来（CSS 看 .cm-only-on）
        const f = document.getElementById('upload-form');
        if (f) f.classList.toggle('cm-only-on', cmOnlyOn());
    }, 0);
    const keys = links.filter(l => CM_BILI_RE.test(l)).map(l => (l.match(/BV[0-9A-Za-z]{10}/) || [l])[0]);
    const n = new Set(keys).size;                // 同一个视频贴两次只算一个（短链认不出 BV，按链接算）
    wrap.classList.toggle('hidden', !n || !!window.VERBATIM_DEMO);
    return n;
}

let cmBatchBusy = false;

async function commentsBatch(links) {
    if (cmBatchBusy) { showToast(T('cm.batchBusy')); return; }
    cmBatchBusy = true;
    try { await cmBatchRun(links); } finally { cmBatchBusy = false; }
}

async function cmBatchRun(links) {
    const side = document.getElementById('transcribe-side');
    let card = document.getElementById('cm-batch');
    if (!card) {
        card = document.createElement('div');
        card.id = 'cm-batch';
        card.className = 'card-shell cm-batch';
        side.prepend(card);
    }
    const rows = links.map(url => ({ url, bili: CM_BILI_RE.test(url), state: 'wait', bvid: '', title: '', have: 0, total: 0, count: 0, dg: null }));
    const dgText = r => !r.dg ? '' : r.dg.status === 'done' ? T('cm.bDigestDone') : r.dg.status === 'failed'
        ? T(r.dg.error === 'empty' ? 'cm.digestEmpty' : 'cm.digestFailed') : commentsProgress(r.dg);
    const draw = () => {
        card.innerHTML = `<h2 class="cm-batch-h">${escapeHtml(T('cm.title'))}</h2>` + rows.map((r, i) => {
            const st = !r.bili ? T('cm.bSkip') : r.state === 'dup' ? T('cm.bDup') : r.state === 'wait' ? T('cm.bWait')
                : r.state === 'run' ? (r.job ? cmFetchText(r.job) : T('cm.fetchingStart'))
                : r.state === 'err' ? r.err : [T('cm.nComments', { n: r.count.toLocaleString() }), dgText(r)].filter(Boolean).join(' · ');
            const spin = r.state === 'run' || (r.dg && r.dg.status === 'running');
            const can = r.state === 'done';
            return `<button type="button" class="cm-brow${can ? '' : ' off'}" data-i="${i}" ${can ? '' : 'disabled'}>
                <span class="cm-bt">${escapeHtml(r.title || (r.url.match(/BV[0-9A-Za-z]{10}/) || [r.url])[0])}</span>
                <span class="cm-bs${r.state === 'err' ? ' cx-err' : ''}">${spin ? '<span class="st-spin" aria-hidden="true"></span>' : ''}${escapeHtml(st)}</span></button>`;
        }).join('');
        card.querySelectorAll('.cm-brow:not(.off)').forEach(b => b.addEventListener('click', () => commentsLinkModal(rows[+b.dataset.i].url)));
    };
    const getJson = url => fetch(url).then(x => x.json());
    draw();
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    const seen = new Set();
    for (const r of rows.filter(x => x.bili)) {
        r.state = 'run';
        draw();
        try {
            const res = await (await fetch('/api/comments/resolve', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: r.url }) })).json();
            if (!res.bvid) throw new Error(T('cm.notBiliLink'));
            if (seen.has(res.bvid)) { r.state = 'dup'; r.title = res.title || ''; draw(); continue; }   // 同一个视频贴了两次
            seen.add(res.bvid);
            r.bvid = res.bvid;
            r.title = res.title || '';
            const st = await cmStartAuto(res);      // 抓（抓完后台自动总结）；抓过的直接总结
            if (st.error) throw new Error(st.error === 'bili_login' ? T('cm.noLogin') : st.error);
            for (;;) {
                const d = await getJson(`/api/comments/${r.bvid}`);
                if (d.job && d.job.running) { r.job = d.job; draw(); await new Promise(ok => setTimeout(ok, 2000)); continue; }
                if (d.job && d.job.error) throw new Error(d.job.error === 'bili_login' ? T('cm.noLogin') : T('cm.failed'));
                r.state = 'done';
                r.count = (d.info || {}).count || 0;
                r.title = (d.info || {}).title || r.title;
                r.dg = d.digest;
                break;
            }
        } catch (e) {
            r.state = 'err';
            r.err = e.message || T('cm.failed');
        }
        draw();
    }
    // 评论都抓完了：总结在后台排队写（同时最多 3 份），这里接着把每行的总结进度刷到写完
    while (rows.some(r => r.state === 'done' && (!r.dg || r.dg.status === 'running'))) {
        await new Promise(ok => setTimeout(ok, 3000));
        for (const r of rows.filter(x => x.state === 'done' && (!x.dg || x.dg.status === 'running'))) {
            try { r.dg = (await getJson(`/api/comments/${r.bvid}`)).digest; } catch { /* 下一轮再看 */ }
        }
        draw();
        if (!card.isConnected) return;
    }
}
