// ========== 博主页：问证据卡 / 话题时间线 / 预测记账 / 证据卡（标签页）==========
// 后端逻辑在 ask.py；这里只管界面。每个回答里的 [#3-12] 都换成可点的出处芯片，
// 点开能看到原话、跳回那期转写的那一秒、跳到原视频的那一秒、生成分享图。
// 依赖 app.js 里的 T / escapeHtml / safeUrl / renderMarkdown / navigate / fmtUsd。

const CX_TABS = ['read', 'ask', 'topics', 'predictions', 'cards', 'episodes'];
const CX_CARD_TABS = ['ask', 'topics', 'predictions', 'cards'];
const CITE_RE = /\[#((?:[A-Z]:)?[dt]?\d+(?:_[0-9a-f]{8})?-\d+)\]/g;   // 3-12 卡片；d2-14 文档段落；t5-3 转写段落
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

// ----- 入口 1：打开项目（app.js openChainDetail）——先复位，中间默认是问答，Studio 里哪些能用等数据到了再定 -----
function exploreOpen(id) {
    cxClearTimers();
    cx = { id, cards: null, chain: null, sub: null, tab: null, avail: { read: null, cards: null },
           picked: false, loaded: {}, timers: {}, people: [], person: null };
    predsState = { v: null, shown: 30 };
    topicsState = { data: null, sel: null, shown: 24, col: null, hlSt: null, cards: {} };
    readState = { doc: null, files: null };
    ['cx-read', 'cx-ask', 'cx-topics', 'cx-preds'].forEach(i => { document.getElementById(i).innerHTML = ''; });
    document.getElementById('cx-digest').innerHTML = '';
    document.querySelectorAll('#chain-explore .cx-tab').forEach(b => {
        b.onclick = () => {
            if (b.classList.contains('off')) { showToast(T(cxHasRec() ? 'nb.needsCards' : 'nb.needsRec')); return; }
            cx.picked = true;
            // 上面的格子都是「做一份新的」：先弹设置框；做好的进下面列表，点列表里的才在工作台栏里展开
            const t = b.dataset.cx;
            if (t === 'read') { readPicker(); return; }
            if (VIEW_TABS.includes(t)) { viewPicker(t); return; }
            if (t === 'compare') { comparePicker(); return; }
            cxShowTab(t);
        };
    });
    if (typeof closeSourceReader === 'function') closeSourceReader();
    if (typeof nbShowPane === 'function') nbShowPane('main');
    cxApplyAvail();
    subLoad(id);
    peopleLoad();
    if (typeof srcLoad === 'function') srcLoad();
    if (typeof stOpen === 'function') stOpen(id);
    if (typeof lastChainDetail !== 'undefined' && lastChainDetail && lastChainDetail.id === id) cxFillHead(lastChainDetail);
}

// ----- 入口 2：证据卡到了（app.js loadChainCards）-----
function exploreInit(id, cardsResp) {
    if (cx.id !== id) exploreOpen(id);
    cx.cards = cardsResp;
    cxSetAvail({ cards: (cardsResp.cards || []).length > 0 });
    cxFillHead();
}

// 可用性：read = 有画像（或镜头）；cards = 有证据卡；src = 有能检索的原文（文档 / 入索引的录音）。null = 还不知道
function cxSetAvail(patch) {
    Object.assign(cx.avail, patch);
    cxApplyAvail();
}

function cxTabOk(t) {
    if (t === 'studio') return typeof stState !== 'undefined' && !!stState.cur;     // 工作台里生成的一份
    if (t === 'episodes') return !!(cx.chain && (cx.chain.videos || []).some(v => v.task_id));
    const pc = cxPeopleCards();                                      // 项目里引用的博主有卡也算
    if (t === 'read') return !!cx.avail.read || !!cx.avail.cards || pc || cx.people.some(p => p.portrait);   // 没画像但有卡：还能在这里生成镜头
    if (t === 'ask') return (!!cx.avail.cards || !!cx.avail.src || pc) && CAN_ASK();
    if (t === 'compare') return cx.people.filter(p => p.has_cards).length >= 2 && CAN_ASK();
    return CX_CARD_TABS.includes(t) && (!!cx.avail.cards || pc);
}

// Studio 的格子一直摆着，暂时用不了的变灰（点了说为什么）；中间默认是问答
function cxApplyAvail() {
    const person = !!(cx.chain && cx.chain.url);
    // 只有文档、没有录音的项目：画像 / 立场 / 预测 / 原话 / 录音这几格根本用不上，不摆
    const noRec = !!cx.chain && !cxHasRec();
    document.querySelectorAll('#chain-explore .cx-tab.nb-tile').forEach(b => {
        const ok = cxTabOk(b.dataset.cx);
        b.classList.toggle('off', !ok);
        b.classList.toggle('hidden', noRec || (b.dataset.cx === 'compare' && !cxTabOk('compare')));   // 对比：项目里至少两个人有卡
        b.setAttribute('aria-disabled', ok ? 'false' : 'true');
        // 悬停说明：能用的写这是什么，用不了的写缺什么
        b.title = !ok && b.dataset.cx !== 'episodes'
            ? T(cxHasRec() ? 'nb.needsCardsShort' : 'nb.needsRecShort') : T('nb.tile.' + b.dataset.cx + (person ? '' : 'Col'));
    });
    const want = cx.tab && cxTabOk(cx.tab) ? cx.tab : (cxTabOk('ask') ? 'ask' : null);
    // 数据还没到齐：先别摆「还没有来源」
    if (!want && (cx.avail.cards === null || cx.avail.src === undefined)) return;
    if (want !== cx.tab || !want) cxShowTab(want, true);
}

// 画像 / 立场 / 预测 / 原话都是从录音里抽的；只有文档的项目点了要说清楚缺的是录音
function cxHasRec() {
    return !!(cx.chain && (cx.chain.url || (cx.chain.videos || []).length)) || (cx.people || []).length > 0;
}

// ================= 项目里的人（一个项目可以有好几个博主）=================
// 画像、镜头、立场、预测、原话跟着「人」走：每个博主在自己的链条里算一次，几个项目共用。
// 这几格打开时顶上一排人名，点谁看谁；问答 / 报告 / 闪卡跟着左栏勾选的来源走，不受这个影响。
const PERSON_TABS = ['read', 'topics', 'predictions', 'cards'];
const VIEW_TABS = ['topics', 'predictions', 'cards'];      // 现算的视图：做一份 = 在列表里记一条「谁的哪一样」
function cxPid() { return cx.person || cx.id; }
function cxPerson(id) { return (cx.people || []).find(p => p.chain_id === (id || cxPid())) || null; }
function cxMulti() { return (cx.people || []).length > 1; }
function cxPeopleCards() { return (cx.people || []).some(p => p.has_cards && p.chain_id !== cx.id); }

function personName(p) {
    if (!p) return '';
    if (p.self && !p.url) return T('pp.loose');          // 没频道的项目自己抽过卡的那些录音
    return p.name || T('pp.unnamed');
}

function personFace(p, size) {
    const s = size || 22;
    if (p.avatar) return `<img class="pp-face" src="${escapeHtml(p.avatar)}" width="${s}" height="${s}" alt="" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'pp-face pp-ini',textContent:'${escapeHtml(personName(p).slice(0, 1))}'}))">`;
    return `<span class="pp-face pp-ini">${escapeHtml(p.emoji || personName(p).slice(0, 1))}</span>`;
}

function peopleChipsHtml() {
    return cx.people.map(p => `<button type="button" class="pp-chip${p.chain_id === cxPid() ? ' on' : ''}" data-pid="${escapeHtml(p.chain_id)}"
        ${p.has_cards || p.portrait ? '' : `title="${escapeHtml(T('pp.notYet'))}"`}>${personFace(p)}<span>${escapeHtml(personName(p))}</span>
        ${p.has_cards || p.portrait ? '' : `<span class="pp-wait">${T('pp.wait')}</span>`}</button>`).join('');
}

async function peopleLoad() {
    const id = cx.id;
    if (!id) return;
    let r;
    try { r = await (await fetch(`/api/chain/${id}/people`)).json(); } catch { return; }
    if (cx.id !== id || !r || !r.people) return;
    const sig = ps => JSON.stringify(ps.map(p => [p.chain_id, p.has_cards, p.portrait, p.lenses, p.final_doc]));
    const changed = sig(r.people) !== sig(cx.people || []);
    cx.people = r.people;
    if (!cx.person || !cxPerson(cx.person)) {
        const first = cx.people.find(p => p.has_cards) || cx.people[0];
        cx.person = first ? first.chain_id : null;
    }
    if (!changed) return;
    if (cx.chain) cxFillHead(cx.chain);          // 「人物画像」还是「综述」跟着项目里有没有博主变
    cxApplyAvail();
    if (typeof stLoad === 'function' && stState.id === id) stLoad();     // 工作台列表里每个人的画像 / 镜头
}

// 换人：按人看的几格都作废重读，正开着的那格马上换成这个人的
function cxSetPerson(pid) {
    if (!pid || pid === cxPid()) return;
    cx.person = pid;
    PERSON_TABS.forEach(t => { delete cx.loaded[t]; });
    predsState = { v: null, shown: 30 };
    topicsState = { data: null, sel: null, shown: 24, col: null, hlSt: null, cards: {} };
    readState = { doc: null, files: null };
    ['cx-read', 'cx-topics', 'cx-preds'].forEach(i => { document.getElementById(i).innerHTML = ''; });
    const tab = cx.tab;
    if (tab === 'read') { cx.loaded.read = true; readLoad(); }
    else if (tab === 'topics') { cx.loaded.topics = true; topicsLoad(); }
    else if (tab === 'predictions') { cx.loaded.predictions = true; predsLoad(); }
    else if (tab === 'cards' && typeof loadChainCards === 'function') loadChainCards(pid);
    if (VIEW_TABS.includes(tab) && cxMulti()) nbvTitle(personName(cxPerson()));
}

function nbEmptyHtml() {
    return `<div class="nb-empty-in"><div class="nb-empty-ic">👋</div>
        <h3>${T('nb.emptyT')}</h3><p>${T('nb.emptyD')}</p>
        ${window.VERBATIM_DEMO ? '' : `<button type="button" class="btn-primary cx-send" onclick="openAddSources()">＋ ${T('as.title')}</button>`}</div>`;
}

// 中间栏永远是对话；工作台里的东西（画像、立场、预测、原话、录音、生成的报告 / 闪卡……）在工作台这一栏里展开，
// 跟 NotebookLM 一样——工作台变宽、对话往左收，来源栏留着对照原文。面板本身挪进 #nbv-body，
// 关掉再挪回中间栏，各面板自己的代码不用知道这回事
const NBV_WIDE = new Set(['topics', 'predictions', 'cards', 'episodes']);   // 表格类，给工作台多分点宽度
function cxShowTab(tab, auto) {
    const viewer = !!tab && tab !== 'ask';
    cx.tab = tab;
    document.querySelectorAll('#chain-explore .cx-tab:not(.nb-tile)').forEach(b => {
        const on = b.dataset.cx === tab;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const ex = document.getElementById('chain-explore');
    const main = document.querySelector('#chain-explore .nb-main');
    const body = document.getElementById('nbv-body');
    if (main && body) {
        [...body.children].forEach(p => { if (p.dataset.cxPanel !== tab) main.appendChild(p); });
        const p = viewer && main.querySelector(`:scope > [data-cx-panel="${tab}"]`);
        if (p) body.appendChild(p);
    }
    if (ex) {
        ex.classList.toggle('nbv-on', viewer);
        ex.classList.toggle('nbv-wide', viewer && NBV_WIDE.has(tab));
        if (viewer) ex.classList.remove('studio-min');   // 工作台收着也得展开，不然打开的东西看不见
    }
    const askOn = cxTabOk('ask');
    document.querySelectorAll('#chain-detail-view [data-cx-panel]').forEach(p => {
        const k = p.dataset.cxPanel;
        p.classList.toggle('hidden', !(k === tab || (k === 'ask' && askOn && (tab === 'ask' || viewer))));
    });
    const empty = document.getElementById('nb-empty');
    if (empty) {
        const show = !tab || (viewer && !askOn);
        empty.classList.toggle('hidden', !show);
        if (show) empty.innerHTML = nbEmptyHtml();
    }
    // 面包屑「工作台 › 格子名」；格子类的不再重复一个大标题，下面一行讲清楚这是什么
    const nbv = document.getElementById('nb-viewer');
    if (nbv) {
        nbv.classList.toggle('hidden', !viewer);
        document.getElementById('nbv-acts').innerHTML = '';
        const tile = document.querySelector(`#chain-explore .nb-tile[data-cx="${tab}"] .nb-tl`);
        nbvKind(tab === 'read' ? T(readIsCol() ? 'st.kindReadCol' : 'st.kindRead')
            : tab === 'episodes' ? T('nb.episodes') : tile ? tile.textContent : '');
        // 按人看的：标题写是谁的（项目里不止一个人时）
        nbvTitle(VIEW_TABS.includes(tab) && cxMulti() ? personName(cxPerson()) : '');
        const back = document.querySelector('#nbv-x span');
        if (back) back.textContent = T(tab === 'episodes' ? 'nb.sources' : 'nb.studio');     // 全部录音是从来源栏打开的
        const colKey = 'cx.desc.' + tab + 'Col';
        const isCol = cx.chain && !cx.chain.url;
        document.getElementById('nbv-desc').textContent = !viewer || tab === 'studio' || tab === 'read' ? ''
            : T(isCol && T(colKey) !== colKey ? colKey : 'cx.desc.' + tab);
        if (viewer) { nbFit(); body.scrollTop = 0; }
    }
    if (tab === 'cards' && typeof chainCards !== 'undefined' && chainCards.id !== cxPid()) loadChainCards(cxPid());
    document.querySelectorAll('#st-list .st-item').forEach(el => el.classList.toggle('on',
        viewer && typeof stState !== 'undefined' && el.dataset.id === stOpenId()));
    if (typeof nbShowPane === 'function' && window.innerWidth < 1080) {
        if (viewer) nbShowPane('studio');
        else if (!auto) nbShowPane('main');
    }
    if (askOn && !cx.loaded.ask && (tab === 'ask' || viewer)) { cx.loaded.ask = true; askLoad(); }
    if (!tab || cx.loaded[tab]) return;
    cx.loaded[tab] = true;
    if (tab === 'read') readLoad();
    else if (tab === 'topics') topicsLoad();
    else if (tab === 'predictions') predsLoad();
}

// 工作台列表里哪一条正开着（报告等是 stState.cur；画像 / 镜头是阅读器里那份）
function stOpenId() {
    if (cx.tab === 'studio') return stState.cur ? stState.cur.id : '';
    if (cx.tab === 'read' && readState.doc) return 'doc:' + cxPid() + ':' + readState.doc;
    if (VIEW_TABS.includes(cx.tab)) {
        const v = (stState.items || []).find(o => o.kind === 'view' && o.view === cx.tab && o.chain === cxPid());
        return v ? v.id : '';
    }
    return '';
}

function nbvTitle(text) {
    const el = document.getElementById('nbv-title');
    if (el) el.textContent = text || '';
}

function nbvKind(text) {
    const el = document.getElementById('nbv-kind');
    if (el) el.textContent = text || '';
}

function nbvClose() {
    if (!cx.tab || cx.tab === 'ask') return;
    cxShowTab(cxTabOk('ask') ? 'ask' : null, true);
}

(function wireViewer() {
    ['nbv-x', 'nbv-shrink'].forEach(id => {
        const b = document.getElementById(id);
        if (b) b.addEventListener('click', nbvClose);
    });
    document.addEventListener('keydown', e => {
        if (e.key !== 'Escape' || document.querySelector('.settings-overlay:not(.hidden)') || document.getElementById('pj-pop')) return;
        const nbv = document.getElementById('nb-viewer');
        if (nbv && !nbv.classList.contains('hidden')) nbvClose();
    });
})();

// ----- 档案头里的两个空位：修辞三指标（#cp-rh）、订阅按钮（#cp-sub）。
// 档案头每次轮询都会整个重画，所以数据缓存在 cx 上，重画后由 app.js 调这里补回去 -----
function cxFillHead(chain) {
    if (chain && chain.id === cx.id) {
        cx.chain = chain;
        const person = !!chain.url;
        // 能检索的原文：文档、项目里额外加的录音，或者开了「转写全文入索引」的录音
        const src = (chain.docs || []).length > 0 || (chain.recordings || []).length > 0
            || (!!chain.index_transcripts && (chain.videos || []).some(v => v.status === 'done'));
        if (cx.avail.src !== src) cxSetAvail({ src });
        const rd = document.querySelector('#chain-explore .nb-tile[data-cx=read] .nb-tl');
        if (rd) rd.textContent = T(person || (cx.people || []).some(p => p.url) ? 'cx.tab.read' : 'cx.tab.readCol');
        const read = !!chain.final_doc || !!chain.analyze && chain.stage === 'done' && !!cx.avail.cards;
        if (cx.avail.read !== read) cxSetAvail({ read });
        cxApplyAvail();
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
    askHeroFill();
    nbFit();
    // 工作台列表里的画像要知道哪份文件是画像（chain.final_doc）：项目数据第一次到时补读一次
    if (cx.chain && cx._stDocsFor !== cx.chain.id && typeof stLoad === 'function') { cx._stDocsFor = cx.chain.id; stLoad(); }
}

// 宽屏时三栏正好一屏高：量出三栏顶到页面顶的距离给 CSS（档案头会因为进度条、报错多一行，所以每次重画都量）
function nbFit() {
    const nb = document.getElementById('chain-explore');
    if (!nb || !nb.offsetParent) return;
    const top = Math.round(nb.getBoundingClientRect().top + window.scrollY);
    document.documentElement.style.setProperty('--nb-top', top + 'px');
}
window.addEventListener('resize', () => { clearTimeout(nbFit.t); nbFit.t = setTimeout(nbFit, 120); });

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
    const isCol = cx.chain && ['collection', 'project'].includes(cx.chain.kind);      // 合集没有频道可订阅
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
            <summary class="btn-secondary cp-btn cx-sub-btn on" title="${escapeHtml(T('sub.f.' + iv) + T('sub.syncing'))}">↻<span class="sub-label"> ${T('sub.f.' + iv)}${T('sub.syncing')} ▾</span></summary>
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
        : `<button class="btn-secondary cp-btn cx-sub-btn" type="button" id="sub-on" title="${escapeHtml(T('sub.offHint'))}">↻<span class="sub-label"> ${T('sub.follow')}</span></button>`;
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

// 画像 / 镜头：大窗口里只放正文（一栏、阅读宽度、不套框），标题和「基于几期 · 生成于何时」在窗口顶栏，
// 重新生成 / 单独打开在右上角。换哪一篇：工作台「人物画像」格子弹出的选择框，或者工作台下面的列表
async function readLoad() {
    const id = cxPid();
    const box = document.getElementById('cx-read');
    box.innerHTML = `<article class="rd-solo"><div class="md-body rd-body" id="rd-body"><div class="cx-thinking">${T('cx.loading')}</div></div></article>`;
    let files = [];
    try {
        const j = await (await fetch(`/api/chain/${id}/files`)).json();
        files = Array.isArray(j) ? j : (j.files || []);
    } catch { /* 当没有 */ }
    if (cxPid() !== id) return;
    const names = files.map(f => (typeof f === 'string' ? f : f.name));
    readState.files = names;
    const fd = cxFinalDoc();
    const portrait = fd && names.includes(fd) ? fd : null;
    const want = readState.want && names.includes(readState.want) ? readState.want : null;
    readState.want = null;
    const first = want || portrait || LENS_KEYS.map(k => `镜头_${k}.md`).find(n => names.includes(n));
    if (first) readOpen(first);
    else {
        document.getElementById('rd-body').innerHTML = `<div class="rd-empty"><h3>${T('rd.noPortraitTitle')}</h3>
            <p class="cx-muted">${T('rd.noPortrait')}</p></div>`;
        nbvTitle('');
    }
}

function readIsCol() {
    const p = cxPerson();
    if (p) return !p.url && ['collection', 'project'].includes(p.kind);      // 项目里看的是某个人
    return !!(cx.chain && ['collection', 'project'].includes(cx.chain.kind));
}

// 正在看的那个人的画像文件名（项目自己的就是 chain.final_doc）
function cxFinalDoc() {
    const p = cxPerson();
    if (p && p.chain_id !== cx.id) return p.final_doc || '';
    return (cx.chain || {}).final_doc || (p && p.final_doc) || '';
}

function readLabel(name) {
    return name.startsWith('镜头_') ? T('lens.' + name.slice(3, -3)) : T(readIsCol() ? 'rd.overview' : 'rd.portrait');
}

// 「基于 6 期 · 生成于 09-27 12:45」
function readMeta(name) {
    const p = cxPerson();
    const n = p && p.chain_id !== cx.id ? p.episodes : ((cx.cards && cx.cards.episodes) || []).length;
    const doc = typeof stState !== 'undefined' ? (stState.docs || []).find(d => d.file === name && d.chain === cxPid()) : null;
    return [n ? T(readIsCol() ? 'rd.basedRec' : 'rd.basedEp', { n }) : '',
        doc && doc.created_at ? T('rd.madeAt', { at: String(doc.created_at).slice(5, 16) }) : ''].filter(Boolean).join(' · ');
}

async function readOpen(name) {
    const id = cxPid();
    readState.doc = name;
    const body = document.getElementById('rd-body');
    if (!body) return;
    if (cx.tab === 'read') {
        const lensName = /^镜头_/.test(name);
        nbvKind(T(lensName ? 'st.kindLens' : readIsCol() ? 'st.kindReadCol' : 'st.kindRead'));
        nbvTitle(readLabel(name));
        const desc = document.getElementById('nbv-desc');
        if (desc) desc.textContent = readMeta(name);
        const acts = document.getElementById('nbv-acts');
        if (acts) {
            const lens = (name.match(/^镜头_(\w+)\.md$/) || [])[1];
            acts.innerHTML = `${lens && !window.VERBATIM_DEMO ? `<button type="button" class="nbv-act" id="rd-regen">${T('rd.regen')}</button>` : ''}
                <button type="button" class="nbv-act" id="rd-full">${T('rd.openFull')}</button>`;
            const regen = acts.querySelector('#rd-regen');
            if (regen) regen.onclick = () => { if (confirm(T('rd.regenConfirm', { name: readLabel(name) }))) readGenerate(lens, true); };
            acts.querySelector('#rd-full').onclick = () => navigate(`chain/${id}/doc/${encodeURIComponent(name)}`);
        }
    }
    document.querySelectorAll('#st-list .st-item').forEach(el => el.classList.toggle('on', el.dataset.id === 'doc:' + id + ':' + name));
    body.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
    let md = '';
    try {
        const r = await fetch(`/api/chain/${id}/file?name=${encodeURIComponent(name)}`);
        md = r.ok ? await r.text() : '';
    } catch { /* 下面报错 */ }
    if (cxPid() !== id || readState.doc !== name) return;
    // 模型偶尔在正文前留一句「好的，这是修订后的画像」：第一个标题之前的寒暄不显示
    const h = md.search(/^#\s/m);
    if (h > 0 && h < 400) md = md.slice(h);
    body.innerHTML = md ? renderMarkdown(md) : `<p class="cx-err">${T('common.couldNotLoad')}</p>`;
    const nbvBody = document.getElementById('nbv-body');
    if (nbvBody) nbvBody.scrollTop = 0;
}

// 「人物画像」格子：先选看哪一篇（像 Gemini 生成前的设置框）。生成过的直接打开，没生成的点了就后台生成，
// 生成好跟别的产出一样出现在工作台下面的列表里
function readPicker() {
    let ov = document.getElementById('rd-pick');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'rd-pick';
        ov.className = 'settings-overlay hidden';
        document.body.appendChild(ov);
    }
    toolOverlay('rd-pick');
    const chain = cx.chain || {};
    const docs = (typeof stState !== 'undefined' && stState.docs) || [];
    const has = f => docs.find(d => d.file === f && d.chain === cxPid());
    const me = cxPerson();
    const canGen = !window.VERBATIM_DEMO && (me ? !!me.has_cards : !!cx.avail.cards);
    const busy = l => typeof stState !== 'undefined' && stState.lensBusy.has(cxPid() + ':' + l);
    const card = (file, title, desc, lens) => {
        const d = has(file);
        const status = busy(lens) ? `<span class="st-spin" aria-hidden="true"></span>${T('rd.generating')}`
            : d ? T('rd.madeAt', { at: String(d.created_at || '').slice(5, 16) })
            : lens ? (canGen ? T('rd.notMade') : T('rd.notYet')) : T('rd.noPortraitShort');
        const act = busy(lens) ? '' : d ? `<button type="button" class="rd-pick-go" data-open="${escapeHtml(file)}">${T('rd.open')}</button>`
            : lens && canGen ? `<button type="button" class="rd-pick-go primary" data-gen="${lens}">${T('rd.generate')}</button>` : '';
        return `<div class="rd-pick-card${d ? ' have' : ''}">
            <b>${escapeHtml(title)}</b><span class="rd-pick-d">${escapeHtml(desc)}</span>
            <div class="rd-pick-f"><span class="rd-pick-s">${status}</span>${act}</div></div>`;
    };
    const col = readIsCol();
    ov.innerHTML = `<div class="cx-modal rd-pick" role="dialog" aria-modal="true" aria-labelledby="rd-pick-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="rd-pick-t">${escapeHtml(T(col ? 'cx.tab.readCol' : 'cx.tab.read'))}</h2>
        <p class="cx-muted">${T(col ? 'rd.pickDCol' : 'rd.pickD')}</p>
        ${cxMulti() ? `<div class="pp-bar" id="rd-pick-pp">${peopleChipsHtml()}</div>` : ''}
        <div class="rd-pick-grid">
            ${card(cxFinalDoc() || '-', T(col ? 'rd.overview' : 'rd.portrait'), T(col ? 'rd.overviewDesc' : 'rd.portraitDesc'), '')}
            ${LENS_KEYS.map(k => card(`镜头_${k}.md`, T('lens.' + k), T('lens.' + k + '.desc'), k)).join('')}
        </div></div>`;
    ov.querySelectorAll('[data-open]').forEach(b => b.onclick = () => { ov.classList.add('hidden'); stOpenDoc(b.dataset.open, cxPid()); });
    ov.querySelectorAll('#rd-pick-pp [data-pid]').forEach(b => b.onclick = () => { cxSetPerson(b.dataset.pid); readPicker(); });
    ov.querySelectorAll('[data-gen]').forEach(b => b.onclick = () => {
        ov.classList.add('hidden');
        readGenerate(b.dataset.gen);
        showToast(T('rd.genStarted', { name: T('lens.' + b.dataset.gen) }));
    });
    ov.classList.remove('hidden');
}

async function readGenerate(lens, force) {
    const id = cxPid();
    const proj = cx.id;
    const key = id + ':' + lens;
    const status = document.getElementById('rd-s-' + lens);
    if (status) status.textContent = T('rd.generating');
    if (force) {                                                  // 重新生成：正文那里也提示一下
        const body = document.getElementById('rd-body');
        if (body && readState.doc === `镜头_${lens}.md`) body.innerHTML = `<div class="cx-thinking">${T('rd.regenerating')}</div>`;
    }
    if (typeof stLensBusy === 'function') stLensBusy(key, true);
    try {
        const r = await (await fetch(`/api/chain/${id}/lens`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ lens, force: !!force }) })).json();
        if (r.error) { if (status) status.textContent = r.error.slice(0, 40); if (typeof stLensBusy === 'function') stLensBusy(key, false); return; }
        const done = () => {
            if (typeof stLensBusy === 'function') stLensBusy(key, false);
            if (cx.id !== proj || cxPid() !== id) return;
            if (!(readState.files || []).includes(`镜头_${lens}.md`)) readState.files = [...(readState.files || []), `镜头_${lens}.md`];
            // 正开着这一篇（重新生成）：换成新版；没开着就只在列表里出现，不打断人
            if (cx.tab === 'read' && readState.doc === `镜头_${lens}.md`) readOpen(`镜头_${lens}.md`);
            else showToast(T('rd.genDone', { name: T('lens.' + lens) }));
        };
        if (r.ready) { done(); return; }
        let n = 60;
        const poll = async () => {
            if (cx.id !== proj) return;
            if (n-- <= 0) {
                if (status) status.textContent = T('chainDetail.stillGenerating');
                if (typeof stLensBusy === 'function') stLensBusy(key, false);
                return;
            }
            try {
                const g = await (await fetch(`/api/chain/${id}/lens/${lens}`)).json();
                if (g.ready) { done(); return; }
                if (g.error) {
                    if (status) status.textContent = T('chainDetail.generationFailed', { message: g.error.slice(0, 40) });
                    if (typeof stLensBusy === 'function') stLensBusy(key, false);
                    return;
                }
            } catch { /* 抖动忽略 */ }
            cx.timers.lens = setTimeout(poll, 3000);
        };
        cx.timers.lens = setTimeout(poll, 3000);
    } catch {
        if (status) status.textContent = T('chainDetail.generationFailedGeneric');
        if (typeof stLensBusy === 'function') stLensBusy(key, false);
    }
}

// ================= 出处芯片 + 来源列表（问答、对比、订阅摘要共用）=================
function citeLabel(c) {
    const who = c.creator ? escapeHtml(String(c.creator).slice(0, 14)) + ' · ' : '';
    const lab = escapeHtml(c.label || 'EP' + c.ep_no);
    if (c.kind === 'doc') {             // 文档：短标题 + 第几页（DOC1 这种编号用户认不出是哪份）
        const t = String(c.episode || c.label || '');
        // 没页码的（粘贴的文字、Markdown、网页）带上小标题，不然同一份文档的几个出处长得一模一样；
        // 小标题就是文档标题（网页开头那个 # 标题）的不重复写
        let h = !c.page && c.heading ? String(c.heading).replace(/^#+\s*/, '') : '';
        if (h && (t.startsWith(h) || h.startsWith(t))) h = '';
        const where = c.page ? ' · p.' + c.page : (h ? ' · ' + escapeHtml(h.length > 10 ? h.slice(0, 9) + '…' : h) : '');
        return `${escapeHtml(t.length > 12 ? t.slice(0, 11) + '…' : t)}${where}`;
    }
    return `${who}${lab}${c.ts ? ' · ' + c.ts : ''}`;
}

function citedHtml(md, citations, order) {
    // 先按 Markdown 渲染（会转义），[#3-12] 里没有要转义的字符，渲染后原样还在。
    // 出处画成小小的数字圆点（跟 Gemini 一样，正文不被一串「EP1 · 00:48」打断）：
    // 编号 = 在这段文字里第一次出现的顺序，跟下面「出处」列表的编号对得上；
    // 悬停看「EP1 · 00:48 + 原话」，点开照样跳到那一秒 / 那一页。
    // order 给了就按它编号（闪卡、自测题一段段分开画，用整份产出的统一顺序）
    order = order || citedOrder(md, citations);
    const one = `\\[#(?:[A-Z]:)?[dt]?\\d+(?:_[0-9a-f]{8})?-\\d+\\]`;
    return renderMarkdown(md || '').replace(new RegExp(`(?:${one}\\s*)+`, 'g'), run => {
        const seen = new Set();
        let out = '';
        for (const m of run.matchAll(CITE_RE)) {
            const c = citations[m[1]];
            if (!c || seen.has(m[1])) continue;
            seen.add(m[1]);
            const n = order.indexOf(m[1]) + 1;
            out += `<button type="button" class="cx-cite" data-cid="${escapeHtml(m[1])}"
            title="${citeLabel(c)} — ${escapeHtml(c.quote.slice(0, 200))}">${n || '·'}</button>`;
        }
        return out + (/\s$/.test(run) ? ' ' : '');
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

function sourceItemHtml(cid, c, n) {
    const num = n ? `<span class="cx-src-num">${n}</span>` : '';     // 跟正文里的数字圆点对得上
    if (c.kind === 'doc') {             // 文档段落：没有「AI 转述」，原文就是全部；按钮是「在文档里打开」
        const where = [c.page ? T('reader.page', { n: c.page }) : '', c.heading || ''].filter(Boolean).join(' · ');
        return `<div class="cx-src" data-cid="${escapeHtml(cid)}" data-st="none">${num}
            <blockquote class="cc-quote">${escapeHtml(c.quote)}</blockquote>
            <div class="cx-src-foot"><span>${escapeHtml(c.label || '')} · ${escapeHtml(c.episode || '')}</span>
                ${where ? `<span>${escapeHtml(where)}</span>` : ''}</div>
            <div class="cx-src-actions"><button type="button" class="cx-link" data-doc="${escapeHtml(c.doc_id)}"
                data-pi="${passageIndexOf(cid)}">${T('cx.openDoc')}</button></div>
        </div>`;
    }
    const transcript = c.task_id
        ? `<button type="button" class="cx-link" data-go="detail/${escapeHtml(c.task_id)}${c.sec != null ? '/t/' + c.sec : ''}">${T('cx.openTranscript', { ts: c.ts || '00:00' })}</button>` : '';
    const watch = c.video_url
        ? `<a class="cx-link" href="${safeUrl(c.video_url)}" target="_blank" rel="noopener">${T('cx.watch', { ts: c.ts || '' })}</a>` : '';
    const stance = c.stance && c.stance !== 'none' ? `<span class="cx-stance st-${c.stance}">${T('stance.' + c.stance)}</span>` : '';
    return `<div class="cx-src" data-cid="${escapeHtml(cid)}" data-st="${escapeHtml(c.stance || 'none')}">${num}
        <blockquote class="cc-quote">${escapeHtml(c.quote || c.obs)}</blockquote>
        ${c.quote && c.obs ? `<div class="cc-obs"><span class="cx-ai-tag">${T('cx.aiNote')}</span> ${escapeHtml(c.obs)}</div>` : ''}
        <div class="cx-src-foot">
            ${c.creator ? `<b>${escapeHtml(c.creator)}</b>` : ''}
            ${c.speaker ? `<span class="cx-who">🎙 ${escapeHtml(c.speaker)}</span>` : ''}
            <span>${escapeHtml(c.label || 'EP' + c.ep_no)} · ${escapeHtml(c.episode || '')}</span>
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
        ${ids.map((k, i) => sourceItemHtml(k, citations[k], i + 1)).join('')}</details>`;
}

// 给某块 HTML 里的芯片 / 跳转 / 分享按钮挂事件
function wireCitations(root, citations) {
    rememberCites(citations);
    root.querySelectorAll('.cx-cite').forEach(b => b.addEventListener('click', () => {
        const c = (citations && citations[b.dataset.cid]) || cxCiteCache[b.dataset.cid];
        // 出处点开在左栏看原文（跟 Gemini 一样）：文档跳到那一段，录音跳到那一秒
        if (c && c.kind === 'doc' && typeof openSourceReader === 'function') {
            openSourceReader(c.doc_id, passageIndexOf(b.dataset.cid));
        } else if (c && c.task_id && typeof openTranscriptViewer === 'function') {
            openTranscriptViewer(c.task_id, c.sec);
        }
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
    root.querySelectorAll('[data-doc]').forEach(b => b.addEventListener('click', () => {
        if (typeof openSourceReader === 'function') openSourceReader(b.dataset.doc, b.dataset.pi === 'null' ? null : +b.dataset.pi);
    }));
    root.querySelectorAll('[data-share]').forEach(b => b.addEventListener('click', () => {
        const c = (citations && citations[b.dataset.share]) || cxCiteCache[b.dataset.share];
        if (c) openShareCard({ ...c, author: c.creator || (cx.cards && cx.cards.author) || '' });
    }));
}

// 所有渲染过的出处都记一份，分享按钮用
const cxCiteCache = {};
function rememberCites(cites) { Object.assign(cxCiteCache, cites || {}); }

// ================= 问 =================
let askState = { mode: 'about', persona: null, busy: false, messages: [], starters: [] };

// ----- 回答方式（原来输入框上面那个「问关于他的事 / 模拟他回答」切换）-----
// 跟 NotebookLM 的「配置对话」一样收进对话栏右上角的设置里：默认状态不占地方；
// 换成模拟时，输入框里出一个能点掉的标签，一直看得见现在是什么状态。
function askPeople() {                // 能被模拟的人：有频道的项目自己 + 引用的博主（有卡的）
    const ppl = (cx.people || []).filter(p => p.has_cards && (p.url || !p.self));
    if (ppl.length) return ppl;
    const ch = cx.chain || {};
    return ch.url ? [{ chain_id: cx.id, name: chainDisplayName(ch), avatar: ch.avatar || '', url: ch.url }] : [];
}
function askPersona() {
    const ppl = askPeople();
    return ppl.find(p => p.chain_id === askState.persona) || ppl[0] || null;
}
function askSetMode(mode, persona) {
    askState.mode = mode === 'as' && askPeople().length ? 'as' : 'about';
    if (persona) askState.persona = persona;
    cxStore('cx.mode.' + cx.id, askState.mode);
    if (askState.persona) cxStore('cx.persona.' + cx.id, askState.persona);
    askModeUi();
}
function askCfgHtml() {
    const ppl = askPeople();
    const cur = askPersona();
    const opt = (mode, t, d, extra = '') => `<label class="cx-cfg-opt${askState.mode === mode ? ' on' : ''}">
        <input type="radio" name="cx-mode" value="${mode}" ${askState.mode === mode ? 'checked' : ''}>
        <span><b>${t}</b><span class="cx-muted">${d}</span>${extra}</span></label>`;
    const who = ppl.length > 1 ? `<div class="pp-bar cx-cfg-who">${ppl.map(p => `<button type="button" class="pp-chip${cur && p.chain_id === cur.chain_id ? ' on' : ''}"
        data-persona="${escapeHtml(p.chain_id)}">${personFace(p)}<span>${escapeHtml(personName(p))}</span></button>`).join('')}</div>` : '';
    return `<div class="cx-cfg-t">${T('cx.cfg')}</div>
        ${opt('about', T('cx.cfgAbout'), T((cx.people || []).length > 1 ? 'cx.cfgAboutDP' : 'cx.cfgAboutD'))}
        ${opt('as', T('cx.cfgAs'), T('cx.cfgAsD'), askState.mode === 'as' ? who : '')}`;
}

// 对话开头那块：表情 / 头像、项目名、「N 个来源 · 日期」、一句话。项目数据可能比对话区晚到，到了再补一遍（cxFillHead）
function askHeroHtml(ch, nSrc) {
    if (!ch || !ch.id) return '';
    const person = !!ch.url;
    const when = String(ch.updated_at || ch.finished_at || ch.created_at || '').slice(0, 10);
    // 换表情的入口在这儿（顶栏不放表情，跟 Gemini 一样）
    const face = typeof chainFaceHtml === 'function' ? chainFaceHtml(ch, 'hero-face') : '';
    return `${window.VERBATIM_DEMO ? face : `<button type="button" class="cx-hero-face" title="${escapeHtml(T('pjm.emoji'))}"
            aria-label="${escapeHtml(T('pjm.emoji'))}" onclick="pjEmojiPicker(this,'${ch.id}')">${face}</button>`}
        <h2 class="cx-hero-t">${escapeHtml(String(chainDisplayName(ch) || '').slice(0, 80))}</h2>
        <div class="cx-hero-m"><span id="cx-hero-n">${T(nSrc === 1 ? 'nb.nSourcesOne' : 'nb.nSources', { n: nSrc })}</span>${when ? ' · ' + escapeHtml(when) : ''}</div>
        <p class="cx-hero-d">${T(person ? 'cx.intro2' : 'cx.intro2P')}</p>`;
}
function askHeroFill() {
    const box = document.getElementById('cx-hero');
    if (!box || !cx.chain) return;
    const nSrc = typeof srcAllIds === 'function' && srcState.data ? srcAllIds(srcState.data).length : (cx.chain.n_sources || 0);
    const html = askHeroHtml(cx.chain, nSrc);
    if (box.dataset.sig !== html) { box.innerHTML = html; box.dataset.sig = html; }   // 轮询重画时别闪
}

async function askLoad() {
    const id = cx.id;
    const box = document.getElementById('cx-ask');
    // 证据卡常比项目数据先到：这时还不知道是不是频道项目（决定「问他 / 模拟他」切换、提示语），等一下再画
    if (!cx.chain || cx.chain.id !== id) {
        box.innerHTML = `<div class="cx-thinking">${T('cx.loading')}</div>`;
        const t0 = Date.now();
        while ((!cx.chain || cx.chain.id !== id) && Date.now() - t0 < 4000) await new Promise(r => setTimeout(r, 60));
        if (cx.id !== id) return;
    }
    // 「用他的口吻回答」只对有人的项目有意义（有频道 / 引用了博主）；纯资料项目问的是一堆材料，不给这个选项
    const ch = cx.chain || {};
    const person = !!ch.url;
    askState = { mode: cxStore('cx.mode.' + id) === 'as' ? 'as' : 'about', persona: cxStore('cx.persona.' + id) || null,
                 busy: false, messages: [], starters: [] };
    if (!askPeople().length) askState.mode = 'about';
    const canAsk = !window.VERBATIM_DEMO || window.VERBATIM_DEMO_ASK;
    const nSrc = typeof srcAllIds === 'function' && srcState.data ? srcAllIds(srcState.data).length : (ch.n_sources || 0);
    box.innerHTML = `
        <div class="cx-chat">
            <div class="cx-chat-h" id="cx-chat-h"></div>
            <div class="cx-scroll" id="cx-scroll">
            <div class="cx-intro" id="cx-intro">
                <!-- 跟 Gemini 一样：大表情 + 项目名 + 「N 个来源 · 日期」+ 一句话，下面三张建议问题卡片 -->
                <div class="cx-hero" id="cx-hero">${askHeroHtml(ch, nSrc)}</div>
                <div class="cx-starters" id="cx-starters"></div>
            </div>
            <div class="cx-thread" id="cx-thread"></div>
            </div>
            ${canAsk ? `<form class="cx-input-row" id="cx-form">
                <span class="cx-mode-chip hidden" id="cx-mode-chip"></span>
                <textarea id="cx-q" rows="1" maxlength="2000" placeholder="${escapeHtml(T(person ? 'cx.placeholder' : 'cx.placeholderP'))}"></textarea>
                <span class="cx-src-n" id="cx-src-n"></span>
                <button class="cx-send-btn" id="cx-send-btn" type="submit"
                    aria-label="${escapeHtml(T('cx.send'))}" title="${escapeHtml(T('cx.send'))}">
                    <svg class="i-send" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 19V5"/><path d="M6 11l6-6 6 6"/></svg>
                    <svg class="i-stop" viewBox="0 0 24 24" width="26" height="26" aria-hidden="true"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="1.8"/><rect x="8.5" y="8.5" width="7" height="7" rx="1.2" fill="currentColor"/></svg>
                </button>
            </form>
            <p class="cx-disclaimer" id="cx-disc">${T(person ? 'cx.disclaimer' : 'cx.disclaimerP')}</p>` : `<p class="cx-muted">${T('cx.demoOff')}</p>`}
        </div>`;
    askHeadWire(id);
    askModeUi();
    const form = box.querySelector('#cx-form');
    if (form) {
        const q = form.querySelector('#cx-q');
        const grow = () => { q.style.height = 'auto'; q.style.height = Math.min(q.scrollHeight, 180) + 'px'; };
        q.addEventListener('input', grow);
        q.addEventListener('keydown', e => {
            if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
        });
        q.addEventListener('keydown', e => {
            if (e.key === 'Escape' && askState.busy) { e.preventDefault(); askStop(); }
        });
        form.addEventListener('submit', e => {
            e.preventDefault();
            if (askState.busy) {               // 生成中这个按钮是「停止」；回车不算点停止
                if (e.submitter) askStop();
                return;
            }
            const text = q.value.trim();
            if (!text) return;
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
    if (typeof srcSyncCount === 'function') srcSyncCount();
    // 从话题雷达「问他」跳过来：带着问题
    let pend = null;
    try { pend = JSON.parse(cxStore('cx.pendingAsk') || 'null'); } catch { /* 无 */ }
    if (pend && pend.id === id && pend.q) {
        cxStore('cx.pendingAsk', '');
        askSend(pend.q);
    }
}

// 对话栏顶上一条：左边「对话」，右边 回答方式（设置）、⋯（导出 / 清空）。跟 NotebookLM 的对话栏头一样
const CX_TUNE_SVG = '<svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>';
function askHeadWire(id) {
    const head = document.getElementById('cx-chat-h');
    if (!head) return;
    const can = askPeople().length > 0 && CAN_ASK();
    head.innerHTML = `<b>${T('cx.chat')}</b><span class="cx-foot-sp"></span>
        ${can ? `<details class="cx-hd-menu" id="cx-cfg"><summary class="cx-ic-btn" title="${escapeHtml(T('cx.cfg'))}" aria-label="${escapeHtml(T('cx.cfg'))}">${CX_TUNE_SVG}</summary>
            <div class="cx-hd-pop cx-cfg-pop" id="cx-cfg-pop"></div></details>` : ''}
        <details class="cx-hd-menu" id="cx-more"><summary class="cx-ic-btn" title="${escapeHtml(T('cx.more'))}" aria-label="${escapeHtml(T('cx.more'))}">⋯</summary>
            <div class="cx-hd-pop">
                <div class="cx-hd-label">${T('cx.exportConv')}</div>
                <button type="button" data-cexp="pdf">PDF</button><button type="button" data-cexp="docx">Word</button><button type="button" data-cexp="md">Markdown</button>
                <hr><button type="button" data-clear class="danger">${T('cx.clear')}</button>
            </div></details>`;
    const cfg = head.querySelector('#cx-cfg');
    if (cfg) cfg.addEventListener('toggle', () => { if (cfg.open) askCfgRender(); });
    head.querySelectorAll('[data-cexp]').forEach(b => b.onclick = () => {
        head.querySelector('#cx-more').open = false;
        if (typeof runExport === 'function') runExport(b.dataset.cexp, askConvDoc());
    });
    head.querySelector('[data-clear]').onclick = async () => {
        head.querySelector('#cx-more').open = false;
        if (!askState.messages.length || !confirm(T('cx.clearConfirm'))) return;
        await fetch(`/api/chain/${id}/ask`, { method: 'DELETE' });
        askState.messages = [];
        askRenderThread();
    };
}
function askCfgRender() {
    const pop = document.getElementById('cx-cfg-pop');
    if (!pop) return;
    pop.innerHTML = askCfgHtml();
    pop.querySelectorAll('input[name="cx-mode"]').forEach(r => r.onchange = () => { askSetMode(r.value); askCfgRender(); });
    pop.querySelectorAll('[data-persona]').forEach(b => b.onclick = () => { askSetMode('as', b.dataset.persona); askCfgRender(); });
}
function askConvDoc() {
    return { title: T('exp.convTitle', { name: chainDisplayName(cx.chain || {}) || '' }),
        blocks: askState.messages.flatMap(x => x.role === 'user' ? [{ heading: x.content, md: '' }]
            : (x.pending || x.error ? [] : [{ md: x.content, citations: x.citations }])) };
}
// 关掉的地方点一下：设置 / ⋯ 收起
document.addEventListener('click', e => {
    document.querySelectorAll('#cx-chat-h details[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; });
});

function askModeUi() {
    const as = askState.mode === 'as';
    const p = as ? askPersona() : null;
    const chip = document.getElementById('cx-mode-chip');
    if (chip) {
        chip.classList.toggle('hidden', !as);
        chip.innerHTML = as ? `${p ? personFace(p, 20) : ''}<span>${escapeHtml(T('cx.asChip', { name: p ? personName(p) : '' }))}</span>
            <button type="button" aria-label="${escapeHtml(T('cx.asOff'))}" title="${escapeHtml(T('cx.asOff'))}">×</button>` : '';
        const x = chip.querySelector('button');
        if (x) x.onclick = () => askSetMode('about');
    }
    const q = document.getElementById('cx-q');
    const person = !!(cx.chain && cx.chain.url) || (cx.people || []).length > 0;
    if (q) q.placeholder = as ? T('cx.asPh', { name: p ? personName(p) : '' }) : T(person ? 'cx.placeholder' : 'cx.placeholderP');
    const disc = document.getElementById('cx-disc');
    if (disc) disc.textContent = as ? T('cx.disclaimerAs') : T(person ? 'cx.disclaimer' : 'cx.disclaimerP');
    const cfg = document.querySelector('#cx-cfg summary');
    if (cfg) cfg.classList.toggle('on', as);
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
    box.innerHTML = askState.starters.slice(0, 3).map(q =>
        `<button type="button" class="cx-starter" data-q="${escapeHtml(q)}"><span>${escapeHtml(q)}</span><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 5v6a3 3 0 0 0 3 3h9"/><path d="M14 10l4 4-4 4"/></svg></button>`).join('');
    box.querySelectorAll('.cx-starter').forEach(b => b.onclick = () => askSend(b.dataset.q));
}

function coverageText(cov) {
    if (!cov) return '';
    // 资料类项目：说「段落 / 来源」，不说「卡片 / 期」
    const P = cx.chain && cx.chain.kind === 'project' && cx.chain.template !== 'creator' ? 'P' : '';
    if (P) {
        const topicP = cov.topic ? T('cx.covTopic', { t: cov.topic }) + ' · ' : '';
        if (cov.mode === 'all') return topicP + T('cx.covAllP', { n: cov.pool_cards, m: cov.pool_episodes });
        if (cov.mode === 'spread') return topicP + T('cx.covSpreadP', { n: cov.pool_cards, m: cov.pool_episodes, k: cov.cards_used });
        return topicP + T('cx.covSearchP', { n: cov.pool_cards, m: cov.pool_episodes, k: cov.cards_used, h: cov.keyword_hits });
    }
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
    if (m.pending) return `<div class="cx-msg cx-bot cx-live">${pendingInner(m)}</div>`;
    if (m.stopped && !m.content) {
        return `<div class="cx-msg cx-bot"><div class="cx-muted">${T('cx.stoppedEmpty')}</div></div>`;
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
        <div class="cx-acts">
            ${m.at && !window.VERBATIM_DEMO && typeof stSaveNote === 'function'
                ? `<button type="button" class="cx-act cx-save-note"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6l-1 6 4 4H6l4-4z"/><path d="M12 13v8"/></svg><span>${T('st.saveNote')}</span></button>` : ''}
            <button type="button" class="cx-act cx-copy" title="${escapeHtml(T('cx.copy'))}" aria-label="${escapeHtml(T('cx.copy'))}"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg></button>
            ${exp}
            <span class="cx-cov">${m.stopped ? `<span class="cx-stopped-tag">${T('cx.stopped')}</span> ` : ''}${escapeHtml(coverageText(m.coverage))}${cost}${escapeHtml(dropped)}</span>
        </div>
    </div>`;
}

function askRenderThread() {
    const box = document.getElementById('cx-thread');
    if (!box) return;
    box.innerHTML = askState.messages.map(msgHtml).join('');
    box.querySelectorAll('.cx-msg.cx-bot[data-i]').forEach(el => {
        const m = askState.messages[+el.dataset.i];
        wireCitations(el, m.citations);
        const save = el.querySelector('.cx-save-note');
        if (save) save.addEventListener('click', () => stSaveNote(m, save));
        const copy = el.querySelector('.cx-copy');
        if (copy) copy.addEventListener('click', async () => {
            // 复制纯文字：出处标记拿掉（贴到别处是一串 [#3-12] 没意义）
            const text = String(m.content || '').replace(CITE_RE, '').replace(/[ \t]+([。，；.,;])/g, '$1');
            try { await navigator.clipboard.writeText(text); showToast(T('cx.copied')); } catch { showToast(T('cx.copyFailed')); }
        });
    });
    const more = document.getElementById('cx-more');     // 没有对话时 ⋯ 里的导出 / 清空没意义
    if (more) more.classList.toggle('hidden', !askState.messages.some(x => x.role === 'assistant' && !x.pending && !x.error));
    const starters = document.getElementById('cx-starters');
    if (starters) starters.classList.toggle('hidden', !!askState.messages.length);
}

// 生成中的那条：阶段提示 + 已经写出来的字。出处标记先画成灰色占位，写完换成能点的
function pendingInner(m) {
    const stage = { search: 'cx.stageSearch', write: 'cx.stageWrite', rewrite: 'cx.stageRewrite' }[m.stage] || 'cx.reading';
    const full = m.text || '';
    const text = full.slice(0, m.shown == null ? full.length : m.shown).replace(/\[#[^\]\n]*$/, '');   // 半截的 [#3- 先别画
    const body = text ? `<div class="cx-answer md-body">${renderMarkdown(text).replace(CITE_RE,
        '<span class="cx-cite cx-cite-pending" aria-hidden="true"></span>')}</div>` : '';
    const showStage = !text || m.stage === 'rewrite';   // 一有字就不再挂「正在写」
    return `${showStage ? `<div class="cx-thinking">${T(stage)}</div>` : ''}${body}`;
}

let askAbort = null;
function askStop() { if (askAbort) askAbort.abort(); }

function askSetBusy(on) {
    askState.busy = on;
    const btn = document.getElementById('cx-send-btn');
    if (!btn) return;
    btn.classList.toggle('busy', on);
    const label = T(on ? 'cx.stop' : 'cx.send');
    btn.setAttribute('aria-label', label);
    btn.title = on ? `${label} (Esc)` : label;
}

async function askSend(question, topic) {
    if (askState.busy) return;
    const id = cx.id;
    askSetBusy(true);
    askState.messages.push({ role: 'user', content: question, topic: topic || null });
    const pending = { role: 'assistant', pending: true, stage: 'search', text: '', shown: 0, mode: askState.mode };
    askState.messages.push(pending);
    askRenderThread();
    const thread = document.getElementById('cx-thread');
    if (thread && thread.lastElementChild) thread.lastElementChild.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    // 只重画正在生成的这一条。模型一次吐一大段（flash-lite 整个回答常常就 5 段），
    // 直接贴上去是一跳一跳的：用定时器把已到的字平滑地「打」出来，积压越多打得越快。
    // 不用 requestAnimationFrame——标签页不在前台时它会停，切回来就只剩最后一下整段出现。
    let tick = 0;
    const paint = () => {
        tick = 0;
        if (cx.id !== id) return;
        const el = document.querySelector('#cx-thread .cx-live');
        if (!el) return;
        const backlog = pending.text.length - pending.shown;
        if (backlog > 0) pending.shown += Math.max(4, Math.ceil(backlog / 6));
        // 宽屏时对话在中间栏里自己滚（#cx-scroll）；窄屏还是整页滚
        const sc = document.getElementById('cx-scroll');
        const near = sc && sc.scrollHeight > sc.clientHeight + 4
            ? sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 160
            : window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
        el.innerHTML = pendingInner(pending);
        if (near) el.scrollIntoView({ block: 'end' });
        if (pending.shown < pending.text.length) repaint();
    };
    const repaint = () => { if (!tick) tick = setTimeout(paint, 33); };
    const caughtUp = () => new Promise(res => {          // 收尾前让打字追上（最多等 0.6 秒）
        const t0 = Date.now();
        const wait = () => (pending.shown >= pending.text.length || Date.now() - t0 > 600) ? res() : setTimeout(wait, 40);
        wait();
    });

    const ctrl = new AbortController();
    askAbort = ctrl;
    let msg = null;
    try {
        const resp = await fetch(`/api/chain/${id}/ask/stream`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ctrl.signal,
            body: JSON.stringify({ question, mode: askState.mode, topic: topic || undefined, ui_lang: currentLang,
                                   persona: askState.mode === 'as' && askPersona() ? askPersona().chain_id : undefined,
                                   scope: typeof srcScopeBody === 'function' ? srcScopeBody() : undefined }),
        });
        if (!resp.ok || !resp.body) {
            const r = await resp.json().catch(() => ({}));
            msg = { role: 'assistant', error: r.error || T('common.couldNotLoad') };
        } else {
            const reader = resp.body.getReader();
            const dec = new TextDecoder();
            let buf = '';
            for (;;) {
                const { value, done } = await reader.read();
                if (done) break;
                buf += dec.decode(value, { stream: true });
                let nl;
                while ((nl = buf.indexOf('\n')) >= 0) {
                    const ln = buf.slice(0, nl).trim();
                    buf = buf.slice(nl + 1);
                    if (!ln) continue;
                    let ev;
                    try { ev = JSON.parse(ln); } catch { continue; }
                    if (ev.type === 'stage') { pending.stage = ev.stage; repaint(); }
                    else if (ev.type === 'delta') { pending.text += ev.text; repaint(); }
                    else if (ev.type === 'done') msg = ev.message;
                    else if (ev.type === 'error') msg = { role: 'assistant', error: ev.error };
                }
            }
            if (!msg) msg = { role: 'assistant', error: T('common.couldNotLoad') };
            else if (!msg.error) await caughtUp();
        }
    } catch (e) {
        if (e && e.name === 'AbortError') {
            // 服务器那边会把半截校验好出处存进记录；这里先照原样摆着，下次打开就是存好的那版
            msg = { role: 'assistant', content: pending.text.replace(/\[#[^\]\n]*$/, ''), citations: {},
                    stopped: true, mode: pending.mode };
        } else {
            msg = { role: 'assistant', error: String(e) };
        }
    }
    if (tick) clearTimeout(tick);
    askAbort = null;
    askSetBusy(false);
    if (cx.id !== id) return;
    askState.messages[askState.messages.indexOf(pending)] = msg;
    askRenderThread();
    const last = document.querySelector('#cx-thread .cx-msg:last-child');
    if (last) last.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    if (msg.stopped && msg.content) askSwapSaved(id, msg);
}

// 停止后先摆的是浏览器手里的半截（出处还没校验，只能先去掉）；服务器校验完存进记录，
// 稍等一下取回那版换上，出处就能点了
async function askSwapSaved(id, local) {
    for (const wait of [700, 1500]) {
        await new Promise(r => setTimeout(r, wait));
        if (cx.id !== id || askState.busy) return;
        let saved;
        try { saved = (await (await fetch(`/api/chain/${id}/ask`)).json()).messages || []; } catch { return; }
        const s = saved[saved.length - 1];
        const i = askState.messages.indexOf(local);
        if (i < 0) return;
        if (s && s.role === 'assistant' && s.stopped && s.content) {
            askState.messages[i] = s;
            askRenderThread();
            return;
        }
    }
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
    const id = cxPid();
    const box = document.getElementById('cx-topics');
    let d;
    try { d = await (await fetch(`/api/chain/${id}/topics`)).json(); } catch {
        box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return;
    }
    if (cxPid() !== id) return;
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
        <div id="tp-beliefs"></div>
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
    beliefsLoad();
}

// ================= 核心信念：同一个主张在多期里反复出现 =================
let beliefsState = { open: {}, shown: {} };

async function beliefsLoad() {
    const id = cxPid();
    const box = document.getElementById('tp-beliefs');
    if (!box) return;
    let d;
    try { d = await (await fetch(`/api/chain/${id}/beliefs`)).json(); } catch { box.innerHTML = ''; return; }
    if (cxPid() !== id || !document.getElementById('tp-beliefs')) return;
    clearTimeout(cx.timers.beliefs);
    const job = d.job && d.job.status === 'running' ? d.job : null;
    if (job) cx.timers.beliefs = setTimeout(beliefsLoad, 4000);
    const est = d.estimate || {};
    const err = d.job && d.job.status === 'error' ? `<p class="cx-err">${escapeHtml(d.job.error)}</p>` : '';
    const btn = (label) => window.VERBATIM_DEMO || job ? '' :
        `<button class="${d.built_at ? 'cx-link' : 'btn-primary cx-send'}" type="button" id="bl-find">${label}</button>`;
    const progress = job ? `<span class="cx-muted">${T('bl.finding', { a: job.done, b: job.total || '…' })}</span>` : '';
    if ((d.episodes || 0) < 3 || !est.topics) {
        box.innerHTML = '';            // 不到 3 期、或没有横跨 3 期的话题：这块不出现
        return;
    }
    if (!d.built_at) {
        box.innerHTML = `<div class="bl-box bl-empty"><h3>${T('bl.title')}</h3>
            <p class="cx-muted">${T('bl.desc')}</p>${progress}${err}
            ${btn(T('bl.findBtn', { cost: fmtUsd(Math.max(0.01, est.est_cost_usd || 0)) }))}</div>`;
    } else {
        const items = d.items || [];
        items.forEach(b => b.cards.forEach(c => { cxCiteCache[c.id] = c; }));
        const list = items.map((b, n) => {
            const key = b.belief;
            const shown = beliefsState.shown[key] || 4;
            const first = b.cards[0] || {};
            const span = b.first && b.last
                ? T('bl.span', { n: b.episodes, a: b.first, b: b.last }) : T('bl.spanNoDate', { n: b.episodes });
            const stance = b.stance && b.stance !== 'none' ? `<span class="cx-stance st-${b.stance}">${T('stance.' + b.stance)}</span>` : '';
            return `<details class="bl-item" data-k="${n}"${beliefsState.open[key] ? ' open' : ''}>
                <summary><div class="bl-belief"><span class="cx-ai-tag">${T('bl.aiTag')}</span> ${escapeHtml(b.belief)}</div>
                    <blockquote class="cc-quote bl-quote">${escapeHtml(first.quote || first.obs || '')}</blockquote>
                    <div class="bl-meta"><b>${span}</b>${stance}<span class="tp-n">${escapeHtml(b.topic || '')}</span></div></summary>
                <div class="bl-cards">${b.cards.slice(0, shown).map(c => sourceItemHtml(c.id, c)).join('')}
                ${b.cards.length > shown ? `<button class="btn-secondary cc-more bl-more" type="button" data-k="${n}">${T('cards.more', { n: b.cards.length - shown })}</button>` : ''}</div>
            </details>`;
        }).join('');
        box.innerHTML = `<div class="bl-box"><div class="bl-head"><h3>${T('bl.title')}</h3>
                <span class="cx-muted">${items.length ? T('bl.count', { n: items.length }) : ''} ${T('bl.builtAt', { d: d.built_at })}</span>
                ${progress}${btn(T('bl.redo'))}</div>
            ${err}${d.failed_topics ? `<p class="cx-muted">${T('bl.failedTopics', { n: d.failed_topics })}</p>` : ''}
            ${items.length ? `<div class="bl-list">${list}</div>` : `<p class="cx-muted">${T('bl.none')}</p>`}</div>`;
        box.querySelectorAll('.bl-item').forEach(el => el.addEventListener('toggle', () => {
            beliefsState.open[items[+el.dataset.k].belief] = el.open;
        }));
        box.querySelectorAll('.bl-more').forEach(m => m.onclick = () => {
            const b = items[+m.dataset.k];
            beliefsState.shown[b.belief] = (beliefsState.shown[b.belief] || 4) + 12;
            beliefsLoad();
        });
        wireCitations(box);
    }
    const f = box.querySelector('#bl-find');
    if (f) f.onclick = async () => {
        f.disabled = true;
        await fetch(`/api/chain/${id}/beliefs/find`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        beliefsLoad();
    };
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
        const id = cxPid(), want = t.topic;
        try {
            const r = await (await fetch(`/api/chain/${id}/topics?topic=${encodeURIComponent(want)}`)).json();
            const hit = (r.topics || []).find(x => x.topic === want);
            cards = topicsState.cards[want] = (hit && hit.cards) || [];
        } catch { box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return; }
        if (cxPid() !== id || topicsState.sel !== want) return;
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
    const id = cxPid();
    const box = document.getElementById('cx-preds');
    let d;
    try { d = await (await fetch(`/api/chain/${id}/predictions`)).json(); } catch {
        box.innerHTML = `<p class="cx-err">${T('common.couldNotLoad')}</p>`; return;
    }
    if (cxPid() !== id) return;
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
    out.innerHTML = cmpResultHtml(r, q);
    wireCitations(out, r.citations);
}

function cmpResultHtml(r, q) {
    rememberCites(r.citations);
    return `<div class="cmp-result">
        <div class="cx-answer md-body">${citedHtml(r.answer, r.citations)}</div>
        ${sourcesHtml(r.citations, r.answer)}
        <div class="cx-cov">${(r.creators || []).map(c => `${escapeHtml(c.author)}: ${c.cards}`).join(' · ')} ${T('cmp.cards')}
            ${typeof exportMenuHtml === 'function' ? exportMenuHtml(() => ({
                title: `${T('cmp.title')}：${q}`.slice(0, 80),
                sub: (r.creators || []).map(c => c.author).join(' · '),
                blocks: [{ md: r.answer, citations: r.citations }] })) : ''}</div></div>`;
}

// ================= 格子的设置框：立场 / 预测对账 / 原话、对比 =================
// 跟「人物画像」的选择框一样弹在页面中间：看谁（项目里不止一个人时）、现在是什么状态、要不要花钱；
// 「生成」= 在工作台下面的列表里记一条（要先打标签的，打完那条才能点开）。做过的直接「打开」。
function stPickOverlay() {
    let ov = document.getElementById('rd-pick');
    if (!ov) {
        ov = document.createElement('div');
        ov.id = 'rd-pick';
        ov.className = 'settings-overlay hidden';
        document.body.appendChild(ov);
    }
    toolOverlay('rd-pick');
    return ov;
}

async function viewPicker(tab, pid) {
    if (pid && pid !== cxPid()) cxSetPerson(pid);
    const ov = stPickOverlay();
    const tile = document.querySelector(`#chain-explore .nb-tile[data-cx="${tab}"] .nb-tl`);
    const isCol = cx.chain && !cx.chain.url && !cxMulti();
    const colKey = 'cx.desc.' + tab + 'Col';
    const desc = T(isCol && T(colKey) !== colKey ? colKey : 'cx.desc.' + tab);
    const who = cxPid();
    const made = (stState.items || []).find(o => o.kind === 'view' && o.view === tab && o.chain === who);
    const render = (status, act) => {
        ov.innerHTML = `<div class="cx-modal rd-pick vp-pick" role="dialog" aria-modal="true" aria-labelledby="vp-t">
            <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
            <h2 class="cx-modal-title" id="vp-t">${escapeHtml(tile ? tile.textContent : '')}</h2>
            <p class="cx-muted">${escapeHtml(desc)}</p>
            ${cxMulti() ? `<div class="pp-bar" id="vp-pp">${peopleChipsHtml()}</div>` : ''}
            <div class="vp-foot"><span class="rd-pick-s">${status}</span>${act}</div></div>`;
        ov.querySelectorAll('#vp-pp [data-pid]').forEach(b => b.onclick = () => viewPicker(tab, b.dataset.pid));
        const go = ov.querySelector('[data-go]');
        if (go) go.onclick = () => viewMake(tab, who, go.dataset.go === 'tag', ov);
        const op = ov.querySelector('[data-open]');
        if (op) op.onclick = () => { ov.classList.add('hidden'); stOpenView(made); };
    };
    const me = cxPerson();
    if (me && !me.has_cards) { render(escapeHtml(T('pp.notYet')), ''); ov.classList.remove('hidden'); return; }
    if (made) {
        render(escapeHtml(made.status === 'running' ? T('vp.running') : T('vp.made', { at: String(made.created_at || '').slice(5, 16) })),
            made.status === 'running' ? '' : `<button type="button" class="rd-pick-go primary" data-open>${T('rd.open')}</button>`);
        ov.classList.remove('hidden');
        return;
    }
    render(`<span class="st-spin" aria-hidden="true"></span>${T('cx.loading')}`, '');
    ov.classList.remove('hidden');
    // 现在是什么状态：立场 / 预测要先给卡打标签（花钱，先说清楚）
    let d = {};
    try { d = await (await fetch(`/api/chain/${who}/topics`)).json(); } catch { /* 当不知道 */ }
    if (cxPid() !== who || ov.classList.contains('hidden')) return;
    const st = d.status || {};
    const job = d.job && d.job.status === 'running';
    const demo = !!window.VERBATIM_DEMO;
    if (tab !== 'cards' && !d.tagged) {
        render(escapeHtml(job ? T('vp.running') : T('vp.needTag', { n: st.cards || 0, cost: fmtUsd(Math.max(0.01, st.est_cost_usd || 0)) })),
            demo ? '' : `<button type="button" class="rd-pick-go primary" data-go="tag">${job ? T('rd.generate') : T('vp.genCost', { cost: fmtUsd(Math.max(0.01, st.est_cost_usd || 0)) })}</button>`);
        return;
    }
    const info = tab === 'topics' ? T('vp.topicsN', { n: (d.topics || []).length })
        : tab === 'cards' ? T('vp.cardsN', { n: d.cards_total || 0 }) : T('vp.ready');
    render(escapeHtml(info), demo ? '' : `<button type="button" class="rd-pick-go primary" data-go="1">${T('rd.generate')}</button>`);
}

async function viewMake(tab, who, tag, ov) {
    let r;
    try {
        r = await (await fetch(`/api/chain/${cx.id}/studio/view`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ view: tab, chain: who, generate: !!tag }) })).json();
    } catch (e) { r = { error: String(e) }; }
    if (r.error) { showToast(r.error); return; }
    ov.classList.add('hidden');
    showToast(T(tag ? 'vp.queued' : 'vp.added'));
    stLoad();
}

// 列表里的一条「谁的立场 / 预测 / 原话」：换到那个人，在工作台栏里展开
function stOpenView(o) {
    if (!o) return;
    if (o.status === 'running') { showToast(T('vp.running')); return; }
    if (o.chain && o.chain !== cxPid()) cxSetPerson(o.chain);
    cxShowTab(o.view);
}

function comparePicker() {
    const ov = stPickOverlay();
    const ppl = (cx.people || []).filter(p => p.has_cards);
    const saved = (cxStore('pc.pick.' + cx.id) || '').split(',').filter(Boolean);
    const on = p => (saved.length ? saved.includes(p.chain_id) : ppl.indexOf(p) < 4);
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick" role="dialog" aria-modal="true" aria-labelledby="pc-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="pc-t">${escapeHtml(T('cx.tab.compare'))}</h2>
        <p class="cx-muted">${escapeHtml(T('cx.desc.compare'))}</p>
        <form id="pc-form" class="pc-form">
            <div class="pp-bar pc-pick">${ppl.map(p => `<label class="pp-chip pc-chip">
                <input type="checkbox" value="${escapeHtml(p.chain_id)}" ${on(p) ? 'checked' : ''}>${personFace(p)}<span>${escapeHtml(personName(p))}</span></label>`).join('')}</div>
            <textarea id="pc-q" class="st-prompt" rows="3" maxlength="500" placeholder="${escapeHtml(T('pc.ph'))}"></textarea>
            <div class="vp-foot"><span class="rd-pick-s" id="pc-msg">${T('pc.hint')}</span>
                <button type="submit" class="rd-pick-go primary">${T('rd.generate')}</button></div>
        </form></div>`;
    ov.classList.remove('hidden');
    const q = ov.querySelector('#pc-q');
    setTimeout(() => q.focus(), 30);
    ov.querySelector('#pc-form').addEventListener('submit', async e => {
        e.preventDefault();
        const ids = [...ov.querySelectorAll('.pc-pick input:checked')].map(i => i.value);
        const msg = ov.querySelector('#pc-msg');
        if (ids.length < 2 || ids.length > 4) { msg.textContent = T('cmp.pickN'); return; }
        if (!q.value.trim()) { q.focus(); return; }
        cxStore('pc.pick.' + cx.id, ids.join(','));
        let r;
        try {
            r = await (await fetch(`/api/chain/${cx.id}/studio/compare`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chains: ids, question: q.value.trim() }) })).json();
        } catch (err) { r = { error: String(err) }; }
        if (r.error) { msg.textContent = r.error; return; }
        ov.classList.add('hidden');
        showToast(T('pc.started'));
        stLoad();
    });
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
