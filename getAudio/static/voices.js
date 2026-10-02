// ================= 说话人（声纹认人，后端 voices.py）=================
// 来源栏底下一行「说话人」→ 工作台栏展开（面包屑「来源 › 说话人」）：每个说话人一行，▶ 试听，改名 / 合并 / 拆开。
// 转写阅读器里每句前面标上是谁、能点 ▶ 听那一句。声纹在本机算，音频不出这台电脑。

let vcState = { id: null, data: null, timer: null, open: {} };
const vcAudio = new Audio();
let vcPlaying = null;                 // {key, end}

const VC_SVG = {
    people: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M15.5 5.6a3.2 3.2 0 0 1 0 5.8"/><path d="M17.5 13.6a5.5 5.5 0 0 1 3 5.4"/>',
    play: '<path d="M8 5.5v13l10.5-6.5z"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="1.5"/>',
};
const vcIcon = (k, s = 16) => `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${VC_SVG[k]}</svg>`;

function vcClock(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0');
}

async function vcLoad() {
    const id = cx.id;
    if (!id) return;
    clearTimeout(vcState.timer);
    if (vcState.id !== id) vcState = { id, data: null, timer: null, open: {} };
    let d;
    try { d = await (await fetch(`/api/chain/${id}/voices`)).json(); } catch { return; }
    if (cx.id !== id || d.error) return;
    vcState.data = d;
    if (typeof srcRender === 'function') srcRender();
    if (cx.tab === 'voices') vcRender();
    // 有录音在后台做声纹：隔几秒再看；离开项目就停（cx.id 变了）
    if ((d.missing || []).some(m => m.job && ['queued', 'running'].includes(m.job.state))) {
        vcState.timer = setTimeout(() => { if (cx.id === id) vcLoad(); }, 6000);
    }
}

// 来源栏里的那一行：项目里有录音、并且装了声纹模型（或者已经认过）才出现
function vcSrcRowHtml() {
    const v = vcState.data;
    if (!v || vcState.id !== cx.id || !v.lessons_total || (!v.available && !v.speakers.length)) return '';
    const sps = v.speakers.filter(s => s.role !== 'other');
    const main = sps.find(s => s.role === 'main');
    const busy = (v.missing || []).some(m => m.job && ['queued', 'running'].includes(m.job.state));
    const sub = busy ? T('vc.srcRun') : !main ? T('vc.srcNone')
        : sps.length > 1 ? T('vc.srcSub', { main: main.name, n: sps.length - 1 }) : main.name;
    return `<div class="sr-row vc-src">
        <span class="sr-ic"><span class="sr-svg">${vcIcon('people', 18)}</span></span>
        <button type="button" class="sr-title" id="vc-src-open">${T('vc.title')}</button>
        <span class="sr-sub${busy ? ' run' : ''}">${escapeHtml(sub)}</span>
    </div>`;
}

function vcWireSrc(box) {
    const b = box.querySelector('#vc-src-open');
    if (b) b.addEventListener('click', () => cxShowTab('voices'));
}

function vcLessonName(tid) {
    const v = vcState.data;
    const i = ((v && v.order) || []).findIndex(x => x.task_id === tid);
    return i < 0 ? '' : `EP${i + 1}`;
}

function vcLessonTitle(tid) {
    const x = ((vcState.data && vcState.data.order) || []).find(o => o.task_id === tid);
    return x ? x.title : '';
}

function vcPlayBtn(key, tid, start, end, label) {
    const on = vcPlaying && vcPlaying.key === key;
    return `<button type="button" class="vc-play${on ? ' on' : ''}" data-play="${escapeHtml(key)}" data-tid="${escapeHtml(tid)}"
        data-start="${start}" data-end="${end}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}">${vcIcon(on ? 'stop' : 'play', 14)}</button>`;
}

