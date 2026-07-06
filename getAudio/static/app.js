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
let batchTotal = 0;
let batchFinished = 0;

// ========== Helpers ==========
const ENGINE_LABELS = {
    whisper: 'Whisper',
    gemini: 'Gemini',
    dashscope: 'DashScope',
    precise: 'Precise (diarization)',
};

function engineLabel(engine) {
    return ENGINE_LABELS[engine] || (engine || 'Unknown');
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

// ========== File input (multi-file) ==========
fileInput.addEventListener('change', () => {
    updateFileLabel(fileInput.files);
});

function updateFileLabel(files) {
    if (!files || files.length === 0) {
        fileLabelText.textContent = 'Choose or drop audio / video files (multiple ok)';
        fileInfo.textContent = '';
        fileLabel.classList.remove('has-file');
        return;
    }

    let totalSize = 0;
    for (const f of files) totalSize += f.size;

    if (files.length === 1) {
        fileLabelText.textContent = files[0].name;
    } else {
        fileLabelText.textContent = `${files.length} files selected`;
    }
    fileInfo.textContent = formatFileSize(totalSize);
    fileLabel.classList.add('has-file');
}

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
    if (files && files.length > 0) {
        fileInput.files = files;
        updateFileLabel(files);
    }
});

// ========== Engine selection: toggle 预计人数 (precise only) ==========
const speakerCountGroup = document.getElementById('speaker-count-group');
document.querySelectorAll('input[name="engine"]').forEach(radio => {
    radio.addEventListener('change', () => {
        const isPrecise = document.querySelector('input[name="engine"]:checked').value === 'precise';
        speakerCountGroup.classList.toggle('hidden', !isPrecise);
    });
});

// ========== Form submission (batch) ==========
// 同时上传的文件数。逐个上传是为了绕开单请求体积上限（几百个文件塞一个请求会超限被拒），
// 上传本身也限流，避免一次性发起过多大文件上传拖垮网络/内存。
// 真正的转写并发由服务器端每引擎的信号量控制（见 config.ENGINE_CONCURRENCY）。
const UPLOAD_CONCURRENCY = 3;

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;
    const files = Array.from(fileInput.files);

    errorSection.classList.add('hidden');
    batchSection.classList.remove('hidden');
    batchList.innerHTML = '';
    batchTotal = files.length;
    batchFinished = 0;
    updateBatchProgress();

    submitBtn.disabled = true;
    submitBtn.textContent = 'Transcribing…';

    // 按选择顺序为每个文件先建一行，再用上传池逐个提交到 /upload
    const jobs = files.map(file => {
        const row = createBatchRow(file.name);
        setRowStatus(row, 'Queued', 'queued');
        batchList.appendChild(row);
        return { file, row };
    });

    await runUploadPool(jobs, engine, UPLOAD_CONCURRENCY);
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

async function uploadOne(file, row, engine) {
    setRowStatus(row, 'Uploading…', 'running');

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
            let msg = `Upload failed (${resp.status})`;
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
        setRowStatus(row, `Upload failed: ${err.message}`, 'error');
        onTaskFinished();
    }
}

function resetSubmitBtn() {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Transcribe';
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
    status.textContent = 'Queued';

    info.appendChild(name);
    info.appendChild(status);

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
}

function setRowProgress(row, percent) {
    row.querySelector('.batch-item-progress-fill').style.width = `${percent}%`;
}

