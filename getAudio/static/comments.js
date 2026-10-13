// ========== B 站评论区（后端 comments.py，ADR 0018）==========
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

// ---------------- 来源栏阅读器：评论区 ----------------
async function commentsFetch(tid) {
    const r = await srcPost(`/api/comments/${encodeURIComponent(tid)}/fetch`, { method: 'POST' });
    if (!r) return;
    showToast(T('cm.fetchStarted'));
    CM_BOXES.forEach(id => {                // 正开着这一期的地方（阅读器、转写页）都刷新
        const box = document.getElementById(id);
        if (box && box.dataset.key === tid) commentsBlock(box, tid);
    });
}

const CM_BOXES = ['src-reader-comments', 'detail-cm-list'];

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
        box.innerHTML = `<div class="cm-line"><span class="st-spin" aria-hidden="true"></span>${escapeHtml(job.reported
            ? T('cm.fetching', { n: job.have.toLocaleString(), m: job.reported.toLocaleString() }) : T('cm.fetchingStart'))}</div>`;
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
    box.innerHTML = `${err}<details class="cm-box"${focusId || box.dataset.wasOpen ? ' open' : ''}>
        <summary>${escapeHtml(T('cm.inReader', { n: (info.count || 0).toLocaleString(), t: info.fetched_at || '' }))}</summary>
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
    return p.stage === 'fetch' ? T('cm.stFetch', { i: Math.min((p.done || 0) + 1, p.total || 1), n: p.total || 1 }) : T('cm.stWrite');
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
        ${cites.length ? `<details class="cx-sources" open><summary>${escapeHtml(T('cx.sources', { n: cites.length }))}</summary>${list}</details>` : ''}</div>`;
}

function commentsWire(box, o, openAt) {
    const cites = o.citations || [];
    openAt = openAt || (c => openTranscriptViewer(c.tid).then(() => readerComments(c.tid, c.id)));
    const open = n => {
        const c = cites[+n - 1];
        if (!c) return;
        box.querySelectorAll('.cx-src.hl').forEach(x => x.classList.remove('hl'));
        const it = box.querySelector(`.cx-src[data-cmn="${n}"]`);
        if (it) it.classList.add('hl');
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

// ---------------- 转写页（资料库里点开的一期）：评论区卡片 ----------------
// 摘要卡片和转写正文之间；只有 B 站的期才出现。上面是这一期的整理（点「整理」做，跟这一期存在一起），下面是全部评论。
async function detailComments(rec) {
    const sec = document.getElementById('detail-comments-section');
    if (!sec) return;
    const tid = rec && rec.id;
    sec.dataset.key = tid || '';
    clearTimeout(sec._poll);
    if (!tid || !/bilibili\.com\/video\/|b23\.tv\/|\bBV[0-9A-Za-z]{10}\b/.test(rec.source_url || '')) { sec.classList.add('hidden'); return; }
    const list = document.getElementById('detail-cm-list');
    const d = await commentsBlock(list, tid);
    if (sec.dataset.key !== tid) return;
    if (!d || !d.bili) { sec.classList.add('hidden'); return; }
    sec.classList.remove('hidden');
    const go = document.getElementById('detail-cm-go');
    const dg = d.digest;
    go.textContent = T(dg && dg.status === 'done' ? 'cm.redigest' : 'cm.digest');
    go.disabled = !!(dg && dg.status === 'running');
    go.classList.toggle('hidden', !!window.VERBATIM_DEMO);
    go.onclick = () => commentsEpisodePicker(tid, rec.ai_title || rec.filename || '');
    const box = document.getElementById('detail-cm-digest');
    if (!dg) { box.innerHTML = `<p class="cx-muted cm-hint">${escapeHtml(T('cm.digestHint'))}</p>`; return; }
    if (dg.status === 'running') {
        box.innerHTML = `<div class="cm-line"><span class="st-spin" aria-hidden="true"></span>${escapeHtml(commentsProgress(dg))}</div>`;
        sec._poll = setTimeout(() => { if (sec.dataset.key === tid) detailComments(rec); }, 2500);
        return;
    }
    if (dg.status === 'failed') {
        box.innerHTML = `<div class="cm-line cx-err">${escapeHtml(dg.error === 'interrupted' ? T('cm.interrupted') : T('cm.digestFailed'))}</div>`;
        return;
    }
    box.innerHTML = `<div class="cm-dg-meta cx-muted">${escapeHtml([String(dg.created_at || '').slice(5, 16), commentsCoverage(dg)].join(' · '))}</div>` + commentsHtml(dg);
    commentsWire(box, dg, c => commentsBlock(list, tid, c.id));
}

async function commentsEpisodePicker(tid, title) {
    const ov = stPickOverlay();
    const st = { length: 'medium' };
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick pod-pick cm-pick" role="dialog" aria-modal="true" aria-labelledby="cm-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="cm-t">${stIcon('comments', 20)}<span>${escapeHtml(T('st.k.comments'))}</span></h2>
        <p class="cx-muted">${escapeHtml(title)}</p>
        <form id="cm-form">
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('cm.length'))}</span><div class="pod-lens" id="cm-lens" role="radiogroup"></div></div>
            <label class="pod-f"><span class="pod-l">${escapeHtml(T('st.focus'))}</span>
                <textarea id="cm-focus" rows="2" maxlength="300" placeholder="${escapeHtml(T('cm.focusPh'))}"></textarea></label>
            <div class="vp-foot"><span class="rd-pick-s" id="cm-msg"></span>
                <button type="submit" class="rd-pick-go primary" id="cm-go">${T('rd.generate')}</button></div>
        </form></div>`;
    const renderLens = () => {
        const box = ov.querySelector('#cm-lens');
        box.innerHTML = CM_LENGTHS.map(l => `<button type="button" class="pod-len${st.length === l ? ' on' : ''}" data-l="${l}"
            role="radio" aria-checked="${st.length === l}">${escapeHtml(T('cm.len.' + l))}</button>`).join('');
        box.querySelectorAll('.pod-len').forEach(b => b.addEventListener('click', () => { st.length = b.dataset.l; renderLens(); }));
    };
    renderLens();
    ov.classList.remove('hidden');
    try {
        const [est, d] = await Promise.all([fetch(`/api/comments/${encodeURIComponent(tid)}/estimate`).then(r => r.json()),
            fetch(`/api/comments/${encodeURIComponent(tid)}`).then(r => r.json())]);
        const go = ov.querySelector('#cm-go');
        if (est.subscription) go.textContent = T('cm.goSub');
        else if (est.usd) go.textContent = T('vs.goCost', { cost: fmtUsd(Math.max(0.01, est.usd)) });
        ov.querySelector('#cm-msg').textContent = d.has ? T('cm.nComments', { n: d.info.count.toLocaleString() }) : T('cm.fetchFirst', { n: 1 });
    } catch { /* 没估价就不写 */ }
    ov.querySelector('#cm-form').addEventListener('submit', async e => {
        e.preventDefault();
        const go = ov.querySelector('#cm-go');
        go.disabled = true;
        let r;
        try {
            r = await (await fetch(`/api/comments/${encodeURIComponent(tid)}/digest`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ length: st.length, focus: ov.querySelector('#cm-focus').value.trim(), lang: currentLang }) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) {
            ov.querySelector('#cm-msg').textContent = r.error === 'bili_login' ? T('cm.noLogin') : r.error;
            go.disabled = false;
            return;
        }
        ov.classList.add('hidden');
        showToast(T('cm.digestStarted'));
        if (typeof detailRecord !== 'undefined' && detailRecord && detailRecord.id === tid) detailComments(detailRecord);
    });
}
