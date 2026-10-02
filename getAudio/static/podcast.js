// ========== 项目工作台：音频播客 ==========
// 后端在 podcast.py：写两人对谈脚本 → 编辑润色 → 语音合成 → 插本人原声 → 拼成一个 mp3。
// 格子 → 中间弹框选形式、长度、重点（估价写在按钮上）→ 后台做 → 列表里一条，点开在工作台栏里：
// 上面播放器，下面逐句文字稿（放到哪句高亮哪句，点句子跳过去，每句带出处）。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx / stPickOverlay / citedHtml / personName）、
// study.js（stState / stLoad / stIcon）、projects.js（srcScopeBody / srcCountText / srcState / srcAllIds）。

const POD_FORMATS = ['deep_dive', 'brief', 'debate'];
const POD_LENGTHS = ['short', 'default', 'long'];

function podClock(sec) {
    sec = Math.max(0, Math.floor(+sec || 0));
    return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}

async function podcastPicker() {
    if (typeof srcState === 'undefined' || !srcState.data || !srcAllIds(srcState.data).length) {
        showToast(T('st.noSources'));
        return;
    }
    const ov = stPickOverlay();
    let est = {};
    try { est = await (await fetch(`/api/chain/${cx.id}/studio/podcast/estimate`)).json(); } catch { /* 没估价就不写 */ }
    const people = (cx.people || []).map(p => personName(p)).filter(Boolean);
    const st = { fmt: 'deep_dive', len: 'default' };
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick pod-pick" role="dialog" aria-modal="true" aria-labelledby="pod-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="pod-t">${stIcon('podcast', 20)}<span>${escapeHtml(T('st.k.podcast'))}</span></h2>
        <p class="cx-muted">${escapeHtml(T('pod.desc'))}</p>
        <form id="pod-form">
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('pod.format'))}</span><div class="st-fmts" id="pod-fmts"></div></div>
            <div class="pod-f hidden" id="pod-sides-f"><span class="pod-l">${escapeHtml(T('pod.sides'))}</span>
                <div class="pod-sides">
                    <input type="text" id="pod-side-a" maxlength="60" list="pod-people" placeholder="${escapeHtml(T('pod.sideA'))}">
                    <span class="cx-muted">vs</span>
                    <input type="text" id="pod-side-b" maxlength="60" list="pod-people" placeholder="${escapeHtml(T('pod.sideB'))}">
                </div>
                <datalist id="pod-people">${people.map(n => `<option value="${escapeHtml(n)}">`).join('')}</datalist></div>
            <div class="pod-f"><span class="pod-l">${escapeHtml(T('pod.length'))}</span><div class="pod-lens" id="pod-lens" role="radiogroup"></div></div>
            <label class="pod-f"><span class="pod-l">${escapeHtml(T('st.focus'))}</span>
                <input type="text" id="pod-focus" maxlength="200" placeholder="${escapeHtml(T('pod.focusPh'))}"></label>
            <div class="vp-foot"><span class="rd-pick-s" id="pod-msg">${escapeHtml(T('st.from', { s: srcCountText() }))}</span>
                <button type="submit" class="rd-pick-go primary" id="pod-go">${escapeHtml(T('rd.generate'))}</button></div>
        </form></div>`;
    const render = () => {
        ov.querySelector('#pod-fmts').innerHTML = POD_FORMATS.map(f => `<button type="button" class="st-fmt${st.fmt === f ? ' on' : ''}" data-f="${f}"
            role="radio" aria-checked="${st.fmt === f}"><b>${escapeHtml(T('pod.f.' + f))}</b><span>${escapeHtml(T('pod.fd.' + f))}</span></button>`).join('');
        ov.querySelector('#pod-lens').innerHTML = POD_LENGTHS.map(l => `<button type="button" class="pod-len${st.len === l ? ' on' : ''}" data-l="${l}"
            role="radio" aria-checked="${st.len === l}">${escapeHtml(T('pod.len.' + l))}</button>`).join('');
        ov.querySelector('#pod-sides-f').classList.toggle('hidden', st.fmt !== 'debate');
        const cost = est[st.len];
        ov.querySelector('#pod-go').textContent = cost ? T('vs.goCost', { cost: fmtUsd(cost) }) : T('rd.generate');
        ov.querySelectorAll('#pod-fmts .st-fmt').forEach(b => b.addEventListener('click', () => { st.fmt = b.dataset.f; render(); }));
        ov.querySelectorAll('#pod-lens .pod-len').forEach(b => b.addEventListener('click', () => { st.len = b.dataset.l; render(); }));
    };
    render();
    ov.classList.remove('hidden');
    ov.querySelector('#pod-form').addEventListener('submit', async e => {
        e.preventDefault();
        const body = { format: st.fmt, length: st.len, focus: ov.querySelector('#pod-focus').value.trim(), scope: srcScopeBody() };
        if (st.fmt === 'debate') {
            body.sides = [ov.querySelector('#pod-side-a').value.trim(), ov.querySelector('#pod-side-b').value.trim()];
            if (!body.sides[0] || !body.sides[1]) { ov.querySelector('#pod-msg').textContent = T('pod.sidesNeed'); return; }
        }
        const go = ov.querySelector('#pod-go');
        go.disabled = true;
        let r;
        try {
            r = await (await fetch(`/api/chain/${cx.id}/studio/podcast`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) { ov.querySelector('#pod-msg').textContent = r.error; go.disabled = false; return; }
        ov.classList.add('hidden');
        showToast(T('pod.started'));
        stLoad();
    });
}

function podcastTitle(o) {
    return T('st.k.podcast') + ' · ' + (o.title || T('pod.f.' + (o.format || 'deep_dive')) + (o.focus ? ' · ' + o.focus : ''));
}

function podcastMeta(o) {
    const p = o.progress || {};
    if (p.stage === 'audio' && p.total) return T('pod.audioing', { i: Math.min(p.done + 1, p.total), n: p.total });
    return T('pod.scripting');
}

// ----- 看一份：播放器 + 逐句文字稿 -----
function podcastHtml(o) {
    const r = o.result || {};
    const cites = o.citations || {};
    const order = Object.keys(cites);
    const lines = (r.lines || []).map((ln, i) => {
        const t = `data-t="${+ln.t || 0}" data-i="${i}"`;
        if (ln.clip) {
            const c = cites[ln.clip] || {};
            const src = [ln.who || c.creator, c.label && c.episode ? `${c.label} · ${c.episode}` : c.episode, c.ts].filter(Boolean).join(' · ');
            return `<div class="pod-line pod-clip" ${t}>
                <span class="pod-who">${escapeHtml(T(ln.fallback ? 'pod.read' : 'pod.clip'))}</span>
                <div class="pod-say"><blockquote class="cc-quote">${escapeHtml(ln.quote || c.quote || '')}</blockquote>
                    <div class="pod-src">${escapeHtml(src)} ${citedHtml(`[#${ln.clip}]`, cites, order)}</div></div></div>`;
        }
        const text = ln.text.replace(/<[^>]*>/g, '').trim();
        const marks = (ln.cite || []).map(c => `[#${c}]`).join('');
        return `<div class="pod-line" ${t}><span class="pod-who">${escapeHtml(ln.speaker)}</span>
            <div class="pod-say">${citedHtml(escapeMd(text) + (marks ? ' ' + marks : ''), cites, order)}</div></div>`;
    }).join('');
    return `<div class="pod">
        <div class="pod-player"><audio controls preload="metadata" src="/api/chain/${encodeURIComponent(o.chain || cx.id)}/studio/${encodeURIComponent(o.id)}/audio"></audio></div>
        <p class="cx-muted pod-hint">${escapeHtml(T('pod.hint'))}</p>
        <div class="pod-lines">${lines}</div></div>`;
}

