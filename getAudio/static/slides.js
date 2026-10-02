// ========== 项目工作台：幻灯片 ==========
// 后端在 slides.py：模型只出每页的内容（挑版式、填字段），排版是固定代码，导出的 .pptx 里字都是真文本框、能直接改。
// 格子 → 中间弹框选样式、页数、侧重（估价写在按钮上）→ 后台做 → 列表里一条，点开在工作台栏里：
// 上面「下载 .pptx」，下面一页页 16:9 预览（跟文件同一套版式），每页能「改这一页」。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx / stPickOverlay / citedHtml / wireCitations）、
// study.js（stState / stLoad / stIcon / stRender）、projects.js（srcScopeBody / srcCountText / srcState / srcAllIds）。

const SL_STYLES = ['detailed', 'presenter'];
const SL_COUNTS = [6, 10, 15];

async function slidesPicker() {
    if (typeof srcState === 'undefined' || !srcState.data || !srcAllIds(srcState.data).length) {
        showToast(T('st.noSources'));
        return;
    }
    const ov = stPickOverlay();
    let est = 0;
    try { est = (await (await fetch(`/api/chain/${cx.id}/studio/slides/estimate`)).json()).usd || 0; } catch { /* 没估价就不写 */ }
    const st = { style: 'detailed', n: 10 };
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick pod-pick sl-pick" role="dialog" aria-modal="true" aria-labelledby="sl-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="sl-t">${stIcon('slides', 20)}<span>${escapeHtml(T('st.k.slides'))}</span></h2>
        <p class="cx-muted">${escapeHtml(T('sl.desc'))}</p>
        <form id="sl-form">
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('sl.style'))}</span><div class="st-fmts" id="sl-styles"></div></div>
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('sl.count'))}</span><div class="pod-lens" id="sl-counts" role="radiogroup"></div></div>
            <label class="pod-f"><span class="pod-l">${escapeHtml(T('st.focus'))}</span>
                <input type="text" id="sl-focus" maxlength="200" placeholder="${escapeHtml(T('st.focusPh'))}"></label>
            <div class="vp-foot"><span class="rd-pick-s" id="sl-msg">${escapeHtml(T('st.from', { s: srcCountText() }))}</span>
                <button type="submit" class="rd-pick-go primary" id="sl-go">${escapeHtml(est ? T('vs.goCost', { cost: fmtUsd(est) }) : T('rd.generate'))}</button></div>
        </form></div>`;
    const render = () => {
        ov.querySelector('#sl-styles').innerHTML = SL_STYLES.map(s => `<button type="button" class="st-fmt${st.style === s ? ' on' : ''}" data-s="${s}"
            role="radio" aria-checked="${st.style === s}"><b>${escapeHtml(T('sl.s.' + s))}</b><span>${escapeHtml(T('sl.sd.' + s))}</span></button>`).join('');
        ov.querySelector('#sl-counts').innerHTML = SL_COUNTS.map(n => `<button type="button" class="pod-len${st.n === n ? ' on' : ''}" data-n="${n}"
            role="radio" aria-checked="${st.n === n}">${escapeHtml(T('sl.nPages', { n }))}</button>`).join('');
        ov.querySelectorAll('#sl-styles .st-fmt').forEach(b => b.addEventListener('click', () => { st.style = b.dataset.s; render(); }));
        ov.querySelectorAll('#sl-counts .pod-len').forEach(b => b.addEventListener('click', () => { st.n = +b.dataset.n; render(); }));
    };
    render();
    ov.classList.remove('hidden');
    ov.querySelector('#sl-form').addEventListener('submit', async e => {
        e.preventDefault();
        const go = ov.querySelector('#sl-go');
        go.disabled = true;
        let r;
        try {
            r = await (await fetch(`/api/chain/${cx.id}/studio/slides`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ style: st.style, n: st.n, focus: ov.querySelector('#sl-focus').value.trim(), scope: srcScopeBody() }) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) { ov.querySelector('#sl-msg').textContent = r.error; go.disabled = false; return; }
        ov.classList.add('hidden');
        showToast(T('sl.started'));
        stLoad();
    });
}

function slidesTitle(o) {
    return T('st.k.slides') + ' · ' + (o.title || T('sl.s.' + (o.style || 'detailed')) + (o.focus ? ' · ' + o.focus : ''));
}

// ----- 看一份：一页页预览，版式跟导出的文件一样 -----
// 出处圆点不画在幻灯片里（缩小后会盖住字），统一放在这页下面那一行，跟 .pptx 页脚的「出处」是一回事
function slIds(s) {
    const ids = [];
    const add = xs => (xs || []).forEach(c => { if (!ids.includes(c)) ids.push(c); });
    (s.bullets || []).forEach(b => add(b.cite));
    ['left', 'right'].forEach(side => s[side] && s[side].bullets.forEach(b => add(b.cite)));
    (s.steps || []).forEach(st => add(st.cite));
    add(s.cite);
    return ids;
}

function slList(items, cites, order) {
    return `<ul class="sl-ul">${(items || []).map(b => `<li>${escapeHtml(b.text)}${b.detail ? `<small class="sl-detail">${escapeHtml(b.detail)}</small>` : ''}</li>`).join('')}</ul>`;
}

function slideHtml(s, k, total, cites, order, img) {
    const dark = ['cover', 'section', 'closing'].includes(s.layout);
    let body = '';
    if (s.layout === 'cover') {
        body = `<div class="sl-eyebrow">Verbatim · ${escapeHtml(T('st.k.slides'))}</div><div class="sl-cover-t">${escapeHtml(s.title)}</div>
            ${s.subtitle ? `<div class="sl-sub">${escapeHtml(s.subtitle)}</div>` : ''}`;
    } else if (s.layout === 'section') {
        body = `<div class="sl-eyebrow">${String(k).padStart(2, '0')}</div><div class="sl-cover-t sl-sec-t">${escapeHtml(s.title)}</div>
            ${s.subtitle ? `<div class="sl-sub">${escapeHtml(s.subtitle)}</div>` : ''}`;
    } else {
        let main = '';
        if (s.layout === 'points' || s.layout === 'closing') main = slList(s.bullets, cites, order);
        else if (s.layout === 'quote') {
            main = `<div class="sl-quote"><span class="sl-qm">“</span><div><div class="sl-q">${escapeHtml(s.quote)}</div>
                ${s.who ? `<div class="sl-who">— ${escapeHtml(s.who)}</div>` : ''}</div></div>`;
        } else if (s.layout === 'stat') {
            main = `<div class="sl-stat"><div class="sl-val">${escapeHtml(s.value)}</div><div class="sl-lab">${escapeHtml(s.label)}</div></div>`;
        } else if (s.layout === 'compare') {
            main = `<div class="sl-cmp">${['left', 'right'].map((side, j) => `<div class="sl-col${j ? ' alt' : ''}">
                <div class="sl-col-t">${escapeHtml(s[side].label)}</div>${slList(s[side].bullets, cites, order)}</div>`).join('')}</div>`;
        } else if (s.layout === 'timeline') {
            main = `<div class="sl-tl" style="--n:${s.steps.length}">${s.steps.map(st => `<div class="sl-step"><div class="sl-when">${escapeHtml(st.when)}</div>
                <i class="sl-dot"></i><div class="sl-what">${escapeHtml(st.what)}</div></div>`).join('')}</div>`;
        }
        body = `<div class="sl-t">${escapeHtml(s.title)}</div><div class="sl-body">${main}</div>`;
    }
    // 本机有 LibreOffice 时，预览就是真 .pptx 渲染出来的图（跟下载到的一模一样）；没有才用 HTML 近似画一张
    const face = img
        ? `<div class="sl-img"><img src="${img}" alt="${escapeHtml(s.title || '')}" loading="lazy"></div>`
        : `<div class="sl-slide${dark ? ' dark' : ''}" data-l="${s.layout}">${body}<span class="sl-no">${k + 1} / ${total}</span></div>`;
    return `<figure class="sl-wrap" data-i="${k}">
        ${face}
        <figcaption class="sl-cap">
            <span class="cx-muted sl-srcs">${escapeHtml(T('sl.l.' + s.layout))}${slIds(s).length
                ? ` · ${escapeHtml(T('sl.sources'))} ${citedHtml(slIds(s).map(c => `[#${c}]`).join(''), cites, order).replace(/^<p>|<\/p>\s*$/g, '')}` : ''}</span>
            ${window.VERBATIM_DEMO ? '' : `<button type="button" class="cx-link sl-rev" data-i="${k}">${escapeHtml(T('sl.revise'))}</button>`}
        </figcaption>
        <form class="sl-rev-f hidden" data-i="${k}">
            <input type="text" maxlength="500" placeholder="${escapeHtml(T('sl.revisePh'))}">
            <button type="submit" class="primary">${escapeHtml(T('sl.reviseGo'))}</button>
            <span class="cx-muted sl-rev-msg"></span>
        </form>
        ${s.notes ? `<details class="sl-notes"><summary>${escapeHtml(T('sl.notes'))}</summary><div class="md-body">${citedHtml(s.notes, cites, order)}</div></details>` : ''}
    </figure>`;
}

function slidesHtml(o) {
    const r = o.result || {};
    const cites = o.citations || {};
    const order = Object.keys(cites);
    const ss = r.slides || [];
    return `<div class="sl">
        <div class="sl-bar"><a class="primary sl-dl" href="/api/chain/${encodeURIComponent(o.chain || cx.id)}/studio/${encodeURIComponent(o.id)}/pptx">${escapeHtml(T('sl.download'))}</a>
            <span class="cx-muted">${escapeHtml(T('sl.editable'))}</span></div>
        ${ss.map((s, k) => slideHtml(s, k, ss.length, cites, order, o.previews >= ss.length
            ? `/api/chain/${encodeURIComponent(o.chain || cx.id)}/studio/${encodeURIComponent(o.id)}/slide/${k + 1}?v=${encodeURIComponent(o.revised_at || o.finished_at || '')}`
            : '')).join('')}</div>`;
}

function slidesWire(box) {
    box.querySelectorAll('.sl-rev').forEach(b => b.addEventListener('click', () => {
        const f = box.querySelector(`.sl-rev-f[data-i="${b.dataset.i}"]`);
        f.classList.toggle('hidden');
        if (!f.classList.contains('hidden')) f.querySelector('input').focus();
    }));
    box.querySelectorAll('.sl-rev-f').forEach(f => f.addEventListener('submit', async e => {
        e.preventDefault();
        const ins = f.querySelector('input').value.trim();
        if (!ins) return;
        const btn = f.querySelector('button');
        const msg = f.querySelector('.sl-rev-msg');
        btn.disabled = true;
        msg.innerHTML = `<span class="st-spin" aria-hidden="true"></span>${escapeHtml(T('sl.revising'))}`;
        const o = stState.cur;
        try {
            const r = await fetch(`/api/chain/${stState.id}/studio/${o.id}/revise`, { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ i: +f.dataset.i, instruction: ins }) });
            const j = await r.json();
            if (!r.ok) throw new Error(j.error || r.status);
            stState.cur = j;
            stRender();
            showToast(T('sl.revised', { n: +f.dataset.i + 1 }));
        } catch (err) {
            msg.textContent = String(err.message || err);
            btn.disabled = false;
        }
    }));
}

function slidesExportDoc(o, title) {
    const r = o.result || {};
    const mark = ids => (ids || []).map(c => `[#${c}]`).join('');
    const blocks = (r.slides || []).map((s, k) => {
        const lines = [];
        (s.bullets || []).forEach(b => lines.push(`- ${b.text} ${mark(b.cite)}`));
        if (s.quote) lines.push(`> ${s.quote} ${mark(s.cite)}${s.who ? '\n>\n> — ' + s.who : ''}`);
        if (s.value) lines.push(`**${s.value}** ${s.label} ${mark(s.cite)}`);
        ['left', 'right'].forEach(side => s[side] && lines.push(`**${s[side].label}**\n` + s[side].bullets.map(b => `- ${b.text} ${mark(b.cite)}`).join('\n')));
        (s.steps || []).forEach(st => lines.push(`- **${st.when}** ${st.what} ${mark(st.cite)}`));
        if (s.subtitle) lines.push(s.subtitle);
        return { heading: `${k + 1}. ${s.title}`, md: lines.join('\n\n'), citations: o.citations || {} };
    });
    return { title: r.title || title, blocks };
}
