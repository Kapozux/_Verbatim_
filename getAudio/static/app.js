/**
 * Client-side logic for audio/video transcription app.
 * Supports batch (multi-file) transcription: drop many files at once, each
 * runs as an independent task with its own progress row. Completed rows link
 * into the existing history detail view. Results persist server-side.
 */

// ========== DOM: Main view ==========
const mainView = document.getElementById('main-view');
const form = document.getElementById('upload-form');
const fileInput = document.getElementById('audio-file');
const fileLabel = document.querySelector('.file-label');
const fileLabelText = document.getElementById('file-label-text');
const fileInfo = document.getElementById('file-info');
const submitBtn = document.getElementById('submit-btn');
const dropZone = document.getElementById('drop-zone');

const batchSection = document.getElementById('batch-section');
const batchList = document.getElementById('batch-list');
const batchProgress = document.getElementById('batch-progress');

const errorSection = document.getElementById('error-section');
const errorText = document.getElementById('error-text');

const clearHistoryBtn = document.getElementById('clear-history-btn');
const historyList = document.getElementById('history-list');
const historyCount = document.getElementById('history-count');
const searchInput = document.getElementById('search-input');
const engineFilters = document.getElementById('engine-filters');
const enrichBtn = document.getElementById('enrich-btn');

// ========== DOM: Detail view ==========
const detailView = document.getElementById('detail-view');
const detailBackBtn = document.getElementById('detail-back-btn');
const detailTitle = document.getElementById('detail-title');
const detailMeta = document.getElementById('detail-meta');
const detailPlayerSection = document.getElementById('detail-player-section');
const detailAudioPlayer = document.getElementById('detail-audio-player');
const detailSummarySection = document.getElementById('detail-summary-section');
const detailSummaryOverview = document.getElementById('detail-summary-overview');
const detailSummarySections = document.getElementById('detail-summary-sections');
const detailSegmentsContainer = document.getElementById('detail-segments-container');
const detailCopyBtn = document.getElementById('detail-copy-btn');
const detailDownloadBtn = document.getElementById('detail-download-btn');
const detailSrtBtn = document.getElementById('detail-srt-btn');

// ========== State ==========
let detailSegments = [];
let detailRecord = null;   // 当前详情视图整条记录（含 ai_title），下载文件名要用
let batchTotal = 0;
let batchFinished = 0;

