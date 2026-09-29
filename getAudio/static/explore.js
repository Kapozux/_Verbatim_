// ========== 博主页：问证据卡 / 话题时间线 / 预测记账 / 证据卡（标签页）==========
// 后端逻辑在 ask.py；这里只管界面。每个回答里的 [#3-12] 都换成可点的出处芯片，
// 点开能看到原话、跳回那期转写的那一秒、跳到原视频的那一秒、生成分享图。
// 依赖 app.js 里的 T / escapeHtml / safeUrl / renderMarkdown / navigate / fmtUsd。

const CX_TABS = ['read', 'ask', 'topics', 'predictions', 'cards', 'episodes'];
const CX_CARD_TABS = ['ask', 'topics', 'predictions', 'cards'];
const CITE_RE = /\[#((?:[A-Z]:)?\d+(?:_[0-9a-f]{8})?-\d+)\]/g;
const CAN_ASK = () => !window.VERBATIM_DEMO || window.VERBATIM_DEMO_ASK;
let cx = { id: null, cards: null, chain: null, sub: null, tab: null, avail: {}, picked: false, loaded: {}, timers: {} };

function cxStore(key, val) {
    try {
        if (val === undefined) return localStorage.getItem(key);
        localStorage.setItem(key, val);
    } catch { /* 隐私模式等拿不到 localStorage：记不住就算了 */ }
    return null;
}

function cxClearTimers() {
    Object.values(cx.timers).forEach(clearTimeout);
    cx.timers = {};
}

// ----- 入口 1：打开博主页（app.js openChainDetail）——先复位，哪些标签可用等数据到了再定 -----
function exploreOpen(id) {
    cxClearTimers();
    cx = { id, cards: null, chain: null, sub: null, tab: null, avail: { read: null, cards: null },
           picked: false, loaded: {}, timers: {} };
    predsState = { v: null, shown: 30 };
    topicsState = { data: null, sel: null, shown: 24, col: null, hlSt: null, cards: {} };
    readState = { doc: null, files: null };
    ['cx-read', 'cx-ask', 'cx-topics', 'cx-preds'].forEach(i => { document.getElementById(i).innerHTML = ''; });
    document.getElementById('cx-digest').innerHTML = '';
    document.querySelectorAll('#chain-explore .cx-tab').forEach(b => {
        b.onclick = () => { cx.picked = true; cxShowTab(b.dataset.cx); };
    });
    cxApplyAvail();
    subLoad(id);
    if (typeof lastChainDetail !== 'undefined' && lastChainDetail && lastChainDetail.id === id) cxFillHead(lastChainDetail);
}

// ----- 入口 2：证据卡到了（app.js loadChainCards）-----
function exploreInit(id, cardsResp) {
    if (cx.id !== id) exploreOpen(id);
    cx.cards = cardsResp;
    cxSetAvail({ cards: true });
    cxFillHead();
}

// 可用性：read = 有画像（或镜头）；cards = 有证据卡。null = 还不知道
function cxSetAvail(patch) {
    Object.assign(cx.avail, patch);
    cxApplyAvail();
}

function cxTabOk(t) {
    if (t === 'episodes') return true;
    if (t === 'read') return !!cx.avail.read || !!cx.avail.cards;   // 没画像但有卡：还能在这里生成镜头
    if (t === 'ask') return !!cx.avail.cards && CAN_ASK();     // 演示版没开提问：不给这个标签
    return CX_CARD_TABS.includes(t) && !!cx.avail.cards;
}

function cxApplyAvail() {
    document.querySelectorAll('#chain-explore .cx-tab').forEach(b =>
        b.classList.toggle('hidden', !cxTabOk(b.dataset.cx)));
    // 选哪个标签：用户这次点过的 > 上次停留的（可用的话）> 解读 > 分集
    let want = cx.tab;
    if (!cx.picked || !cxTabOk(want)) {
        const stored = cxStore('cx.tab');
        const demoAsk = stored === 'ask' && !CAN_ASK();
        // 有画像先看解读；没画像但有卡先去问（演示版去话题）；都没有就是分集
        const fallback = cx.avail.read ? 'read' : cx.avail.cards ? (CAN_ASK() ? 'ask' : 'topics')
            : cx.avail.read === null || cx.avail.cards === null ? null : 'episodes';
        want = stored && cxTabOk(stored) && !demoAsk ? stored : fallback;
        // 还在等数据、而上次停留的标签可能马上可用：先别乱跳
        if (!want) want = cx.avail.read === null ? (cxTabOk(stored) ? stored : 'episodes') : 'episodes';
    }
    if (want !== cx.tab) cxShowTab(want, true);
}

function cxShowTab(tab, auto) {
    cx.tab = tab;
    if (!auto) cxStore('cx.tab', tab);
    document.querySelectorAll('#chain-explore .cx-tab').forEach(b => {
        const on = b.dataset.cx === tab;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('#chain-explore [data-cx-panel]').forEach(p =>
        p.classList.toggle('hidden', p.dataset.cxPanel !== tab));
    const desc = document.getElementById('cx-tab-desc');      // 标签名负责好认，这一行负责讲清楚
    const colKey = 'cx.desc.' + tab + 'Col';
    const isCol = cx.chain && cx.chain.kind === 'collection';
    if (desc) desc.textContent = T(isCol && T(colKey) !== colKey ? colKey : 'cx.desc.' + tab);
    if (cx.loaded[tab]) return;
    cx.loaded[tab] = true;
    if (tab === 'read') readLoad();
    else if (tab === 'ask') askLoad();
    else if (tab === 'topics') topicsLoad();
    else if (tab === 'predictions') predsLoad();
}

// ----- 档案头里的两个空位：修辞三指标（#cp-rh）、订阅按钮（#cp-sub）。
// 档案头每次轮询都会整个重画，所以数据缓存在 cx 上，重画后由 app.js 调这里补回去 -----
function cxFillHead(chain) {
    if (chain && chain.id === cx.id) {
        cx.chain = chain;
        const isCol = chain.kind === 'collection';
        const epTab = document.querySelector('#chain-explore .cx-tab[data-cx=episodes] [data-i18n]');
        if (epTab) epTab.textContent = T(isCol ? 'col.items' : 'chainDetail.episodes');
        const rdTab = document.querySelector('#chain-explore .cx-tab[data-cx=read]');
        if (rdTab) rdTab.textContent = T(isCol ? 'cx.tab.readCol' : 'cx.tab.read');
        const desc = document.getElementById('cx-tab-desc');
        if (desc && cx.tab && isCol && T('cx.desc.' + cx.tab + 'Col') !== 'cx.desc.' + cx.tab + 'Col') desc.textContent = T('cx.desc.' + cx.tab + 'Col');
        const read = !!chain.final_doc || !!chain.analyze && chain.stage === 'done';
        if (cx.avail.read !== read) cxSetAvail({ read });
    }
    const rh = document.getElementById('cp-rh');
    const r = cx.cards && cx.cards.rhetoric;
    if (rh) {
        const pct = v => (v == null ? '—' : Math.round(v * 100) + '%');
        rh.innerHTML = r ? `
            <div class="cp-stat cp-rhs" title="${escapeHtml(T('rh.hypeHint') + ' ' + T('rh.note', { n: r.episodes }))}">
                <div class="n">${r.hype_per_ep}</div><div class="l">${T('rh.hype')}</div></div>
            <div class="cp-stat cp-rhs" title="${escapeHtml(T('rh.hedgeHint'))}">
                <div class="n">${r.hedge_per_ep}</div><div class="l">${T('rh.hedge')}</div></div>
            <div class="cp-stat cp-rhs" title="${escapeHtml(T('rh.tradeHint', { a: r.tradeoff, b: r.tech }))}">
                <div class="n">${pct(r.tradeoff_ratio)}</div><div class="l">${T('rh.trade')}</div></div>` : '';
    }
    subRender();
}

// ================= 订阅（档案头右上角：按钮 + 下拉设置）=================
async function subLoad(id) {
    if (window.VERBATIM_DEMO) return;
    try {
        const s = await (await fetch(`/api/chain/${id}/subscription`)).json();
        if (cx.id !== id) return;
        cx.sub = s;
    } catch { return; }
    subRender();
    digestRender(cx.sub.digest);
    if (cx.sub.running) cx.timers.sub = setTimeout(() => subLoad(id), 8000);
}

function subRender() {
    const box = document.getElementById('cp-sub');
    const s = cx.sub;
    const isCol = cx.chain && cx.chain.kind === 'collection';      // 合集没有频道可订阅
    if (!box || !s || window.VERBATIM_DEMO || isCol) { if (box) box.innerHTML = ''; return; }
    const id = cx.id;
    const on = !!s.on;
    const kw = (s.keywords || []).join(', ');
    const last = s.last_run_at ? T('sub.lastRun', { at: s.last_run_at, n: s.last_new || 0 }) : '';
    const wasOpen = !!box.querySelector('details[open]');
    const freq = [24, 168, 336, 720];
    const iv = freq.includes(+s.interval_h) ? +s.interval_h : 168;
    box.innerHTML = on ? `
        <details class="cp-subd"${wasOpen ? ' open' : ''}>
            <summary class="btn-secondary cp-btn cx-sub-btn on">↻ ${T('sub.f.' + iv)}${T('sub.syncing')} ▾</summary>
            <div class="cp-ops-panel cp-sub-panel">
                <p class="cx-muted">${T('sub.onHint')}</p>
                <label class="cx-sub-kw">${T('sub.freq')}
                    <select id="sub-freq">${freq.map(h => `<option value="${h}"${h === iv ? ' selected' : ''}>${T('sub.f.' + h)}</option>`).join('')}</select></label>
                ${s.next_run_at ? `<p class="cx-muted">${T('sub.nextRun', { at: s.next_run_at })}</p>` : ''}
                <label class="cx-sub-kw">${T('sub.keywords')}
                    <input type="text" id="sub-kw" value="${escapeHtml(kw)}" placeholder="${escapeHtml(T('sub.kwPlaceholder'))}"></label>
                ${last ? `<p class="cx-muted">${escapeHtml(last)}</p>` : ''}
                ${s.last_error ? `<p class="cx-err">${escapeHtml(String(s.last_error).slice(0, 140))}</p>` : ''}
                <div class="ci-actions">
                    <button class="btn-secondary ci-btn" type="button" id="sub-now" ${s.running ? 'disabled' : ''}>${s.running ? T('sub.running') : T('sub.checkNow')}</button>
                    <button class="btn-secondary ci-btn" type="button" id="sub-off">${T('sub.unfollow')}</button>
                </div>
            </div>
        </details>`
        : `<button class="btn-secondary cp-btn cx-sub-btn" type="button" id="sub-on" title="${escapeHtml(T('sub.offHint'))}">${T('sub.follow')}</button>`;
    const post = body => fetch(`/api/chain/${id}/subscription`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const q = sel => box.querySelector(sel);
    if (q('#sub-on')) q('#sub-on').onclick = async () => { await post({ on: true }); subLoad(id); };
    if (q('#sub-off')) q('#sub-off').onclick = async () => { await post({ on: false }); subLoad(id); };
    if (q('#sub-now')) q('#sub-now').onclick = async () => {
        await post({ run_now: true });
        cx.sub.running = true; subRender();
        cx.timers.sub = setTimeout(() => subLoad(id), 8000);
    };
    if (q('#sub-freq')) q('#sub-freq').onchange = async e => {
        await post({ interval_h: +e.target.value });
        subLoad(id);
    };
    if (q('#sub-kw')) q('#sub-kw').onchange = e => {
        cx.sub.keywords = e.target.value.split(/[,，]/).map(x => x.trim()).filter(Boolean);
        post({ keywords: cx.sub.keywords });
    };
}

function digestRender(d) {
    const box = document.getElementById('cx-digest');
    if (!box) return;
    if (!d || !d.new_videos) { box.innerHTML = ''; return; }
    box.innerHTML = `
        <details class="cx-digest"${d.seen ? '' : ' open'}>
            <summary>${d.seen ? '' : `<span class="creator-new">${T('sub.newShort')}</span>`}
                ${T('sub.digestTitle', { n: d.new_videos, at: d.at })}</summary>
            <div class="cx-answer md-body">${d.markdown ? citedHtml(d.markdown, d.citations || {})
                : `<p class="cx-muted">${T('sub.digestEmpty')}</p>`}</div>
            ${sourcesHtml(d.citations || {}, d.markdown || '')}
            ${(d.keyword_hits || []).length ? `<p class="cx-muted">${T('sub.kwHits', { n: d.keyword_hits.length })}</p>` : ''}
        </details>`;
    wireCitations(box, d.citations);
    // 展开着摆出来就算看过了：下次进博主页折叠起来，博主卡片上的「新」也消掉
    if (!d.seen) {
        fetch(`/api/chain/${cx.id}/subscription`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seen: true }) });
    }
}

// ================= 解读：画像 + 五个镜头，在页内切换着读 =================
const LENS_KEYS = ['roast', 'craft', 'fun', 'quotes', 'worldview'];
let readState = { doc: null, files: null };

async function readLoad() {
    const id = cx.id;
    const box = document.getElementById('cx-read');
    box.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
    let files = [];
    try {
        const j = await (await fetch(`/api/chain/${id}/files`)).json();
        files = Array.isArray(j) ? j : (j.files || []);
    } catch { /* 当没有 */ }
    if (cx.id !== id) return;
    const names = files.map(f => (typeof f === 'string' ? f : f.name));
    readState.files = names;
    const chain = cx.chain || {};
    const portrait = chain.final_doc && names.includes(chain.final_doc) ? chain.final_doc : null;
    box.innerHTML = `
        <div class="rd-grid">
            <article class="rd-doc">
                <div class="rd-doc-bar">
                    <span class="rd-doc-name" id="rd-doc-name"></span>
                    <span class="rd-doc-tools">
                        <button class="cx-link" type="button" id="rd-full">${T('rd.openFull')}</button>
                    </span>
                </div>
                <div class="md-body rd-body" id="rd-body"></div>
            </article>
            <aside class="rd-side">
                <div class="rd-sec-title">${T('rd.docs')}</div>
                <div class="rd-list" id="rd-list"></div>
                ${chain.raw_doc ? `<button class="btn-secondary rd-raw" type="button" id="rd-raw">${T('creators.fullTranscript')} ↗</button>` : ''}
                <div class="rd-sec-title rd-toc-title">${T('doc.toc')}</div>
                <nav class="rd-toc" id="rd-toc"></nav>
            </aside>
        </div>`;
    const raw = box.querySelector('#rd-raw');
    if (raw) raw.onclick = () => navigate(`chain/${id}/doc/${encodeURIComponent(chain.raw_doc)}`);
    box.querySelector('#rd-full').onclick = () => {
        if (readState.doc) navigate(`chain/${id}/doc/${encodeURIComponent(readState.doc)}`);
    };
    readRenderList(portrait);
    const first = portrait || LENS_KEYS.map(k => `镜头_${k}.md`).find(n => names.includes(n));
    if (first) readOpen(first);
    else {
        document.getElementById('rd-body').innerHTML = `<div class="rd-empty"><h3>${T('rd.noPortraitTitle')}</h3>
            <p class="cx-muted">${T('rd.noPortrait')}</p></div>`;
        document.getElementById('rd-doc-name').textContent = T('rd.portrait');
        document.querySelector('#cx-read .rd-toc-title').classList.add('hidden');
    }
}

function readRenderList(portrait) {
    const list = document.getElementById('rd-list');
    if (!list) return;
    const names = readState.files || [];
    const canGen = !window.VERBATIM_DEMO && cx.avail.cards;
    const item = (name, title, desc, have, key) => `
        <button type="button" class="rd-item${readState.doc === name ? ' on' : ''}${have ? '' : ' rd-missing'}"
            data-name="${escapeHtml(name)}" ${key ? `data-lens="${key}"` : ''} ${!have && !canGen ? 'disabled' : ''}>
            <span class="rd-item-t">${title}</span>
            <span class="rd-item-d">${desc}</span>
            <span class="rd-item-s" id="rd-s-${key || 'portrait'}">${have ? '' : (canGen ? T('rd.generate') : T('rd.notYet'))}</span>
        </button>`;
    const isCol = cx.chain && cx.chain.kind === 'collection';
    list.innerHTML = (portrait ? item(portrait, T(isCol ? 'rd.overview' : 'rd.portrait'), T(isCol ? 'rd.overviewDesc' : 'rd.portraitDesc'), true, '') : '')
        + `<div class="rd-sec-title rd-sub">${T('rd.otherAngles')}</div>`
        + LENS_KEYS.map(k => item(`镜头_${k}.md`, T('lens.' + k), T('lens.' + k + '.desc'),
            names.includes(`镜头_${k}.md`), k)).join('');
    list.querySelectorAll('.rd-item').forEach(b => b.onclick = () => {
        if (names.includes(b.dataset.name)) readOpen(b.dataset.name);
        else if (b.dataset.lens) readGenerate(b.dataset.lens);
    });
}

async function readOpen(name) {
    const id = cx.id;
    readState.doc = name;
    document.querySelectorAll('#rd-list .rd-item').forEach(b => b.classList.toggle('on', b.dataset.name === name));
    const body = document.getElementById('rd-body');
    const label = name.startsWith('镜头_') ? T('lens.' + name.slice(3, -3))
        : T(cx.chain && cx.chain.kind === 'collection' ? 'rd.overview' : 'rd.portrait');
    document.getElementById('rd-doc-name').textContent = label;
    body.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
    let md = '';
    try {
        const r = await fetch(`/api/chain/${id}/file?name=${encodeURIComponent(name)}`);
        md = r.ok ? await r.text() : '';
    } catch { /* 下面报错 */ }
    if (cx.id !== id || readState.doc !== name) return;
    // 模型偶尔在正文前留一句「好的，这是修订后的画像」：第一个标题之前的寒暄不显示
    const h = md.search(/^#\s/m);
    if (h > 0 && h < 400) md = md.slice(h);
    body.innerHTML = md ? renderMarkdown(md) : `<p class="cx-err">${T('common.couldNotLoad')}</p>`;
    // 本页目录：二级标题
    const toc = document.getElementById('rd-toc');
    const hs = [...body.querySelectorAll('h2, h3')];
    hs.forEach((h, i) => { h.id = 'rd-h-' + i; });
    toc.innerHTML = hs.map((h, i) => `<a href="#" data-h="${i}" class="${h.tagName === 'H3' ? 'rd-h3' : ''}">${escapeHtml(h.textContent)}</a>`).join('');
    toc.previousElementSibling.classList.toggle('hidden', !hs.length);
    toc.querySelectorAll('a').forEach(a => a.onclick = e => {
        e.preventDefault();
        document.getElementById('rd-h-' + a.dataset.h).scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    if (window.scrollY > document.getElementById('chain-explore').offsetTop) {
        document.getElementById('chain-explore').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

async function readGenerate(lens) {
    const id = cx.id;
    const status = document.getElementById('rd-s-' + lens);
    if (status) status.textContent = T('rd.generating');
    try {
        const r = await (await fetch(`/api/chain/${id}/lens`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lens }) })).json();
        if (r.error) { if (status) status.textContent = r.error.slice(0, 40); return; }
        const done = () => {
            if (cx.id !== id) return;
            readState.files = [...(readState.files || []), `镜头_${lens}.md`];
            readRenderList(cx.chain && cx.chain.final_doc);
            readOpen(`镜头_${lens}.md`);
        };
        if (r.ready) { done(); return; }
        let n = 60;
        const poll = async () => {
            if (cx.id !== id) return;
            if (n-- <= 0) { if (status) status.textContent = T('chainDetail.stillGenerating'); return; }
            try {
                const g = await (await fetch(`/api/chain/${id}/lens/${lens}`)).json();
                if (g.ready) { done(); return; }
                if (g.error) { if (status) status.textContent = T('chainDetail.generationFailed', { message: g.error.slice(0, 40) }); return; }
            } catch { /* 抖动忽略 */ }
            cx.timers.lens = setTimeout(poll, 3000);
        };
        cx.timers.lens = setTimeout(poll, 3000);
    } catch { if (status) status.textContent = T('chainDetail.generationFailedGeneric'); }
}

// ================= 出处芯片 + 来源列表（问答、对比、订阅摘要共用）=================
function citeLabel(c) {
    const who = c.creator ? escapeHtml(String(c.creator).slice(0, 14)) + ' · ' : '';
    return `${who}EP${c.ep_no}${c.ts ? ' · ' + c.ts : ''}`;
}

function citedHtml(md, citations) {
    // 先按 Markdown 渲染（会转义），[#3-12] 里没有要转义的字符，渲染后原样还在
    return renderMarkdown(md || '').replace(CITE_RE, (m, cid) => {
        const c = citations[cid];
        if (!c) return '';
        return `<button type="button" class="cx-cite" data-cid="${escapeHtml(cid)}"
            title="${escapeHtml(c.quote.slice(0, 200))}">${citeLabel(c)}</button>`;
    });
}

function citedOrder(md, citations) {
    const out = [];
    for (const m of String(md || '').matchAll(CITE_RE)) {
        if (citations[m[1]] && !out.includes(m[1])) out.push(m[1]);
    }
    Object.keys(citations).forEach(k => { if (!out.includes(k)) out.push(k); });
    return out;
}

function sourceItemHtml(cid, c) {
    const transcript = c.task_id
        ? `<button type="button" class="cx-link" data-go="detail/${escapeHtml(c.task_id)}${c.sec != null ? '/t/' + c.sec : ''}">${T('cx.openTranscript', { ts: c.ts || '00:00' })}</button>` : '';
    const watch = c.video_url
        ? `<a class="cx-link" href="${safeUrl(c.video_url)}" target="_blank" rel="noopener">${T('cx.watch', { ts: c.ts || '' })}</a>` : '';
    const stance = c.stance && c.stance !== 'none' ? `<span class="cx-stance st-${c.stance}">${T('stance.' + c.stance)}</span>` : '';
    return `<div class="cx-src" data-cid="${escapeHtml(cid)}" data-st="${escapeHtml(c.stance || 'none')}">
        <blockquote class="cc-quote">${escapeHtml(c.quote || c.obs)}</blockquote>
        ${c.quote && c.obs ? `<div class="cc-obs"><span class="cx-ai-tag">${T('cx.aiNote')}</span> ${escapeHtml(c.obs)}</div>` : ''}
        <div class="cx-src-foot">
            ${c.creator ? `<b>${escapeHtml(c.creator)}</b>` : ''}
            ${c.speaker ? `<span class="cx-who">🎙 ${escapeHtml(c.speaker)}</span>` : ''}
            <span>EP${c.ep_no} · ${escapeHtml(c.episode || '')}</span>
            ${c.date ? `<span>${escapeHtml(c.date)}</span>` : ''}${stance}
        </div>
        <div class="cx-src-actions">${transcript}${watch}${typeof clipButtonHtml === 'function' ? clipButtonHtml(c) : ''}
            <button type="button" class="cx-link" data-share="${escapeHtml(cid)}">${T('share.title')}</button></div>
    </div>`;
}

function sourcesHtml(citations, md) {
    const ids = citedOrder(md, citations);
    if (!ids.length) return '';
    return `<details class="cx-sources"><summary>${T('cx.sources', { n: ids.length })}</summary>
        ${ids.map(k => sourceItemHtml(k, citations[k])).join('')}</details>`;
}

// 给某块 HTML 里的芯片 / 跳转 / 分享按钮挂事件
function wireCitations(root, citations) {
    rememberCites(citations);
    root.querySelectorAll('.cx-cite').forEach(b => b.addEventListener('click', () => {
        const block = b.closest('.cx-msg, .cx-digest, .cmp-result') || root;
        const det = block.querySelector('.cx-sources');
        if (!det) return;
        det.open = true;
        const it = det.querySelector(`.cx-src[data-cid="${CSS.escape(b.dataset.cid)}"]`);
        if (it) {
            det.querySelectorAll('.cx-src.hl').forEach(x => x.classList.remove('hl'));
            it.classList.add('hl');
            it.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }));
    root.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));
    root.querySelectorAll('[data-share]').forEach(b => b.addEventListener('click', () => {
        const c = (citations && citations[b.dataset.share]) || cxCiteCache[b.dataset.share];
        if (c) openShareCard({ ...c, author: c.creator || (cx.cards && cx.cards.author) || '' });
    }));
}

// 所有渲染过的出处都记一份，分享按钮用
const cxCiteCache = {};
function rememberCites(cites) { Object.assign(cxCiteCache, cites || {}); }

// ================= 问 =================
let askState = { mode: 'about', busy: false, messages: [], starters: [] };

async function askLoad() {
    const id = cx.id;
    const box = document.getElementById('cx-ask');
    askState = { mode: cxStore('cx.mode') === 'as' ? 'as' : 'about', busy: false, messages: [], starters: [] };
    const canAsk = !window.VERBATIM_DEMO || window.VERBATIM_DEMO_ASK;
    box.innerHTML = `
        <div class="cx-ask-grid">
            <div class="cx-ask-main">
                <div class="cx-intro" id="cx-intro">
                    <h3>${T(cx.chain && cx.chain.kind === 'collection' ? 'cx.introTitleCol' : 'cx.introTitle', { name: escapeHtml((cx.cards && cx.cards.author) || '') })}</h3>
                    <ul>
                        <li>${T('cx.intro1', { n: (cx.cards && cx.cards.cards || []).length, m: (cx.cards && cx.cards.episodes || []).length })}</li>
                        <li>${T('cx.intro2')}</li>
                        <li>${T('cx.intro3')}</li>
                    </ul>
                </div>
                <div class="cx-thread" id="cx-thread"></div>
                ${canAsk ? `<form class="cx-input-row" id="cx-form">
                    <textarea id="cx-q" rows="1" maxlength="2000" placeholder="${escapeHtml(T('cx.placeholder'))}"></textarea>
                    <button class="btn-primary cx-send" type="submit">${T('cx.send')}</button>
                </form>` : `<p class="cx-muted">${T('cx.demoOff')}</p>`}
            </div>
            <aside class="cx-ask-side">
                <div class="cx-seg" role="radiogroup" aria-label="${T('cx.mode')}">
                    <button type="button" data-mode="about" role="radio">${T('cx.modeAbout')}</button>
                    <button type="button" data-mode="as" role="radio">${T('cx.modeAs')}</button>
                </div>
                <p class="cx-muted" id="cx-mode-hint"></p>
                <div class="cx-starters" id="cx-starters"></div>
                <div class="cx-side-actions">
                    <span id="cx-exp-all"></span>
                    <button type="button" class="cx-link cx-clear" id="cx-clear">${T('cx.clear')}</button>
                </div>
            </aside>
        </div>`;
    box.querySelectorAll('.cx-seg button').forEach(b => b.onclick = () => {
        askState.mode = b.dataset.mode;
        cxStore('cx.mode', askState.mode);
        askModeUi();
    });
    askModeUi();
    box.querySelector('#cx-clear').onclick = async () => {
        if (!askState.messages.length) return;
        await fetch(`/api/chain/${id}/ask`, { method: 'DELETE' });
        askState.messages = [];
        askRenderThread();
    };
    const form = box.querySelector('#cx-form');
    if (form) {
        const q = form.querySelector('#cx-q');
        const grow = () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 180) + 'px'; };
        q.addEventListener('input', grow);
        q.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
        });
        form.addEventListener('submit', e => {
            e.preventDefault();
            const text = q.value.trim();
            if (!text || askState.busy) return;
            q.value = ''; grow();
            askSend(text);
        });
    }
    try {
        const h = await (await fetch(`/api/chain/${id}/ask`)).json();
        if (cx.id !== id) return;
        askState.messages = h.messages || [];
    } catch { /* 读不到历史就当新对话 */ }
    askRenderThread();
    askLoadStarters(id);
    // 从话题雷达「问他」跳过来：带着问题
    let pend = null;
    try { pend = JSON.parse(cxStore('cx.pendingAsk') || 'null'); } catch { /* 无 */ }
    if (pend && pend.id === id && pend.q) {
        cxStore('cx.pendingAsk', '');
        askSend(pend.q);
    }
}

