// 画面描述（scenes.py，2026-11-16）：转写详情页里，画面轨按时间插在转写正文之间（灰色「画面」行 + 小缩略图），
// 标题栏「画面」按钮开 / 关（记在本机）。没做过、有视频链接 → 按钮写「描述画面」，点了后台做、这里每 5 秒看一次。
// 没声音的视频（引擎 scenes）正文本身就是画面描述，不再叠一层。
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

    function clearRows() {
        detailSegmentsContainer.querySelectorAll('.scene-row').forEach(el => el.remove());
    }

    function drawRows(tid, list) {
        clearRows();
        if (!shown()) return;
        const rows = Array.from(detailSegmentsContainer.querySelectorAll('.segment'));
        const turns = detailSegmentsContainer.classList.contains('has-turns');
        list.forEach(s => {
            const div = document.createElement('div');
            div.className = 'scene-row' + (turns ? ' in-turns' : '');
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
            const tag = document.createElement('span');
            tag.className = 'scene-tag';
            tag.textContent = T('scenes.tag');
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
            if (s.caption) {            // 字幕模式：画面上的字（这类视频的「话」）放前面，正常字体
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
            div.append(ts, tag, body);
            const next = rows.find(r => parseFloat(r.dataset.startSec) > s.start);
            detailSegmentsContainer.insertBefore(div, next || null);
        });
    }

    function fmtClock(sec) {
        sec = Math.floor(sec || 0);
        const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
        const two = n => String(n).padStart(2, '0');
        return h ? `${h}:${two(m)}:${two(ss)}` : `${two(m)}:${two(ss)}`;
    }

    let current = null;      // {tid, data}

    function render(tid, d) {
        const b = button();
        current = { tid, d };
        b.disabled = false;
        b.classList.remove('hidden', 'on');
        if (d.scenes && d.scenes.length) {
            b.textContent = T('scenes.toggle', { n: d.scenes.length });
            b.title = T('scenes.toggleTitle');
            b.classList.toggle('on', shown());
            b.setAttribute('aria-pressed', shown() ? 'true' : 'false');
            drawRows(tid, d.scenes);
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
        poll = setTimeout(() => load(tid), 5000);
    }

    async function load(tid) {
        let d;
        try { d = await (await fetch(`/api/scenes/${tid}`)).json(); } catch { return; }
        if (!detailRecord || detailRecord.id !== tid) return;       // 等的时候换到别的期了
        render(tid, d);
    }

    async function onClick() {
        if (!current) return;
        const { tid, d } = current;
        if (d.scenes && d.scenes.length) {
            setShown(!shown());
            render(tid, d);
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
        if (!data || !data.id || data.engine === 'scenes') return;
        load(data.id);
    };
})();