// ========== Helpers ==========
function engineLabel(engine) {
    return STRINGS.en['engine.' + engine] ? T('engine.' + engine) : (engine || 'Unknown');
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '';
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    return `${m}:${String(sec).padStart(2, '0')}`;
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ========== 统一入口：文件 + 链接 + 本地路径 ==========
// 文件的事实来源是 intakeFiles（可多次追加、单条删除）；
// 文本行的事实来源是 #mixed-input 的内容（一行一条，实时解析出预览）。
let intakeFiles = [];
let intakeSubmitting = false;
const mixedInput = document.getElementById('mixed-input');
const intakeList = document.getElementById('intake-list');

fileInput.addEventListener('change', () => {
    addFiles(fileInput.files);
    fileInput.value = '';   // 清掉原生选择，允许再次添加同名文件；预览列表才是事实来源
});

function addFiles(files) {
    for (const f of files || []) {
        // 同名同大小视为重复，跳过
        if (!intakeFiles.some(x => x.name === f.name && x.size === f.size)) {
            intakeFiles.push(f);
        }
    }
    renderIntake();
}

function updateFileLabel() {
    if (intakeFiles.length === 0) {
        fileLabelText.textContent = T('transcribe.dropLabel');
        fileInfo.textContent = '';
        fileLabel.classList.remove('has-file');
        return;
    }
    let totalSize = 0;
    for (const f of intakeFiles) totalSize += f.size;
    fileLabelText.textContent = intakeFiles.length === 1
        ? intakeFiles[0].name : `${intakeFiles.length} files added`;
    fileInfo.textContent = formatFileSize(totalSize);
    fileLabel.classList.add('has-file');
}

// 逐行分类：http(s):// → 链接；/ 或 ~ 开头（允许引号包裹）→ 本地路径；其余非空行 → invalid
function parseTextLines() {
    const out = [];
    (mixedInput.value || '').split('\n').forEach((raw, line) => {
        const s = raw.trim();
        if (!s) return;
        let kind = 'invalid';
        if (/^https?:\/\//i.test(s)) kind = 'link';
        else if (/^[\/~]/.test(s.replace(/^['"]/, ''))) kind = 'path';
        // 链接可带行尾时间段后缀 " @10:00-25:00"（只转那一段）；预览里拆出来显示
        let label = s, clip = '';
        if (kind === 'link') {
            const m = s.match(/\s+@\s*([0-9:]+)\s*-\s*([0-9:]*)\s*$/);
            if (m) {
                label = s.slice(0, m.index).trim();
                clip = `${m[1]}–${m[2] || 'end'}`;
            }
        }
        out.push({ kind, text: s, label, clip, line });
    });
    return out;
}

function intakeBadge(kind) { return T('intake.' + kind); }

function buildIntakeRow(r) {
    const row = document.createElement('div');
    row.className = 'intake-row' + (r.kind === 'invalid' ? ' intake-invalid' : '');

    const badge = document.createElement('span');
    badge.className = `intake-badge intake-badge-${r.kind}`;
    badge.textContent = intakeBadge(r.kind);
    row.appendChild(badge);

    const name = document.createElement('span');
    name.className = 'intake-name';
    name.textContent = r.label;
    name.title = r.label;
    row.appendChild(name);

    if (r.sub || r.kind === 'invalid') {
        const sub = document.createElement('span');
        sub.className = 'intake-sub';
        sub.textContent = r.kind === 'invalid'
            ? T('intake.invalidHint') : r.sub;
        row.appendChild(sub);
    }

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'intake-del';
    del.textContent = '×';
    del.title = T('common.remove');
    del.addEventListener('click', () => removeIntakeRow(r));
    row.appendChild(del);
    return row;
}

function removeIntakeRow(r) {
    if (r.kind === 'file') {
        intakeFiles.splice(r.fileIndex, 1);
    } else {
        const lines = mixedInput.value.split('\n');
        lines.splice(r.line, 1);
        mixedInput.value = lines.join('\n');
    }
    renderIntake();
}

function renderIntake() {
    const rows = [];
    intakeFiles.forEach((f, i) => rows.push(
        { kind: 'file', label: f.name, sub: formatFileSize(f.size), fileIndex: i }));
    parseTextLines().forEach(t => rows.push({
        kind: t.kind, label: t.label || t.text, line: t.line,
        sub: t.clip ? `⏱ ${t.clip}` : '',
    }));
    intakeList.innerHTML = '';
    rows.forEach(r => intakeList.appendChild(buildIntakeRow(r)));
    updateFileLabel();
    updateSubmitBtn();
}

function intakeValidCount() {
    return intakeFiles.length
        + parseTextLines().filter(t => t.kind !== 'invalid').length;
}

function updateSubmitBtn() {
    if (intakeSubmitting) { submitBtn.disabled = true; return; }
    const n = intakeValidCount();
    submitBtn.textContent = n > 0
        ? T('transcribe.submitCount', { n, itemWord: n === 1 ? 'item' : 'items' })
        : T('transcribe.submit');
    submitBtn.disabled = n === 0;
}

let mixedInputTimer = null;
if (mixedInput) mixedInput.addEventListener('input', () => {
    clearTimeout(mixedInputTimer);
    mixedInputTimer = setTimeout(renderIntake, 250);
});

// ========== Drag & Drop (multi-file) ==========
['dragenter', 'dragover'].forEach(evt => {
    dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
    });
});

['dragleave', 'drop'].forEach(evt => {
    dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
    });
});

dropZone.addEventListener('drop', (e) => {
    const files = e.dataTransfer.files;
    if (files && files.length > 0) addFiles(files);
});

// ========== Engine selection: toggle 预计人数 (precise only) ==========
const speakerCountGroup = document.getElementById('speaker-count-group');
document.querySelectorAll('input[name="engine"]').forEach(radio => {
    radio.addEventListener('change', () => {
        const isPrecise = document.querySelector('input[name="engine"]:checked').value === 'precise';
        speakerCountGroup.classList.toggle('hidden', !isPrecise);
    });
});

// ========== 国内云引擎（DashScope / Precise）：默认隐藏 + 风险确认 ==========
// 这两个引擎把音频送阿里云，阿里强制内容审核 —— 敏感/政治内容会被拒或篡改。
const showMainlandBtn = document.getElementById('show-mainland');
const mainlandEngines = document.getElementById('mainland-engines');
if (showMainlandBtn && mainlandEngines) {
    showMainlandBtn.addEventListener('click', () => {
        mainlandEngines.classList.toggle('hidden');
        showMainlandBtn.classList.toggle('open');
    });
}
document.querySelectorAll('input[name="engine"][value="qwenasr"], input[name="engine"][value="precise"]')
    .forEach(radio => {
        radio.addEventListener('change', () => {
            if (radio.checked && !confirm(T('confirm.mainlandWarning'))) {
                // 拒绝 → 退回 Whisper
                const w = document.querySelector('input[name="engine"][value="whisper"]');
                w.checked = true;
                w.dispatchEvent(new Event('change'));
            }
        });
    });

// ========== Form submission (batch) ==========
// 同时上传的文件数。逐个上传是为了绕开单请求体积上限（几百个文件塞一个请求会超限被拒），
// 上传本身也限流，避免一次性发起过多大文件上传拖垮网络/内存。
// 真正的转写并发由服务器端每引擎的信号量控制（见 config.ENGINE_CONCURRENCY）。
const UPLOAD_CONCURRENCY = 3;

// 混合批次提交：文件 → /upload（上传池），链接 → /api/transcribe_urls（整批），
// 本地路径 → /api/transcribe_local（逐条）。三路并发，共用一个 Queue。
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (intakeSubmitting) return;
    const files = intakeFiles.slice();
    const texts = parseTextLines();
    const linkItems = texts.filter(t => t.kind === 'link');
    const links = linkItems.map(t => t.text);   // 带 @后缀，服务器端解析
    const paths = texts.filter(t => t.kind === 'path').map(t => t.text);
    const total = files.length + links.length + paths.length;
    if (!total) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;
    errorSection.classList.add('hidden');
    batchSection.classList.remove('hidden');
    batchList.innerHTML = '';
    batchTotal = total;
    batchFinished = 0;
    updateBatchProgress();

    intakeSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = T('transcribe.submitting');

    // 按预览顺序先建行（文件 → 链接 → 路径），invalid 行不进队列
    const fileJobs = files.map(file => {
        const row = createBatchRow(file.name);
        setRowStatus(row, T('rowStatus.queued'), 'queued');
        batchList.appendChild(row);
        return { file, row };
    });
    const linkRows = linkItems.map(t => {
        // Queue 里显示干净 URL + 时间段徽标，别露出 @后缀
        const row = createBatchRow(t.clip ? `${t.label}  (⏱ ${t.clip})` : t.label);
        setRowStatus(row, T('rowStatus.queued'), 'queued');
        batchList.appendChild(row);
        return row;
    });
    const pathRows = paths.map(p => {
        const row = createBatchRow(p.split('/').pop() || p);
        setRowStatus(row, T('rowStatus.queued'), 'queued');
        batchList.appendChild(row);
        return row;
    });

    // 提交即清空输入区（invalid 行留在文本框里，用户可改）
    intakeFiles = [];
    const keptLines = mixedInput.value.split('\n')
        .filter(l => { const s = l.trim(); return s && !(/^https?:\/\//i.test(s)) && !(/^[\/~]/.test(s.replace(/^['"]/, ''))); });
    mixedInput.value = keptLines.join('\n');
    renderIntake();

    await Promise.all([
        runUploadPool(fileJobs, engine, UPLOAD_CONCURRENCY),
        submitLinks(links, linkRows, engine),
        submitPaths(paths, pathRows, engine),
    ]);
});

async function runUploadPool(jobs, engine, concurrency) {
    let cursor = 0;

    async function worker() {
        while (cursor < jobs.length) {
            const job = jobs[cursor++];
            await uploadOne(job.file, job.row, engine);
        }
    }

    const workers = [];
    for (let i = 0; i < Math.min(concurrency, jobs.length); i++) {
        workers.push(worker());
    }
    await Promise.all(workers);
}

// —— 链接批：一次 POST，服务器按行返回 tasks（与 rows 顺序一一对应，上限 20）——
async function submitLinks(links, rows, engine) {
    if (!links.length) return;
    try {
        const resp = await fetch('/api/transcribe_urls', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                urls: links.join('\n'), engine,
                max_videos: parseInt((document.getElementById('url-max-videos') || {}).value, 10) || 20,
                subs: (document.getElementById('url-subs') || {}).value || 'auto',
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.error) {
            rows.forEach(row => {
                setRowStatus(row, data.error || T('rowStatus.failedCode', { status: resp.status }), 'error');
                onTaskFinished();
            });
            return;
        }
        // 注意：一行**合集/播放列表**链接会展开成多个任务，所以 tasks 可能比 rows 多。
        // 多出来的当场补行（用服务器返回的标题），别让它们没有进度显示。
        data.tasks.forEach((t, i) => {
            let row = rows[i];
            if (!row) {
                row = createBatchRow(t.title || t.url);
                batchList.appendChild(row);
                batchTotal += 1;
                updateBatchProgress();
            } else if (t.title) {
                const nameEl = row.querySelector('.batch-item-name');
                if (nameEl) nameEl.textContent = t.title;   // 展开后用真实标题替掉合集URL
            }
            setRowStatus(row, T('rowStatus.downloading'), 'running');
            connectBatchSSE(t.task_id, row);
        });
        // 超出服务器单批上限被截掉的行，明确标出而不是悄悄消失
        for (let i = data.tasks.length; i < rows.length; i++) {
            setRowStatus(rows[i], T('rowStatus.skippedMax20'), 'error');
            onTaskFinished();
        }
        // 合集枚举失败之类的问题，提示出来而不是静默
        if (data.errors && data.errors.length) {
            showToast(T('toast.someLinksNotExpanded', { message: data.errors[0] }));
        }
    } catch (err) {
        rows.forEach(row => {
            setRowStatus(row, T('rowStatus.failedMessage', { message: err.message }), 'error');
            onTaskFinished();
        });
    }
}

// —— 本地路径批：逐条 POST（服务器软链读盘，零上传）——
async function submitPaths(paths, rows, engine) {
    for (let i = 0; i < paths.length; i++) {
        const row = rows[i];
        setRowStatus(row, T('rowStatus.readingLocal'), 'running');
        const body = { path: paths[i], engine };
        const speakerEl = document.getElementById('speaker-count');
        if (engine === 'precise' && speakerEl && speakerEl.value.trim()) {
            body.speaker_count = speakerEl.value.trim();
        }
        try {
            const resp = await fetch('/api/transcribe_local', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.error) {
                setRowStatus(row, data.error || T('rowStatus.failedCode', { status: resp.status }), 'error');
                onTaskFinished();
            } else {
                connectBatchSSE(data.task_id, row);
            }
        } catch (err) {
            setRowStatus(row, T('rowStatus.failedMessage', { message: err.message }), 'error');
            onTaskFinished();
        }
    }
}

async function uploadOne(file, row, engine) {
    setRowStatus(row, T('rowStatus.uploading'), 'running');

    const formData = new FormData();
    formData.append('audio', file);
    formData.append('engine', engine);

    // 精准模式下把"预计人数"一起带上（留空则不带，服务器自动判断）
    const speakerCountEl = document.getElementById('speaker-count');
    if (engine === 'precise' && speakerCountEl && speakerCountEl.value.trim()) {
        formData.append('speaker_count', speakerCountEl.value.trim());
    }

    try {
        const resp = await fetch('/upload', { method: 'POST', body: formData });
        if (!resp.ok) {
            let msg = T('rowStatus.uploadFailedCode', { status: resp.status });
            try {
                const d = await resp.json();
                if (d.error) msg = d.error;
            } catch { /* 413 等可能不是 JSON */ }
            setRowStatus(row, `${msg}`, 'error');
            onTaskFinished();
            return;
        }

        const data = await resp.json();
        if (data.error) {
            setRowStatus(row, `${data.error}`, 'error');
            onTaskFinished();
            return;
        }

        // 上传成功 → 服务器已排队，接 SSE 看进度
        connectBatchSSE(data.task_id, row);
    } catch (err) {
        setRowStatus(row, T('rowStatus.uploadFailedMessage', { message: err.message }), 'error');
        onTaskFinished();
    }
}

function resetSubmitBtn() {
    intakeSubmitting = false;
    updateSubmitBtn();   // 恢复 "Transcribe N items"（批次跑完后输入区通常已空 → 禁用）
}

// ========== Batch rows ==========
function createBatchRow(filename) {
    const row = document.createElement('div');
    row.className = 'batch-item';

    const info = document.createElement('div');
    info.className = 'batch-item-info';

    const name = document.createElement('span');
    name.className = 'batch-item-name';
    name.textContent = filename;

    const status = document.createElement('span');
    status.className = 'batch-item-status';
    status.textContent = T('rowStatus.queued');

    // 右侧：状态文字 + 计时（已用 / 预计还要）
    const right = document.createElement('span');
    right.className = 'batch-item-right';
    right.appendChild(status);
    const clock = document.createElement('span');
    clock.className = 'batch-item-time';
    right.appendChild(clock);

    info.appendChild(name);
    info.appendChild(right);

    const bar = document.createElement('div');
    bar.className = 'batch-item-progress';
    const fill = document.createElement('div');
    fill.className = 'batch-item-progress-fill';
    bar.appendChild(fill);

    const actions = document.createElement('div');
    actions.className = 'batch-item-actions';

    row.appendChild(info);
    row.appendChild(bar);
    row.appendChild(actions);
    return row;
}

function setRowStatus(row, text, state) {
    const status = row.querySelector('.batch-item-status');
    status.textContent = text;
    status.className = 'batch-item-status';
    if (state) status.classList.add(`status-${state}`);
    // 所有队列行的状态变化都经过这里 → 顺手刷新全局任务指示器
    updateGlobalIndicator('transcribe', collectQueueTasks());
}

// Queue 的事实来源就是 #batch-list 的行：未完成 = queued / running
function collectQueueTasks() {
    return [...batchList.querySelectorAll('.batch-item')]
        .filter(r => r.querySelector('.status-queued, .status-running'))
        .map(r => ({
            label: r.querySelector('.batch-item-name').textContent,
            progress: r.querySelector('.batch-item-status').textContent,
            tab: 'transcribe',
        }));
}

function setRowProgress(row, percent) {
    row.querySelector('.batch-item-progress-fill').style.width = `${percent}%`;
}

function connectBatchSSE(taskId, row) {
    const actions = row.querySelector('.batch-item-actions');
    const source = new EventSource(`/stream/${taskId}`);
    startRowClock(row);

    source.onmessage = (event) => {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
            case 'queued':
                setRowStatus(row, T('rowStatus.queued'), 'queued');
                break;

            // 服务器抽完音频、知道时长后，按同引擎历史速度给的预估；null = 样本不够，不猜
            case 'eta':
                row._eta = msg.expected_s ? { at: Date.now(), expected: msg.expected_s } : null;
                tickRowClock(row);
                break;

            case 'progress':
                setRowStatus(row, T('rowStatus.transcribing', { percent: msg.percent }), 'running');
                setRowProgress(row, msg.percent);
                break;

            // segment / summary 事件在此忽略：结果自动进历史，点“查看”看详情
            case 'segment':
            case 'summary':
                break;

            case 'done':
                stopRowClock(row);
                setRowStatus(row, doneLabel(msg.timing), 'done');
                setRowProgress(row, 100);
                addViewButton(actions, taskId);
                source.close();
                onTaskFinished();
                renderHistory();
                loadEngineSpeed();          // 多了一个样本，引擎旁的速度提示跟着更新
                break;

            case 'error':
                stopRowClock(row);
                setRowStatus(row, `${msg.message}`, 'error');
                source.close();
                onTaskFinished();
                break;
        }
    };

    source.onerror = () => {
        // SSE 会自动重连；只有队列里已经推完 done/error 才真正结束。
        // 这里不主动报错，避免瞬时断连误报。
    };
}

// ===== 队列行计时：已用时长每秒走，预估来了就并排显示「还要约 X」 =====
function fmtElapsed(seconds) {
    const s = Math.max(0, Math.round(seconds || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}h${String(m).padStart(2, '0')}m`;
    if (m) return `${m}m${String(sec).padStart(2, '0')}s`;
    return `${sec}s`;
}

function startRowClock(row) {
    stopRowClock(row);
    row._t0 = Date.now();
    row._eta = null;
    tickRowClock(row);
    row._clock = setInterval(() => tickRowClock(row), 1000);
}

function stopRowClock(row) {
    if (row._clock) clearInterval(row._clock);
    row._clock = null;
    const el = row.querySelector('.batch-item-time');
    if (el) el.textContent = '';
}

function tickRowClock(row) {
    const el = row.querySelector('.batch-item-time');
    if (!el || !row._t0) return;
    let txt = '⏱ ' + fmtElapsed((Date.now() - row._t0) / 1000);
    if (row._eta && row._eta.expected) {
        const remain = row._eta.expected - (Date.now() - row._eta.at) / 1000;
        txt += ' · ' + (remain > 5 ? T('row.eta', { t: fmtElapsed(remain) }) : T('row.finishing'));
    }
    el.textContent = txt;
}

// 完成行文案：「完成 · 2m48s (13×)」——processing 不含排队；13× = 音频时长 ÷ 转写本体
function doneLabel(timing) {
    if (!timing || !timing.processing_s) return T('rowStatus.done');
    let t = fmtElapsed(timing.processing_s);
    if (timing.speed_x) t += ` (${timing.speed_x}×)`;
    return T('rowStatus.doneIn', { t });
}

// 详情页悬停：分阶段明细
const TIMING_STAGES = [
    ['queued_s', 'timing.queued'], ['subs_check_s', 'timing.subs'], ['download_s', 'timing.download'],
    ['extract_s', 'timing.extract'], ['transcribe_s', 'timing.transcribe'], ['summary_s', 'timing.summary'],
    ['save_s', 'timing.save'], ['enrich_s', 'timing.enrich'],
];
function timingBreakdown(tm) {
    const parts = TIMING_STAGES.filter(([k]) => tm[k] != null && tm[k] >= 0.5)
        .map(([k, key]) => `${T(key)} ${fmtElapsed(tm[k])}`);
    if (tm.fallback_from) parts.push(T('timing.fallback', { engine: engineLabel(tm.fallback_from) }));
    return parts.join(' · ');
}

// ===== 引擎选择处的速度提示：「每小时音频约 X 分钟 · 近 N 次」，把数字放在做决定的地方 =====
function fmtMinutes(m) {
    if (m == null) return '—';
    if (m < 1) return '<1';
    return m < 10 ? String(Math.round(m * 10) / 10) : String(Math.round(m));
}

async function loadEngineSpeed() {
    let sp;
    try { sp = await (await fetch('/api/speed')).json(); } catch { return; }
    document.querySelectorAll('input[name="engine"]').forEach(inp => {
        const opt = inp.closest('.radio-option');
        const label = opt && opt.querySelector('.radio-label');
        if (!label) return;
        let el = label.querySelector('.engine-speed');
        const row = sp[inp.value];
        if (!row || !row.n) { if (el) el.remove(); return; }
        if (!el) { el = document.createElement('small'); el.className = 'engine-speed'; label.appendChild(el); }
        el.textContent = T('transcribe.speedHint', { min: fmtMinutes(row.min_per_hour), n: row.n });
    });
}

function addViewButton(actions, taskId) {
    if (actions.querySelector('.batch-view-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'btn-secondary btn-small batch-view-btn';
    btn.textContent = T('common.view');
    btn.addEventListener('click', () => navigate('detail/' + taskId));
    actions.appendChild(btn);
}

function onTaskFinished() {
    batchFinished += 1;
    updateBatchProgress();
    if (batchFinished >= batchTotal) {
        resetSubmitBtn();
    }
}

function updateBatchProgress() {
    batchProgress.textContent = `${batchFinished} / ${batchTotal} done`;
}

// ========== Segment rendering (shared with detail view) ==========
function parseTimestampToSeconds(ts) {
    const parts = ts.split(':').map(Number);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return 0;
}

function setupTimeSync(player, container, stateObj) {
    player.addEventListener('timeupdate', () => {
        const currentTime = player.currentTime;
        const segs = container.querySelectorAll('.segment');
        if (segs.length === 0) return;

        let active = null;
        for (let i = segs.length - 1; i >= 0; i--) {
            if (currentTime >= parseFloat(segs[i].dataset.startSec)) {
                active = segs[i];
                break;
            }
        }

        if (active === stateObj.last) return;
        if (stateObj.last) stateObj.last.classList.remove('active');
        if (active) {
            active.classList.add('active');
            active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
        stateObj.last = active;
    });
}

const detailSyncState = { last: null };
setupTimeSync(detailAudioPlayer, detailSegmentsContainer, detailSyncState);

function appendSegment(seg, container, player) {
    const div = document.createElement('div');
    div.className = 'segment';
    div.dataset.startSec = parseTimestampToSeconds(seg.timestamp);

    const ts = document.createElement('span');
    ts.className = 'timestamp clickable';
    ts.textContent = seg.timestamp;
    ts.title = 'Jump to this point';
    ts.addEventListener('click', () => {
        if (!player.src) return;
        player.currentTime = parseTimestampToSeconds(seg.timestamp);
        player.play();
    });

    const text = document.createElement('span');
    text.className = 'segment-text';
    text.textContent = seg.text;

    div.appendChild(ts);
    div.appendChild(text);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showError(message) {
    errorSection.classList.remove('hidden');
    errorText.textContent = message;
}

function renderSummary(section, overviewEl, sectionsEl, data) {
    if (!data || !data.overview) return;

    section.classList.remove('hidden');
    overviewEl.textContent = data.overview;
    sectionsEl.innerHTML = '';

    if (data.sections && data.sections.length > 0) {
        data.sections.forEach(sec => {
            const block = document.createElement('div');
            block.className = 'summary-section-item';

            const header = document.createElement('div');
            header.className = 'summary-section-header';

            const title = document.createElement('span');
            title.className = 'summary-section-title';
            title.textContent = sec.title;
            header.appendChild(title);

            if (sec.time_range) {
                const time = document.createElement('span');
                time.className = 'summary-section-time';
                time.textContent = sec.time_range;
                header.appendChild(time);
            }

            const desc = document.createElement('p');
            desc.className = 'summary-section-desc';
            desc.textContent = sec.summary;

            block.appendChild(header);
            block.appendChild(desc);
            sectionsEl.appendChild(block);
        });
    }
}

// ========== Copy & Download helpers ==========
function segmentLines(segs) {
    return segs.map(seg => `[${seg.timestamp}] ${seg.text}`);
}

// ========== Copy & Download (detail view) ==========
detailCopyBtn.addEventListener('click', () => {
    copyToClipboard(segmentLines(detailSegments).join('\n'));
});

detailDownloadBtn.addEventListener('click', () => {
    const title = (detailRecord && detailRecord.ai_title) || (detailRecord && detailRecord.filename) || T('common.transcriptFallback');
    const body = `# ${title}\n\n` + segmentLines(detailSegments).join('\n');
    downloadFile(body, transcriptDownloadName(detailRecord, 'md'), 'text/markdown;charset=utf-8');
    showToast(T('toast.downloadedMd'));
});

detailSrtBtn.addEventListener('click', () => {
    if (detailSegments.length === 0) return;
    downloadFile(buildSRT(detailSegments),
        transcriptDownloadName(detailRecord, 'srt'), 'text/plain;charset=utf-8');
    showToast(T('toast.downloadedSrt'));
});

// ========== SRT ==========
function buildSRT(segments) {
    const lines = [];
    for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const startSec = parseTimestampToSeconds(seg.timestamp);
        const endSec = (i + 1 < segments.length)
            ? parseTimestampToSeconds(segments[i + 1].timestamp)
            : startSec + 5;
        lines.push(String(i + 1));
        lines.push(`${formatSRTTime(startSec)} --> ${formatSRTTime(endSec)}`);
        lines.push(seg.text);
        lines.push('');
    }
    return lines.join('\n');
}

function formatSRTTime(totalSeconds) {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    return `${pad2(h)}:${pad2(m)}:${pad2(s)},000`;
}

function pad2(n) { return String(n).padStart(2, '0'); }

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast(T('toast.copied'));
    }).catch(() => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast(T('toast.copied'));
    });
}

function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function dateStr() {
    return new Date().toISOString().slice(0, 10);
}