function askModeUi() {
    document.querySelectorAll('#cx-ask .cx-seg button').forEach(b => {
        const on = b.dataset.mode === askState.mode;
        b.classList.toggle('on', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    const hint = document.getElementById('cx-mode-hint');
    if (hint) hint.textContent = askState.mode === 'as' ? T('cx.modeAsHint') : T('cx.modeAboutHint');
}

async function askLoadStarters(id) {
    const box = document.getElementById('cx-starters');
    if (!box || (window.VERBATIM_DEMO && !window.VERBATIM_DEMO_ASK)) return;
    box.innerHTML = `<div class="cx-muted cx-thinking">${T('cx.startersLoading')}</div>`;
    try {
        const r = await (await fetch(`/api/chain/${id}/ask/starters?lang=${currentLang}`)).json();
        if (cx.id !== id) return;
        askState.starters = r.starters || [];
    } catch { return; }
    askRenderStarters();
}

function askRenderStarters() {
    const box = document.getElementById('cx-starters');
    if (!box) return;
    if (!askState.starters.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<div class="cx-muted">${T('cx.tryAsking')}</div>` + askState.starters.map(q =>
        `<button type="button" class="cx-starter">${escapeHtml(q)}</button>`).join('');
    box.querySelectorAll('.cx-starter').forEach(b => b.onclick = () => askSend(b.textContent));
}

function coverageText(cov) {
    if (!cov) return '';
    const topic = cov.topic ? T('cx.covTopic', { t: cov.topic }) + ' · ' : '';
    if (cov.mode === 'all') return topic + T('cx.covAll', { n: cov.pool_cards, m: cov.pool_episodes });
    if (cov.mode === 'spread') return topic + T('cx.covSpread', { n: cov.pool_cards, m: cov.pool_episodes, k: cov.cards_used });
    return topic + T('cx.covSearch', { n: cov.pool_cards, m: cov.pool_episodes, k: cov.cards_used, h: cov.keyword_hits });
}

function msgHtml(m, i) {
    if (m.role === 'user') {
        const topic = m.topic ? `<span class="cx-topic-pill">${escapeHtml(m.topic)}</span>` : '';
        return `<div class="cx-msg cx-user">${topic}${escapeHtml(m.content)}</div>`;
    }
    if (m.pending) {
        return `<div class="cx-msg cx-bot"><div class="cx-thinking">${T('cx.reading')}</div></div>`;
    }
    if (m.error) {
        return `<div class="cx-msg cx-bot"><div class="cx-err">${escapeHtml(m.error)}</div></div>`;
    }
    rememberCites(m.citations);
    const sim = m.mode === 'as' ? `<div class="cx-sim">${T('cx.simLabel')}</div>` : '';
    const cost = m.cost_usd ? ' · ' + fmtUsd(m.cost_usd) : '';
    const dropped = m.dropped_citations ? ' · ' + T('cx.dropped', { n: m.dropped_citations }) : '';
    const q = (askState.messages[i - 1] || {}).content || '';
    const exp = typeof exportMenuHtml === 'function' ? exportMenuHtml(() => ({
        title: `${(cx.cards && cx.cards.author) || ''}：${q}`.slice(0, 80),
        sub: m.mode === 'as' ? T('cx.simLabel') : '',
        blocks: [{ heading: T('exp.question'), md: q }, { heading: T('exp.answer'), md: m.content, citations: m.citations }],
    })) : '';
    return `<div class="cx-msg cx-bot" data-i="${i}">${sim}
        <div class="cx-answer md-body">${citedHtml(m.content, m.citations || {})}</div>
        ${sourcesHtml(m.citations || {}, m.content)}
        <div class="cx-cov">${escapeHtml(coverageText(m.coverage))}${cost}${escapeHtml(dropped)} ${exp}</div>
    </div>`;
}

function askRenderThread() {
    const box = document.getElementById('cx-thread');
    if (!box) return;
    box.innerHTML = askState.messages.map(msgHtml).join('');
    box.querySelectorAll('.cx-msg.cx-bot[data-i]').forEach(el =>
        wireCitations(el, askState.messages[+el.dataset.i].citations));
    const clear = document.getElementById('cx-clear');
    if (clear) clear.classList.toggle('hidden', !askState.messages.length);
    const intro = document.getElementById('cx-intro');
    if (intro) intro.classList.toggle('hidden', !!askState.messages.length);
    const expAll = document.getElementById('cx-exp-all');
    if (expAll && typeof exportMenuHtml === 'function') {
        const done = askState.messages.filter(x => x.role === 'assistant' && !x.pending && !x.error);
        expAll.innerHTML = done.length > 1 ? exportMenuHtml(() => ({
            title: T('exp.convTitle', { name: (cx.cards && cx.cards.author) || '' }),
            blocks: askState.messages.flatMap((x, k) => x.role === 'user' ? [{ heading: x.content, md: '' }]
                : (x.pending || x.error ? [] : [{ md: x.content, citations: x.citations }])),
        }), 'exp-all') : '';
    }
}

async function askSend(question, topic) {
    if (askState.busy) return;
    const id = cx.id;
    askState.busy = true;
    askState.messages.push({ role: 'user', content: question, topic: topic || null });
    const pending = { role: 'assistant', pending: true };
    askState.messages.push(pending);
    askRenderThread();
    const thread = document.getElementById('cx-thread');
    if (thread && thread.lastElementChild) thread.lastElementChild.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    let msg;
    try {
        const resp = await fetch(`/api/chain/${id}/ask`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question, mode: askState.mode, topic: topic || undefined, ui_lang: currentLang }),
        });
        const r = await resp.json();
        msg = r.ok ? r.message : { role: 'assistant', error: r.error || T('common.couldNotLoad') };
    } catch (e) {
        msg = { role: 'assistant', error: String(e) };
    }
    askState.busy = false;
    if (cx.id !== id) return;
    askState.messages[askState.messages.indexOf(pending)] = msg;
    askRenderThread();
    const last = document.querySelector('#cx-thread .cx-msg:last-child');
    if (last) last.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ================= 话题 / 立场时间线 =================
let topicsState = { data: null, sel: null, shown: 24, col: null, hlSt: null, cards: {} };

// 话题下的卡按期分组，只先摆 shown 张（一个话题可能上百张卡）
function tpPage(byEp) {
    let left = topicsState.shown;
    const out = [];
    for (const g of byEp) {
        if (left <= 0) break;
        out.push({ ...g, cards: g.cards.slice(0, left) });
        left -= g.cards.length;
    }
    return out;
}
function tpLeft(byEp) {
    return Math.max(0, byEp.reduce((n, g) => n + g.cards.length, 0) - topicsState.shown);
}

async function topicsLoad() {
    const id = cx.id;
    const box = document.getElementById('cx-topics');
    let d;
    try { d = await (await fetch(`/api/chain/${id}/topics`)).json(); } catch {
        box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return;
    }
    if (cx.id !== id) return;
    const job = d.job && d.job.status === 'running' ? d.job : null;
    const djob = d.dates_job && d.dates_job.status === 'running' ? d.dates_job : null;
    // 已经在看话题了、后台还在打标签 / 补日期：只更新进度行，别整页重画（会把滚动位置和筛选冲掉）
    const prog = box.querySelector('#tp-progress');
    if (prog && (job || djob) && topicsState.data && topicsState.data.tagged) {
        prog.textContent = job ? T('tp.tagging', { a: job.done, b: job.total || '…' })
            : T('tp.fetchingDates', { a: djob.done, b: djob.total || '…' });
        cx.timers.topics = setTimeout(topicsLoad, 5000);
        return;
    }
    const wasRunning = topicsState.data && topicsState.data.running;
    topicsState.data = d;
    d.running = !!(job || djob);
    if (job || djob) cx.timers.topics = setTimeout(topicsLoad, 5000);
    if (wasRunning || !topicsState.cards) topicsState.cards = {};

    if (!d.tagged) {
        const st = d.status || {};
        box.innerHTML = `<div class="cx-empty">
            <h3>${T('tp.untaggedTitle')}</h3>
            <p>${T('tp.untaggedBody', { n: st.cards || 0 })}</p>
            ${job ? `<p class="cx-muted">${T('tp.tagging', { a: job.done, b: job.total || '…' })}</p>`
                : (d.job && d.job.status === 'error' ? `<p class="cx-err">${escapeHtml(d.job.error)}</p>` : '')}
            ${window.VERBATIM_DEMO ? '' : `<button class="btn-primary cx-send" id="tp-tag" type="button" ${job ? 'disabled' : ''}>
                ${T('tp.tagBtn', { cost: fmtUsd(Math.max(0.01, st.est_cost_usd || 0)) })}</button>`}
        </div>`;
        const b = box.querySelector('#tp-tag');
        if (b) b.onclick = async () => {
            b.disabled = true;
            await fetch(`/api/chain/${id}/tag`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
            topicsLoad();
        };
        return;
    }
    const tps = d.topics || [];
    if (!topicsState.sel || !tps.find(t => t.topic === topicsState.sel)) topicsState.sel = tps.length ? tps[0].topic : null;
    const datesNote = d.has_dates ? '' : `<div class="cx-note">${T('tp.noDates')}
        ${window.VERBATIM_DEMO ? '' : (djob ? `<span class="cx-muted">${T('tp.fetchingDates', { a: djob.done, b: djob.total || '…' })}</span>`
            : `<button class="cx-link" type="button" id="tp-dates">${T('tp.fetchDates')}</button>`)}</div>`;
    const tagNote = d.tagged < d.cards_total ? `<span class="cx-muted">${T('tp.partial', { a: d.tagged, b: d.cards_total })}</span>` : '';
    box.innerHTML = `
        <div class="tp-head"><div class="cx-muted">${T('tp.sub', { n: tps.length })} ${tagNote}
            <span id="tp-progress" class="cx-muted">${job ? T('tp.tagging', { a: job.done, b: job.total || '…' }) : ''}</span></div></div>
        ${datesNote}
        <div class="tp-chips">${tps.map(t => `<button type="button" class="tp-chip${t.topic === topicsState.sel ? ' on' : ''}" data-t="${escapeHtml(t.topic)}">
            <span class="tp-name">${escapeHtml(t.topic)}</span><span class="tp-n">${t.count}</span>${stanceBar(t.stances, t.count)}</button>`).join('')}</div>
        <div id="tp-detail"></div>`;
    box.querySelectorAll('.tp-chip').forEach(b => b.onclick = () => {
        topicsState.sel = b.dataset.t;
        topicsState.shown = 24;
        topicsState.col = null;
        box.querySelectorAll('.tp-chip').forEach(x => x.classList.toggle('on', x === b));
        topicRenderDetail();
    });
    const db = box.querySelector('#tp-dates');
    if (db) db.onclick = async () => {
        db.disabled = true;
        const r = await (await fetch(`/api/chain/${id}/dates`, { method: 'POST' })).json();
        if (r.error) { db.outerHTML = `<span class="cx-err">${escapeHtml(r.error)}</span>`; return; }
        topicsLoad();
    };
    topicRenderDetail();
}

let cxResizeT = null;
window.addEventListener('resize', () => {
    clearTimeout(cxResizeT);
    cxResizeT = setTimeout(() => {
        if (cx.tab === 'topics' && topicsState.data && document.getElementById('tp-detail')) topicRenderDetail();
    }, 200);
});

const STANCE_ORDER = ['pro', 'mixed', 'neutral', 'con', 'none'];
function stanceBar(st, total) {
    return `<span class="tp-bar">${STANCE_ORDER.filter(k => st[k]).map(k =>
        `<i class="st-bg-${k}" style="width:${(st[k] / total * 100).toFixed(1)}%"></i>`).join('')}</span>`;
}

// 时间线分桶：期数多（夸克说 166 期）时一期一列会挤成一团，合成约 30 段；每段 = 一段时间或连续几期
const TP_MAX_COLS = 36;
function tpBuckets(d, t) {
    const epNos = [...new Set(t.points.map(p => p[0]))];
    const info = n => d.eps[n] || ['', '', 0];
    const useDates = d.has_dates && epNos.every(n => info(n)[0]);
    let keyOf, labelOf;
    if (useDates) {
        const ts = epNos.map(n => Date.parse(info(n)[0]));
        const lo = Math.min(...ts), hi = Math.max(...ts);
        const nb = Math.min(TP_MAX_COLS, epNos.length);
        const span = Math.max(1, hi - lo);
        keyOf = n => nb <= 1 ? 0 : Math.min(nb - 1, Math.floor((Date.parse(info(n)[0]) - lo) / span * nb));
        labelOf = eps => {
            const ds = eps.map(n => info(n)[0]).sort();
            return ds[0] === ds[ds.length - 1] ? ds[0] : `${ds[0]} – ${ds[ds.length - 1]}`;
        };
        const cols = new Map();
        epNos.forEach(n => { const k = keyOf(n); if (!cols.has(k)) cols.set(k, []); cols.get(k).push(n); });
        return { useDates, lo, hi, nb, cols, labelOf,
                 xFrac: k => nb <= 1 ? 0.5 : (k + 0.5) / nb };
    }
    const sorted = epNos.slice().sort((x, y) => info(x)[2] - info(y)[2]);
    const per = Math.max(1, Math.ceil(sorted.length / TP_MAX_COLS));
    const cols = new Map();
    sorted.forEach((n, i) => { const k = Math.floor(i / per); if (!cols.has(k)) cols.set(k, []); cols.get(k).push(n); });
    const nb = cols.size;
    labelOf = eps => eps.length === 1 ? `EP${eps[0]}` : T('tp.nEps', { n: eps.length });
    return { useDates, nb, cols, labelOf, xFrac: k => nb <= 1 ? 0.5 : k / (nb - 1) };
}

async function topicRenderDetail() {
    const box = document.getElementById('tp-detail');
    const d = topicsState.data;
    const t = (d.topics || []).find(x => x.topic === topicsState.sel);
    if (!box || !t) { if (box) box.innerHTML = ''; return; }
    // 这个话题的卡片按需取（全量带卡，大博主会有二十来 MB）
    topicsState.cards = topicsState.cards || {};
    let cards = topicsState.cards[t.topic];
    if (!cards) {
        box.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
        const id = cx.id, want = t.topic;
        try {
            const r = await (await fetch(`/api/chain/${id}/topics?topic=${encodeURIComponent(want)}`)).json();
            const hit = (r.topics || []).find(x => x.topic === want);
            cards = topicsState.cards[want] = (hit && hit.cards) || [];
        } catch { box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return; }
        if (cx.id !== id || topicsState.sel !== want) return;
    }
    cards.forEach(c => { cxCiteCache[c.id] = c; });

    // 时间线：横轴时间（或期的先后），纵轴三道——看好在上、看空在下；气泡面积 ∝ 张数
    const B = tpBuckets(d, t);
    const W = Math.max(280, Math.round((box.clientWidth || 1000) - 150)), H = 132, padL = 24, padR = 24;
    const lane = { pro: 24, mixed: 60, mid: 60, con: 96 };
    const colOf = new Map();
    B.cols.forEach((eps, k) => eps.forEach(n => colOf.set(n, k)));
    const agg = new Map();
    t.points.forEach(([n, st, cnt]) => {
        const L = st === 'neutral' || st === 'none' ? 'mid' : st;
        const k = colOf.get(n) + '|' + L;
        if (!agg.has(k)) agg.set(k, { col: colOf.get(n), L, n: 0, by: {} });
        const g = agg.get(k);
        g.n += cnt; g.by[st] = (g.by[st] || 0) + cnt;
    });
    const maxN = Math.max(1, ...[...agg.values()].map(g => g.n));
    const rMax = Math.min(18, Math.max(6, (W - padL - padR) / B.nb / 2 - 1));
    const dots = [...agg.values()].map(g => {
        const x = padL + B.xFrac(g.col) * (W - padL - padR);
        const y = lane[g.L];
        const r = Math.max(3.5, rMax * Math.sqrt(g.n / maxN));
        const eps = B.cols.get(g.col);
        const label = `${B.labelOf(eps)} — ` + Object.entries(g.by).map(([k, v]) => `${T('stance.' + k)} ${v}`).join(', ');
        return `<g class="st-dot" data-col="${g.col}" data-st="${g.L}" tabindex="0" role="button" aria-label="${escapeHtml(label)}">
            <circle cx="${x.toFixed(1)}" cy="${y}" r="${r.toFixed(1)}" class="st-fill-${g.L === 'mid' ? 'neutral' : g.L}"/>
            ${g.n > 1 && r >= 8 ? `<text x="${x.toFixed(1)}" y="${y + 3.5}" text-anchor="middle" class="st-n">${g.n}</text>` : ''}
            <title>${escapeHtml(label)}</title></g>`;
    }).join('');
    const allEps = [...B.cols.values()].flat();
    const first = B.useDates ? B.labelOf(B.cols.get(Math.min(...B.cols.keys()))).split(' – ')[0] : T('tp.older');
    const last = B.useDates ? B.labelOf(B.cols.get(Math.max(...B.cols.keys()))).split(' – ').pop() : T('tp.newer');
    const mid = B.useDates ? '' : (B.nb < allEps.length ? T('tp.byOrderBinned', { n: Math.ceil(allEps.length / B.nb) }) : T('tp.byOrder'));
    const svg = `<svg class="tp-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="group" aria-label="${escapeHtml(T('tp.timelineAria', { t: t.topic }))}">
        <line x1="${padL}" x2="${W - padR}" y1="24" y2="24" class="tp-lane"/><line x1="${padL}" x2="${W - padR}" y1="60" y2="60" class="tp-lane"/>
        <line x1="${padL}" x2="${W - padR}" y1="96" y2="96" class="tp-lane"/>
        ${dots}</svg>`;

    // 卡片列表：按期分组；点了气泡就只看那一段
    const filter = topicsState.col != null ? new Set(B.cols.get(topicsState.col) || []) : null;
    const byEp = [];
    cards.forEach(c => {
        if (filter && !filter.has(c.ep_no)) return;
        let g = byEp.find(x => x.ep_no === c.ep_no);
        if (!g) byEp.push(g = { ep_no: c.ep_no, title: c.episode, date: c.date, cards: [] });
        g.cards.push(c);
    });
    const st = t.stances;
    const canAsk = !window.VERBATIM_DEMO || window.VERBATIM_DEMO_ASK;
    const shown = filter ? byEp : tpPage(byEp);
    box.innerHTML = `
        <div class="tp-card">
            <div class="tp-title-row">
                <h3>${escapeHtml(t.topic)}</h3>
                <span class="cx-muted">${T('tp.stats', { n: t.count, m: t.episodes })}</span>
                ${canAsk ? `<button class="btn-secondary cx-send" type="button" id="tp-ask">${T('tp.askChange')}</button>` : ''}
                ${typeof exportMenuHtml === 'function' ? exportMenuHtml(() => topicExportDoc(t, cards)) : ''}
            </div>
            <div class="tp-legend">${STANCE_ORDER.filter(k => st[k]).map(k =>
                `<span><i class="st-fill-${k}"></i>${T('stance.' + k)} ${st[k]}</span>`).join('')}</div>
            <div class="tp-chart">
                <div class="tp-ylab"><span>${T('stance.pro')}</span><span>${T('tp.laneMid')}</span><span>${T('stance.con')}</span></div>
                ${svg}
            </div>
            <div class="tp-axis"><span>${escapeHtml(first)}</span><span>${escapeHtml(mid)}</span><span>${escapeHtml(last)}</span></div>
        </div>
        ${filter ? `<div class="cx-note">${T('tp.oneCol', { what: escapeHtml(B.labelOf([...filter])), n: byEp.reduce((a, g) => a + g.cards.length, 0) })}
            <button class="cx-link" type="button" id="tp-all">${T('tp.allEps')}</button></div>` : ''}
        <div class="tp-list">${shown.map(g => `
            <div class="tp-ep" data-ep="${g.ep_no}"><div class="tp-ep-h">${g.date ? `<b>${escapeHtml(g.date)}</b> · ` : ''}EP${g.ep_no} · ${escapeHtml(g.title || '')}</div>
            ${g.cards.map(c => sourceItemHtml(c.id, c)).join('')}</div>`).join('')}</div>
        ${!filter && tpLeft(byEp) ? `<button class="btn-secondary cc-more" type="button" id="tp-more">${T('cards.more', { n: tpLeft(byEp) })}</button>` : ''}`;
    const ask = box.querySelector('#tp-ask');
    if (ask) ask.onclick = () => {
        cxShowTab('ask');
        const go = () => askSend(T('tp.askChangeQ', { t: t.topic }), t.topic);
        setTimeout(go, document.querySelector('#cx-thread') ? 300 : 800);   // 问答页第一次打开要先加载历史
    };
    const all = box.querySelector('#tp-all');
    if (all) all.onclick = () => { topicsState.col = null; topicsState.hlSt = null; topicRenderDetail(); };
    if (filter) {
        const want = topicsState.hlSt === 'mid' ? ['neutral', 'none'] : [topicsState.hlSt];
        const it = [...box.querySelectorAll('.tp-list .cx-src')].find(x => want.includes(x.dataset.st));
        if (it) { it.classList.add('hl'); it.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    }
    const more = box.querySelector('#tp-more');
    if (more) more.onclick = () => { topicsState.shown += 24; topicRenderDetail(); };
    const jump = dot => {
        topicsState.col = +dot.dataset.col;
        topicsState.hlSt = dot.dataset.st;
        topicRenderDetail();
    };
    box.querySelectorAll('.st-dot').forEach(dot => {
        dot.addEventListener('click', () => jump(dot));
        dot.addEventListener('keydown', e => { if (e.key === 'Enter') jump(dot); });
    });
    wireCitations(box);
}

function topicExportDoc(t, cards) {
    const cites = {};
    const lines = [];
    let lastEp = null;
    cards.forEach(c => {
        cites[c.id] = c;
        if (c.ep_no !== lastEp) {
            lines.push('', `### ${c.date ? c.date + ' · ' : ''}EP${c.ep_no} ${c.episode || ''}`);
            lastEp = c.ep_no;
        }
        lines.push(`- 〔${T('stance.' + (c.stance || 'none'))}〕${c.obs} [#${c.id}]`);
    });
    const st = STANCE_ORDER.filter(k => t.stances[k]).map(k => `${T('stance.' + k)} ${t.stances[k]}`).join(' · ');
    return { title: `${(cx.cards && cx.cards.author) || ''}：${t.topic}`,
             sub: T('tp.stats', { n: t.count, m: t.episodes }) + ' · ' + st,
             blocks: [{ md: lines.join('\n'), citations: cites }] };
}

// ================= 预测记账 =================
let predsState = { v: null, shown: 30 };

async function predsLoad() {
    const id = cx.id;
    const box = document.getElementById('cx-preds');
    let d;
    try { d = await (await fetch(`/api/chain/${id}/predictions`)).json(); } catch {
        box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return;
    }
    if (cx.id !== id) return;
    const job = d.job && d.job.status === 'running' ? d.job : null;
    if (job) cx.timers.preds = setTimeout(predsLoad, 4000);
    if (!d.tagged) {
        box.innerHTML = `<div class="cx-empty"><h3>${T('pr.untaggedTitle')}</h3><p>${T('pr.untaggedBody')}</p>
            <button class="btn-secondary cx-send" type="button" id="pr-go-topics">${T('pr.goTopics')}</button></div>`;
        box.querySelector('#pr-go-topics').onclick = () => cxShowTab('topics');
        return;
    }
    const items = d.items || [];
    items.forEach(i => { cxCiteCache[i.id] = i; });
    const c = d.counts || {};
    const unchecked = (c.unchecked || 0) + (c.pending || 0);
    const score = d.resolved >= 5
        ? `<div class="pr-score"><div class="n">${Math.round(d.hit_rate * 100)}%</div>
            <div class="l">${T('pr.hitRate', { a: c.true || 0, b: d.resolved })}</div></div>`
        : `<div class="pr-score pr-score-na"><div class="l">${T('pr.tooFew', { n: d.resolved || 0 })}</div></div>`;
    // 判定筛选：点徽章只看这一类（马司库有三百多条，全摆出来没法看）
    const counts = ['true', 'false', 'pending', 'unclear', 'unchecked', 'na'].filter(k => c[k]).map(k =>
        `<button type="button" class="pr-badge pr-${k} pr-filter${predsState.v === k ? ' on' : ''}" data-v="${k}">${T('pr.v.' + k)} ${c[k]}</button>`).join('');
    // 「不算预测」默认不列，点那个徽章才看
    const verdictOf = i => (i.check || {}).verdict || 'unchecked';
    const list = items.filter(i => predsState.v ? verdictOf(i) === predsState.v : verdictOf(i) !== 'na');
    const err = d.job && d.job.status === 'error' ? `<p class="cx-err">${escapeHtml(d.job.error)}</p>` : '';
    box.innerHTML = `
        <div class="pr-head">
            ${score}
            <div class="pr-meta">
                <div>${T('pr.found', { n: items.length - (c.na || 0) })}</div>
                <div class="pr-counts">${counts}</div>
                ${job ? `<div class="cx-muted">${T('pr.checking', { a: job.done, b: job.total || '…' })}</div>`
                    : (window.VERBATIM_DEMO || !unchecked ? '' : `<button class="btn-primary cx-send" type="button" id="pr-check">${T('pr.checkBtn', { n: unchecked, cost: fmtUsd(Math.max(0.01, Math.ceil(unchecked / 8) * 0.021)) })}</button>`)}
                ${err}
                <div class="cx-muted">${T('pr.rules')}</div>
                ${typeof exportMenuHtml === 'function' ? exportMenuHtml(() => ({
                    title: `${(cx.cards && cx.cards.author) || ''}：${T('cx.tab.predictions')}`,
                    sub: d.resolved >= 5 ? T('pr.hitRate', { a: c.true || 0, b: d.resolved }) + ' ' + Math.round(d.hit_rate * 100) + '%' : '',
                    blocks: [{ md: items.filter(i => ((i.check || {}).verdict || 'unchecked') !== 'na').map(i =>
                        `- **${T('pr.v.' + ((i.check || {}).verdict || 'unchecked'))}** ${i.obs} [#${i.id}]${(i.check || {}).why ? '\n  ' + i.check.why : ''}`).join('\n'),
                        citations: Object.fromEntries(items.map(i => [i.id, i])) }] })) : ''}
            </div>
        </div>
        <div class="pr-list">${list.slice(0, predsState.shown).map(i => predItemHtml(i)).join('') || `<p class="cx-muted">${T('pr.none')}</p>`}</div>
        ${list.length > predsState.shown ? `<button class="btn-secondary cc-more" type="button" id="pr-more">${T('cards.more', { n: list.length - predsState.shown })}</button>` : ''}`;
    box.querySelectorAll('.pr-filter').forEach(f => f.onclick = () => {
        predsState.v = predsState.v === f.dataset.v ? null : f.dataset.v;
        predsState.shown = 30;
        predsLoad();
    });
    const pm = box.querySelector('#pr-more');
    if (pm) pm.onclick = () => { predsState.shown += 60; predsLoad(); };
    const b = box.querySelector('#pr-check');
    if (b) b.onclick = async () => {
        b.disabled = true;
        await fetch(`/api/chain/${id}/predictions/check`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        predsLoad();
    };
    wireCitations(box);
}

function predItemHtml(i) {
    const ck = i.check || {};
    const v = ck.verdict || 'unchecked';
    // 一条预测一张卡：上面是他的原话和出处，下面是核对结论
    return `<div class="pr-item pr-item-${v}">
        ${sourceItemHtml(i.id, i)}
        <div class="pr-check">
            <div class="pr-verdict"><span class="pr-badge pr-${v}">${T('pr.v.' + v)}</span>
                ${ck.checked_at ? `<span class="cx-muted">${T('pr.checkedAt', { d: ck.checked_at })}</span>` : ''}
                ${ck.source ? `<a class="cx-link" href="${safeUrl(ck.source)}" target="_blank" rel="noopener">${T('pr.source')} ↗</a>` : ''}</div>
            ${ck.why ? `<div class="pr-why">${escapeHtml(ck.why)}</div>` : ''}
        </div>
    </div>`;
}

// ================= 跨博主对比 =================
function compareOpen() {
    const ov = document.getElementById('compare-overlay');
    ov.classList.remove('hidden');
    const pick = document.getElementById('cmp-pick');
    pick.innerHTML = `<p class="cx-muted">${T('cx.reading')}</p>`;
    fetch('/api/chains').then(r => r.json()).then(chains => {
        const seen = new Set();
        const list = chains.filter(c => c.has_cards && !c.merged_into).filter(c => {
            const k = (c.author || '') + '|' + normalizeChainUrl(c.url);
            if (seen.has(k)) return false;
            seen.add(k); return true;
        });
        const saved = (cxStore('cmp.pick') || '').split(',');
        pick.innerHTML = list.map(c => `<label class="cmp-item">
            <input type="checkbox" value="${escapeHtml(c.id)}" ${saved.includes(c.id) ? 'checked' : ''}>
            <span>${escapeHtml(chainDisplayName(c))}</span>
            <span class="cx-muted">${(c.videos || []).filter(v => v.status === 'done').length} ${T('cmp.eps')}</span></label>`).join('')
            || `<p class="cx-muted">${T('cmp.noneReady')}</p>`;
    });
    setTimeout(() => document.getElementById('cmp-q').focus(), 50);
}

async function compareRun(e) {
    e.preventDefault();
    const ids = [...document.querySelectorAll('#cmp-pick input:checked')].map(i => i.value);
    const q = document.getElementById('cmp-q').value.trim();
    const out = document.getElementById('cmp-out');
    if (ids.length < 2 || ids.length > 4) { out.innerHTML = `<p class="cx-err">${T('cmp.pickN')}</p>`; return; }
    if (!q) return;
    cxStore('cmp.pick', ids.join(','));
    out.innerHTML = `<div class="cx-thinking">${T('cx.reading')}</div>`;
    let r;
    try {
        r = await (await fetch('/api/compare', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ chains: ids, question: q }) })).json();
    } catch (err) { r = { error: String(err) }; }
    if (r.error) { out.innerHTML = `<p class="cx-err">${escapeHtml(r.error)}</p>`; return; }
    rememberCites(r.citations);
    out.innerHTML = `<div class="cmp-result">
        <div class="cx-answer md-body">${citedHtml(r.answer, r.citations)}</div>
        ${sourcesHtml(r.citations, r.answer)}
        <div class="cx-cov">${(r.creators || []).map(c => `${escapeHtml(c.author)}: ${c.cards}`).join(' · ')} ${T('cmp.cards')}
            ${typeof exportMenuHtml === 'function' ? exportMenuHtml(() => ({
                title: `${T('cmp.title')}：${q}`.slice(0, 80),
                sub: (r.creators || []).map(c => c.author).join(' · '),
                blocks: [{ md: r.answer, citations: r.citations }] })) : ''}</div></div>`;
    wireCitations(out, r.citations);
}

// ================= 分享图 =================
let shareCur = null;

function openShareCard(c) {
    shareCur = c;
    document.getElementById('share-overlay').classList.remove('hidden');
    document.getElementById('share-msg').textContent = '';
    const draw = () => drawShare(document.getElementById('share-canvas'), c);
    // 等衬线字体就绪再画，不然第一次会用回退字体
    (document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(draw);
}

function wrapLines(ctx, text, maxW) {
    // 中文按字断、英文按词断
    const tokens = String(text).match(/[㐀-鿿＀-￯　-〿]|[^\s㐀-鿿＀-￯　-〿]+|\s+/g) || [];
    const lines = [];
    let cur = '';
    for (const tk of tokens) {
        const next = cur + tk;
        if (ctx.measureText(next).width > maxW && cur.trim()) {
            lines.push(cur.trim());
            cur = tk.trim() ? tk : '';
        } else cur = next;
    }
    if (cur.trim()) lines.push(cur.trim());
    return lines;
}

function drawShare(cv, c) {
    const ctx = cv.getContext('2d');
    const W = cv.width, H = cv.height, P = 88;
    const css = getComputedStyle(document.documentElement);
    const col = n => css.getPropertyValue(n).trim() || '#000';
    const serif = css.getPropertyValue('--serif').trim() || 'Georgia, serif';
    const sans = css.getPropertyValue('--sans').trim() || 'sans-serif';
    ctx.fillStyle = col('--paper'); ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = col('--coral'); ctx.fillRect(0, 0, W, 14);
    // 作者
    ctx.fillStyle = col('--coral-deep');
    ctx.font = `700 30px ${sans}`;
    ctx.fillText(String(c.author || '').toUpperCase().slice(0, 40), P, P + 20);
    // 原话：从大号往下试，放得下为止
    const quote = `“${c.quote}”`;
    const boxTop = P + 70, boxBottom = H - 300;
    let size = 64, lines;
    for (; size >= 26; size -= 2) {
        ctx.font = `600 ${size}px ${serif}`;
        lines = wrapLines(ctx, quote, W - 2 * P);
        if (lines.length * size * 1.32 <= boxBottom - boxTop) break;
    }
    const maxLines = Math.floor((boxBottom - boxTop) / (size * 1.32));
    if (lines.length > maxLines) { lines = lines.slice(0, maxLines); lines[maxLines - 1] = lines[maxLines - 1].replace(/.{0,2}$/, '…”'); }
    ctx.fillStyle = col('--ink');
    lines.forEach((ln, i) => ctx.fillText(ln, P, boxTop + size + i * size * 1.32));
    // 出处
    let y = H - 250;
    ctx.fillStyle = col('--line-2'); ctx.fillRect(P, y, W - 2 * P, 2);
    y += 52;
    ctx.fillStyle = col('--ink-soft'); ctx.font = `600 28px ${sans}`;
    const ep = wrapLines(ctx, c.episode || '', W - 2 * P);
    ctx.fillText(ep[0] + (ep.length > 1 ? '…' : ''), P, y);
    y += 44;
    ctx.fillStyle = col('--muted'); ctx.font = `400 26px ${sans}`;
    const where = [c.date, c.ts ? '▶ ' + c.ts : ''].filter(Boolean).join('   ');
    ctx.fillText(where, P, y);
    y += 40;
    ctx.fillStyle = col('--coral-deep'); ctx.font = `400 22px ${sans}`;
    const link = String(c.video_url || '').replace(/^https?:\/\/(www\.)?/, '');
    ctx.fillText(link.length > 70 ? link.slice(0, 69) + '…' : link, P, y);
    // 品牌
    ctx.fillStyle = col('--faint'); ctx.font = `600 22px ${sans}`;
    ctx.fillText('Verbatim · ' + T('share.tagline'), P, H - 56);
}

function shareDownload() {
    const cv = document.getElementById('share-canvas');
    const c = shareCur || {};
    const name = `${(c.author || 'quote').replace(/[\\/:*?"<>|]/g, '')}-EP${c.ep_no || ''}${c.ts ? '-' + c.ts.replace(/:/g, '') : ''}.png`;
    cv.toBlob(b => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(b);
        a.download = name;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
    }, 'image/png');
}

async function shareCopy() {
    const c = shareCur || {};
    const msg = document.getElementById('share-msg');
    const text = `“${c.quote}” — ${c.author || ''}${c.video_url ? '\n' + c.video_url : ''}`;
    try { await navigator.clipboard.writeText(text); msg.textContent = T('share.copied'); }
    catch { msg.textContent = c.video_url || ''; }
}

// ----- 弹窗通用：点遮罩 / × / Esc 关闭 -----
['compare-overlay', 'share-overlay'].forEach(id => {
    const ov = document.getElementById(id);
    if (!ov) return;
    ov.addEventListener('click', e => {
        if (e.target === ov || e.target.closest('[data-close]')) ov.classList.add('hidden');
    });
});
document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    ['share-overlay', 'compare-overlay'].forEach(id => {
        const ov = document.getElementById(id);
        if (ov && !ov.classList.contains('hidden')) ov.classList.add('hidden');
    });
});
(function wireStatic() {
    const o = document.getElementById('compare-open');
    if (o && window.VERBATIM_DEMO && !window.VERBATIM_DEMO_ASK) o.closest('.creators-tools').remove();   // 演示版不花钱
    else if (o) o.addEventListener('click', compareOpen);
    const f = document.getElementById('cmp-form');
    if (f) f.addEventListener('submit', compareRun);
    const d = document.getElementById('share-download');
    if (d) d.addEventListener('click', shareDownload);
    const c = document.getElementById('share-copy');
    if (c) c.addEventListener('click', shareCopy);
})();

// 切界面语言：重画当前博主的这几块（档案头由 app.js 的 refreshChainDetail 重画）
document.addEventListener('langchange', () => {
    if (!cx.id || document.getElementById('chain-detail-view').classList.contains('hidden')) return;
    const tab = cx.tab;
    cx.loaded = {};
    ['cx-read', 'cx-ask', 'cx-topics', 'cx-preds'].forEach(i => { document.getElementById(i).innerHTML = ''; });
    cxFillHead();
    if (cx.sub) digestRender(cx.sub.digest);
    cx.tab = null;
    cxShowTab(tab, true);
});

// 直接打开博主页链接（刷新 / 书签）时，app.js 先路由到这一页、本文件还没加载：补一次初始化
if (typeof chainDetailId !== 'undefined' && chainDetailId && cx.id !== chainDetailId) exploreOpen(chainDetailId);