function connectBatchSSE(taskId, row) {
    const actions = row.querySelector('.batch-item-actions');
    const source = new EventSource(`/stream/${taskId}`);

    source.onmessage = (event) => {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
            case 'queued':
                setRowStatus(row, 'Queued', 'queued');
                break;

            case 'progress':
                setRowStatus(row, `Transcribing ${msg.percent}%`, 'running');
                setRowProgress(row, msg.percent);
                break;

            // segment / summary 事件在此忽略：结果自动进历史，点“查看”看详情
            case 'segment':
            case 'summary':
                break;

            case 'done':
                setRowStatus(row, 'Done', 'done');
                setRowProgress(row, 100);
                addViewButton(actions, taskId);
                source.close();
                onTaskFinished();
                renderHistory();
                break;

            case 'error':
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

function addViewButton(actions, taskId) {
    if (actions.querySelector('.batch-view-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'btn-secondary btn-small batch-view-btn';
    btn.textContent = 'View';
    btn.addEventListener('click', () => openDetailView(taskId));
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
    downloadFile(segmentLines(detailSegments).join('\n'),
        `transcription_${dateStr()}.txt`, 'text/plain;charset=utf-8');
    showToast('Downloaded TXT');
});

detailSrtBtn.addEventListener('click', () => {
    if (detailSegments.length === 0) return;
    downloadFile(buildSRT(detailSegments),
        `subtitles_${dateStr()}.srt`, 'text/plain;charset=utf-8');
    showToast('Downloaded SRT');
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
        showToast('Copied to clipboard');
    }).catch(() => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('Copied to clipboard');
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

// ========== History (server API) ==========
// 搜索 + 引擎筛选状态
let activeEngineFilter = '';
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

        historyCount.textContent = entries.length ? `(${entries.length})` : '';

        if (entries.length === 0) {
            historyList.innerHTML = query
                ? '<p class="history-empty">No matching records</p>'
                : '<p class="history-empty">No transcripts yet</p>';
            return;
        }

        historyList.innerHTML = '';
        entries.forEach(entry => historyList.appendChild(buildHistoryCard(entry)));
    } catch {
        historyList.innerHTML = '<p class="history-empty">Failed to load history</p>';
    }
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
    parts.push(`${entry.segment_count} segments`);
    if (entry.ai_title) parts.push(entry.filename);
    meta.textContent = parts.join(' · ');
    info.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'history-item-actions';

    const viewBtn = document.createElement('button');
    viewBtn.className = 'btn-secondary btn-small';
    viewBtn.textContent = 'View';
    viewBtn.addEventListener('click', () => openDetailView(entry.id));

    const delBtn = document.createElement('button');
    delBtn.className = 'btn-secondary btn-small btn-danger';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', async () => {
        const label = entry.ai_title || entry.filename;
        if (!confirm(`Delete "${label}"? This cannot be undone.`)) return;
        try {
            const resp = await fetch(`/api/history/${entry.id}`, { method: 'DELETE' });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                showToast(data.error || 'Delete failed');
                return;
            }
            renderHistory();
            showToast('Deleted');
        } catch {
            showToast('Delete failed');
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

// ========== AI 整理（批量生成标题/标签） ==========
enrichBtn.addEventListener('click', async () => {
    enrichBtn.disabled = true;
    enrichBtn.textContent = 'Enriching…';
    try {
        await fetch('/api/enrich_all', { method: 'POST' });
        pollEnrichStatus();
    } catch {
        showToast('Failed to start auto-titling');
        resetEnrichBtn();
    }
});

function resetEnrichBtn() {
    enrichBtn.disabled = false;
    enrichBtn.textContent = 'Auto-title';
}

async function pollEnrichStatus() {
    try {
        const resp = await fetch('/api/enrich_status');
        const st = await resp.json();
        if (st.running) {
            enrichBtn.textContent = `${st.done}/${st.total}`;
            // 每整理完几条就刷新列表，让标题逐步冒出来
            if (st.done > 0 && st.done % 10 === 0) renderHistory();
            setTimeout(pollEnrichStatus, 1500);
        } else {
            resetEnrichBtn();
            renderHistory();
            if (st.total > 0) {
                showToast(`Auto-title done: ${st.total - st.failed}/${st.total} succeeded`);
            } else {
                showToast('All records already have titles');
            }
        }
    } catch {
        resetEnrichBtn();
    }
}

// ========== Detail View ==========
async function openDetailView(taskId) {
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

        detailTitle.textContent = data.filename;
        const segCount = (data.segments || []).length;
        const parts = [data.date, engineLabel(data.engine)];
        if (data.duration_seconds) parts.push(formatDuration(data.duration_seconds));
        parts.push(`${segCount} segments`);
        detailMeta.textContent = parts.join(' · ');

        detailAudioPlayer.pause();
        detailAudioPlayer.currentTime = 0;
        detailAudioPlayer.src = `/api/history/${taskId}/audio`;
        detailPlayerSection.classList.remove('hidden');

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
        showToast('Could not load record');
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
    detailSyncState.last = null;
    window.scrollTo({ top: 0 });
}

detailBackBtn.addEventListener('click', closeDetailView);

clearHistoryBtn.addEventListener('click', async () => {
    if (!confirm('Clear all history? This deletes every saved audio file and transcript.')) return;

    try {
        const resp = await fetch('/api/history', { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.error || 'Clear failed');
            return;
        }
        renderHistory();
        let msg = `Cleared ${data.deleted || 0}`;
        if (data.skipped) msg += ` (${data.skipped} in-progress skipped)`;
        showToast(msg);
    } catch {
        showToast('Clear failed');
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

// ========== 链条：URL → 下载 → 转写 → 分析 → 总合成 ==========
const chainUrl = document.getElementById('chain-url');
const chainAuthor = document.getElementById('chain-author');
const chainMax = document.getElementById('chain-max');
const chainEngine = document.getElementById('chain-engine');
const chainAnalyze = document.getElementById('chain-analyze');
const chainStartBtn = document.getElementById('chain-start');
const chainList = document.getElementById('chain-list');

const CHAIN_STAGE_LABELS = {
    starting: 'Starting',
    downloading: 'Downloading',
    transcribing: 'Transcribing',
    analyzing: 'Analyzing',
    synthesizing: 'Synthesizing',
    done: 'Done',
    failed: 'Failed',
};

let chainPollTimer = null;

chainStartBtn.addEventListener('click', async () => {
    const url = (chainUrl.value || '').trim();
    if (!url) { chainUrl.focus(); return; }
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
                analyze: chainAnalyze.checked,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Failed to create');
        chainUrl.value = '';
        loadChains();
    } catch (err) {
        alert('Failed to start pipeline: ' + err.message);
    } finally {
        chainStartBtn.disabled = false;
    }
});

function chainProgressText(c) {
    const parts = [];
    const vids = c.videos || [];
    if (c.download_total) {
        const dlFail = vids.filter(v => v.status === 'download_failed').length;
        parts.push(`Downloaded ${c.download_done || 0}/${c.download_total}` +
            (dlFail ? ` (${dlFail} failed)` : ''));
    }
    // 转写基数只算下载成功、真正提交了转写的视频
    const submitted = vids.filter(v => v.status !== 'download_failed');
    if (submitted.length) {
        const done = submitted.filter(v => v.status === 'done').length;
        const failed = submitted.filter(v => v.status === 'failed').length;
        parts.push(`Transcribed ${done}/${submitted.length}` + (failed ? ` (${failed} failed)` : ''));
    }
    if (c.analyze && c.analyzed_done != null && submitted.length) {
        parts.push(`Analyzed ${c.analyzed_done}/${submitted.length}`);
    }
    return parts.join(' · ');
}

function renderChains(chains) {
    if (!chains.length) {
        chainList.innerHTML = '';
        return;
    }
    chainList.innerHTML = chains.map(c => {
        const stage = CHAIN_STAGE_LABELS[c.stage] || c.stage;
        const active = !['done', 'failed'].includes(c.stage);
        const prog = chainProgressText(c);
        let links = '';
        if (c.final_doc) {
            links += `<a class="chain-doc-link" href="#"
                onclick="openDocView('${c.id}','${encodeURIComponent(c.final_doc)}');return false;">Read synthesis</a>`;
        }
        if (['done', 'failed'].includes(c.stage)) {
            links += `<a class="chain-doc-link" href="#"
                onclick="gotoDocs();return false;">All documents</a>
                <a class="chain-doc-link chain-del" href="#"
                onclick="deleteChain('${c.id}');return false;">Delete</a>`;
        }
        const err = c.stage === 'failed'
            ? `<div class="chain-error">${(c.error || '').slice(0, 200)}</div>` : '';
        const cur = active && c.current
            ? `<div class="chain-current">${c.current.slice(0, 60)}</div>` : '';
        return `<div class="chain-item ${active ? 'chain-active' : ''}"
                onclick="openChainDetail('${c.id}')" title="点击查看每个视频的进度">
            <div class="chain-item-top">
                <span class="chain-stage">${stage}</span>
                <span class="chain-url" title="${c.url}">${c.author !== '该博主' ? c.author + ' · ' : ''}${c.url.slice(0, 60)}</span>
                <span class="chain-links" onclick="event.stopPropagation()">${links}</span>
            </div>
            <div class="chain-progress">${prog}</div>
            ${cur}${err}
        </div>`;
    }).join('');
}

// ========== 链条详情：视频封面网格 + 每个视频状态 ==========
const chainDetailView = document.getElementById('chain-detail-view');
const chainDetailBack = document.getElementById('chain-detail-back');
const chainDetailTitle = document.getElementById('chain-detail-title');
const chainDetailMeta = document.getElementById('chain-detail-meta');
const chainDetailGrid = document.getElementById('chain-detail-grid');

const VIDEO_STATUS = {
    downloading: { label: 'Downloading', cls: 'vs-active' },
    transcribing: { label: 'Transcribing', cls: 'vs-active' },
    pending: { label: 'Queued', cls: 'vs-active' },
    done: { label: 'Transcribed', cls: 'vs-done' },
    failed: { label: 'Failed', cls: 'vs-fail' },
    download_failed: { label: 'Download failed', cls: 'vs-fail' },
};

let chainDetailId = null;
let chainDetailTimer = null;

async function openChainDetail(id) {
    chainDetailId = id;
    mainView.classList.add('hidden');
    chainDetailView.classList.remove('hidden');
    window.scrollTo({ top: 0 });
    await refreshChainDetail();
}

async function refreshChainDetail() {
    if (!chainDetailId) return;
    let c;
    try {
        const resp = await fetch(`/api/chain/${chainDetailId}`);
        if (!resp.ok) throw new Error('not found');
        c = await resp.json();
    } catch {
        chainDetailGrid.innerHTML = '<p class="history-empty">Could not load</p>';
        return;
    }
    chainDetailTitle.textContent =
        (c.author && c.author !== '该博主') ? c.author : (c.url || 'Pipeline');
    chainDetailMeta.textContent =
        `${CHAIN_STAGE_LABELS[c.stage] || c.stage} · ${chainProgressText(c)}`;

    const vids = c.videos || [];
    chainDetailGrid.innerHTML = vids.map(v => {
        const st = VIDEO_STATUS[v.status] || { label: v.status, cls: '' };
        const clickable = v.status === 'done' && v.task_id;
        const thumb = v.thumbnail
            ? `<img class="vg-thumb" src="${v.thumbnail}" loading="lazy" alt=""
                 onerror="this.style.display='none'">`
            : '<div class="vg-thumb vg-noimg">▷</div>';
        const onclick = clickable
            ? ` onclick="openDetailView('${v.task_id}')" title="View transcript"` : '';
        return `<div class="vg-card ${clickable ? 'vg-clickable' : ''}"${onclick}>
            ${thumb}
            <div class="vg-badge ${st.cls}">${st.label}</div>
            <div class="vg-title" title="${(v.title || '').replace(/"/g, '&quot;')}">${v.title || ''}</div>
        </div>`;
    }).join('') || '<p class="history-empty">Resolving episode list…</p>';

    clearTimeout(chainDetailTimer);
    if (!['done', 'failed'].includes(c.stage)) {
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

if (chainDetailBack) chainDetailBack.addEventListener('click', closeChainDetail);

async function loadChains() {
    try {
        const resp = await fetch('/api/chains');
        const chains = await resp.json();
        renderChains(chains);
        const anyActive = chains.some(c => !['done', 'failed'].includes(c.stage));
        clearTimeout(chainPollTimer);
        if (anyActive) {
            chainPollTimer = setTimeout(loadChains, 4000);
            // 转写阶段会往历史里落新条目，顺手刷新历史
            if (typeof renderHistory === 'function') renderHistory();
        }
    } catch (e) { /* 服务重启瞬间的抖动，忽略 */ }
}

// 跳到「资料库 → 分析文档」子分区
function gotoDocs() {
    switchTab('library');
    switchLib('docs');
}

async function deleteChain(chainId) {
    if (!confirm('Delete this pipeline’s documents? (transcripts stay in the library)')) return;
    await fetch(`/api/chain/${chainId}`, { method: 'DELETE' });
    loadChains();
}

loadChains();

// ========== Tab 切换（转写 / 链条 / 资料库） ==========
const navTabs = document.querySelectorAll('.nav-tab');
const tabPanels = {
    transcribe: document.getElementById('tab-transcribe'),
    chain: document.getElementById('tab-chain'),
    library: document.getElementById('tab-library'),
};

function switchTab(name) {
    navTabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    Object.entries(tabPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'library') { renderHistory(); }
    if (name === 'chain') { loadChains(); }
}

navTabs.forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
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
    btn.addEventListener('click', () => switchLib(btn.dataset.lib));
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
            docsList.innerHTML = '<p class="history-empty">No analysis documents yet — run a Pipeline</p>';
            return;
        }
        const blocks = await Promise.all(withDocs.map(async c => {
            let files = [];
            try { files = await (await fetch(`/api/chain/${c.id}/files`)).json(); }
            catch { files = []; }
            files = (Array.isArray(files) ? files : []).filter(f => f.endsWith('.md'));
            if (!files.length) return '';
            // 总分析置顶，其余按名排序
            files.sort((a, b) => (a === '总分析.md' ? -1 : b === '总分析.md' ? 1 : a.localeCompare(b)));
            const title = (c.author && c.author !== '该博主') ? c.author : c.url;
            const stageBadge = c.stage === 'done' ? ''
                : `<span class="doc-stage">（${CHAIN_STAGE_LABELS[c.stage] || c.stage}）</span>`;
            const items = files.map(f => {
                const isTotal = f === 'total' || f === '总分析.md';
                const label = f === '总分析.md' ? 'Synthesis' : '' + f.replace(/^分析_\d+_/, '').replace(/\.md$/, '');
                return `<button class="doc-item ${isTotal ? 'doc-total' : ''}"
                    onclick="openDocView('${c.id}','${encodeURIComponent(f)}')">${label}</button>`;
            }).join('');
            return `<div class="doc-group">
                <div class="doc-group-title">${escapeHtml(title.slice(0, 70))} ${stageBadge}
                    <span class="doc-count">${files.length} 篇</span></div>
                <div class="doc-items">${items}</div>
            </div>`;
        }));
        const html = blocks.filter(Boolean).join('');
        docsList.innerHTML = html || '<p class="history-empty">No analysis documents yet</p>';
    } catch (e) {
        docsList.innerHTML = '<p class="history-empty">Failed to load</p>';
    }
}

async function openDocView(chainId, encName) {
    const name = decodeURIComponent(encName);
    try {
        const resp = await fetch(`/api/chain/${chainId}/file?name=${encodeURIComponent(name)}`);
        if (!resp.ok) throw new Error('not found');
        const raw = await resp.text();
        currentDoc = { chainId, name, raw };
        docTitle.textContent = name.replace(/\.md$/, '');
        docContent.innerHTML = renderMarkdown(raw);
        mainView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
    } catch {
        showToast('Could not load document');
    }
}

function closeDocView() {
    docView.classList.add('hidden');
    mainView.classList.remove('hidden');
    docContent.innerHTML = '';
    window.scrollTo({ top: 0 });
}

if (docBackBtn) docBackBtn.addEventListener('click', closeDocView);
if (docDownloadBtn) docDownloadBtn.addEventListener('click', () => {
    if (currentDoc.raw) {
        downloadFile(currentDoc.raw, currentDoc.name || 'document.md',
            'text/markdown;charset=utf-8');
        showToast('Downloaded .md');
    }
});

// ========== 轻量 Markdown 渲染（无外部依赖） ==========
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function renderInline(s) {
    // 先转义，再套内联格式
    s = escapeHtml(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
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

// ========== 左下角个人数据展板 ==========
const statsToggle = document.getElementById('stats-toggle');
const statsPanel = document.getElementById('stats-panel');
let statsLoaded = false;

function fmtBig(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

// 自绘 SVG 折线（面积填充 + 端点强调），不依赖外部库
function sparkline(values, w = 296, h = 84) {
    if (!values || values.length < 2) {
        return '<div class="spark-empty">Not enough data yet</div>';
    }
    const pad = 6;
    const maxY = Math.max(...values, 1);
    const X = i => pad + (i / (values.length - 1)) * (w - 2 * pad);
    const Y = v => h - pad - (v / maxY) * (h - 2 * pad);
    const pts = values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    const area = `${X(0)},${h - pad} ${pts} ${X(values.length - 1)},${h - pad}`;
    const last = values.length - 1;
    return `<svg viewBox="0 0 ${w} ${h}" class="spark" preserveAspectRatio="none">
        <defs><linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="var(--coral)" stop-opacity="0.18"/>
            <stop offset="1" stop-color="var(--coral)" stop-opacity="0"/>
        </linearGradient></defs>
        <polygon points="${area}" fill="url(#sparkfill)"/>
        <polyline points="${pts}" fill="none" stroke="var(--coral)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${X(last)}" cy="${Y(values[last])}" r="3" fill="var(--coral)"/>
    </svg>`;
}

async function loadStats() {
    try {
        const s = await (await fetch('/api/stats')).json();
        document.getElementById('stat-hours').textContent = s.totals.hours;
        document.getElementById('stat-chars').textContent = fmtBig(s.totals.chars);
        document.getElementById('stat-count').textContent = s.totals.transcripts;

        // 累计小时折线
        let cum = 0;
        const cumHours = (s.timeline || []).map(p => { cum += p.minutes / 60; return cum; });
        document.getElementById('stats-chart').innerHTML = sparkline(cumHours);

        // 关注领域：横向条
        const tags = s.top_tags || [];
        const max = tags.length ? tags[0].count : 1;
        document.getElementById('stats-tags').innerHTML = tags.length
            ? tags.map(t => `<div class="stat-tag">
                    <span class="stat-tag-name" title="${t.tag}">${t.tag}</span>
                    <span class="stat-tag-bar"><i style="width:${Math.max(6, t.count / max * 100)}%"></i></span>
                    <span class="stat-tag-num">${t.count}</span>
                </div>`).join('')
            : '<div class="spark-empty">No tags yet — run “Auto-title”</div>';
        statsLoaded = true;
    } catch {
        document.getElementById('stats-chart').innerHTML = '<div class="spark-empty">Failed to load</div>';
    }
}

statsToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = statsPanel.classList.toggle('hidden');
    if (!open && !statsLoaded) loadStats();      // 首次打开才拉数据
    if (!open) loadStats();                        // 每次打开刷新
});
// 点面板外部关闭
document.addEventListener('click', (e) => {
    if (!statsPanel.classList.contains('hidden') &&
        !document.getElementById('stats-fab').contains(e.target)) {
        statsPanel.classList.add('hidden');
    }
});