// 文件名安全化：去掉三大平台都不认的字符，压掉多余空白，防止导出后打不开/自动改名。
function sanitizeFilenamePart(text, fallback) {
    const cleaned = (text || '').trim()
        .replace(/[\\/:*?"<>|]/g, '')
        .replace(/\s+/g, ' ')
        .slice(0, 60)
        .trim();
    return cleaned || fallback;
}

// 转写详情页的下载文件名：「博主-名字 日期.md」
//   博主：meta 里的 creator（新转写下载时抓的 uploader；链条任务从 chain.json 反查），没有就省掉
//   名字：原文件名本身像个标题（enrich 阶段 AI 判断的 filename_meaningful）就用原文件名，
//         否则用 AI 标题；两者都没有退回原文件名/日期
//   日期：转写日期（meta.date 前 10 位）
// 不再加随机串：浏览器遇到同名下载会自己加 (1)。SRT/TXT 用同一个主体名，只换后缀。
const VID_SUFFIX_RE = /\s*\[[A-Za-z0-9_-]{6,}\]\s*$/;
function filenameStem(name) {
    return (name || '').replace(/\.[a-zA-Z0-9]{1,5}$/, '').replace(VID_SUFFIX_RE, '').trim();
}
function transcriptDownloadName(record, ext) {
    const r = record || {};
    const stem = filenameStem(r.filename);
    const aiTitle = (r.ai_title || '').trim();
    const aiUsable = aiTitle && !/内容为空|无效|invalid|empty/i.test(aiTitle);
    let name;
    if (r.filename_meaningful && stem) name = stem;
    else if (aiUsable) name = aiTitle;
    else name = stem;
    name = sanitizeFilenamePart(name, `transcript_${dateStr()}`);
    const creator = sanitizeFilenamePart(r.creator, '').replace(/-/g, '‐');   // 博主名里的连字符换成 U+2010，别和分隔符混
    const date = (r.date || '').slice(0, 10) || dateStr();
    return `${creator ? creator + '-' : ''}${name} ${date}.${ext}`;
}

// ========== History (server API) ==========
// 搜索 + 引擎筛选状态
let activeEngineFilter = '';
let activeSourceFilter = '';    // '' | 'mine' | 'pipeline'
let searchTimer = null;

async function renderHistory() {
    const query = (searchInput.value || '').trim();
    try {
        const url = query
            ? `/api/search?q=${encodeURIComponent(query)}`
            : '/api/history';
        const resp = await fetch(url);
        let entries = await resp.json();

        if (activeEngineFilter) {
            entries = entries.filter(e => e.engine === activeEngineFilter);
        }
        if (activeSourceFilter) {
            entries = entries.filter(e => (e.source || 'mine') === activeSourceFilter);
        }

        historyCount.textContent = entries.length ? `(${entries.length})` : '';

        if (entries.length === 0) {
            historyList.innerHTML = query
                ? `<p class="history-empty">${T('library.noMatches')}</p>`
                : `<p class="history-empty">${T('library.noTranscripts')}</p>`;
            return;
        }

        historyList.innerHTML = '';
        entries.forEach(entry => historyList.appendChild(buildHistoryCard(entry)));
    } catch {
        historyList.innerHTML = `<p class="history-empty">${T('library.failedToLoad')}</p>`;
    }
}

function buildSourceLink(url) {
    const a = document.createElement('a');
    a.className = 'source-link';
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = '🔗';
    a.title = `${T('library.openSource')} · ${url}`;
    a.addEventListener('click', e => e.stopPropagation());
    return a;
}

function buildHistoryCard(entry) {
    const item = document.createElement('div');
    item.className = 'history-item';

    const info = document.createElement('div');
    info.className = 'history-item-info';

    // 第一行：AI 标题（没有则退回文件名）+ 引擎徽章
    const titleRow = document.createElement('div');
    titleRow.className = 'history-item-title-row';

    const name = document.createElement('span');
    name.className = 'history-item-name';
    name.textContent = entry.ai_title || entry.filename;
    name.title = entry.filename;
    titleRow.appendChild(name);

    const badge = document.createElement('span');
    badge.className = `engine-badge engine-${entry.engine || 'unknown'}`;
    badge.textContent = engineLabel(entry.engine);
    titleRow.appendChild(badge);

    // Pipeline 跑出来的挂上博主名，一眼看出这条不是我自己传的
    if (entry.source === 'pipeline') {
        const src = document.createElement('span');
        src.className = 'source-badge';
        src.textContent = `🎬 ${entry.creator || T('nav.creators')}`;
        src.title = T('library.fromPipelineRun');
        titleRow.appendChild(src);
    }

    // 来自链接的转写：挂个 🔗 直达原视频（点它不进详情页）
    if (entry.source_url) {
        titleRow.appendChild(buildSourceLink(entry.source_url));
    }

    info.appendChild(titleRow);

    // 第二行：一句话简介（或搜索命中片段）
    const oneLineText = entry.snippet || entry.ai_one_line;
    if (oneLineText) {
        const oneLine = document.createElement('span');
        oneLine.className = entry.snippet
            ? 'history-item-oneline snippet' : 'history-item-oneline';
        oneLine.textContent = oneLineText;
        info.appendChild(oneLine);
    }

    // 第三行：标签 + 元信息
    const meta = document.createElement('span');
    meta.className = 'history-item-meta';
    const parts = [];
    (entry.ai_tags || []).forEach(t => parts.push(`#${t}`));
    parts.push(entry.date);
    if (entry.duration_seconds) parts.push(formatDuration(entry.duration_seconds));
    parts.push(T('common.segmentsCount', { n: entry.segment_count }));
    if (entry.ai_title) parts.push(entry.filename);
    meta.textContent = parts.join(' · ');
    info.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'history-item-actions';

    const viewBtn = document.createElement('button');
    viewBtn.className = 'btn-secondary btn-small';
    viewBtn.textContent = T('common.view');
    viewBtn.addEventListener('click', () => navigate('detail/' + entry.id));

    const delBtn = document.createElement('button');
    delBtn.className = 'btn-secondary btn-small btn-danger';
    delBtn.textContent = T('common.delete');
    delBtn.addEventListener('click', async () => {
        const label = entry.ai_title || entry.filename;
        if (!confirm(T('confirm.deleteHistory', { label }))) return;
        try {
            const resp = await fetch(`/api/history/${entry.id}`, { method: 'DELETE' });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                showToast(data.error || T('toast.deleteFailed'));
                return;
            }
            renderHistory();
            showToast(T('toast.deleted'));
        } catch {
            showToast(T('toast.deleteFailed'));
        }
    });

    actions.appendChild(viewBtn);
    actions.appendChild(delBtn);

    item.appendChild(info);
    item.appendChild(actions);
    return item;
}

// 搜索：输入防抖 300ms
searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderHistory, 300);
});

// 引擎筛选 chips
engineFilters.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    engineFilters.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    activeEngineFilter = chip.dataset.engine || '';
    renderHistory();
});

// 来源筛选 chips：我自己弄的 vs Creators 流水线跑的
const sourceFilters = document.getElementById('source-filters');
if (sourceFilters) sourceFilters.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    sourceFilters.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    activeSourceFilter = chip.dataset.source || '';
    renderHistory();
});

// ========== AI 整理（批量生成标题/标签） ==========
enrichBtn.addEventListener('click', async () => {
    enrichBtn.disabled = true;
    enrichBtn.textContent = T('library.enriching');
    try {
        await fetch('/api/enrich_all', { method: 'POST' });
        pollEnrichStatus();
    } catch {
        showToast(T('toast.autoTitleStartFailed'));
        resetEnrichBtn();
    }
});

function resetEnrichBtn() {
    enrichBtn.disabled = false;
    enrichBtn.textContent = T('library.autoTitle');
}

let enrichWasPolling = false;   // 后台标签暂停 enrich 轮询时的续跑标记

async function pollEnrichStatus() {
    try {
        const resp = await fetch('/api/enrich_status');
        const st = await resp.json();
        if (st.running) {
            enrichBtn.textContent = `${st.done}/${st.total}`;
            // 每整理完几条就刷新列表，让标题逐步冒出来
            if (st.done > 0 && st.done % 10 === 0) renderHistory();
            if (!document.hidden) setTimeout(pollEnrichStatus, 1500);
            else enrichWasPolling = true;      // 后台暂停，回前台再续
        } else {
            resetEnrichBtn();
            renderHistory();
            if (st.total > 0) {
                showToast(T('toast.autoTitleDone', { done: st.total - st.failed, total: st.total }));
            } else {
                showToast(T('toast.allAlreadyTitled'));
            }
        }
    } catch {
        resetEnrichBtn();
    }
}

// ========== Detail View ==========
let detailReturnTo = 'tab/library';   // 打开详情前在哪，返回按钮/路由跳回用

async function openDetailView(taskId) {
    // 记住"从哪来"：如果是从某个博主的详情页点进某一期，返回也回那个博主详情，
    // 而不是死板地弹回主 Tab（旧版一直是这个毛病）。
    detailReturnTo = (chainDetailView && !chainDetailView.classList.contains('hidden') && chainDetailId)
        ? `chain/${chainDetailId}` : 'tab/library';
    try {
        const resp = await fetch(`/api/history/${taskId}`);
        if (!resp.ok) throw new Error('Not found');
        const data = await resp.json();

        // 隐藏其它可能正在显示的顶层视图（可能是从链条详情/文档页点进来的）
        mainView.classList.add('hidden');
        if (typeof chainDetailView !== 'undefined' && chainDetailView) {
            chainDetailView.classList.add('hidden');
            clearTimeout(chainDetailTimer);
        }
        if (typeof docView !== 'undefined' && docView) docView.classList.add('hidden');
        detailView.classList.remove('hidden');
        window.scrollTo({ top: 0 });

        detailRecord = data;
        detailTitle.textContent = data.filename;
        const segCount = (data.segments || []).length;
        const parts = [data.date, engineLabel(data.engine)];
        if (data.duration_seconds) parts.push(formatDuration(data.duration_seconds));
        parts.push(T('common.segmentsCount', { n: segCount }));
        if (data.engine === 'subtitle' && data.subtitle_lang) {
            parts.push(T('detail.subLang', { lang: data.subtitle_lang }));
        }
        detailMeta.textContent = parts.join(' · ');
        const tm = data.timing || {};
        if (tm.processing_s && !tm.approx) {
            detailMeta.appendChild(document.createTextNode(' · '));
            const sp = document.createElement('span');
            sp.className = 'detail-timing';
            sp.textContent = '⚡ ' + fmtElapsed(tm.processing_s) + (tm.speed_x ? ` · ${tm.speed_x}×` : '');
            sp.title = timingBreakdown(tm);
            detailMeta.appendChild(sp);
        }
        if (data.source_url) {
            detailMeta.appendChild(document.createTextNode(' · '));
            const link = buildSourceLink(data.source_url);
            link.textContent = '🔗 ' + T('library.openSource');
            detailMeta.appendChild(link);
        }

        detailAudioPlayer.pause();
        detailAudioPlayer.currentTime = 0;
        if (data.has_audio) {
            detailAudioPlayer.src = `/api/history/${taskId}/audio`;
            detailPlayerSection.classList.remove('hidden');
        } else {
            detailAudioPlayer.src = '';
            detailPlayerSection.classList.add('hidden');
        }

        detailSegments = data.segments || [];
        detailSegmentsContainer.innerHTML = '';
        detailSyncState.last = null;
        detailSegments.forEach(seg => {
            appendSegment(seg, detailSegmentsContainer, detailAudioPlayer);
        });

        if (data.summary && data.summary.overview) {
            renderSummary(detailSummarySection, detailSummaryOverview,
                detailSummarySections, data.summary);
        } else {
            detailSummarySection.classList.add('hidden');
        }
    } catch {
        showToast(T('toast.couldNotLoadRecord'));
        // 冷启动时 URL 里带着失效/已删除的 taskId 会走到这——保底别留白屏，退回资料库。
        _showMain();
        navigate('tab/library', { replace: true });
    }
}

function closeDetailView() {
    detailView.classList.add('hidden');
    mainView.classList.remove('hidden');
    detailAudioPlayer.pause();
    detailAudioPlayer.src = '';
    detailSegmentsContainer.innerHTML = '';
    detailSummaryOverview.textContent = '';
    detailSummarySections.innerHTML = '';
    detailSummarySection.classList.add('hidden');
    detailSegments = [];
    detailRecord = null;
    detailSyncState.last = null;
    window.scrollTo({ top: 0 });
}

detailBackBtn.addEventListener('click', () => navigate(detailReturnTo));

clearHistoryBtn.addEventListener('click', async () => {
    if (!confirm(T('confirm.clearHistory'))) return;

    try {
        const resp = await fetch('/api/history', { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.error || T('toast.clearFailed'));
            return;
        }
        renderHistory();
        let msg = T('toast.cleared', { n: data.deleted || 0 });
        if (data.skipped) msg += T('toast.clearedSkipped', { n: data.skipped });
        showToast(msg);
    } catch {
        showToast(T('toast.clearFailed'));
    }
});

// ========== Toast ==========
function showToast(message) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}

// ========== Init ==========
renderHistory();
renderIntake();   // 初始化提交按钮状态（0 条 → 禁用）

// ========== 链条：URL → 下载 → 转写 → 分析 → 总合成 ==========
const chainUrl = document.getElementById('chain-url');
const chainAuthor = document.getElementById('chain-author');
const chainMax = document.getElementById('chain-max');
const chainEngine = document.getElementById('chain-engine');
const chainPreferSubs = document.getElementById('chain-prefer-subs');
const chainVerify = document.getElementById('chain-verify');
const chainSelfVerify = document.getElementById('chain-self-verify');
const chainFallbackWhisper = document.getElementById('chain-fallback-whisper');
const chainCritique = document.getElementById('chain-critique');
const chainProvider = document.getElementById('chain-provider');
const chainStartBtn = document.getElementById('chain-start');

// 分析模型的用户可读名（analysis_preset 是内部字段，展示层别裸露）
function brainLabel(preset) {
    const key = 'brain.' + (preset || 'gemini');
    return STRINGS.en[key] ? T(key) : preset;
}

function stageLabel(stage) {
    const key = 'stage.' + stage;
    return STRINGS.en[key] ? T(key) : stage;
}

let chainPollTimer = null;

let chainSubmitting = false;
chainStartBtn.addEventListener('click', async () => {
    if (chainSubmitting) return;               // 防连点重复建链（每条都烧钱）
    const url = (chainUrl.value || '').trim();
    if (!url) { chainUrl.focus(); return; }
    // 花钱确认：分析/合成会按视频数调用付费模型
    {
        const extra = chainVerify.checked ? T('confirm.chainStartVerifyExtra') : '';
        if (!confirm(T('confirm.chainStart', { extra }))) {
            return;
        }
    }
    chainSubmitting = true;
    chainStartBtn.disabled = true;
    try {
        const resp = await fetch('/api/chain', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url,
                author: chainAuthor.value.trim(),
                max_videos: parseInt(chainMax.value, 10) || 0,
                engine: chainEngine.value,
                analyze: true,
                prefer_subs: chainPreferSubs.checked,
                sub_lang: (document.getElementById('chain-sub-lang') || {}).value || 'auto',
                fallback_whisper: chainFallbackWhisper.checked,
                verify: chainVerify.checked,
                self_verify: chainSelfVerify.checked,
                lang: (document.getElementById('chain-lang') || {}).value || 'auto',
                critique_level: chainCritique.value,
                analysis_preset: chainProvider.value,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || T('creators.failedToCreate'));
        chainUrl.value = '';
        loadChains();
    } catch (err) {
        alert(T('alert.failedToStartAnalysis', { message: err.message }));
    } finally {
        chainSubmitting = false;
        chainStartBtn.disabled = false;
    }
});

function chainProgressText(c) {
    const vids = c.videos || [];
    const by = s => vids.filter(v => v.status === s).length;
    const parts = [];

    if (c.download_total) {
        const f = by('download_failed');
        parts.push(T('chainProgress.downloaded', { done: c.download_done || 0, total: c.download_total })
            + (f ? ` (${T('chainProgress.failedCount', { n: f })})` : ''));
    }

    // 转写：显示已完成 + 正在转写 + 失败，让进度"会动"（不只在转完才 +1）
    const submitted = vids.filter(v => v.status !== 'download_failed');
    const done = by('done'), transcribing = by('transcribing'), failed = by('failed');
    if (submitted.length && (done || transcribing || failed || c.stage !== 'downloading')) {
        const extra = [];
        if (transcribing) extra.push(T('chainProgress.inProgress', { n: transcribing }));
        if (failed) extra.push(T('chainProgress.failedCount', { n: failed }));
        parts.push(T('chainProgress.transcribed', { done, total: submitted.length })
            + (extra.length ? ` (${extra.join(', ')})` : ''));
    }

    if (c.analyze && c.analyzed_done != null && submitted.length) {
        parts.push(T('chainProgress.analyzed', { done: c.analyzed_done, total: submitted.length }));
    }
    return parts.join(' · ');
}