// 文字稿当 Markdown 渲染（出处芯片要走 citedHtml），台词里的 * _ # 之类别被当成格式
function escapeMd(s) {
    return String(s || '').replace(/([\\`*_#>\[\]])/g, '\\$1');
}

function podcastWire(box) {
    const audio = box.querySelector('.pod-player audio');
    const rows = [...box.querySelectorAll('.pod-line')];
    if (!audio || !rows.length) return;
    let cur = -1;
    audio.addEventListener('timeupdate', () => {
        const t = audio.currentTime;
        let k = -1;
        rows.forEach((el, i) => { if (+el.dataset.t <= t + 0.05) k = i; });
        if (k === cur) return;
        if (cur >= 0) rows[cur].classList.remove('on');
        cur = k;
        if (k >= 0) {
            rows[k].classList.add('on');
            const lst = box.closest('.nb-viewer-body, #cx-studio') || box;
            const r = rows[k].getBoundingClientRect(), b = lst.getBoundingClientRect();
            if (r.top < b.top + 80 || r.bottom > b.bottom - 20) rows[k].scrollIntoView({ block: 'center', behavior: 'smooth' });
        }
    });
    rows.forEach(el => el.addEventListener('click', e => {
        if (e.target.closest('.cx-cite, a, button')) return;     // 点出处照旧去看原文
        audio.currentTime = +el.dataset.t || 0;
        audio.play().catch(() => {});
    }));
}

function podcastExportDoc(o, title) {
    const r = o.result || {};
    const cites = o.citations || {};
    const md = (r.lines || []).map(ln => ln.clip
        ? `> ${ln.quote || ''} [#${ln.clip}]`
        : `**${ln.speaker}**：${ln.text.replace(/<[^>]*>/g, '').trim()} ${(ln.cite || []).map(c => `[#${c}]`).join('')}`).join('\n\n');
    return { title: r.title || title, blocks: [{ md, citations: cites }] };
}
