// ========== 项目工作台：画面证据卡（测试版） ==========
// 后端在 frames.py / study.start_visual：重下画面、挑关键帧、读幻灯片 / 图表 / 公式 / 代码上的字。
// 格子 → 中间弹框选哪几期（全部或勾选）、看估价 → 后台做 → 列表里一条，点开在工作台栏里看。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx / stPickOverlay）、
// study.js（stState / stLoad）、projects.js（openTranscriptViewer）。

function vsClock(sec) {
    sec = Math.max(0, Math.floor(+sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + ':' + String(s).padStart(2, '0');
}

async function visualPicker() {
    const ov = stPickOverlay();
    const head = `<button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="vs-t">${escapeHtml(T('st.k.visual'))}</h2>
        <p class="cx-muted">${escapeHtml(T('vs.desc'))}</p>`;
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick vs-pick" role="dialog" aria-modal="true" aria-labelledby="vs-t">${head}
        <div class="vp-foot"><span class="rd-pick-s"><span class="st-spin" aria-hidden="true"></span>${T('cx.loading')}</span></div></div>`;
    ov.classList.remove('hidden');
    let eps = [];
    try {
        const r = await fetch(`/api/chain/${cx.id}/visual/episodes`);
        eps = (await r.json()).episodes || [];
    } catch { /* 下面按空的处理 */ }
    if (ov.classList.contains('hidden')) return;
    const usable = eps.filter(e => e.has_video || e.done);
    if (!usable.length) {
        ov.querySelector('.vs-pick').innerHTML = `${head}<div class="vp-foot"><span class="rd-pick-s">${escapeHtml(T(eps.length ? 'vs.noVideo' : 'vs.none'))}</span></div>`;
        return;
    }
    const row = e => {
        const can = e.has_video || e.done;
        const st = e.done ? T('vs.doneN', { n: e.n }) : !e.has_video ? T('vs.noLink') : '';
        return `<label class="vs-ep${can ? '' : ' off'}">
            <input type="checkbox" value="${escapeHtml(e.task_id)}" ${can ? '' : 'disabled'}>
            <span class="vs-ep-t">${escapeHtml(e.title)}</span>
            <span class="vs-ep-m">${escapeHtml([e.who, e.duration ? vsClock(e.duration) : '', st].filter(Boolean).join(' · '))}</span>
        </label>`;
    };
    ov.querySelector('.vs-pick').innerHTML = `${head}
        <form id="vs-form">
            <label class="vs-ep vs-all"><input type="checkbox" id="vs-all"><span class="vs-ep-t">${escapeHtml(T('vs.all', { n: usable.length }))}</span></label>
            <div class="vs-eps">${eps.map(row).join('')}</div>
            <div class="vp-foot"><span class="rd-pick-s" id="vs-msg"></span>
                <button type="submit" class="rd-pick-go primary" id="vs-go" disabled>${T('rd.generate')}</button></div>
        </form>`;
    const boxes = [...ov.querySelectorAll('.vs-eps input:not(:disabled)')];
    const all = ov.querySelector('#vs-all');
    const sync = () => {
        const on = boxes.filter(b => b.checked).map(b => eps.find(e => e.task_id === b.value));
        const cost = on.reduce((s, e) => s + (e.est_usd || 0), 0);
        all.checked = on.length === boxes.length;
        all.indeterminate = on.length > 0 && on.length < boxes.length;
        ov.querySelector('#vs-msg').textContent = on.length ? T('vs.picked', { n: on.length }) : T('vs.pickSome');
        const go = ov.querySelector('#vs-go');
        go.disabled = !on.length;
        go.textContent = on.length && cost ? T('vs.goCost', { cost: fmtUsd(Math.max(0.01, cost)) }) : T('rd.generate');
    };
    all.addEventListener('change', () => { boxes.forEach(b => { b.checked = all.checked; }); sync(); });
    boxes.forEach(b => b.addEventListener('change', sync));
    sync();
    ov.querySelector('#vs-form').addEventListener('submit', async e => {
        e.preventDefault();
        const ids = boxes.filter(b => b.checked).map(b => b.value);
        if (!ids.length) return;
        const go = ov.querySelector('#vs-go');
        go.disabled = true;
        let r;
        try {
            r = await (await fetch(`/api/chain/${cx.id}/studio/visual`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task_ids: ids }) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) { ov.querySelector('#vs-msg').textContent = r.error; go.disabled = false; return; }
        ov.classList.add('hidden');
        showToast(T('vs.started'));
        stLoad();
    });
}

// ----- 看一份：按期分组，每张卡 = 关键帧 + 屏幕上的字 + 描述 + 那时在说什么 -----
function visualHtml(o) {
    const eps = (o.result || {}).episodes || [];
    return `<div class="vs">${eps.map(e => `
        <section class="vs-sec">
            <h3 class="vs-sec-t">${escapeHtml(e.title || '')}</h3>
            <p class="cx-muted">${escapeHtml(e.error ? T('vs.epFailed', { e: e.error }) : T('vs.nCards', { n: (e.cards || []).length }))}</p>
            <div class="vs-grid">${(e.cards || []).map(c => `
                <article class="vs-card">
                    <a class="vs-img" href="/api/visual/${encodeURIComponent(e.task_id)}/${encodeURIComponent(c.img)}" target="_blank" rel="noopener">
                        <img src="/api/visual/${encodeURIComponent(e.task_id)}/${encodeURIComponent(c.img)}" alt="${escapeHtml(c.title || '')}" loading="lazy"></a>
                    <div class="vs-body">
                        <div class="vs-meta"><button type="button" class="vs-at" data-tid="${escapeHtml(e.task_id)}" data-sec="${+c.at || 0}"
                            title="${escapeHtml(T('vs.jump'))}">${vsClock(c.at)}</button><span>${escapeHtml(T('vs.kind.' + c.kind))}</span></div>
                        <b class="vs-title">${escapeHtml(c.title || '')}</b>
                        ${c.text ? `<div class="vs-text">${escapeHtml(c.text)}</div>` : ''}
                        ${c.desc ? `<p class="vs-desc">${escapeHtml(c.desc)}</p>` : ''}
                        ${c.obs ? `<p class="vs-obs">${escapeHtml(c.obs)}</p>` : ''}
                        ${c.said ? `<details class="vs-said"><summary>${T('vs.said')}</summary><p>${escapeHtml(c.said)}</p></details>` : ''}
                    </div>
                </article>`).join('')}</div>
        </section>`).join('')}</div>`;
}

function visualWire(box) {
    box.querySelectorAll('.vs-at').forEach(b => b.addEventListener('click', () => {
        if (typeof openTranscriptViewer === 'function') openTranscriptViewer(b.dataset.tid, +b.dataset.sec);
    }));
}

function visualExportDoc(o, title) {
    return { title, blocks: ((o.result || {}).episodes || []).flatMap(e => (e.cards || []).map(c => ({
        heading: `${e.title} · ${vsClock(c.at)} · ${c.title || ''}`,
        md: [c.text ? '> ' + c.text.replace(/\n/g, '\n> ') : '', c.desc, c.obs].filter(Boolean).join('\n\n'),
        citations: {} }))) };
}
