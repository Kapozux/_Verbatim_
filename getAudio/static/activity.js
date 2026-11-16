// 「进行中」（2026-11-16，用户：「我某个地方开启转写，我即使页面关了也能显示」）。
// 任务本来就在服务器后台跑；以前页面上的队列只在浏览器内存里，关了再开就看不到。
// 这里问服务器（/api/activity）：还在跑的转写、画面描述放回「转写」页的队列卡片，
// 顶栏 ⟳N 也算上它们和工作台正在生成的产出。有活时每 4 秒问一次，没活时每 20 秒，标签页在后台不问。
(function () {
    const ACTIVE = new Set(['queued', 'running']);
    let timer = null;

    function rowFor(key) {
        return batchList.querySelector(`.batch-item[data-task-id="${key}"]`);
    }

    function statusText(it) {
        if (it.status === 'queued') return T('rowStatus.queued');
        if (it.status === 'running') {
            if (it.kind === 'scenes') return T('activity.describing');
            if (it.phase === 'scenes') return T('activity.readingPicture');
            return it.percent != null ? T('rowStatus.transcribing', { percent: it.percent }) : T('activity.running');
        }
        if (it.status === 'done') return T('rowStatus.done');
        return it.error || T('activity.failed');
    }

    function stateClass(it) {
        return it.status === 'failed' ? 'error' : it.status;
    }

    // 队列卡片：服务器上有、这一页没有的行补上；补上的行每次跟着服务器更新（自己提交的那些由 SSE 管）
    function syncQueue(items) {
        let added = false;
        items.filter(it => it.kind === 'transcribe' || it.kind === 'scenes').forEach(it => {
            const key = (it.kind === 'scenes' ? 'scenes:' : '') + it.id;
            let row = rowFor(key);
            if (!row) {
                if (!ACTIVE.has(it.status)) return;           // 做完的只更新已有的行，不再新加
                row = createBatchRow(it.kind === 'scenes' ? `${T('scenes.tag')} · ${it.title}` : it.title);
                row.dataset.taskId = key;
                row.dataset.srv = '1';
                batchList.appendChild(row);
                added = true;
            }
            if (row.dataset.srv !== '1') return;
            setRowStatus(row, statusText(it), stateClass(it));
            if (it.percent != null) setRowProgress(row, it.percent);
            if (it.status === 'done') {
                setRowProgress(row, 100);
                addViewButton(row.querySelector('.batch-item-actions'), it.id);
            }
        });
        if (added) batchSection.classList.remove('hidden');
    }

    // 顶栏 ⟳N：队列卡片里有行的已经算在 'transcribe' 里（setRowStatus 那边），这里只加没有行的
    function syncIndicator(items) {
        const rows = new Set([...batchList.querySelectorAll('.batch-item')].map(r => r.dataset.taskId));
        const live = items.filter(it => ACTIVE.has(it.status));
        updateGlobalIndicator('transcribe-bg', live
            .filter(it => it.kind === 'transcribe' && !rows.has(it.id))
            .map(it => ({ label: it.title, progress: statusText(it), tab: 'transcribe' })));
        updateGlobalIndicator('scenes', live.filter(it => it.kind === 'scenes' && !rows.has('scenes:' + it.id))
            .map(it => ({ label: it.title, progress: statusText(it), href: 'detail/' + it.id })));
        updateGlobalIndicator('studio', live.filter(it => it.kind === 'studio')
            .map(it => ({
                label: `${it.title} · ${T('activity.studio.' + it.studio_kind) !== 'activity.studio.' + it.studio_kind
                    ? T('activity.studio.' + it.studio_kind) : it.studio_kind}`,
                progress: it.total ? `${it.done || 0}/${it.total}` : T('activity.running'),
                href: 'chain/' + it.chain,
            })));
    }

    async function poll() {
        clearTimeout(timer);
        if (document.hidden) return;
        let items = [];
        try {
            const r = await fetch('/api/activity');
            if (r.ok) items = (await r.json()).items || [];
        } catch { /* 服务器重启的瞬间：下一轮再问 */ }
        syncQueue(items);
        syncIndicator(items);
        const busy = items.some(it => ACTIVE.has(it.status))
            || batchList.querySelector('.status-queued, .status-running');
        timer = setTimeout(poll, busy ? 4000 : 20000);
    }

    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    window.activityPoll = poll;          // 提交新任务后马上问一次（app.js）
    poll();
})();
