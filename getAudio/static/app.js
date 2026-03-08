/**
 * Client-side logic for audio/video transcription app.
 * Uses server-side persistence for history (audio + transcript + summary).
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

const progressSection = document.getElementById('progress-section');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');

const playerSection = document.getElementById('player-section');
const audioPlayer = document.getElementById('audio-player');

const resultsSection = document.getElementById('results-section');
const segmentsContainer = document.getElementById('segments-container');

const summarySection = document.getElementById('summary-section');
const summaryOverview = document.getElementById('summary-overview');
const summarySections = document.getElementById('summary-sections');

const errorSection = document.getElementById('error-section');
const errorText = document.getElementById('error-text');

const copyBtn = document.getElementById('copy-btn');
const downloadBtn = document.getElementById('download-btn');
const srtBtn = document.getElementById('srt-btn');
const clearHistoryBtn = document.getElementById('clear-history-btn');
const historyList = document.getElementById('history-list');

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
let currentAudioBlobUrl = null;
let currentFilename = '';
let currentSegments = [];
let currentSummary = null;
let lastActiveSegment = null;
let detailSegments = [];
let detailLastActive = null;

// ========== File input ==========
fileInput.addEventListener('change', () => {
    handleFileSelect(fileInput.files[0]);
});

function handleFileSelect(file) {
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;

    fileLabelText.textContent = file.name;
    fileInfo.textContent = formatFileSize(file.size);
    fileLabel.classList.add('has-file');
    currentFilename = file.name;

    if (currentAudioBlobUrl) URL.revokeObjectURL(currentAudioBlobUrl);
    currentAudioBlobUrl = URL.createObjectURL(file);
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ========== Drag & Drop ==========
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
    const file = e.dataTransfer.files[0];
    if (file) handleFileSelect(file);
});

// ========== Form submission ==========
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;

    resetUI();
    progressSection.classList.remove('hidden');
    submitBtn.disabled = true;
    submitBtn.textContent = '转写中...';

    const formData = new FormData();
    formData.append('audio', fileInput.files[0]);
    formData.append('engine', engine);

    try {
        updateProgress(0, '正在上传文件...');
        const resp = await fetch('/upload', { method: 'POST', body: formData });
        const data = await resp.json();

        if (data.error) {
            showError(data.error);
            return;
        }

        connectSSE(data.task_id);
    } catch (err) {
        showError('上传失败: ' + err.message);
    }
});

// ========== SSE streaming ==========
function connectSSE(taskId) {
    const source = new EventSource(`/stream/${taskId}`);

    source.onmessage = (event) => {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
            case 'progress':
                updateProgress(msg.percent, msg.message);
                break;

            case 'segment':
                resultsSection.classList.remove('hidden');
                appendSegment(msg, segmentsContainer, audioPlayer);
                currentSegments.push(msg);
                break;

            case 'summary':
                currentSummary = msg;
                renderSummary(summarySection, summaryOverview, summarySections, msg);
                break;

            case 'done':
                if (msg.summary) {
                    currentSummary = msg.summary;
                    renderSummary(summarySection, summaryOverview, summarySections, msg.summary);
                }
                showAudioPlayer(msg.task_id);
                updateProgress(100, '✅ 转写完成！');
                submitBtn.disabled = false;
                submitBtn.textContent = '开始转写';
                source.close();
                renderHistory();
                break;

            case 'error':
                showError(msg.message);
                source.close();
                break;
        }
    };

    source.onerror = () => {
        showError('连接中断，请重试');
        source.close();
        submitBtn.disabled = false;
        submitBtn.textContent = '开始转写';
    };
}

// ========== Audio playback ==========
function showAudioPlayer(taskId) {
    if (currentAudioBlobUrl) {
        audioPlayer.src = currentAudioBlobUrl;
    } else if (taskId) {
        audioPlayer.src = `/api/history/${taskId}/audio`;
    }
    playerSection.classList.remove('hidden');
}

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

const mainSyncState = { last: null };
const detailSyncState = { last: null };
setupTimeSync(audioPlayer, segmentsContainer, mainSyncState);
setupTimeSync(detailAudioPlayer, detailSegmentsContainer, detailSyncState);

// ========== UI updates ==========
function updateProgress(percent, message) {
    progressFill.style.width = `${percent}%`;
    progressText.textContent = message || `转写中... ${percent}%`;
}

function appendSegment(seg, container, player) {
    const div = document.createElement('div');
    div.className = 'segment';
    div.dataset.startSec = parseTimestampToSeconds(seg.timestamp);

    const ts = document.createElement('span');
    ts.className = 'timestamp clickable';
    ts.textContent = seg.timestamp;
    ts.title = '点击跳转播放';
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
    submitBtn.disabled = false;
    submitBtn.textContent = '开始转写';
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

function resetUI() {
    errorSection.classList.add('hidden');
    resultsSection.classList.add('hidden');
    summarySection.classList.add('hidden');
    progressSection.classList.add('hidden');
    playerSection.classList.add('hidden');
    segmentsContainer.innerHTML = '';
    summaryOverview.textContent = '';
    summarySections.innerHTML = '';
    progressFill.style.width = '0%';
    progressText.textContent = '准备中...';
    currentSegments = [];
    currentSummary = null;
    mainSyncState.last = null;
}

// ========== Copy & Download (main view) ==========
function segmentLines(segs) {
    return segs.map(seg => `[${seg.timestamp}] ${seg.text}`);
}

copyBtn.addEventListener('click', () => {
    copyToClipboard(segmentLines(currentSegments).join('\n'));
});

downloadBtn.addEventListener('click', () => {
    downloadFile(segmentLines(currentSegments).join('\n'),
        `transcription_${dateStr()}.txt`, 'text/plain;charset=utf-8');
    showToast('已下载 TXT 文件');
});

srtBtn.addEventListener('click', () => {
    if (currentSegments.length === 0) return;
    downloadFile(buildSRT(currentSegments),
        `subtitles_${dateStr()}.srt`, 'text/plain;charset=utf-8');
    showToast('已下载 SRT 字幕文件');
});

// ========== Copy & Download (detail view) ==========
detailCopyBtn.addEventListener('click', () => {
    copyToClipboard(segmentLines(detailSegments).join('\n'));
});

detailDownloadBtn.addEventListener('click', () => {
    downloadFile(segmentLines(detailSegments).join('\n'),
        `transcription_${dateStr()}.txt`, 'text/plain;charset=utf-8');
    showToast('已下载 TXT 文件');
});

detailSrtBtn.addEventListener('click', () => {
    if (detailSegments.length === 0) return;
    downloadFile(buildSRT(detailSegments),
        `subtitles_${dateStr()}.srt`, 'text/plain;charset=utf-8');
    showToast('已下载 SRT 字幕文件');
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
        showToast('已复制到剪贴板');
    }).catch(() => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('已复制到剪贴板');
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
async function renderHistory() {
    try {
        const resp = await fetch('/api/history');
        const entries = await resp.json();

        if (entries.length === 0) {
            historyList.innerHTML = '<p class="history-empty">暂无历史记录</p>';
            return;
        }

        historyList.innerHTML = '';
        entries.forEach(entry => {
            const item = document.createElement('div');
            item.className = 'history-item';

            const info = document.createElement('div');
            info.className = 'history-item-info';

            const name = document.createElement('span');
            name.className = 'history-item-name';
            name.textContent = entry.filename;

            const meta = document.createElement('span');
            meta.className = 'history-item-meta';
            const engineLabel = entry.engine === 'gemini' ? 'Gemini' : 'Whisper';
            meta.textContent = `${entry.date} · ${engineLabel} · ${entry.segment_count} 段`;

            info.appendChild(name);
            info.appendChild(meta);

            const actions = document.createElement('div');
            actions.className = 'history-item-actions';

            const viewBtn = document.createElement('button');
            viewBtn.className = 'btn-secondary btn-small';
            viewBtn.textContent = '查看';
            viewBtn.addEventListener('click', () => openDetailView(entry.id));

            const delBtn = document.createElement('button');
            delBtn.className = 'btn-secondary btn-small btn-danger';
            delBtn.textContent = '删除';
            delBtn.addEventListener('click', async () => {
                await fetch(`/api/history/${entry.id}`, { method: 'DELETE' });
                renderHistory();
                showToast('已删除');
            });

            actions.appendChild(viewBtn);
            actions.appendChild(delBtn);

            item.appendChild(info);
            item.appendChild(actions);
            historyList.appendChild(item);
        });
    } catch {
        historyList.innerHTML = '<p class="history-empty">加载历史记录失败</p>';
    }
}

// ========== Detail View ==========
async function openDetailView(taskId) {
    try {
        const resp = await fetch(`/api/history/${taskId}`);
        if (!resp.ok) throw new Error('Not found');
        const data = await resp.json();

        mainView.classList.add('hidden');
        detailView.classList.remove('hidden');
        window.scrollTo({ top: 0 });

        detailTitle.textContent = data.filename;
        const engineLabel = data.engine === 'gemini' ? 'Gemini' : 'Whisper';
        detailMeta.textContent = `${data.date} · ${engineLabel} · ${data.segments.length} 段`;

        detailAudioPlayer.src = `/api/history/${taskId}/audio`;
        detailPlayerSection.classList.remove('hidden');

        detailSegments = data.segments;
        detailSegmentsContainer.innerHTML = '';
        detailSyncState.last = null;
        data.segments.forEach(seg => {
            appendSegment(seg, detailSegmentsContainer, detailAudioPlayer);
        });

        if (data.summary && data.summary.overview) {
            renderSummary(detailSummarySection, detailSummaryOverview,
                detailSummarySections, data.summary);
        } else {
            detailSummarySection.classList.add('hidden');
        }
    } catch {
        showToast('无法加载该记录');
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
    if (!confirm('确定清空所有历史记录？这将删除所有保存的音频和转写结果。')) return;

    try {
        const resp = await fetch('/api/history');
        const entries = await resp.json();
        await Promise.all(
            entries.map(e => fetch(`/api/history/${e.id}`, { method: 'DELETE' }))
        );
        renderHistory();
        showToast('历史记录已清空');
    } catch {
        showToast('清空失败');
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
