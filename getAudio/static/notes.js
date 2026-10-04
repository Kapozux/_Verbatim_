// ========== 读书笔记（notes.py，ADR 0010）==========
// 每份来源读一次存成笔记，工作台放不下全部原文时按笔记定计划。这里管两处界面：
//   · 左栏来源清单最后一行「读书笔记」：读了几份；没读的写估价、点了才读（单独加的文档后台已经自动读了）；
//   · 来源栏阅读器：这份来源的笔记（每块讲什么 + 要点），可折叠。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx）、projects.js（srcState / srcPost）。

let ntState = { id: null, data: null, est: null, estFor: null, timer: null };

async function ntLoad() {
    const id = cx.id;
    if (!id) return;
    clearTimeout(ntState.timer);
    if (ntState.id !== id) ntState = { id, data: null, est: null, estFor: null, timer: null };
    let d;
    try { d = await (await fetch(`/api/chain/${id}/notes`)).json(); } catch { return; }
    if (cx.id !== id || d.error) return;
    ntState.data = d;
    if (d.unread && !d.reading && !window.VERBATIM_DEMO && ntState.estFor !== d.unread) {
        ntState.estFor = d.unread;                    // 没读的份数变了才重新估（转写多时估一次要一两秒）
        try { ntState.est = (await (await fetch(`/api/chain/${id}/notes?estimate=1`)).json()).estimate_usd; } catch { ntState.est = null; }
    }
    ntRender();
    if (d.reading) ntState.timer = setTimeout(() => { if (cx.id === id) ntLoad(); }, 3000);
}

function ntSrcRowHtml() {
    const d = ntState.data;
    if (!d || ntState.id !== cx.id || !d.total) return '<div class="sr-row nt-src hidden"></div>';
    const sub = d.reading ? T('nt.reading', { n: d.reading })
        : d.unread ? T('nt.some', { r: d.read, n: d.total }) : T('nt.allRead', { n: d.total });
    const btn = d.unread && !d.reading && !window.VERBATIM_DEMO
        ? `<button type="button" class="cx-link nt-go" id="nt-go">${escapeHtml(ntState.est
            ? T('nt.readRest', { n: d.unread, cost: fmtUsd(Math.max(0.01, ntState.est)) }) : T('nt.readRestNoCost', { n: d.unread }))}</button>` : '';
    return `<div class="sr-row nt-src" title="${escapeHtml(T('nt.hint'))}">
        <span class="sr-ic"><span class="sr-svg">${ntIcon()}</span></span>
        <span class="sr-title nt-t">${escapeHtml(T('nt.title'))}</span>
        <span class="sr-sub${d.reading ? ' run' : ''}">${escapeHtml(sub)}</span>
        ${btn ? `<span class="nt-go-wrap">${btn}</span>` : ''}
    </div>`;
}

function ntIcon() {
    return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
        stroke-linejoin="round" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5"/><path d="M8 8h8M8 11.5h6"/></svg>`;
}

// 只重画这一行（不重画整个来源清单，免得打断勾选）
function ntRender() {
    const old = document.querySelector('#cx-sources .nt-src');
    const list = document.querySelector('#cx-sources .sr-list');
    if (!old && !list) return;
    const tmp = document.createElement('div');
    tmp.innerHTML = ntSrcRowHtml();
    const row = tmp.firstElementChild;
    if (old) old.replaceWith(row);
    else list.appendChild(row);          // 清单画的时候本文件还没加载（没留占位行）：接在最后
    ntWireSrc(row.parentElement || document);
}

function ntWireSrc(box) {
    const go = box.querySelector('#nt-go');
    if (go) go.addEventListener('click', async () => {
        go.disabled = true;
        const r = await srcPost(`/api/chain/${cx.id}/notes/read`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: '{}' });
        if (r) showToast(T('nt.started'));
        ntLoad();
    });
}

// 来源栏阅读器：这份来源的笔记（没读过就不出现）
async function readerNotes(kind, id) {
    const box = document.getElementById('src-reader-notes');
    if (!box) return;
    const key = `${kind}:${id}`;
    box.dataset.key = key;
    box.innerHTML = '';
    box.classList.add('hidden');
    let d;
    try { d = await (await fetch(`/api/sources/notes?kind=${kind}&id=${encodeURIComponent(id)}`)).json(); } catch { return; }
    if (box.dataset.key !== key || !d || !d.notes || !(d.notes.units || []).length) return;
    const units = d.notes.units;
    box.innerHTML = `<details><summary>${escapeHtml(T('nt.inReader', { n: units.length }))}</summary>
        ${units.map(u => `<div class="nt-unit"><div class="nt-where">${escapeHtml(u.where || '')}</div>
            <p>${escapeHtml(u.summary || '')}</p>
            ${(u.points || []).length ? `<ul>${u.points.slice(0, 8).map(p => `<li>${escapeHtml(p.text)}</li>`).join('')}</ul>` : ''}</div>`).join('')}
        ${(d.notes.skipped || []).length ? `<p class="nt-skip">${escapeHtml(T('nt.skipped'))} ${escapeHtml(d.notes.skipped.join('；'))}</p>` : ''}
    </details>`;
    box.classList.remove('hidden');
}

// 直接打开笔记本页面时，projects.js 的 srcLoad 可能在本文件加载完之前就跑完了（那时还没有 ntLoad）：补拉一次
if (typeof cx !== 'undefined' && cx.id && typeof srcState !== 'undefined' && srcState.data) ntLoad();