function vcRender() {
    const box = document.getElementById('cx-voices');
    const v = vcState.data;
    if (!box) return;
    if (!v) { box.innerHTML = `<p class="cx-thinking">${T('cx.loading')}</p>`; return; }
    const ro = !!window.VERBATIM_DEMO;
    const missing = v.missing || [];
    const jobs = missing.filter(m => m.job && ['queued', 'running'].includes(m.job.state));
    const others = v.speakers.filter(s => s.role !== 'main');
    let head = `<div class="vc-bar"><span>${T('vc.coverage', { done: v.lessons_with_voices, total: v.lessons_total })}</span>`;
    if (!v.available) head += `<span class="vc-warn">${T('vc.noModels')}</span>`;
    else if (missing.length > jobs.length && !ro) head += `<button type="button" class="cx-link" id="vc-find">${T('vc.find')}</button>`;
    head += '</div>';
    if (missing.length && v.available) {
        head += `<div class="vc-missing">${missing.map(m => {
            const st = m.job ? m.job.state : '';
            const tag = st === 'queued' ? T('vc.queued') : st === 'running' ? T('vc.running') : st === 'failed' ? T('vc.failed')
                : vcState.notFound && vcState.notFound.has(m.task_id) ? T('vc.notFound') : '';
            const pick = !ro && !['queued', 'running'].includes(st)
                ? `<label class="cx-link vc-pick">${T('vc.pick')}<input type="file" accept="audio/*,video/*" data-pick="${escapeHtml(m.task_id)}" hidden></label>` : '';
            return `<div class="vc-mrow"><span class="vc-ep">${vcLessonName(m.task_id)}</span>
                <span class="vc-mt" title="${escapeHtml(m.title || '')}">${escapeHtml(m.title || '')}</span>
                ${tag ? `<span class="vc-st${st === 'failed' ? ' bad' : st ? ' run' : ''}">${tag}</span>` : ''}${pick}</div>`;
        }).join('')}<p class="vc-fine">${T('vc.findHint')}</p></div>`;
    }
    if (!v.speakers.length) {
        box.innerHTML = head + `<div class="st-empty"><span class="st-empty-ic">${vcIcon('people', 22)}</span><span>${T('vc.empty')}</span></div>`;
        vcWire(box);
        return;
    }
    const rows = v.speakers.map(s => vcSpeakerHtml(s, others, ro)).join('');
    const unclear = v.unclear_seconds >= 30
        ? `<p class="vc-fine">${T('vc.unclear', { min: Math.round(v.unclear_seconds / 60) || 1, label: v.unclear_label || '' })}</p>` : '';
    box.innerHTML = head + `<div class="vc-list">${rows}</div>${unclear}<p class="vc-fine">${T('vc.how')}</p>`;
    vcWire(box);
}

function vcSpeakerHtml(s, others, ro) {
    const mins = s.seconds >= 60 ? T('vc.min', { n: Math.round(s.seconds / 60) }) : T('vc.sec', { n: Math.round(s.seconds) });
    const role = s.role === 'main' ? T('vc.role.main') : s.role === 'group' ? T('vc.role.group') : s.role === 'other' ? T('vc.role.other') : '';
    const meta = [role, mins, T('vc.nLessons', { n: s.lessons })].filter(Boolean).join(' · ');
    const clips = (s.clips || []).map((c, i) => vcPlayBtn(`${s.id}:${i}`, c[0], c[1], c[2],
        T('vc.playAt', { ep: vcLessonName(c[0]), t: vcClock(c[1]) }))).join('');
    const opt = (act, label, extra = '') => `<button type="button" data-act="${act}" ${extra}>${escapeHtml(label)}</button>`;
    const menu = ro ? '' : `<details class="sr-menu vc-menu"><summary aria-label="⋯">⋯</summary><div>
        ${opt('rename', T('vc.rename'))}
        ${s.role !== 'main' ? opt('role', T('vc.setMain'), 'data-role="main"') : ''}
        ${s.role !== 'student' ? opt('role', T('vc.setStudent'), 'data-role="student"') : ''}
        ${s.role !== 'group' ? opt('role', T('vc.setGroup'), 'data-role="group"') : ''}
        ${s.role !== 'other' ? opt('role', T('vc.setOther'), 'data-role="other"') : ''}
        ${s.groups.length > 1 ? opt('where', T('vc.where')) : ''}
        ${v_mergeTargets(s).length ? `<span class="vc-menu-h">${T('vc.mergeInto')}</span>`
            + v_mergeTargets(s).map(o => opt('merge', o.name, `data-into="${escapeHtml(o.id)}"`)).join('') : ''}
    </div></details>`;
    let notes = '';
    if (s.suggest && !ro) {
        const o = vcState.data.speakers.find(x => x.id === s.suggest.id);
        const oc = o && (o.clips || [])[0];
        notes += `<div class="vc-note">${T('vc.suggest', { name: `<b>${escapeHtml(s.suggest.name)}</b>` })}
            ${oc ? vcPlayBtn(`${o.id}:s`, oc[0], oc[1], oc[2], T('vc.listen', { name: o.name })) : ''}
            <button type="button" class="vc-btn" data-act="merge" data-into="${escapeHtml(s.suggest.id)}">${T('vc.same')}</button>
            <button type="button" class="vc-btn ghost" data-act="not_same" data-other="${escapeHtml(s.suggest.id)}">${T('vc.notSame')}</button></div>`;
    }
    if (s.hint && s.named !== 'user' && !ro) {
        notes += `<div class="vc-note">${T('vc.hint', { name: escapeHtml(s.hint.name), quote: escapeHtml(s.hint.quote) })}
            <button type="button" class="vc-btn" data-act="accept_hint">${T('vc.useName')}</button></div>`;
    }
    const where = vcState.open[s.id] || s.groups.length === 1 ? '' : 'hidden';
    const groups = s.groups.length > 1 ? `<div class="vc-groups ${where}">${s.groups.map(g => {
        const ref = g.ref.split(':');
        return `<div class="vc-grow"><span class="vc-ep">${vcLessonName(g.tid)}</span>
            <span class="vc-mt" title="${escapeHtml(vcLessonTitle(g.tid))}">${escapeHtml(vcLessonTitle(g.tid))}</span>
            <span class="vc-gs">${g.seconds >= 60 ? T('vc.min', { n: Math.round(g.seconds / 60) }) : T('vc.sec', { n: Math.round(g.seconds) })}</span>
            ${vcGroupPlay(s, g)}
            ${ro ? '' : `<button type="button" class="vc-btn ghost" data-act="detach" data-group="${escapeHtml(g.ref)}">${T('vc.notThem')}</button>`}</div>`;
    }).join('')}</div>` : '';
    return `<div class="vc-sp" data-id="${escapeHtml(s.id)}" data-role="${escapeHtml(s.role)}">
        <div class="vc-hd">
            <span class="vc-av" aria-hidden="true">${escapeHtml(s.role === 'student' && !s.named && s.n ? String(s.n) : (s.name || '?').slice(0, 1))}</span>
            <div class="vc-nm"><b class="vc-name" ${ro ? '' : 'tabindex="0" role="button"'} title="${escapeHtml(T('vc.rename'))}">${escapeHtml(s.name)}</b>
                <span class="vc-meta">${escapeHtml(meta)}</span></div>
            <div class="vc-clips">${clips}</div>${menu}
        </div>${notes}${groups}
    </div>`;
}

