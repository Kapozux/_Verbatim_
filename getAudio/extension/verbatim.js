// 插件和本机 Verbatim 之间的全部往来：设置、提交链接、查进度、记下提交过的任务。
// 后台（background.js）和弹窗（popup.js）都用这一份，别各写一套。

const DEFAULTS = { server: 'http://localhost:5001', password: '', engine: '', subs: 'auto' };
const JOBS_KEY = 'jobs';
const MAX_JOBS = 30;

export async function getSettings() {
    const s = await chrome.storage.local.get(Object.keys(DEFAULTS));
    return { ...DEFAULTS, ...s, server: (s.server || DEFAULTS.server).replace(/\/+$/, '') };
}

export async function saveSettings(patch) {
    await chrome.storage.local.set(patch);
}

async function api(path, opts = {}) {
    const { server, password } = await getSettings();
    const headers = { ...(opts.headers || {}) };
    if (password) headers.Authorization = 'Bearer ' + password;    // 设了 GETAUDIO_TOKEN / 登录密码时
    let resp;
    try {
        resp = await fetch(server + path, { ...opts, headers });
    } catch (e) {
        const err = new Error('offline');
        err.code = 'offline';
        throw err;
    }
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 401) { const err = new Error('auth'); err.code = 'auth'; throw err; }
    if (!resp.ok) {             // 200 里的 error 是任务本身的失败原因（/api/task），不算请求出错
        const err = new Error(data.error || `HTTP ${resp.status}`);
        err.code = 'server';
        throw err;
    }
    return data;
}

// 视频页的地址整理成「这一个视频」：YouTube 带着 list= 时后端会当播放列表整个展开，
// t= / 跟踪参数也去掉；B 站只留分 P 的 p=。别的网站原样交给 yt-dlp。
export function cleanVideoUrl(raw) {
    let u;
    try { u = new URL(raw); } catch (e) { return raw; }
    const host = u.hostname.replace(/^www\.|^m\./, '');
    if (host === 'youtube.com' && u.pathname === '/watch' && u.searchParams.get('v')) {
        return `https://www.youtube.com/watch?v=${u.searchParams.get('v')}`;
    }
    if (host === 'youtu.be') return `https://youtu.be${u.pathname}`;
    if (host === 'bilibili.com' && /^\/video\//.test(u.pathname)) {
        const p = u.searchParams.get('p');
        return `https://www.bilibili.com${u.pathname.replace(/\/?$/, '/')}${p && p !== '1' ? '?p=' + p : ''}`;
    }
    return raw;
}

export function isWebPage(url) {
    return /^https?:\/\//.test(url || '') && !/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\//.test(url);
}

export function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60;
    const mm = String(m).padStart(h ? 2 : 1, '0'), ss = String(s).padStart(2, '0');
    return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

// 提交一个视频。startSec 给了就只转从那一秒到片尾（后端的 ' @start-' 后缀，时间戳还原成原视频位置）。
export async function submit({ url, title, startSec = null, projectId = '' }) {
    const { engine, subs } = await getSettings();
    const line = cleanVideoUrl(url) + (startSec ? ` @${Math.floor(startSec)}-` : '');
    const data = await api('/api/transcribe_urls', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: line, engine, subs, project_id: projectId || '' }),
    });
    const now = Date.now();
    const jobs = (data.tasks || []).map(t => ({
        task_id: t.task_id, url: t.url, title: t.title || title || t.url,
        start: startSec ? Math.floor(startSec) : null, status: 'pending', progress: null, created: now,
    }));
    const old = await getJobs();
    await chrome.storage.local.set({ [JOBS_KEY]: [...jobs, ...old].slice(0, MAX_JOBS) });
    return jobs;
}

export async function getJobs() {
    return (await chrome.storage.local.get(JOBS_KEY))[JOBS_KEY] || [];
}

export function isOpen(job) {
    return job.status === 'pending' || job.status === 'running';
}

// 查一遍还没完的任务；返回这一轮刚完成 / 刚失败的（后台据此弹通知）。
export async function refreshJobs() {
    const jobs = await getJobs();
    const finished = [];
    for (const job of jobs.filter(isOpen)) {
        try {
            const st = await api('/api/task/' + job.task_id);
            job.status = st.status || job.status;
            job.progress = st.progress;
            job.error = st.error || '';
            if (!isOpen(job)) finished.push(job);
        } catch (e) {
            if (e.code === 'offline') break;         // Verbatim 没开：下一轮再查
            if (/not found/i.test(e.message)) { job.status = 'failed'; job.error = e.message; finished.push(job); }
        }
    }
    const fresh = await getJobs();                   // 查的这段时间里可能又提交了新的：按 id 合并，别覆盖掉
    const byId = Object.fromEntries(jobs.map(j => [j.task_id, j]));
    await chrome.storage.local.set({ [JOBS_KEY]: fresh.map(j => byId[j.task_id] || j) });
    return finished;
}

export async function listNotebooks() {
    const chains = await api('/api/chains');
    return (Array.isArray(chains) ? chains : chains.chains || [])
        .filter(c => c.kind === 'project')
        .map(c => ({ id: c.id, name: c.author || c.name || c.id }));
}

export async function transcriptUrl(taskId) {
    const { server } = await getSettings();
    return `${server}/#/detail/${taskId}`;
}

export async function openVerbatim(hash = '') {
    const { server } = await getSettings();
    await chrome.tabs.create({ url: server + '/' + (hash ? '#/' + hash : '') });
}

// 读当前页面里正在放的视频：放到第几秒、多长。拿不到（没有 video 元素、跨域 iframe）就是 null。
export async function readPlayer(tabId) {
    try {
        const [res] = await chrome.scripting.executeScript({
            target: { tabId },
            func: () => {
                const vids = [...document.querySelectorAll('video')].filter(v => v.duration > 0);
                if (!vids.length) return null;
                const v = vids.sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight))[0];
                return { current: v.currentTime, duration: v.duration };
            },
        });
        return res && res.result;
    } catch (e) {
        return null;
    }
}
