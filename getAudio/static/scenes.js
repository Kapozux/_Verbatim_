// 画面描述（scenes.py，2026-11-16）：转写详情页里，画面轨按时间插在转写正文之间（灰色「画面」行 + 小缩略图），
// 标题栏「画面」按钮开 / 关（记在本机）。没做过、有视频链接 → 按钮写「描述画面」，点了后台做、这里每 5 秒看一次。
// 两栏（11-16）：左边说的话、右边画面，按画面切行对齐。没声音的视频（引擎 scenes）正文本身就是画面描述 → 只显示画面那一栏。
(function () {
    const KEY = 'verbatim_scenes_shown';
    let poll = null;

    function shown() {
        try { return localStorage.getItem(KEY) !== '0'; } catch { return true; }
    }
    function setShown(on) {
        try { localStorage.setItem(KEY, on ? '1' : '0'); } catch { /* 隐私模式：只这次有效 */ }
    }

    function button() {
        let b = document.getElementById('detail-scenes-btn');
        if (!b) {
            b = document.createElement('button');
            b.id = 'detail-scenes-btn';
            b.className = 'btn-secondary hidden';
            const acts = document.querySelector('#detail-results-section .results-actions');
            acts.insertBefore(b, acts.firstChild);
            b.addEventListener('click', onClick);
        }
        return b;
    }

    // 11-16 用户：「转写稿的区域切两块，左边是音频提取的，右边是画面的」。
    // 一个画面一行：右边这张画面（缩略图 + 画面上的字 + 描述），左边是这张画面停留期间说的话（原来的 .segment 行挪进来）。
    // 第一张画面之前说的话单独一行。没人说话的期（正文就是画面描述）左边空着 → 只剩画面一栏。窄屏上下排。
    function clearRows() {
        const c = detailSegmentsContainer;
        c.querySelectorAll('.av-head').forEach(el => el.remove());
        c.querySelectorAll('.av-row').forEach(row => {
            row.querySelectorAll('.av-said > .segment').forEach(seg => c.insertBefore(seg, row));
            row.remove();
        });
        c.querySelectorAll('.scene-row').forEach(el => el.remove());
        c.classList.remove('av-split', 'av-nospeech');
    }

    function card(tid, s) {
        const div = document.createElement('div');
        div.className = 'scene-row av-card';
        div.dataset.startSec = s.start;
        const ts = document.createElement('span');
        ts.className = 'timestamp clickable';
        ts.textContent = fmtClock(s.start);
        ts.title = T('detail.jumpHere');
        ts.addEventListener('click', () => {
            if (!detailAudioPlayer.src) return;
            detailAudioPlayer.currentTime = s.start;
            detailAudioPlayer.play();
        });
        const body = document.createElement('div');
        body.className = 'scene-body';
        if (s.img) {
            const img = document.createElement('img');
            img.src = `/api/scenes/${tid}/${s.img}`;
            img.alt = '';
            img.loading = 'lazy';
            img.className = 'scene-thumb';
            img.addEventListener('click', () => img.classList.toggle('big'));
            body.appendChild(img);
        }
        const words = document.createElement('div');
        words.className = 'scene-words';
        words.appendChild(ts);
        if (s.caption) {            // 画面上的字放前面，正常字体
            const cap = document.createElement('p');
            cap.className = 'scene-cap';
            cap.textContent = s.caption;
            words.appendChild(cap);
        }
        if (s.text) {
            const p = document.createElement('p');
            p.textContent = s.text;
            words.appendChild(p);
        }
        body.appendChild(words);
        div.appendChild(body);
        return div;
    }

    function drawRows(tid, list, nospeech) {
        clearRows();
        if (!shown() || !list.length) return;
        const c = detailSegmentsContainer;
        const segs = Array.from(c.querySelectorAll(':scope > .segment'));
        // 插入位置用一个固定标记：第一段话自己会被挪进第一行，不能拿它当参照
        const mark = document.createComment('av');
        c.insertBefore(mark, segs[0] || null);
        c.classList.add('av-split');
        c.classList.toggle('av-nospeech', !!nospeech);
        const head = document.createElement('div');
        head.className = 'av-head';
        head.innerHTML = `<span>${T('scenes.colSaid')}</span><span>${T('scenes.colPic')}</span>`;
        c.insertBefore(head, mark);
        const rows = [];
        const row = pic => {
            const r = document.createElement('div');
            r.className = 'av-row';
            const said = document.createElement('div');
            said.className = 'av-said';
            const right = document.createElement('div');
            right.className = 'av-pic';
            if (pic) right.appendChild(pic);
            r.append(said, right);
            c.insertBefore(r, mark);
            rows.push(r);
            return said;
        };
        const sorted = list.slice().sort((a, b) => a.start - b.start);
        const lead = segs.filter(el => parseFloat(el.dataset.startSec) < sorted[0].start - 0.5);
        if (lead.length && !nospeech) {
            const said = row(null);
            lead.forEach(el => said.appendChild(el));
        }
        sorted.forEach((s, k) => {
            const said = row(card(tid, s));
            if (nospeech) return;
            const end = k + 1 < sorted.length ? sorted[k + 1].start : Infinity;
            segs.forEach(el => {
                const t = parseFloat(el.dataset.startSec);
                if (el.parentNode === c && t >= s.start - 0.5 && t < end - 0.5) said.appendChild(el);
            });
        });
        // 时间对不上的（没 startSec）留在原地，放到最后一行左边，别丢
        if (!nospeech && rows.length) segs.filter(el => el.parentNode === c)
            .forEach(el => rows[rows.length - 1].querySelector('.av-said').appendChild(el));
        mark.remove();
    }

    function fmtClock(sec) {
        sec = Math.floor(sec || 0);
        const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
        const two = n => String(n).padStart(2, '0');
        return h ? `${h}:${two(m)}:${two(ss)}` : `${two(m)}:${two(ss)}`;
    }

    let current = null;      // {tid, data}

    function render(tid, d, nospeech) {
        const b = button();
        current = { tid, d, nospeech };
        b.disabled = false;
        b.classList.remove('hidden', 'on');
        if (d.scenes && d.scenes.length) {
            b.textContent = T('scenes.toggle', { n: d.scenes.length });
            b.title = T('scenes.toggleTitle');
            b.classList.toggle('on', shown());
            b.setAttribute('aria-pressed', shown() ? 'true' : 'false');
            drawRows(tid, d.scenes, current.nospeech);
        } else if (d.status === 'queued' || d.status === 'running') {
            b.textContent = T('scenes.working');
            b.disabled = true;
            schedule(tid);
        } else if (d.can_make) {
            b.textContent = T(d.status === 'failed' ? 'scenes.retry' : 'scenes.make');
            b.title = d.status === 'failed' && d.error ? d.error : T('scenes.makeTitle');
        } else {
            b.classList.add('hidden');
        }
    }

    function schedule(tid) {
        clearTimeout(poll);
        poll = setTimeout(() => load(tid, current && current.nospeech), 5000);
    }

    async function load(tid, nospeech) {
        let d;
        try { d = await (await fetch(`/api/scenes/${tid}`)).json(); } catch { return; }
        if (!detailRecord || detailRecord.id !== tid) return;       // 等的时候换到别的期了
        render(tid, d, nospeech);
    }

    async function onClick() {
        if (!current) return;
        const { tid, d } = current;
        if (d.scenes && d.scenes.length) {
            setShown(!shown());
            render(tid, d, current.nospeech);
            return;
        }
        const b = button();
        b.disabled = true;
        b.textContent = T('scenes.working');
        try {
            const r = await fetch(`/api/scenes/${tid}`, { method: 'POST' });
            const j = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(j.error || r.status);
            schedule(tid);
        } catch (e) {
            showToast(T('scenes.failed', { message: e.message }));
            b.disabled = false;
            b.textContent = T('scenes.make');
        }
    }

    window.detailScenes = function (data) {
        clearTimeout(poll);
        current = null;
        clearRows();
        button().classList.add('hidden');
        if (!data || !data.id) return;
        load(data.id, data.engine === 'scenes');      // 没人说话的期：正文就是画面描述 → 只显示画面一栏（带缩略图）
    };
})();