function v_mergeTargets(s) {
    return (vcState.data.speakers || []).filter(o => o.id !== s.id && o.role !== 'other');
}

function vcGroupPlay(s, g) {
    // 这一组的片段：名单里只带了每个说话人最长的几段；这组要是不在里面，就从这组最长那段开头放 8 秒
    const c = (s.clips || []).find(x => x[0] === g.tid);
    return c ? vcPlayBtn(`${g.ref}`, c[0], c[1], c[2], T('vc.playAt', { ep: vcLessonName(g.tid), t: vcClock(c[1]) })) : '';
}

function vcWire(box) {
    box.querySelectorAll('[data-play]').forEach(b => b.addEventListener('click', () => vcToggle(b)));
    const find = box.querySelector('#vc-find');
    if (find) find.addEventListener('click', vcFind);
    box.querySelectorAll('input[data-pick]').forEach(inp => inp.addEventListener('change', () => vcUpload(inp)));
    box.querySelectorAll('.vc-sp').forEach(row => {
        const id = row.dataset.id;
        const name = row.querySelector('.vc-name[role="button"]');
        if (name) {
            name.addEventListener('click', () => vcRename(row, id));
            name.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); vcRename(row, id); } });
        }
        row.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => {
            const m = row.querySelector('.vc-menu');
            if (m) m.open = false;
            const act = b.dataset.act;
            if (act === 'rename') return vcRename(row, id);
            if (act === 'where') { vcState.open[id] = !vcState.open[id]; return vcRender(); }
            const body = { action: act, speaker: id };
            if (act === 'role') body.role = b.dataset.role;
            if (act === 'merge') body.into = b.dataset.into;
            if (act === 'not_same') body.other = b.dataset.other;
            if (act === 'detach') body.group = b.dataset.group;
            vcAct(body);
        }));
    });
}

async function vcAct(body) {
    const id = vcState.id;
    try {
        const r = await (await fetch(`/api/chain/${id}/voices`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        })).json();
        if (r.error) { showToast(r.error); return; }
        if (cx.id !== id) return;
        vcState.data = r;
        vcRender();
        if (typeof srcRender === 'function') srcRender();
    } catch (e) { showToast(String(e)); }
}

