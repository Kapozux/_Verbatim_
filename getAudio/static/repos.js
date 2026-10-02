// ========== 来源：代码库（本机 git 仓库，读码 agent Daemon 读出证据卡）==========
// 后端：repos.py、app.py 的 /api/chain/<id>/sources/repo*、/api/repos/<id>(/file)；设计见 docs/adr/0005。
// 位置：加 = 「添加来源」弹框里一个选项；读 = 来源那一行的 ⋯ → 中间弹框（问题 + 估价）；
// 看 = 来源栏阅读器（卡片按文件分组；出处点开是文件本身，那几行高亮）。
// 依赖 app.js（T / escapeHtml / showToast / fmtUsd）、explore.js（cx / stPickOverlay）、
// projects.js（SRC_PATHS / srcSvg / srcRow / srcState / srcLoad / srcPost / readerOpen / closeAddSources）。

SRC_PATHS.code = '<path d="M8 8l-4 4 4 4"/><path d="M16 8l4 4-4 4"/><path d="M13.5 5l-3 14"/>';
const REPO_LAYER = { '主张': 'claim', '自证': 'transcript', '核实': 'verified' };

function repoIcon() {
    return `<span class="sr-svg" data-k="code">${srcSvg('code')}</span>`;
}

// 左栏的行：在读的转圈；没卡的不能勾、不能点开
function repoSrcRows(d) {
    const out = [];
    (d.repos || []).forEach(r => {
        const reading = r.status === 'reading';
        const failed = !reading && !r.cards && r.error;
        const sub = reading ? T('repo.reading') : r.cards ? T('repo.nCards', { n: r.cards })
            : failed ? T('repo.noCards') : T('repo.notRead');
        out.push(srcRow('repo', r.repo_id, repoIcon(), `${r.title} @${r.commit}`, sub, {
            ready: r.cards > 0, subCls: reading ? 'run' : failed ? 'bad' : '',
            menu: (reading ? '' : `<button type="button" data-act="read">${T(r.cards ? 'repo.readAgain' : 'repo.read')}</button>`)
                + `<button type="button" data-act="remove">${T('src.remove')}</button>` }));
        if (failed) out.push(`<div class="sr-err">${escapeHtml(r.error.slice(0, 180))}</div>`);
    });
    return out;
}

function repoRailItems(d, it) {
    return (d.repos || []).map(r => it('repo', r.repo_id, srcSvg('code'), r.title, r.cards > 0));
}

// ---- 读：中间弹框，问题可空（= 导览），估价写在按钮上 ----
function repoReadPicker(repoId) {
    const r = ((srcState.data || {}).repos || []).find(x => x.repo_id === repoId);
    if (!r) return;
    const ov = stPickOverlay();
    const skipped = (r.skipped || []).slice(0, 6).join(', ') + ((r.skipped || []).length > 6 ? ' …' : '');
    ov.innerHTML = `<div class="cx-modal rd-pick vp-pick repo-pick" role="dialog" aria-modal="true" aria-labelledby="repo-t">
        <button class="settings-x" type="button" data-close aria-label="${escapeHtml(T('common.close'))}">×</button>
        <h2 class="cx-modal-title" id="repo-t"><svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"
            stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${SRC_PATHS.code}</svg><span>${escapeHtml(T('repo.readTitle'))}</span></h2>
        <p class="cx-muted">${escapeHtml(T('repo.readDesc', { t: r.title, c: r.commit }))}</p>
        ${r.dirty ? `<p class="cx-muted repo-note">${escapeHtml(T('repo.dirty'))}</p>` : ''}
        ${skipped ? `<p class="cx-muted repo-note">${escapeHtml(T('repo.skipped', { list: skipped }))}</p>` : ''}
        <form id="repo-form">
            <label class="repo-f"><span class="repo-l">${escapeHtml(T('repo.question'))}</span>
                <textarea id="repo-q" rows="3" maxlength="600" placeholder="${escapeHtml(T('repo.questionPh'))}"></textarea></label>
            <div class="vp-foot"><span class="rd-pick-s" id="repo-msg">${escapeHtml(T('repo.readNote'))}</span>
                <button type="submit" class="rd-pick-go primary" id="repo-go">${escapeHtml(r.estimate
                    ? T('repo.goCost', { cost: fmtUsd(r.estimate) }) : T('repo.go'))}</button></div>
        </form></div>`;
    ov.classList.remove('hidden');
    ov.querySelector('#repo-q').focus();
    ov.querySelector('#repo-form').addEventListener('submit', async e => {
        e.preventDefault();
        const go = ov.querySelector('#repo-go');
        go.disabled = true;
        const res = await srcPost(`/api/chain/${cx.id}/sources/repo/${repoId}/read`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ question: ov.querySelector('#repo-q').value.trim() }) });
        if (!res) { go.disabled = false; return; }
        ov.classList.add('hidden');
        showToast(T('repo.started'));
        srcLoad();
    });
}

