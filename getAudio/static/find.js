// 资料库「问」：在搜索框里写一句描述，回车或点「问」→ AI 找最像的几份转写，每份一句理由 + 跳到那一秒（libask.py）。
// 打字照旧即时搜（app.js 的 renderHistory）；问的结果摆在列表上面，收起就回到普通列表。
(function () {
    const btn = document.getElementById('find-btn');
    const panel = document.getElementById('find-panel');
    if (!btn || !panel || typeof searchInput === 'undefined') return;
    let timer = null;
    let open = false;

    const secs = s => Math.max(0, Math.round(s));
    const elapsed = st => {
        const s = secs((st.running ? st.now : st.ended) - st.started);
        return s >= 60 ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` : `${s}s`;
    };
    const shortTs = ts => String(ts || '').replace(/^00:(?=\d\d:\d\d$)/, '');

    function stepsList(steps) {
        const ul = document.createElement('ul');
        ul.className = 'find-steps';
        steps.forEach(s => {
            const li = document.createElement('li');
            const b = document.createElement('b');
            b.textContent = T('find.step.' + s.k);
            li.appendChild(b);
            if (s.d) li.appendChild(document.createTextNode(' ' + s.d));
            ul.appendChild(li);
        });
        return ul;
    }

    function resultCard(r) {
        const card = buildHistoryCard(r);
        const info = card.querySelector('.history-item-info');
        const oneLine = info.querySelector('.history-item-oneline');
        if (oneLine) oneLine.remove();          // 换成理由：比 AI 简介更能说明为什么是它
        const why = document.createElement('div');
        why.className = 'find-why';
        why.textContent = r.why;
        info.insertBefore(why, info.querySelector('.history-item-meta'));
        const target = 'detail/' + r.id + (r.sec != null ? '/t/' + r.sec : '');
        if (r.at) {
            const q = document.createElement('div');
            q.className = 'find-quote';
            const a = document.createElement('a');
            a.href = '#/' + target;
            a.textContent = T('find.jump', { t: shortTs(r.at) });
            const span = document.createElement('span');
            span.textContent = r.quote || '';
            q.append(a, span);
            info.insertBefore(q, why.nextSibling);
        }
        card.addEventListener('click', e => {
            if (e.target.closest('a, button')) return;
            navigate(target);
        });
        return card;
    }

    function render(st) {
        panel.classList.remove('hidden');
        panel.innerHTML = '';
        const head = document.createElement('div');
        head.className = 'find-head';
        const status = document.createElement('div');
        status.className = 'find-status';
        const res = st.result;
        if (st.running) {
            status.innerHTML = '<i class="find-spin" aria-hidden="true"></i>';
            status.appendChild(document.createTextNode(T('find.looking', { q: st.q, n: st.considered || st.total || '…', time: elapsed(st) })));
        } else if (st.error) {
            status.textContent = st.error === 'stopped' ? T('find.stopped') : T('find.failed', { reason: st.error });
        } else if (res) {
            status.textContent = res.results.length
                ? T('find.found', { k: res.results.length, q: st.q })
                : T('find.none', { n: res.considered, q: st.q });
        }
        const act = document.createElement('button');
        act.type = 'button';
        act.className = 'btn-secondary btn-small';
        act.textContent = st.running ? T('find.stop') : T('find.close');
        act.addEventListener('click', st.running
            ? () => fetch('/api/find/stop', { method: 'POST' }).then(poll)
            : close);
        head.append(status, act);
        panel.appendChild(head);

        if (st.running) {
            if (st.steps.length) panel.appendChild(stepsList(st.steps.slice(-6)));
        } else if (st.steps.length) {
            const d = document.createElement('details');
            d.className = 'find-trace';
            const sum = document.createElement('summary');
            sum.textContent = T('find.steps', { n: st.steps.length, time: elapsed(st) });
            d.append(sum, stepsList(st.steps));
            panel.appendChild(d);
        }
        if (res && res.mode === 'catalog') {
            const p = document.createElement('p');
            p.className = 'find-note';
            p.textContent = T('find.catalogOnly', { n: res.considered });
            panel.appendChild(p);
        }
        if (res && res.note) {
            const p = document.createElement('p');
            p.className = 'find-note';
            p.textContent = res.note;
            panel.appendChild(p);
        }
        if (res && res.results.length) {
            const list = document.createElement('div');
            list.className = 'find-results';
            res.results.forEach(r => list.appendChild(resultCard(r)));
            panel.appendChild(list);
        }
        btn.disabled = !!st.running;
    }

    async function poll() {
        clearTimeout(timer);
        try {
            const st = await (await fetch('/api/find')).json();
            if (open || st.running) { open = true; render(st); }
            if (st.running) timer = setTimeout(poll, 1000);
        } catch { timer = setTimeout(poll, 3000); }
    }

    function close() {
        open = false;
        clearTimeout(timer);
        panel.classList.add('hidden');
        panel.innerHTML = '';
    }

    async function ask() {
        const q = searchInput.value.trim();
        if (!q) { searchInput.focus(); return; }
        const r = await fetch('/api/find', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ q }),
        });
        if (r.status === 409) showToast(T('find.busy'));
        open = true;
        poll();
    }

    btn.addEventListener('click', ask);
    searchInput.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); ask(); }
    });
    poll();     // 刷新页面时正在找：接着显示进度
})();