// ===== URL 归一化：仅用于卡片归组比较，不改提交逻辑和存储 =====
// 去名单方式：只删已知跟踪参数，其余参数一律保留 ——
// YouTube 的 watch?v= / playlist?list= 是内容标识，误删会把不同内容合成一张卡。
const TRACKING_PARAMS = new Set([
    // Bilibili 分享链接
    'share_source', 'share_medium', 'share_plat', 'share_session_id',
    'share_tag', 'share_from', 'share_times', 'unique_k',
    'vd_source', 'from_spmid', 'spm_id_from', 'spm', 'from_source',
    'buvid', 'trackid', 'plat_id', 'is_story_h5', '-arouter',
    // YouTube 分享链接
    'si', 'feature',
    // 通用
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
]);

function normalizeChainUrl(raw) {
    try {
        const u = new URL(String(raw || '').trim());
        const kept = [...u.searchParams.entries()]
            .filter(([k]) => !TRACKING_PARAMS.has(k.toLowerCase()));
        kept.sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
        const q = kept.map(([k, v]) => `${k}=${v}`).join('&');
        return u.hostname.toLowerCase().replace(/^www\./, '')
            + u.pathname.replace(/\/+$/, '')
            + (q ? '?' + q : '');
    } catch {
        return String(raw || '').trim();
    }
}

// 进行中卡片的细进度条：按 下载/转写/分析 三步的完成数粗估百分比（纯展示）
function chainPercent(c) {
    const vids = c.videos || [];
    const total = c.download_total || vids.length;
    if (!total) return 2;
    const steps = c.analyze === false ? 2 : 3;
    const done = vids.filter(v => v.status === 'done').length;
    const num = (c.download_done || 0) + done
        + (c.analyze === false ? 0 : (c.analyzed_done || 0));
    return Math.max(2, Math.min(99, Math.round(num / (total * steps) * 100)));
}

// 一条 chain → 一张卡。四态：进行中（进度） / 完成（现状不变） / 失败（错误+Retry）
// / 停止或中断（Stopped+Continue，别让旧数据从界面消失）。
function buildChainCard(c) {
    const active = !['done', 'failed', 'cancelled'].includes(c.stage);
    const author = (c.author && c.author !== '该博主') ? c.author : (c.url || 'Creator');
    const vids = c.videos || [];
    const img = c.avatar || (vids.find(v => v.thumbnail) || {}).thumbnail || '';
    const thumb = img
        ? `<div class="creator-thumb" style="background-image:url('${escapeHtml(img).replace(/[()'"\\]/g, '')}')"></div>`
        : `<div class="creator-thumb creator-noimg">▷</div>`;
    const name = `<div class="creator-name">${escapeHtml(String(author).slice(0, 60))}</div>`;

    let body;
    if (active) {
        const prog = chainProgressText(c) || stageLabel(c.stage);
        body = `<div class="creator-meta">${escapeHtml(prog)}</div>
            <div class="creator-progressbar"><i style="width:${chainPercent(c)}%"></i></div>`
            + (c.current ? `<div class="creator-current">${escapeHtml(c.current.slice(0, 60))}</div>` : '');
    } else if (c.stage === 'done' && c.final_doc) {
        const nEp = vids.filter(v => v.status === 'done').length || vids.length;
        body = `<div class="creator-meta">${nEp} episode${nEp === 1 ? '' : 's'} · ${brainLabel(c.analysis_preset)}</div>`;
    } else if (c.stage === 'failed') {
        body = `<div class="creator-meta creator-error">⚠ ${escapeHtml(String(c.error || T('stage.failed')).slice(0, 90))}</div>
            <button class="btn-secondary btn-small creator-retry"
                onclick="continueChain('${c.id}', event)">${T('creators.retry')}</button>`;
    } else {
        // cancelled，或 done 但没产出画像（中断/未合成）
        const prog = chainProgressText(c);
        body = `<div class="creator-meta">${T('stage.cancelled')}${prog ? ' · ' + escapeHtml(prog) : ''}</div>
            <button class="btn-secondary btn-small creator-retry"
                onclick="continueChain('${c.id}', event)">${T('creators.continue')}</button>`;
    }
    return `<div class="creator-card ${active ? 'creator-running' : ''}"
        onclick="navigate('chain/${c.id}')" title="${escapeHtml(author)}">
        ${thumb}<div class="creator-body">${name}${body}</div></div>`;
}

function renderChains(chains) {
    const grid = document.getElementById('creators-grid');
    const empty = document.getElementById('creators-empty');
    if (!grid) return;
    // 同一 URL（去跟踪参数后）只留最新一条：卡片代表"这个博主"，显示最新一次分析；
    // 旧 run 的文档仍在 Library → Analyses。/api/chains 已按 created_at 倒序。
    const seen = new Set();
    const latest = [];
    for (const c of chains) {
        const key = normalizeChainUrl(c.url);
        if (seen.has(key)) continue;
        seen.add(key);
        latest.push(c);
    }
    if (empty) empty.classList.toggle('hidden', latest.length > 0);
    grid.innerHTML = latest.map(buildChainCard).join('');
}

// ========== 链条详情：视频封面网格 + 每个视频状态 ==========
const chainDetailView = document.getElementById('chain-detail-view');
const chainDetailBack = document.getElementById('chain-detail-back');
const chainDetailTitle = document.getElementById('chain-detail-title');
const chainDetailMeta = document.getElementById('chain-detail-meta');
const chainDetailGrid = document.getElementById('chain-detail-grid');

const VIDEO_STATUS_CLS = {
    downloading: 'vs-active', transcribing: 'vs-active', pending: 'vs-active',
    done: 'vs-done', failed: 'vs-fail', download_failed: 'vs-fail',
};
function videoStatusOf(status) {
    const key = 'videoStatus.' + status;
    return { label: STRINGS.en[key] ? T(key) : status, cls: VIDEO_STATUS_CLS[status] || '' };
}

let chainDetailId = null;
let chainDetailTimer = null;

async function openChainDetail(id) {
    chainDetailId = id;
    mainView.classList.add('hidden');
    chainDetailView.classList.remove('hidden');
    // 每次打开先收起分集网格：先看设置/模型/进度，要看每期再展开
    chainDetailGrid.classList.add('hidden');
    const tg = document.getElementById('chain-episodes-toggle');
    if (tg) tg.classList.remove('open');
    window.scrollTo({ top: 0 });
    renderMergeBar();   // 购物车里有跨博主选的期 → 进来就显操作条
    await refreshChainDetail();
}

// Episodes 折叠开关
const episodesToggle = document.getElementById('chain-episodes-toggle');
if (episodesToggle) episodesToggle.addEventListener('click', () => {
    const open = chainDetailGrid.classList.toggle('hidden');
    episodesToggle.classList.toggle('open', !open);
});

async function refreshChainDetail() {
    if (!chainDetailId) return;
    let c;
    try {
        const resp = await fetch(`/api/chain/${chainDetailId}`);
        if (!resp.ok) throw new Error('not found');
        c = await resp.json();
    } catch {
        chainDetailGrid.innerHTML = `<p class="history-empty">${T('common.couldNotLoad')}</p>`;
        return;
    }
    chainDetailTitle.textContent = T('nav.creators');   // 顶栏只当面包屑，名字在下面的封面里
    chainDetailMeta.textContent = '';          // 卡片已含状态，别重复这行灰字

    const vids = c.videos || [];
    const chainTerminal = ['done', 'failed', 'cancelled'].includes(c.stage);

    // ===== Info 面板：设置 / 模型（含降级留痕）/ 操作 =====
    const onoff = b => b ? T('common.on') : T('common.off');
    const fell = vids.filter(v => v.status === 'done' && v.engine_used
        && v.engine_used !== c.engine).length;
    const fellNote = fell
        ? `<div class="cd-alert">⚠ ${T('chainDetail.fellBack', { n: fell })}</div>` : '';
    const err = c.error
        ? `<div class="cd-alert">⚠ ${String(c.error).replace(/</g, '&lt;').slice(0, 180)}</div>` : '';
    const actions = chainTerminal
        ? `<button class="btn-primary ci-btn" onclick="continueChain('${c.id}')">${T('creators.continue')}</button>
           <button class="btn-secondary ci-btn" onclick="reanalyzeChain('${c.id}')">${T('creators.reanalyze')}</button>
           <button class="btn-secondary ci-btn" onclick="closeChainDetail();navigate('tab/library/docs')">${T('creators.episodeDocs')}</button>
           <button class="btn-secondary ci-btn btn-danger" onclick="deleteChain('${c.id}', true)">${T('common.delete')}</button>
           <span class="ci-hint">${T('chainDetail.actionsHint')}</span>`
        : `<button class="btn-secondary ci-btn" onclick="stopChain('${c.id}')">${T('creators.stop')}</button>`;
    // ===== 布局原则：主角是「这个博主 + 读他的解读」；运维细节全部折叠 =====
    const author = (c.author && c.author !== '该博主') ? c.author : '';
    const doneN = vids.filter(v => v.status === 'done').length;
    // 主 CTA：读画像 / 合并原文（核心内容，做大）
    let ctas = '';
    if (c.final_doc) ctas += `<button class="btn-primary cd-cta"
        onclick="navigate('chain/${c.id}/doc/${encodeURIComponent(c.final_doc)}')">📖 ${T('creators.report')}</button>`;
    if (c.raw_doc) ctas += `<button class="btn-secondary cd-cta"
        onclick="navigate('chain/${c.id}/doc/${encodeURIComponent(c.raw_doc)}')">📜 ${T('creators.fullTranscript')}</button>`;
    // 镜头：核心动作，大 chip
    const LENSES = ['roast', 'craft', 'fun', 'quotes', 'worldview'].map(k => [k, T('lens.' + k)]);
    const lensBlock = (chainTerminal && c.analyze !== false)
        ? `<div class="cd-lens-title">${T('chainDetail.lensTitle')} <span class="ci-hint">${T('chainDetail.lensHint')}</span></div>
           <div class="cd-lens-row">`
          + LENSES.map(([k, label]) =>
              `<button class="lens-btn" onclick="runLens('${c.id}','${k}')">${label}</button>`).join('')
          + `</div>`
        : '';
    // 运维细节 + 次要操作：折叠（活跃时展开显进度）
    const opsOpen = chainTerminal ? '' : ' open';
    // 真头像（取不到/加载失败 → 名字首字的珊瑚章）+ 真数据条
    const ch = (author || c.url || '?').trim().slice(0, 1) || '?';
    const avatarHtml = `<div class="cd-avatar">${ch}${c.avatar
        ? `<img class="cd-avatar-img" src="${escapeHtml(c.avatar || '')}" alt="" onerror="this.remove()">`
        : ''}</div>`;
    const totalViews = vids.reduce((s, v) => s + (v.view_count || 0), 0);
    const stats = [`<div class="cd-stat"><div class="n">${doneN}</div><div class="l">${T('chainDetail.episodesRead')}</div></div>`];
    if (c.followers) stats.push(`<div class="cd-stat"><div class="n">${fmtCount(c.followers)}</div><div class="l">${T('chainDetail.followers')}</div></div>`);
    if (totalViews) stats.push(`<div class="cd-stat"><div class="n">${fmtCount(totalViews)}</div><div class="l">${T('chainDetail.totalPlays')}</div></div>`);
    stats.push(`<div class="cd-stat"><div class="n">${brainLabel(c.analysis_preset)}</div><div class="l">${T('chainDetail.analysisModel')}</div></div>`);
    document.getElementById('chain-detail-info').innerHTML = `
        <div class="cd-cover">
            <div class="cd-cover-top">
                ${avatarHtml}
                <div class="cd-id">
                    <div class="cd-eyebrow">${T('chainDetail.eyebrow', { n: doneN })}</div>
                    <div class="cd-name">${escapeHtml((author || c.url || T('chainDetail.creatorFallback')).slice(0, 60))}</div>
                    <div class="cd-sub">${stageLabel(c.stage)}${c.finished_at ? ' · ' + c.finished_at : ''}</div>
                </div>
            </div>
            <div class="cd-stats">${stats.join('')}</div>
            <div class="cd-body">
                <div class="cd-ctas">${ctas}</div>
                ${lensBlock}
            </div>
        </div>
        ${fellNote}
        <details class="chain-ops"${opsOpen}>
            <summary>⚙ ${T('chainDetail.runDetails')}${err ? ' · <span class="ops-flag">' + T('chainDetail.hasErrors') + '</span>' : ''}</summary>
            <div class="chain-info">
                ${err}
                <div class="ci-row"><span class="ci-k">${T('chainDetail.source')}</span>
                    <span class="ci-v"><a href="${safeUrl(c.url)}" target="_blank" rel="noopener">${escapeHtml((c.url || '').slice(0, 80))}</a></span></div>
                <div class="ci-row"><span class="ci-k">${T('settings.title')}</span>
                    <span class="ci-v">engine <b>${c.engine || '-'}</b> · analyze <b>${onoff(c.analyze)}</b>
                    · model <b>${brainLabel(c.analysis_preset)}</b> · level <b>${c.critique_level || 'analytical'}</b>
                    · subs-first <b>${onoff(c.prefer_subs)}</b> · web-verify <b>${onoff(c.verify)}</b>
                    · self-verify <b>${onoff(c.self_verify)}</b>
                    · whisper-fallback <b>${onoff(c.fallback_whisper)}</b></span></div>
                <div class="ci-row"><span class="ci-k">${T('chainDetail.progress')}</span>
                    <span class="ci-v">${chainProgressText(c)}</span></div>
                <div class="ci-actions">${actions}</div>
            </div>
        </details>`;

    document.getElementById('chain-episodes-count').textContent = `(${vids.length})`;

    chainDetailGrid.innerHTML = vids.map((v, idx) => {
        const st = videoStatusOf(v.status);
        const clickable = v.status === 'done' && v.task_id;
        const thumb = v.thumbnail
            ? `<img class="vg-thumb" src="${v.thumbnail}" loading="lazy" alt=""
                 onerror="this.style.display='none'">`
            : '<div class="vg-thumb vg-noimg">▷</div>';
        // 转写中且有进度 → 封面上盖珊瑚半透明板 + 大号百分比
        const pct = (v.status === 'transcribing' && typeof v.progress === 'number')
            ? v.progress : null;
        const overlay = pct != null
            ? `<div class="vg-prog" style="--p:${pct}%"><span>${pct}%</span></div>` : '';
        const onclick = clickable
            ? ` onclick="navigate('detail/${v.task_id}')" title="${T('chainDetail.viewTranscript')}"` : '';
        // 降级留痕：这期实际用的引擎和链条引擎不同（如 gemini 链落了 whisper）
        const engBadge = (v.engine_used && v.engine_used !== c.engine)
            ? `<span class="vg-eng" title="${T('chainDetail.engineFellBack', { engine: v.engine_used })}">${v.engine_used}</span>` : '';
        // 只有完成的分集能加入合并购物车；勾选框吞掉点击，不触发打开转写
        const pick = clickable
            ? `<label class="vg-pick" onclick="event.stopPropagation()" title="${T('chainDetail.addToMerge')}">
                 <input type="checkbox" ${mergeCart.has(v.task_id) ? 'checked' : ''}
                   onchange="toggleMergePick('${v.task_id}', this.checked)">
               </label>` : '';
        return `<div class="vg-card ${clickable ? 'vg-clickable' : ''} ${mergeCart.has(v.task_id) ? 'vg-picked' : ''}"${onclick}>
            <div class="vg-thumb-wrap">${thumb}${overlay}${pick}</div>
            <div class="vg-badge ${st.cls}">${st.label}${engBadge}</div>
            <div class="vg-title" title="${escapeHtml(v.title || '')}">${escapeHtml(v.title || '')}</div>
        </div>`;
    }).join('') || `<p class="history-empty">${T('chainDetail.resolving')}</p>`;

    clearTimeout(chainDetailTimer);
    // 链条在跑、或有单个视频在重转中 → 继续轮询刷新
    const anyBusy = vids.some(v => ['downloading', 'transcribing'].includes(v.status));
    if ((!chainTerminal || anyBusy) && !document.hidden) {
        chainDetailTimer = setTimeout(refreshChainDetail, 4000);
    }
}

