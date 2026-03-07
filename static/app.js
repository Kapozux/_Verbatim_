/**
 * Client-side logic for audio/video transcription app.
 * Handles file upload, SSE progress streaming, and result display.
 */

// DOM elements
const form = document.getElementById('upload-form');
const fileInput = document.getElementById('audio-file');
const fileLabel = document.querySelector('.file-label');
const fileLabelText = document.getElementById('file-label-text');
const fileInfo = document.getElementById('file-info');
const submitBtn = document.getElementById('submit-btn');

const progressSection = document.getElementById('progress-section');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');

const resultsSection = document.getElementById('results-section');
const segmentsContainer = document.getElementById('segments-container');

const summarySection = document.getElementById('summary-section');
const summaryOverview = document.getElementById('summary-overview');
const summarySections = document.getElementById('summary-sections');

const errorSection = document.getElementById('error-section');
const errorText = document.getElementById('error-text');

const copyBtn = document.getElementById('copy-btn');
const downloadBtn = document.getElementById('download-btn');

// ========== File input display ==========
fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (file) {
        fileLabelText.textContent = file.name;
        fileInfo.textContent = formatFileSize(file.size);
        fileLabel.classList.add('has-file');
    } else {
        fileLabelText.textContent = '选择音频/视频文件';
        fileInfo.textContent = '';
        fileLabel.classList.remove('has-file');
    }
});

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ========== Form submission ==========
form.addEventListener('submit', async (e) => {
    e.preventDefault();

    if (!fileInput.files.length) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;

    // Reset UI
    resetUI();
    progressSection.classList.remove('hidden');
    submitBtn.disabled = true;
    submitBtn.textContent = '转写中...';

    // Build form data
    const formData = new FormData();
    formData.append('audio', fileInput.files[0]);
    formData.append('engine', engine);

    try {
        // Upload file
        updateProgress(0, '正在上传文件...');
        const resp = await fetch('/upload', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();

        if (data.error) {
            showError(data.error);
            return;
        }

        // Connect to SSE stream
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
                // Show results section on first segment
                resultsSection.classList.remove('hidden');
                appendSegment(msg);
                break;

            case 'summary':
                renderSummary(msg);
                break;

            case 'done':
                if (msg.summary) {
                    renderSummary(msg.summary);
                }
                updateProgress(100, '✅ 转写完成！');
                submitBtn.disabled = false;
                submitBtn.textContent = '开始转写';
                source.close();
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

// ========== UI updates ==========
function updateProgress(percent, message) {
    progressFill.style.width = `${percent}%`;
    progressText.textContent = message || `转写中... ${percent}%`;
}

function appendSegment(seg) {
    const div = document.createElement('div');
    div.className = 'segment';

    const ts = document.createElement('span');
    ts.className = 'timestamp';
    ts.textContent = `${seg.timestamp}`;

    const text = document.createElement('span');
    text.className = 'segment-text';
    text.textContent = seg.text;

    div.appendChild(ts);
    div.appendChild(text);
    segmentsContainer.appendChild(div);

    // Auto-scroll to latest segment
    segmentsContainer.scrollTop = segmentsContainer.scrollHeight;
}

function showError(message) {
    errorSection.classList.remove('hidden');
    errorText.textContent = message;
    submitBtn.disabled = false;
    submitBtn.textContent = '开始转写';
}

function renderSummary(data) {
    if (!data || !data.overview) return;

    summarySection.classList.remove('hidden');
    summaryOverview.textContent = data.overview;
    summarySections.innerHTML = '';

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
            summarySections.appendChild(block);
        });
    }
}

function resetUI() {
    errorSection.classList.add('hidden');
    resultsSection.classList.add('hidden');
    summarySection.classList.add('hidden');
    progressSection.classList.add('hidden');
    segmentsContainer.innerHTML = '';
    summaryOverview.textContent = '';
    summarySections.innerHTML = '';
    progressFill.style.width = '0%';
    progressText.textContent = '准备中...';
}

// ========== Copy & Download ==========
copyBtn.addEventListener('click', () => {
    const segments = document.querySelectorAll('.segment');
    const lines = Array.from(segments).map(seg => {
        const ts = seg.querySelector('.timestamp').textContent;
        const text = seg.querySelector('.segment-text').textContent;
        return `[${ts}] ${text}`;
    });
    const fullText = lines.join('\n');

    navigator.clipboard.writeText(fullText).then(() => {
        showToast('已复制到剪贴板');
    }).catch(() => {
        // Fallback for older browsers
        const textarea = document.createElement('textarea');
        textarea.value = fullText;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('已复制到剪贴板');
    });
});

downloadBtn.addEventListener('click', () => {
    const segments = document.querySelectorAll('.segment');
    const lines = Array.from(segments).map(seg => {
        const ts = seg.querySelector('.timestamp').textContent;
        const text = seg.querySelector('.segment-text').textContent;
        return `[${ts}] ${text}`;
    });
    const fullText = lines.join('\n');

    const blob = new Blob([fullText], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcription_${new Date().toISOString().slice(0, 10)}.txt`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('已下载文件');
});

// ========== Toast ==========
function showToast(message) {
    // Remove existing toast
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);

    setTimeout(() => toast.remove(), 2000);
}