// ---- 加：「添加来源」里填本机路径；加完直接弹「读」的确认（花钱前先看估价）----
(function wireAddRepo() {
    const ov = document.getElementById('addsrc-overlay');
    const btn = ov && ov.querySelector('#as-repo-btn');
    if (!btn) return;
    if (window.VERBATIM_DEMO) { btn.remove(); return; }
    const form = ov.querySelector('#as-repo');
    btn.addEventListener('click', () => {
        form.classList.toggle('hidden');
        if (!form.classList.contains('hidden')) ov.querySelector('#as-repo-path').focus();
    });
    form.addEventListener('submit', async e => {
        e.preventDefault();
        const path = ov.querySelector('#as-repo-path').value.trim();
        if (!path) return;
        const go = form.querySelector('button[type="submit"]');
        go.disabled = true;
        const r = await srcPost(`/api/chain/${cx.id}/sources/repo`, { method: 'POST',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) });
        go.disabled = false;
        if (!r) return;
        ov.querySelector('#as-repo-path').value = '';
        form.classList.add('hidden');
        closeAddSources();
        await srcLoad();
        if (r.repo && !r.repo.cards) repoReadPicker(r.repo_id);
    });
})();

// ---- 看：卡片按文件分组；点卡片 / 出处 → 文件本身，那几行高亮 ----
async function repoGet(repoId) {
    try {
        const m = await (await fetch(`/api/repos/${repoId}`)).json();
        if (m.error) { showToast(m.error); return null; }
        return m;
    } catch (e) { showToast(String(e)); return null; }
}

function repoCodeHtml(text, first) {
    return `<pre class="repo-code">${String(text || '').split('\n').map((ln, i) =>
        `<span class="repo-ln"><span class="repo-no">${first + i}</span>${escapeHtml(ln) || ' '}</span>`).join('')}</pre>`;
}

async function openRepoReader(repoId) {
    const m = await repoGet(repoId);
    if (!m) return;
    const body = readerOpen(`${m.title} @${(m.commit || '').slice(0, 7)}`, T('repo.nCards', { n: m.cards.length }), '');
    const byFile = {};
    m.cards.forEach(c => (byFile[c.path] = byFile[c.path] || []).push(c));
    body.innerHTML = Object.keys(byFile).sort().map(path => `
        <h3 class="rd-h repo-file"><span>${escapeHtml(path)}</span>
            <button type="button" class="cx-link" data-file="${escapeHtml(path)}">${T('repo.wholeFile')}</button></h3>
        ${byFile[path].sort((a, b) => a.start - b.start).map(c => `
            <div class="repo-card" data-path="${escapeHtml(c.path)}" data-start="${c.start}" data-end="${c.end}" tabindex="0">
                <div class="repo-loc"><span>${T('repo.lines', { a: c.start, b: c.end })}</span>
                    <span class="repo-layer" data-l="${REPO_LAYER[c.layer] || 'transcript'}">${T('repo.layer.' + (REPO_LAYER[c.layer] || 'transcript'))}</span>
                    ${c.topic ? `<span>${escapeHtml(c.topic)}</span>` : ''}</div>
                ${repoCodeHtml(c.quote, c.start)}
                ${c.obs ? `<div class="cc-obs"><span class="cx-ai-tag">${T('cx.aiNote')}</span> ${escapeHtml(c.obs)}</div>` : ''}
            </div>`).join('')}`).join('');
    body.scrollTop = 0;
    body.querySelectorAll('[data-file]').forEach(b => b.addEventListener('click', () => openRepoFile(repoId, b.dataset.file)));
    body.querySelectorAll('.repo-card').forEach(el => {
        const open = () => openRepoFile(repoId, el.dataset.path, +el.dataset.start, +el.dataset.end);
        el.addEventListener('click', open);
        el.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
    });
}

const REPO_WINDOW = 400;          // 很长的文件只画目标附近这么多行

async function openRepoFile(repoId, path, start, end) {
    let f, m;
    try {
        [f, m] = await Promise.all([
            fetch(`/api/repos/${repoId}/file?path=${encodeURIComponent(path)}`).then(r => r.json()), repoGet(repoId)]);
    } catch (e) { showToast(String(e)); return; }
    if (!f || f.error) { showToast((f && f.error) || T('common.couldNotLoad')); return; }
    const lines = f.text.split('\n');
    const body = readerOpen(path, m ? `${m.title} @${(m.commit || '').slice(0, 7)}` : '', '');
    end = end || start;
    let a = 1, b = lines.length;
    if (lines.length > REPO_WINDOW * 2 && start) {
        a = Math.max(1, start - REPO_WINDOW);
        b = Math.min(lines.length, (end || start) + REPO_WINDOW);
    }
    body.innerHTML = `<button type="button" class="cx-link rd-open" id="repo-back">${T('repo.allCards')}</button>
        ${a > 1 || b < lines.length ? `<p class="cx-muted repo-note">${T('repo.partial', { a, b, n: lines.length })}</p>` : ''}
        <pre class="repo-code repo-src">${lines.slice(a - 1, b).map((ln, i) => {
            const n = a + i;
            const hl = start && n >= start && n <= end ? ' hl' : '';
            return `<span class="repo-ln${hl}" data-n="${n}"><span class="repo-no">${n}</span>${escapeHtml(ln) || ' '}</span>`;
        }).join('')}</pre>`;
    body.querySelector('#repo-back').addEventListener('click', () => openRepoReader(repoId));
    const hit = start && body.querySelector(`.repo-ln[data-n="${start}"]`);
    if (hit) setTimeout(() => hit.scrollIntoView({ block: 'center' }), 30);
    else body.scrollTop = 0;
}

// 出处卡片的行号范围：heading 是「文件:起-止」
function repoCiteEnd(c) {
    const m = String(c.heading || '').match(/:(\d+)-(\d+)$/);
    return m ? +m[2] : c.line;
}

// 直接打开项目页时，左栏可能在本文件加载之前就画过了（那时还没有 repoSrcRows）：数据已经到了就补画一次
if (typeof srcState !== 'undefined' && srcState.data) srcRender();