function closeChainDetail() {
    clearTimeout(chainDetailTimer);
    chainDetailId = null;
    chainDetailView.classList.add('hidden');
    mainView.classList.remove('hidden');
    window.scrollTo({ top: 0 });
}

// ========== 合并转写「购物车」：跨博主挑期，按 task_id 攒着 ==========
// 选择单位是单期转写，与博主解耦；切博主不清空，最后一起合并成一份纯文本。
const mergeCart = new Set();

function toggleMergePick(taskId, checked) {
    if (checked) mergeCart.add(taskId); else mergeCart.delete(taskId);
    // 同步卡片高亮（重绘时也会带上 vg-picked）
    const box = document.querySelector(`.vg-pick input[onchange*="${taskId}"]`);
    if (box) box.closest('.vg-card')?.classList.toggle('vg-picked', checked);
    renderMergeBar();
}

function renderMergeBar() {
    let bar = document.getElementById('merge-bar');
    if (!mergeCart.size) { if (bar) bar.remove(); return; }
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'merge-bar';
        bar.className = 'merge-bar';
        document.body.appendChild(bar);
    }
    const n = mergeCart.size;
    bar.innerHTML = `
        <span class="merge-bar-count">${T('chainDetail.selectedCount', { n })}</span>
        <button class="btn-secondary btn-small" onclick="clearMergeCart()">${T('common.clear')}</button>
        <button class="btn-primary btn-small" onclick="runMergeTranscripts()">📄 ${T('chainDetail.merge')}</button>`;
}

function clearMergeCart() {
    mergeCart.clear();
    document.querySelectorAll('.vg-card.vg-picked').forEach(c => {
        c.classList.remove('vg-picked');
        const box = c.querySelector('.vg-pick input');
        if (box) box.checked = false;
    });
    renderMergeBar();
}

async function runMergeTranscripts() {
    if (!mergeCart.size) return;
    const btn = document.querySelector('#merge-bar .btn-primary');
    if (btn) { btn.disabled = true; btn.textContent = T('chainDetail.merging'); }
    try {
        const r = await (await fetch('/api/transcripts/merge', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_ids: [...mergeCart] }),
        })).json();
        if (r.error) { alert(r.error); return; }
        // 复用现成的内存文档视图（和小红书报告同一条路）——这个视图不接路由：
        // 内容只存在于这一次 POST 响应里，没有可重新拉取的地方；刷新页面丢失是
        // 已知的、可接受的局限（跟合并购物车本身一样只活在内存里）。
        currentDoc = { chainId: null, name: r.filename || '合并转写.md', raw: r.markdown };
        docTitle.textContent = T('chainDetail.mergedTitle', { count: r.count });
        docContent.innerHTML = renderMarkdown(r.markdown);
        docReturnTo = (chainDetailView && !chainDetailView.classList.contains('hidden'))
            ? 'chainDetail' : 'main';
        mainView.classList.add('hidden');
        if (chainDetailView) chainDetailView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
        if (r.missing) showToast(T('chainDetail.mergeSkipped', { n: r.missing }));
    } catch {
        alert(T('toast.mergeFailed'));
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📄 ' + T('chainDetail.merge'); }
    }
}

if (chainDetailBack) chainDetailBack.addEventListener('click', () => navigate('tab/creators'));

async function loadChains() {
    try {
        const resp = await fetch('/api/chains');
        const chains = await resp.json();
        renderChains(chains);
        updateGlobalIndicator('chains', chains
            .filter(c => !['done', 'failed', 'cancelled'].includes(c.stage))
            .map(c => ({
                label: (c.author && c.author !== '该博主') ? c.author : c.url,
                progress: chainProgressText(c) || stageLabel(c.stage),
                tab: 'creators',
                chainId: c.id,
            })));
        const anyActive = chains.some(c => !['done', 'failed', 'cancelled'].includes(c.stage));
        clearTimeout(chainPollTimer);
        // 只在有活跃链条、且标签页在前台时才继续轮询：
        // 后台标签不空转；也不再每 4 秒全量重绘历史（几百条卡片重绘会烧满渲染进程）。
        if (anyActive && !document.hidden) {
            chainPollTimer = setTimeout(loadChains, 4000);
        }
    } catch (e) { /* 服务重启瞬间的抖动，忽略 */ }
}

// 标签页切到后台：停掉所有轮询，别在后台烧电；切回前台再恢复。
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearTimeout(chainPollTimer);
        clearTimeout(chainDetailTimer);
        clearTimeout(xhsTimer);
        clearTimeout(xhsAnTimer);
    } else {
        loadChains();
        if (chainDetailId) refreshChainDetail();
        if (enrichWasPolling) { enrichWasPolling = false; pollEnrichStatus(); }
        // XHS：仅在上次已知有活跃任务时恢复（各自查一次，idle 就地停，不空转）
        if (activeTasks['xhs-scrape'].length) pollXhs();
        if (activeTasks['xhs-analyze'].length) pollXhsAnalyze();
    }
});

// Continue：补全一切缺失——已完成的复用，没下的下，转写失败的（音频在就直接重转、
// 云引擎失败自动落 Whisper），最后补分析 + 合成。用上面表单的引擎/分析大脑设置。
async function continueChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    if (!confirm(T('confirm.chainContinueFull'))) return;
    try {
        const r = await (await fetch(`/api/chain/${chainId}/retry`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ engine: chainEngine.value, analysis_preset: chainProvider.value }),
        })).json();
        if (!r.ok) { alert(r.error || T('alert.couldNotContinue')); }
    } catch { alert(T('alert.couldNotContinue')); }
    loadChains();
}

async function stopChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    if (!confirm(T('confirm.chainStopFull'))) return;
    try { await fetch(`/api/chain/${chainId}/stop`, { method: 'POST' }); } catch { /* ignore */ }
    loadChains();
}

async function deleteChain(chainId, fromDetail) {
    if (!confirm(T('confirm.chainDeleteFull'))) return;
    await fetch(`/api/chain/${chainId}`, { method: 'DELETE' });
    if (fromDetail) closeChainDetail();
    loadChains();
}

// Re-analyze：只对已有转写重跑分析 + 合成（不碰转写）。用上面表单的分析大脑 / 档位 / 核实。
async function reanalyzeChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    const verify = chainVerify.checked;
    const selfVerify = chainSelfVerify.checked;
    if (!confirm(T('confirm.chainReanalyzeFull', {
        verifyExtra: verify ? T('confirm.chainReanalyzeVerifyExtra') : '',
        selfVerifyExtra: selfVerify ? T('confirm.chainReanalyzeSelfVerifyExtra') : '',
    }))) return;
    try {
        const r = await (await fetch(`/api/chain/${chainId}/reanalyze`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ verify, self_verify: selfVerify,
                                   lang: (document.getElementById('chain-lang') || {}).value || 'auto',
                                   critique_level: chainCritique.value,
                                   analysis_preset: chainProvider.value }),
        })).json();
        if (!r.ok) { alert(r.error || T('alert.couldNotStartReanalysis')); }
    } catch { alert(T('alert.couldNotStartReanalysis')); }
    loadChains();   // stage 变 analyzing → 轮询自动接管显示进度
}

loadChains();

// ========== Tab 切换（转写 / 链条 / 资料库） ==========
const navTabs = document.querySelectorAll('.nav-tab');
const tabPanels = {
    transcribe: document.getElementById('tab-transcribe'),
    xhs: document.getElementById('tab-xhs'),
    creators: document.getElementById('tab-creators'),
    library: document.getElementById('tab-library'),
};

function switchTab(name) {
    navTabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    Object.entries(tabPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'library') { renderHistory(); }
    if (name === 'creators') { loadChains(); }
    if (name === 'xhs') { pollXhs(); pollXhsAnalyze(); }
}