function vcRename(row, id) {
    const b = row.querySelector('.vc-name');
    if (!b || row.querySelector('.vc-name-in')) return;
    const s = vcState.data.speakers.find(x => x.id === id);
    const inp = document.createElement('input');
    inp.className = 'vc-name-in';
    inp.maxLength = 40;
    inp.value = s && s.named === 'user' ? s.name : '';
    inp.placeholder = s ? s.name : T('vc.namePh');
    inp.setAttribute('aria-label', T('vc.rename'));
    b.replaceWith(inp);
    inp.focus();
    let done = false;
    const finish = save => {
        if (done) return;
        done = true;
        const v = inp.value.trim();
        if (save && (v || (s && s.named === 'user'))) vcAct({ action: 'rename', speaker: id, name: v });
        else vcRender();
    };
    inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); finish(true); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    inp.addEventListener('blur', () => finish(true));
}

async function vcFind() {
    const id = vcState.id;
    try {
        const r = await (await fetch(`/api/chain/${id}/voices/fingerprint`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
        if (r.error) { showToast(r.error); return; }
        vcState.notFound = new Set((r.not_found || []).map(x => x.task_id));
        showToast(r.queued.length ? T('vc.foundN', { n: r.queued.length }) : T('vc.foundNone'));
        vcLoad();
    } catch (e) { showToast(String(e)); }
}

async function vcUpload(inp) {
    const f = inp.files && inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append('audio', f);
    try {
        const r = await (await fetch(`/api/voices/${inp.dataset.pick}/fingerprint`, { method: 'POST', body: fd })).json();
        if (r.error) { showToast(r.error === 'duration_mismatch' ? T('vc.mismatch') : r.error); return; }
        showToast(T('vc.foundN', { n: 1 }));
        vcLoad();
    } catch (e) { showToast(String(e)); }
}

// ---- 试听：一个播放器，放完自己停 ----
function vcToggle(btn) {
    const key = btn.dataset.play;
    if (vcPlaying && vcPlaying.key === key) { vcStop(); return; }
    vcStop();
    const tid = btn.dataset.tid;
    const start = +btn.dataset.start;
    vcPlaying = { key, end: +btn.dataset.end };
    const src = `/api/voices/${tid}/audio`;
    const go = () => { vcAudio.currentTime = start; vcAudio.play().catch(() => vcStop()); };
    if (!vcAudio.src.endsWith(src)) {
        vcAudio.src = src;
        vcAudio.addEventListener('loadedmetadata', go, { once: true });
    } else go();
    vcMark();
}

function vcStop() {
    vcAudio.pause();
    vcPlaying = null;
    vcMark();
}

function vcMark() {
    document.querySelectorAll('[data-play]').forEach(b => {
        const on = !!vcPlaying && vcPlaying.key === b.dataset.play;
        b.classList.toggle('on', on);
        b.innerHTML = vcIcon(on ? 'stop' : 'play', 14);
    });
}

vcAudio.addEventListener('timeupdate', () => { if (vcPlaying && vcAudio.currentTime >= vcPlaying.end) vcStop(); });
vcAudio.addEventListener('ended', vcStop);

// ---- 转写阅读器：每句前面标是谁，▶ 从这一句开始听 ----
async function vcDecorateTranscript(body, taskId, segs) {
    const pid = cx.id;
    if (!pid || !vcState.data || !(vcState.data.speakers || []).length) return;
    let r;
    try { r = await (await fetch(`/api/chain/${pid}/voices/${taskId}`)).json(); } catch { return; }
    if (!r || r.error || !(r.labels || []).length || cx.id !== pid) return;
    const role = {};
    (vcState.data.speakers || []).forEach(s => { role[s.id] = s.role; });
    const secs = segs.map(s => parseTimestampToSeconds(s.timestamp || '0:00'));
    r.labels.forEach(l => {
        const row = body.querySelector(`.rd-seg[data-si="${l.i}"]`);
        if (!row) return;
        const txt = row.children[1];
        if (txt && l.name) {
            txt.textContent = txt.textContent.replace(/^\s*说话人\s*\d+\s*[：:]\s*/, '');
            txt.insertAdjacentHTML('afterbegin', `<span class="vc-who" data-role="${escapeHtml(role[l.speaker] || 'unclear')}">${escapeHtml(l.name)}</span>`);
        }
        if (r.audio) {
            const end = secs.slice(l.i + 1).find(x => x > secs[l.i]) || secs[l.i] + 30;
            row.children[0].insertAdjacentHTML('afterend', vcPlayBtn(`seg:${taskId}:${l.i}`, taskId, secs[l.i], end,
                T('vc.playFrom', { t: vcClock(secs[l.i]) })));
            const b = row.querySelector('[data-play]');
            b.addEventListener('click', () => vcToggle(b));
            row.classList.add('vc-has-play');
        }
    });
}