// ========== 小红书采集 ==========
let xhsTimer = null;
async function pollXhs() {
    const box = document.getElementById('xhs-progress');
    if (!box) return;
    let s;
    try { s = await (await fetch('/api/xhs/status')).json(); }
    catch { return; }
    clearTimeout(xhsTimer);
    updateGlobalIndicator('xhs-scrape', s.running
        ? [{ label: s.kw || T('xhs.scrapingNotes'), progress: T('xhs.scrapedCount', { n: s.scraped }), tab: 'xhs' }]
        : []);
    if (!s.running && !s.log?.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    const dot = s.running ? `<span class="xhs-live">● ${T('xhs.scraping')}</span>` : `<span class="xhs-done">✓ ${T('stage.cancelled')}</span>`;
    const stopBtn = s.running ? `<button class="lens-btn" onclick="stopXhs()">■ ${T('creators.stop')}</button>` : '';
    box.innerHTML = `
        <div class="xhs-head">${dot}
            <span>${T('xhs.scrapedThisRun', { n: s.scraped, total: s.total })}${s.kw ? ' · ' + escapeHtml(s.kw) : ''}</span>
            ${stopBtn}</div>
        <pre class="xhs-log">${(s.log || []).map(l => escapeHtml(l)).join('\n')}</pre>`;
    const startBtn = document.getElementById('xhs-start');
    if (startBtn) startBtn.disabled = s.running;
    if (s.running && !document.hidden) xhsTimer = setTimeout(pollXhs, 3000);
}

async function stopXhs() {
    if (!confirm(T('confirm.xhsStop'))) return;
    try {
        const r = await (await fetch('/api/xhs/stop', { method: 'POST' })).json();
        if (!r.ok) alert(r.error || T('alert.couldNotStop'));
    } catch { alert(T('alert.couldNotStop')); }
    setTimeout(pollXhs, 500);
}

// —— 分析：逐篇读图+评论 → 聚合报告 ——
let xhsAnTimer = null;
async function pollXhsAnalyze() {
    const box = document.getElementById('xhs-analyze-progress');
    const btn = document.getElementById('xhs-analyze-btn');
    if (!box) return;
    let s;
    try { s = await (await fetch('/api/xhs/analyze_status')).json(); }
    catch { return; }
    clearTimeout(xhsAnTimer);
    updateGlobalIndicator('xhs-analyze', s.running
        ? [{ label: T('xhs.notesAnalysis'), progress: T('xhs.notesProgress', { done: s.done, total: s.total }), tab: 'xhs' }]
        : []);
    if (s.running) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-live">● ${T('xhs.analyzing')}</span> ${T('xhs.readNotes', { done: s.done, total: s.total })}`;
        if (btn) btn.disabled = true;
        if (!document.hidden) xhsAnTimer = setTimeout(pollXhsAnalyze, 2000);
        return;
    }
    if (btn) btn.disabled = false;
    if (s.error) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-done">${T('xhs.analysisFailed', { message: String(s.error).replace(/</g, '&lt;') })}</span>`;
    } else if (s.has_report) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-done">✓ ${T('xhs.reportReady')}</span>
            <button class="lens-btn" onclick="navigate('xhs-report')">📖 ${T('xhs.readReport')}</button>`;
    } else {
        box.classList.add('hidden');
    }
}
async function openXhsReport() {
    try {
        const r = await (await fetch('/api/xhs/report')).json();
        if (r.markdown) showXhsReport(r.markdown); else alert(r.error || T('alert.noReportYet'));
    } catch { alert(T('alert.couldNotLoadReport')); }
}
function showXhsReport(md) {
    currentDoc = { chainId: null, name: '小红书报告.md', raw: md };
    docTitle.textContent = T('xhs.reportTitle');
    docContent.innerHTML = renderMarkdown(md);
    docReturnTo = 'main';
    mainView.classList.add('hidden');
    if (chainDetailView) chainDetailView.classList.add('hidden');
    docView.classList.remove('hidden');
    window.scrollTo({ top: 0 });
}
const xhsAnalyzeBtn = document.getElementById('xhs-analyze-btn');
if (xhsAnalyzeBtn) xhsAnalyzeBtn.addEventListener('click', async () => {
    const keywords = (document.getElementById('xhs-keywords').value || '').trim();
    const scopeMsg = keywords ? T('confirm.xhsAnalyzeKeywords') : T('confirm.xhsAnalyzeAll');
    if (!confirm(scopeMsg + T('confirm.xhsAnalyzeSuffix'))) return;
    xhsAnalyzeBtn.disabled = true;
    try {
        const r = await (await fetch('/api/xhs/analyze', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keywords,
                lang: (document.getElementById('xhs-lang') || {}).value || 'auto' }),
        })).json();
        if (!r.ok) { alert(r.error || T('alert.failedToStart')); xhsAnalyzeBtn.disabled = false; return; }
        pollXhsAnalyze();
    } catch { alert(T('alert.failedToStart')); xhsAnalyzeBtn.disabled = false; }
});

const xhsStartBtn = document.getElementById('xhs-start');
if (xhsStartBtn) xhsStartBtn.addEventListener('click', async () => {
    const keywords = document.getElementById('xhs-keywords').value.trim();
    if (!keywords) { document.getElementById('xhs-keywords').focus(); return; }
    if (!confirm(T('confirm.xhsStart'))) return;
    xhsStartBtn.disabled = true;
    try {
        const r = await (await fetch('/api/xhs/scrape', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                keywords,
                max_notes: parseInt(document.getElementById('xhs-max-notes').value, 10) || 20,
                max_comments: parseInt(document.getElementById('xhs-max-comments').value, 10) || 400,
            }),
        })).json();
        if (!r.ok) { alert(r.error || T('alert.failedToStart')); xhsStartBtn.disabled = false; return; }
        pollXhs();
    } catch { alert(T('alert.failedToStart')); xhsStartBtn.disabled = false; }
});

// 页面加载各查一次：刷新页面时若有爬取/分析仍在跑（服务端进行中），
// 不用点进 XHS tab 也能接上轮询、点亮全局指示器；idle 则就地停，不是常驻轮询。
pollXhs();
pollXhsAnalyze();

// ========== 全局任务指示器（顶栏 ⟳N + popover）==========
// 极简聚合，不新建任何轮询：三处现有刷新（setRowStatus / loadChains / pollXhs*）
// 每次拿到新数据后调 updateGlobalIndicator(source, tasks) 更新这里并重绘。
const activeTasks = { transcribe: [], chains: [], 'xhs-scrape': [], 'xhs-analyze': [] };
const gtBtn = document.getElementById('global-tasks');
const gtCount = document.getElementById('global-tasks-count');
const gtPop = document.getElementById('global-tasks-pop');

function updateGlobalIndicator(source, tasks) {
    activeTasks[source] = tasks || [];
    const total = Object.values(activeTasks).reduce((s, a) => s + a.length, 0);
    if (!total) {
        gtBtn.classList.add('hidden');
        gtPop.classList.add('hidden');
        return;
    }
    gtBtn.classList.remove('hidden');
    gtCount.textContent = total;
    if (!gtPop.classList.contains('hidden')) renderGtPop();   // 打开时跟着刷新
}

function renderGtPop() {
    gtPop.innerHTML = '';
    Object.entries(activeTasks).forEach(([source, tasks]) => {
        tasks.forEach(t => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'gt-row';
            const kind = document.createElement('span');
            kind.className = 'gt-kind';
            kind.textContent = T('gtSource.' + source);
            const name = document.createElement('span');
            name.className = 'gt-name';
            name.textContent = t.label;
            name.title = t.label;
            const prog = document.createElement('span');
            prog.className = 'gt-prog';
            prog.textContent = t.progress || '';
            row.appendChild(kind);
            row.appendChild(name);
            row.appendChild(prog);
            row.addEventListener('click', () => {
                gtPop.classList.add('hidden');
                navigate('tab/' + t.tab);
                if (t.chainId) flashCreatorCard(t.chainId);
            });
            gtPop.appendChild(row);
        });
    });
}

// 从 popover 点进 Creators：滚动到对应卡片并短暂高亮
function flashCreatorCard(chainId) {
    const card = document.querySelector(`.creator-card[onclick*="${chainId}"]`);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('card-flash');
    setTimeout(() => card.classList.remove('card-flash'), 1600);
}

if (gtBtn) gtBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const nowHidden = gtPop.classList.toggle('hidden');
    if (!nowHidden) renderGtPop();
});
document.addEventListener('click', (e) => {
    if (!gtPop.classList.contains('hidden')
        && !gtPop.contains(e.target) && !gtBtn.contains(e.target)) {
        gtPop.classList.add('hidden');
    }
});

// 大数字人性化：中文界面用 万/亿，英文界面用 K/M（这个函数以前写死中文，
// 界面语言能切英文之后不跟着切就是个真 bug，不只是缺翻译）
function fmtCount(n) {
    n = Number(n) || 0;
    if (currentLang === 'zh') {
        if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, '') + '亿';
        if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, '') + '万';
        return String(n);
    }
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(n);
}

navTabs.forEach(btn => {
    btn.addEventListener('click', () => navigate('tab/' + btn.dataset.tab));
});

// ========== 资料库子分区（转写记录 / 分析文档） ==========
const subTabs = document.querySelectorAll('.sub-tab');
const libPanels = {
    transcripts: document.getElementById('lib-transcripts'),
    docs: document.getElementById('lib-docs'),
};

function switchLib(name) {
    subTabs.forEach(b => b.classList.toggle('active', b.dataset.lib === name));
    Object.entries(libPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'docs') { loadDocs(); }
}

subTabs.forEach(btn => {
    btn.addEventListener('click', () => navigate('tab/library/' + btn.dataset.lib));
});

// ========== 分析文档浏览 ==========
const docsList = document.getElementById('docs-list');
const docsRefreshBtn = document.getElementById('docs-refresh');
const docView = document.getElementById('doc-view');
const docBackBtn = document.getElementById('doc-back-btn');
const docTitle = document.getElementById('doc-title');
const docContent = document.getElementById('doc-content');
const docDownloadBtn = document.getElementById('doc-download-btn');

let currentDoc = { chainId: null, name: null, raw: '' };

if (docsRefreshBtn) docsRefreshBtn.addEventListener('click', loadDocs);

async function loadDocs() {
    try {
        const chains = await (await fetch('/api/chains')).json();
        // 只列出有产物的链条（分析过的）
        const withDocs = chains.filter(c => c.analyze);
        if (!withDocs.length) {
            docsList.innerHTML = `<p class="history-empty">${T('library.noDocs')}</p>`;
            return;
        }
        const blocks = await Promise.all(withDocs.map(async c => {
            let files = [];
            try { files = await (await fetch(`/api/chain/${c.id}/files`)).json(); }
            catch { files = []; }
            files = (Array.isArray(files) ? files : []).filter(f => f.endsWith('.md'));
            if (!files.length) return '';
            // 排序：Report(总分析) > Full transcript(合并原文) > 其余按名
            const rank = f => f === '总分析.md' ? 0 : f === '合并原文.md' ? 1 : 2;
            files.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
            const title = (c.author && c.author !== '该博主') ? c.author : c.url;
            const stageBadge = c.stage === 'done' ? ''
                : `<span class="doc-stage">（${stageLabel(c.stage)}）</span>`;
            const items = files.map(f => {
                const isTotal = f === '总分析.md';
                const isRaw = f === '合并原文.md';
                const label = isTotal ? T('creators.report') : isRaw ? T('creators.fullTranscript')
                    : f.replace(/^分析_\d+_/, '').replace(/\.md$/, '');
                const cls = isTotal ? 'doc-total' : isRaw ? 'doc-raw' : '';
                return `<button class="doc-item ${cls}"
                    onclick="navigate('chain/${c.id}/doc/${encodeURIComponent(f)}')">${label}</button>`;
            }).join('');
            return `<div class="doc-group">
                <div class="doc-group-title">${escapeHtml(title.slice(0, 70))} ${stageBadge}
                    <span class="doc-count">${T('library.docCount', { n: files.length })}</span></div>
                <div class="doc-items">${items}</div>
            </div>`;
        }));
        const html = blocks.filter(Boolean).join('');
        docsList.innerHTML = html || `<p class="history-empty">${T('library.noDocs')}</p>`;
    } catch (e) {
        docsList.innerHTML = `<p class="history-empty">${T('library.failedToLoad')}</p>`;
    }
}

let docReturnTo = 'main';   // 打开文档前在哪：'main' | 'chainDetail'

async function openDocView(chainId, encName) {
    const name = decodeURIComponent(encName);
    try {
        const resp = await fetch(`/api/chain/${chainId}/file?name=${encodeURIComponent(name)}`);
        if (!resp.ok) throw new Error('not found');
        const raw = await resp.text();
        currentDoc = { chainId, name, raw };
        docTitle.textContent = name.replace(/\.md$/, '');
        docContent.innerHTML = renderMarkdown(raw);
        // 记住来源并把它藏掉（之前只藏 mainView，从详情页打开会两个视图叠在一起）
        docReturnTo = (chainDetailView && !chainDetailView.classList.contains('hidden'))
            ? 'chainDetail' : 'main';
        mainView.classList.add('hidden');
        if (chainDetailView) chainDetailView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
    } catch {
        showToast(T('toast.couldNotLoadDoc'));
        // 冷启动时 URL 里带着失效的链条/文档名会走到这——保底退回资料库，别留白屏。
        _showMain();
        navigate('tab/library', { replace: true });
    }
}

function closeDocView() {
    docView.classList.add('hidden');
    if (docReturnTo === 'chainDetail' && chainDetailView) {
        chainDetailView.classList.remove('hidden');   // 回到博主详情，不是回主页
    } else {
        mainView.classList.remove('hidden');
    }
    docContent.innerHTML = '';
    window.scrollTo({ top: 0 });
}

// 换个角度看博主：拿现成证据卡跑一个镜头 → 后台生成 → 轮询 → 用 openDocView 展示
async function runLens(chainId, lens) {
    const open = () => navigate(`chain/${chainId}/doc/${encodeURIComponent(`镜头_${lens}.md`)}`);
    showToast(T('toast.regenerating'));
    try {
        const r = await (await fetch(`/api/chain/${chainId}/lens`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lens }),
        })).json();
        if (r.error) { showToast(r.error); return; }
        if (r.ready) { open(); return; }
        let n = 40;   // 最多轮询 ~2 分钟（大链条 map-reduce 要点时间）
        const poll = async () => {
            if (n-- <= 0) { showToast(T('chainDetail.stillGenerating')); return; }
            try {
                const g = await (await fetch(`/api/chain/${chainId}/lens/${lens}`)).json();
                if (g.ready) { open(); return; }
                if (g.error) { showToast(T('chainDetail.generationFailed', { message: g.error })); return; }
            } catch { /* 抖动忽略，继续轮 */ }
            setTimeout(poll, 3000);
        };
        setTimeout(poll, 3000);
    } catch { showToast(T('chainDetail.generationFailedGeneric')); }
}

if (docBackBtn) docBackBtn.addEventListener('click', () =>
    navigate(docReturnTo === 'chainDetail' ? 'chain/' + currentDoc.chainId : 'tab/library'));
if (docDownloadBtn) docDownloadBtn.addEventListener('click', () => {
    if (currentDoc.raw) {
        downloadFile(currentDoc.raw, currentDoc.name || 'document.md',
            'text/markdown;charset=utf-8');
        showToast(T('doc.downloaded'));
    }
});

// ========== 轻量 Markdown 渲染（无外部依赖） ==========
function escapeHtml(s) {
    // & < > 以及引号都转义，这样对「文本」和「属性值」两种上下文都安全
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 只放行安全协议的 URL，挡住 javascript:/data: 等注入
function safeUrl(u) {
    const s = String(u || '').trim();
    return /^(https?:|mailto:|\/|#)/i.test(s) ? s : '#';
}

function renderInline(s) {
    // 先转义，再套内联格式
    s = escapeHtml(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) =>
        `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${txt}</a>`);
    return s;
}

function renderMarkdown(md) {
    const lines = md.replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
        let line = lines[i];

        // 代码块
        if (/^```/.test(line)) {
            const buf = [];
            i++;
            while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
            i++;
            out.push('<pre><code>' + escapeHtml(buf.join('\n')) + '</code></pre>');
            continue;
        }
        // 标题
        const h = line.match(/^(#{1,6})\s+(.*)$/);
        if (h) {
            const lvl = h[1].length;
            out.push(`<h${lvl}>${renderInline(h[2])}</h${lvl}>`);
            i++; continue;
        }
        // 分割线
        if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
        // 表格
        if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && /\|/.test(lines[i + 1])) {
            const parseRow = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
            const header = parseRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) {
                rows.push(parseRow(lines[i])); i++;
            }
            let t = '<table><thead><tr>' + header.map(c => `<th>${renderInline(c)}</th>`).join('') + '</tr></thead><tbody>';
            t += rows.map(r => '<tr>' + r.map(c => `<td>${renderInline(c)}</td>`).join('') + '</tr>').join('');
            t += '</tbody></table>';
            out.push(t); continue;
        }
        // 引用
        if (/^>\s?/.test(line)) {
            const buf = [];
            while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i++; }
            out.push('<blockquote>' + renderInline(buf.join(' ')) + '</blockquote>');
            continue;
        }
        // 无序列表
        if (/^\s*[-*]\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
                buf.push('<li>' + renderInline(lines[i].replace(/^\s*[-*]\s+/, '')) + '</li>'); i++;
            }
            out.push('<ul>' + buf.join('') + '</ul>');
            continue;
        }
        // 有序列表
        if (/^\s*\d+\.\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
                buf.push('<li>' + renderInline(lines[i].replace(/^\s*\d+\.\s+/, '')) + '</li>'); i++;
            }
            out.push('<ol>' + buf.join('') + '</ol>');
            continue;
        }
        // 空行
        if (!line.trim()) { i++; continue; }
        // 段落（合并连续非空行）
        const buf = [line];
        i++;
        while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>\s?|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i]) && !/^\s*([-*_])\1{2,}\s*$/.test(lines[i])) {
            buf.push(lines[i]); i++;
        }
        out.push('<p>' + renderInline(buf.join(' ')) + '</p>');
    }
    return out.join('\n');
}

// ========== Reflect：回顾这段时间在听什么 ==========
const reflectBody = document.getElementById('reflect-body');
const reflectRangeSel = document.getElementById('reflect-range');
const reflectRefreshBtn = document.getElementById('reflect-refresh');
let reflectData = null;
let reflectMetric = 'count';      // 'count' | 'hours'
let reflectRange = localStorage.getItem('getaudio_reflect_range') || '1m';
let reflectReq = 0;               // 防止旧请求覆盖新结果
let reflectPollTimer = null;      // 后台在重算时，隔几秒再拉一次

// 主题分段条的色阶：深珊瑚 → 极浅，多出来的段落用最后一个
const REFLECT_SHADES = ['#A9502B', '#B87355', '#C89078', '#D9AC98', '#E9D0C5', '#F2E3DB'];

function fmtBig(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

function reflectHour(h) {
    if (h === null || h === undefined) return '—';
    if (currentLang === 'zh') return `${h}:00`;
    const ap = h < 12 ? 'AM' : 'PM';
    const hh = h % 12 === 0 ? 12 : h % 12;
    return `${hh} ${ap}`;
}

function reflectDate(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    if (currentLang === 'zh') return `${y}年${m}月${d}日`;
    const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return `${M[m - 1]} ${d} ${y}`;
}

// 单调三次插值（Fritsch–Carlson）：平滑但不会在 0 附近下冲出负值
function monotonePath(xs, ys) {
    const n = xs.length;
    if (n < 2) return '';
    const d = [], m = [];
    for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]));
    m[0] = d[0]; m[n - 1] = d[n - 2];
    for (let i = 1; i < n - 1; i++) {
        m[i] = (d[i - 1] * d[i] <= 0) ? 0 : (d[i - 1] + d[i]) / 2;
    }
    for (let i = 0; i < n - 1; i++) {
        if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
        const a = m[i] / d[i], b = m[i + 1] / d[i], h = Math.hypot(a, b);
        if (h > 3) { m[i] = 3 * a / h * d[i]; m[i + 1] = 3 * b / h * d[i]; }
    }
    let path = `M${xs[0].toFixed(1)},${ys[0].toFixed(1)}`;
    for (let i = 0; i < n - 1; i++) {
        const dx = (xs[i + 1] - xs[i]) / 3;
        path += ` C${(xs[i] + dx).toFixed(1)},${(ys[i] + m[i] * dx).toFixed(1)} `
              + `${(xs[i + 1] - dx).toFixed(1)},${(ys[i + 1] - m[i + 1] * dx).toFixed(1)} `
              + `${xs[i + 1].toFixed(1)},${ys[i + 1].toFixed(1)}`;
    }
    return path;
}

// 整齐的 y 轴刻度上限：1,2,3,5,10,20,30,50…
function niceCeil(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const k of [1, 2, 3, 4, 5, 6, 8, 10]) if (k * p >= v) return k * p;
    return 10 * p;
}

function reflectChart(series, prevSeries, metric) {
    const W = 900, H = 270, L = 44, R = 12, T = 18, B = 34;
    const val = p => metric === 'hours' ? p.minutes / 60 : p.count;
    const cur = series.map(val);
    const prev = prevSeries.map(val);
    if (cur.length < 2) return `<div class="reflect-empty">${T('stats.notEnoughData')}</div>`;

    const maxV = niceCeil(Math.max(...cur, ...prev, metric === 'hours' ? 0.5 : 1));
    const X = i => L + i / (cur.length - 1) * (W - L - R);
    const Y = v => H - B - v / maxV * (H - T - B);
    const xs = cur.map((_, i) => X(i));
    const prevXs = prev.map((_, i) => X(i + (cur.length - prev.length)));

    // 两条虚线网格 + 底线
    const ticks = [maxV, maxV * 0.6];
    const fmtTick = v => metric === 'hours' ? (v >= 10 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, '')) : Math.round(v);
    const grid = ticks.map(v =>
        `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
         <text class="ylab" x="${L - 12}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${fmtTick(v)}</text>`).join('');
    const base = `<line class="base" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>
                  <text class="ylab" x="${L - 12}" y="${Y(0) + 4}" text-anchor="end">0</text>`;

    // x 轴 4 个日期
    const idxs = [0, Math.round((cur.length - 1) / 3), Math.round((cur.length - 1) * 2 / 3), cur.length - 1];
    const xl = idxs.map((i, k) => {
        const anchor = k === 0 ? 'start' : (k === 3 ? 'end' : 'middle');
        return `<text class="xlab" x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="${anchor}">${reflectDate(series[i].date)}</text>`;
    }).join('');

    const prevPath = prev.length >= 2 ? `<path class="prev" d="${monotonePath(prevXs, prev.map(Y))}"/>` : '';
    const curPath = `<path class="cur" d="${monotonePath(xs, cur.map(Y))}"/>`;
    return `<svg class="reflect-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${base}${xl}${prevPath}${curPath}</svg>`;
}

function renderReflect() {
    const d = reflectData;
    if (!d) return;
    if (!d.totals.count) {
        reflectBody.innerHTML = `<div class="reflect-empty">${T('reflect.empty')}</div>`;
        return;
    }
    const topics = d.topics || [];
    const bar = topics.map((t, i) =>
        `<i style="flex:${t.percent};background:${REFLECT_SHADES[Math.min(i, REFLECT_SHADES.length - 1)]}"></i>`).join('');
    const list = topics.map((t, i) => `
        <div class="reflect-topic">
            <span class="dot" style="background:${REFLECT_SHADES[Math.min(i, REFLECT_SHADES.length - 1)]}"></span>
            <span class="name">${escapeHtml(t.name)}</span>
            <span class="pct">${t.percent}%</span>
            <div class="desc">${escapeHtml(t.desc || T('reflect.topicFallback', { n: t.count }))}</div>
        </div>`).join('');

    // 后台在重算：有旧的就先显示旧的并提示；一份都没有就显示"正在生成"
    const updating = d.regenerating
        ? `<div class="reflect-updating"><span class="reflect-updating-dot"></span>${T('reflect.updating')}</div>` : '';
    const headline = (!d.generated && d.regenerating) ? T('reflect.generatingTitle') : d.headline;
    const narrative = (!d.generated && d.regenerating) ? T('reflect.generatingBody') : d.narrative;
    reflectBody.innerHTML = `
        ${updating}
        <div class="reflect-headline">${escapeHtml(headline)}</div>
        <p class="reflect-narrative${d.generated ? '' : ' draft'}">${escapeHtml(narrative)}</p>
        <div class="reflect-kpis">
            <div class="reflect-kpi"><b class="text">${escapeHtml(d.most_active_weekday_label || '—')}</b><span>${T('reflect.mostActiveDay')}</span></div>
            <div class="reflect-kpi"><b>${reflectHour(d.peak_hour)}</b><span>${T('reflect.peakHour')}</span></div>
            <div class="reflect-kpi"><b>${d.totals.count}</b><span>${T('reflect.totalTranscripts')}</span></div>
            <div class="reflect-kpi"><b>${d.totals.hours}</b><span>${T('reflect.hoursOfAudio')}</span></div>
        </div>
        <div class="reflect-sec">
            <div class="reflect-sec-head">
                <span class="reflect-sec-label">${T('reflect.yourTime')}</span>
                <span class="reflect-seg">
                    <button type="button" data-metric="count" class="${reflectMetric === 'count' ? 'active' : ''}">${T('reflect.metricCount')}</button>
                    <button type="button" data-metric="hours" class="${reflectMetric === 'hours' ? 'active' : ''}">${T('reflect.metricHours')}</button>
                </span>
            </div>
            <div id="reflect-chart-wrap">${reflectChart(d.series, d.prev_series || [], reflectMetric)}</div>
            <div class="reflect-legend"><span><i></i>${T('reflect.thisPeriod')}</span><span><i class="prev"></i>${T('reflect.prevPeriod')}</span></div>
        </div>
        <div class="reflect-sec" style="margin-top:34px">
            <div class="reflect-sec-head"><span class="reflect-sec-label">${T('reflect.spentOn')}</span></div>
            <div class="reflect-bar">${bar}</div>
            <div class="reflect-topics">${list}</div>
        </div>`;

    reflectBody.querySelectorAll('.reflect-seg button').forEach(b => b.addEventListener('click', () => {
        reflectMetric = b.dataset.metric;
        reflectBody.querySelectorAll('.reflect-seg button').forEach(x => x.classList.toggle('active', x === b));
        document.getElementById('reflect-chart-wrap').innerHTML = reflectChart(d.series, d.prev_series || [], reflectMetric);
    }));
}

async function loadReflect(refresh = false) {
    const my = ++reflectReq;
    clearTimeout(reflectPollTimer);
    reflectRefreshBtn.classList.add('spinning');
    if (!reflectData || refresh) {
        reflectBody.innerHTML = `<div class="reflect-loading">${refresh ? T('reflect.regenerating') : T('reflect.loading')}</div>`;
    }
    try {
        const url = `/api/reflect?range=${reflectRange}&lang=${currentLang}${refresh ? '&refresh=1' : ''}`;
        const d = await (await fetch(url)).json();
        if (my !== reflectReq) return;
        reflectData = d;
        renderReflect();
        // 后台在重算：每 6 秒再拉一次，直到拿到新的（面板关了或切走就停）
        if (d.regenerating) {
            reflectPollTimer = setTimeout(() => {
                if (!settingsOverlay.classList.contains('hidden') && activeSettingsPane === 'reflect') loadReflect();
            }, 6000);
        }
    } catch {
        if (my !== reflectReq) return;
        reflectBody.innerHTML = `<div class="reflect-empty">${T('library.failedToLoad')}</div>`;
    } finally {
        if (my === reflectReq) reflectRefreshBtn.classList.remove('spinning');
    }
}

document.getElementById('reflect-open').addEventListener('click', () => openSettings('reflect'));
reflectRangeSel.addEventListener('change', () => {
    reflectRange = reflectRangeSel.value;
    localStorage.setItem('getaudio_reflect_range', reflectRange);
    reflectData = null;
    loadReflect();
});
reflectRefreshBtn.addEventListener('click', () => loadReflect(true));


// ========== Library：资料库总量（复用 Reflect 的视觉）==========
const libraryBody = document.getElementById('library-body');
let libraryData = null;
let libraryTagLang = 'zh';   // 关注领域标签：'zh' | 'en'

function engineLabel(key) {
    const k = 'engine.' + key;
    const t = T(k);
    if (t !== k) return t;
    return { subtitle: currentLang === 'zh' ? '字幕直取' : 'Subtitles', unknown: currentLang === 'zh' ? '未知' : 'Unknown' }[key] || key;
}

// 累计小时折线：x 为自然日铺满，y 为累计小时
function libraryChart(timeline) {
    if (!timeline || timeline.length < 2) return `<div class="reflect-empty">${T('stats.notEnoughData')}</div>`;
    // 按自然日铺满，没记录的日子累计值不变
    const byDate = Object.fromEntries(timeline.map(p => [p.date, p.minutes]));
    const first = new Date(timeline[0].date + 'T00:00:00');
    const last = new Date(timeline[timeline.length - 1].date + 'T00:00:00');
    const days = [];
    let cum = 0;
    for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
        const k = d.toISOString().slice(0, 10);
        cum += (byDate[k] || 0) / 60;
        days.push({ date: k, v: cum });
    }
    const W = 900, H = 270, L = 48, R = 12, Tp = 18, B = 34;
    const maxV = niceCeil(days[days.length - 1].v);
    const X = i => L + i / (days.length - 1) * (W - L - R);
    const Y = v => H - B - v / maxV * (H - Tp - B);
    const ticks = [maxV, maxV * 0.6];
    const fmtTick = v => v >= 10 ? Math.round(v) : v.toFixed(1).replace(/\.0$/, '');
    const grid = ticks.map(v =>
        `<line class="grid" x1="${L}" x2="${W - R}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
         <text class="ylab" x="${L - 12}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${fmtTick(v)}</text>`).join('');
    const base = `<line class="base" x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}"/>
                  <text class="ylab" x="${L - 12}" y="${Y(0) + 4}" text-anchor="end">0</text>`;
    const idxs = [0, Math.round((days.length - 1) / 3), Math.round((days.length - 1) * 2 / 3), days.length - 1];
    const xl = idxs.map((i, k) => {
        const anchor = k === 0 ? 'start' : (k === 3 ? 'end' : 'middle');
        return `<text class="xlab" x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="${anchor}">${reflectDate(days[i].date)}</text>`;
    }).join('');
    const xs = days.map((_, i) => X(i)), ys = days.map(p => Y(p.v));
    const area = `M${xs[0]},${Y(0)} L` + xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`).join(' ') + ` L${xs[xs.length - 1]},${Y(0)} Z`;
    return `<svg class="reflect-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <defs><linearGradient id="libfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#A9502B" stop-opacity="0.16"/><stop offset="1" stop-color="#A9502B" stop-opacity="0"/>
        </linearGradient></defs>
        ${grid}${base}${xl}
        <path d="${area}" fill="url(#libfill)"/>
        <path class="cur" d="${monotonePath(xs, ys)}"/>
        <circle cx="${xs[xs.length - 1]}" cy="${ys[ys.length - 1]}" r="4" fill="#A9502B"/>
    </svg>`;
}

function renderLibraryTags() {
    const el = document.getElementById('library-tags');
    if (!el) return;
    const tags = libraryData.top_tags || [];
    if (!tags.length) { el.innerHTML = `<div class="reflect-empty">${T('library.noTagsYet')}</div>`; return; }
    const max = tags[0].count || 1;
    el.innerHTML = tags.map(t => {
        const label = libraryTagLang === 'en' ? (t.tag_en || t.tag) : t.tag;
        return `<div class="lib-tag">
            <span class="name" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
            <span class="bar"><i style="width:${Math.max(3, t.count / max * 100)}%"></i></span>
            <span class="num">${t.count}</span>
        </div>`;
    }).join('');
}

// 各引擎速度表：精确计时的记录按「每小时音频几分钟 / 倍速 / 次数」；
// 只有回填近似值（含排队）的引擎单独一行浅色，不和精确数据混
function speedTable(speed) {
    const rows = Object.entries(speed || {})
        .filter(([, v]) => v && (v.n || (v.approx && v.approx.n)))
        .sort((a, b) => (a[1].min_per_hour ?? 1e9) - (b[1].min_per_hour ?? 1e9));
    if (!rows.length) return `<div class="reflect-empty">${T('library.speedEmpty')}</div>`;
    const body = rows.map(([k, v]) => v.n
        ? `<tr><td>${escapeHtml(engineLabel(k))}</td><td>${fmtMinutes(v.min_per_hour)} min</td>
               <td>${v.speed_x ? v.speed_x + '×' : '—'}</td><td>${v.n}</td></tr>`
        : `<tr class="approx"><td>${escapeHtml(engineLabel(k))}</td><td>≈ ${fmtMinutes(v.approx.min_per_hour)} min</td>
               <td>—</td><td>${v.approx.n}<i>${T('library.speedApprox')}</i></td></tr>`).join('');
    return `<table class="speed-table"><thead><tr><th></th><th>${T('library.speedPerHour')}</th>
        <th>${T('library.speedX')}</th><th>${T('library.speedRuns')}</th></tr></thead><tbody>${body}</tbody></table>`;
}

function renderLibrary() {
    const s = libraryData;
    if (!s || !s.totals.transcripts) {
        libraryBody.innerHTML = `<div class="reflect-empty">${T('library.statsEmpty')}</div>`;
        return;
    }
    const engines = Object.entries(s.engines || {}).sort((a, b) => b[1] - a[1]);
    const engTotal = engines.reduce((a, [, n]) => a + n, 0) || 1;
    const bar = engines.map(([, n], i) =>
        `<i style="flex:${n};background:${REFLECT_SHADES[Math.min(i, REFLECT_SHADES.length - 1)]}"></i>`).join('');
    const list = engines.map(([k, n], i) => `
        <div class="reflect-topic">
            <span class="dot" style="background:${REFLECT_SHADES[Math.min(i, REFLECT_SHADES.length - 1)]}"></span>
            <span class="name">${escapeHtml(engineLabel(k))}</span>
            <span class="pct">${Math.round(n / engTotal * 100)}%</span>
            <div class="desc">${T('library.engineCount', { n })}</div>
        </div>`).join('');

    libraryBody.innerHTML = `
        <div class="reflect-kpis" style="padding-top:8px">
            <div class="reflect-kpi"><b>${s.totals.hours}</b><span>${T('stats.hoursTranscribed')}</span></div>
            <div class="reflect-kpi"><b>${fmtBig(s.totals.chars)}</b><span>${T('stats.characters')}</span></div>
            <div class="reflect-kpi"><b>${s.totals.transcripts}</b><span>${T('stats.transcripts')}</span></div>
            <div class="reflect-kpi"><b>${fmtBig(s.totals.segments || 0)}</b><span>${T('library.segments')}</span></div>
        </div>
        <div class="reflect-sec">
            <div class="reflect-sec-head"><span class="reflect-sec-label">${T('stats.cumulativeHours')}</span></div>
            ${libraryChart(s.timeline)}
        </div>
        <div class="reflect-sec" style="margin-top:34px">
            <div class="reflect-sec-head">
                <span class="reflect-sec-label">${T('stats.fieldsYouFollow')}</span>
                <button id="library-tag-lang" class="stats-lang" type="button" title="${T('stats.toggleTagLang')}">${libraryTagLang === 'zh' ? 'EN' : '中'}</button>
            </div>
            <div id="library-tags" class="lib-tags"></div>
        </div>
        <div class="reflect-sec" style="margin-top:34px">
            <div class="reflect-sec-head"><span class="reflect-sec-label">${T('library.byEngine')}</span></div>
            <div class="reflect-bar">${bar}</div>
            <div class="reflect-topics">${list}</div>
        </div>
        <div class="reflect-sec" style="margin-top:34px">
            <div class="reflect-sec-head"><span class="reflect-sec-label">${T('library.speedByEngine')}</span></div>
            ${speedTable(s.speed)}
        </div>`;
    renderLibraryTags();
    document.getElementById('library-tag-lang').addEventListener('click', (e) => {
        libraryTagLang = libraryTagLang === 'zh' ? 'en' : 'zh';
        e.currentTarget.textContent = libraryTagLang === 'zh' ? 'EN' : '中';
        renderLibraryTags();
    });
}

async function loadLibrary(force = false) {
    if (libraryData && !force) { renderLibrary(); return; }
    libraryBody.innerHTML = `<div class="reflect-loading">${T('reflect.loading')}</div>`;
    try {
        libraryData = await (await fetch('/api/stats')).json();
        renderLibrary();
    } catch {
        libraryBody.innerHTML = `<div class="reflect-empty">${T('library.failedToLoad')}</div>`;
    }
}


// ===== Settings modal =====
const settingsOverlay = document.getElementById('settings-overlay');
const setGeminiKey = document.getElementById('set-gemini-key');
const setGeminiBase = document.getElementById('set-gemini-base');
const setDashKey = document.getElementById('set-dashscope-key');
const setOpenrouterKey = document.getElementById('set-openrouter-key');
const settingsMsg = document.getElementById('settings-msg');

let activeSettingsPane = 'reflect';

async function openSettings(pane) {
    if (pane) showSettingsPane(pane);
    settingsOverlay.classList.remove('hidden');
    // 拉当前状态：填回 base URL、用占位符提示 key 是否已存在
    try {
        const s = await (await fetch('/api/settings')).json();
        setGeminiBase.value = s.gemini_base_url || '';
        setGeminiKey.value = '';
        setDashKey.value = '';
        setOpenrouterKey.value = '';
        setGeminiKey.placeholder = s.gemini.set
            ? T('settings.savedKeyHint', { hint: s.gemini.hint }) : T('settings.pasteKey');
        setDashKey.placeholder = s.dashscope.set
            ? T('settings.savedKeyHint', { hint: s.dashscope.hint }) : T('settings.pasteKey');
        setOpenrouterKey.placeholder = (s.openrouter && s.openrouter.set)
            ? T('settings.savedKeyHint', { hint: s.openrouter.hint }) : T('settings.pasteKey');
        // Models（空 = 默认）
        document.getElementById('set-whisper-model').value = s.whisper_model || '';
        document.getElementById('set-gemini-transcribe').value = s.gemini_transcribe_model || '';
        document.getElementById('set-gemini-analysis').value = s.gemini_analysis_model || '';
        document.getElementById('set-gemini-extract').value = s.gemini_extract_model || '';
        document.getElementById('set-keep-audio').checked = s.keep_audio === '1';
    } catch { /* 打开即可，拉取失败不阻塞 */ }
    document.getElementById('test-gemini-res').textContent = '';
    document.getElementById('test-dashscope-res').textContent = '';
    document.getElementById('test-openrouter-res').textContent = '';
    settingsMsg.textContent = '';
}
function closeSettings() { settingsOverlay.classList.add('hidden'); }

// 左侧导航切换面板
function showSettingsPane(name) {
    activeSettingsPane = name;
    document.querySelectorAll('.settings-nav-item').forEach(b =>
        b.classList.toggle('active', b.dataset.pane === name));
    document.querySelectorAll('.settings-pane').forEach(p =>
        p.classList.toggle('active', p.dataset.pane === name));
    // 数据栏目没有"保存"，藏掉页脚
    document.getElementById('settings-foot').classList.toggle('hidden', name === 'reflect' || name === 'library');
    document.querySelector('.settings-panes').scrollTop = 0;
    if (name === 'storage') loadStorage();
    if (name === 'reflect') { reflectRangeSel.value = reflectRange; loadReflect(); }
    if (name === 'library') loadLibrary();
}

// 左侧导航搜索：按栏目名过滤
const settingsSearch = document.getElementById('settings-search');
settingsSearch.addEventListener('input', () => {
    const q = settingsSearch.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll('.settings-nav-item').forEach(b => {
        const hit = !q || b.textContent.toLowerCase().includes(q);
        b.classList.toggle('hidden', !hit);
        if (hit) shown++;
    });
    document.querySelectorAll('.settings-nav-group').forEach(g => g.classList.toggle('hidden', !!q));
    document.querySelector('.settings-nav-empty').classList.toggle('hidden', shown > 0);
});

function fmtBytes(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(0) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB';
    return (n || 0) + ' B';
}

let compressPollTimer = null;

async function loadStorage() {
    const sizeEl = document.getElementById('storage-size');
    const doneEl = document.getElementById('storage-done');
    sizeEl.textContent = '…';
    try {
        const s = await (await fetch('/api/storage')).json();
        sizeEl.textContent = fmtBytes(s.audio_bytes);
        doneEl.textContent = `${s.compressed_count} / ${s.audio_count}`;
        document.getElementById('storage-uploads').textContent =
            `${fmtBytes(s.upload_bytes || 0)} · ${s.upload_count || 0}`;
    } catch { sizeEl.textContent = '—'; }
    // 若已有批量压缩在跑，接着显示进度
    try {
        const st = await (await fetch('/api/compress_status')).json();
        if (st.running) {
            document.getElementById('compress-all').disabled = true;
            pollCompress();
        }
    } catch { /* ignore */ }
}

async function pollCompress() {
    const res = document.getElementById('compress-res');
    const btn = document.getElementById('compress-all');
    clearTimeout(compressPollTimer);
    try {
        const s = await (await fetch('/api/compress_status')).json();
        if (s.running) {
            res.className = 'test-res testing';
            res.textContent = T('settings.compressing', { done: s.done, total: s.total, saved: fmtBytes(s.saved) });
            if (!document.hidden) compressPollTimer = setTimeout(pollCompress, 2000);
        } else {
            btn.disabled = false;
            if (s.total > 0) {
                res.className = 'test-res ok';
                res.textContent = T('settings.compressDone', { saved: fmtBytes(s.saved) })
                    + (s.errors ? ` (${T('settings.compressErrors', { n: s.errors })})` : '');
                loadStorage();
            }
        }
    } catch {
        res.className = 'test-res bad';
        res.textContent = T('settings.statusCheckFailed');
        btn.disabled = false;
    }
}

document.getElementById('compress-all').addEventListener('click', async () => {
    const res = document.getElementById('compress-res');
    const btn = document.getElementById('compress-all');
    res.className = 'test-res testing';
    res.textContent = T('settings.starting');
    try {
        const r = await (await fetch('/api/compress_all', { method: 'POST' })).json();
        if (!r.ok) {
            res.className = 'test-res bad';
            res.textContent = r.error || T('settings.couldNotStart');
            return;
        }
    } catch {
        res.className = 'test-res bad';
        res.textContent = T('settings.couldNotStart');
        return;
    }
    btn.disabled = true;
    pollCompress();
});

// 删除全部音频副本 + 残留上传（转写稿不动）。后台线程 + 轮询，跟压缩同一套节奏。
let purgePollTimer = null;
async function pollPurge() {
    const res = document.getElementById('purge-res');
    const btn = document.getElementById('purge-audio');
    clearTimeout(purgePollTimer);
    try {
        const s = await (await fetch('/api/audio/purge_status')).json();
        if (s.running) {
            btn.disabled = true;
            res.textContent = T('settings.purging', { done: s.done, total: s.total, freed: fmtBytes(s.freed) });
            if (!document.hidden) purgePollTimer = setTimeout(pollPurge, 2000);
        } else {
            btn.disabled = false;
            if (s.total) {
                res.textContent = T('settings.purgeDone', { freed: fmtBytes(s.freed) })
                    + (s.errors ? ` (${T('settings.compressErrors', { n: s.errors })})` : '');
            }
            loadStorage();
        }
    } catch { btn.disabled = false; }
}
document.getElementById('purge-audio').addEventListener('click', async () => {
    if (!confirm(T('settings.purgeConfirm'))) return;
    const res = document.getElementById('purge-res');
    const btn = document.getElementById('purge-audio');
    btn.disabled = true;
    res.textContent = '…';
    try {
        const r = await (await fetch('/api/audio/purge', { method: 'POST' })).json();
        if (!r.ok) { res.textContent = r.error || T('settings.saveFailed'); btn.disabled = false; return; }
        pollPurge();
    } catch { res.textContent = T('settings.saveFailed'); btn.disabled = false; }
});
document.querySelectorAll('.settings-nav-item').forEach(b =>
    b.addEventListener('click', () => showSettingsPane(b.dataset.pane)));

document.getElementById('settings-open').addEventListener('click', () => openSettings('gemini'));
document.getElementById('settings-close').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', (e) => {
    if (e.target === settingsOverlay) closeSettings();      // 点遮罩关闭
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !settingsOverlay.classList.contains('hidden')) closeSettings();
});

async function testEngine(engine, resEl, payload) {
    resEl.className = 'test-res testing';
    resEl.textContent = T('settings.testing');
    try {
        const r = await (await fetch('/api/settings/test', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({engine, ...payload}),
        })).json();
        resEl.className = 'test-res ' + (r.ok ? 'ok' : 'bad');
        resEl.textContent = (r.ok ? '✓ ' : '✗ ') + (r.reason || '');
    } catch {
        resEl.className = 'test-res bad';
        resEl.textContent = '✗ ' + T('settings.requestFailed');
    }
}

document.getElementById('test-gemini').addEventListener('click', () => {
    testEngine('gemini', document.getElementById('test-gemini-res'), {
        gemini_key: setGeminiKey.value.trim(),
        gemini_base_url: setGeminiBase.value.trim(),
    });
});
document.getElementById('test-dashscope').addEventListener('click', () => {
    testEngine('dashscope', document.getElementById('test-dashscope-res'), {
        dashscope_key: setDashKey.value.trim(),
    });
});
document.getElementById('test-openrouter').addEventListener('click', () => {
    testEngine('openrouter', document.getElementById('test-openrouter-res'), {
        openrouter_key: setOpenrouterKey.value.trim(),
    });
});

document.getElementById('settings-save').addEventListener('click', async () => {
    const body = {
        gemini_base_url: setGeminiBase.value.trim(),
        whisper_model: document.getElementById('set-whisper-model').value.trim(),
        gemini_transcribe_model: document.getElementById('set-gemini-transcribe').value.trim(),
        gemini_analysis_model: document.getElementById('set-gemini-analysis').value.trim(),
        gemini_extract_model: document.getElementById('set-gemini-extract').value.trim(),
        keep_audio: document.getElementById('set-keep-audio').checked ? '1' : '',
    };
    if (setGeminiKey.value.trim()) body.gemini_key = setGeminiKey.value.trim();
    if (setDashKey.value.trim()) body.dashscope_key = setDashKey.value.trim();
    if (setOpenrouterKey.value.trim()) body.openrouter_key = setOpenrouterKey.value.trim();
    settingsMsg.className = 'settings-msg';
    settingsMsg.textContent = T('settings.saving');
    try {
        const r = await (await fetch('/api/settings', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        })).json();
        if (r.ok) {
            settingsMsg.className = 'settings-msg ok';
            settingsMsg.textContent = T('settings.saved');
            openSettings();                    // 刷新占位符、清空已输入的 key
            settingsMsg.textContent = T('settings.saved');
        } else {
            settingsMsg.className = 'settings-msg bad';
            settingsMsg.textContent = r.error || T('settings.saveFailed');
        }
    } catch {
        settingsMsg.className = 'settings-msg bad';
        settingsMsg.textContent = T('settings.saveFailed');
    }
});

// ========== Routing（URL hash 跟随导航）==========
// 用 hash（#/...）而不是 History API + 服务端路由：hash 从不发到服务器，
// 刷新一个带 hash 的 URL 照样命中 Flask 唯一的 GET / 路由，index.html 正常渲染，
// 剩下的状态恢复全在这一段客户端代码里完成，app.py 完全不用改。
//
// 设计：openDetailView/openChainDetail/openDocView/switchTab/switchLib 这些函数本身
// 不动——它们本来就是「传个 ID 进去，现查现渲染」，路由层只是在原来直接调用它们的地方
// 换成 navigate(hash)，由 hashchange → applyRoute() 统一分发，单一入口，不会重复渲染。
function navigate(hash, { replace = false } = {}) {
    if (replace) { history.replaceState(null, '', '#/' + hash); applyRoute(); }
    else location.hash = '/' + hash;   // 触发 'hashchange' → applyRoute
}

// 回到主 Tab 视图前，把三个可能叠在上面的顶层视图统一隐藏——
// closeDetailView/closeChainDetail/closeDocView 各自也有类似逻辑，这里是路由层的统一入口。
function _showMain() {
    mainView.classList.remove('hidden');
    detailView.classList.add('hidden');
    if (chainDetailView) { chainDetailView.classList.add('hidden'); clearTimeout(chainDetailTimer); }
    if (docView) docView.classList.add('hidden');
}

function applyRoute() {
    const parts = (location.hash.replace(/^#\/?/, '') || 'tab/transcribe').split('/').map(decodeURIComponent);
    if (parts[0] === 'tab') {
        _showMain();
        switchTab(parts[1] || 'transcribe');
        if (parts[1] === 'library') switchLib(parts[2] || 'transcripts');
    } else if (parts[0] === 'detail' && parts[1]) {
        openDetailView(parts[1]);
    } else if (parts[0] === 'chain' && parts[1] && parts[2] === 'doc' && parts[3]) {
        openDocView(parts[1], encodeURIComponent(parts[3]));
    } else if (parts[0] === 'chain' && parts[1]) {
        openChainDetail(parts[1]);
    } else if (parts[0] === 'xhs-report') {
        openXhsReport();
    } else {
        navigate('tab/transcribe', { replace: true });
    }
}
window.addEventListener('hashchange', applyRoute);
applyRoute();   // 恢复页面首次加载/刷新时应该显示的视图

// 切换界面语言：nav/按钮等静态文案由 setLang() 里的 applyStaticI18n() 处理；
// 这里补上"已经在屏幕上的动态内容"——按当前路由重新渲染一遍（复用现成的 fetch+渲染逻辑，
// 不是重新发明），外加不受路由管的统计弹窗（如果正开着）。
const uiLangToggle = document.getElementById('ui-lang-toggle');
if (uiLangToggle) {
    uiLangToggle.textContent = currentLang === 'zh' ? 'EN' : '中';
    uiLangToggle.addEventListener('click', () => {
        setLang(currentLang === 'zh' ? 'en' : 'zh');
        uiLangToggle.textContent = currentLang === 'zh' ? 'EN' : '中';
    });
}
document.addEventListener('langchange', () => {
    applyRoute();
    if (!settingsOverlay.classList.contains('hidden')) {
        if (activeSettingsPane === 'reflect') { reflectData = null; loadReflect(); }
        if (activeSettingsPane === 'library') renderLibrary();
    }
});


// ===== 字幕偏好（记住上次选择）+ 引擎速度提示 =====
(function initSubsAndSpeed() {
    const subsSel = document.getElementById('url-subs');
    if (subsSel) {
        try { const v = localStorage.getItem('verbatim.subs'); if (v) subsSel.value = v; } catch { /* 无痕模式等 */ }
        subsSel.addEventListener('change', () => {
            try { localStorage.setItem('verbatim.subs', subsSel.value); } catch { /* ignore */ }
        });
    }
    const chainSel = document.getElementById('chain-sub-lang');
    if (chainSel) {
        try { const v = localStorage.getItem('verbatim.chainSubLang'); if (v) chainSel.value = v; } catch { /* ignore */ }
        chainSel.addEventListener('change', () => {
            try { localStorage.setItem('verbatim.chainSubLang', chainSel.value); } catch { /* ignore */ }
        });
    }
    loadEngineSpeed();
    document.addEventListener('langchange', loadEngineSpeed);
})();
