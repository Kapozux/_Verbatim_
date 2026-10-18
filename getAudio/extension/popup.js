// 工具栏弹窗：当前页一键转写（整段 / 从播放器当前时间开始）、选放进哪个笔记本、看最近提交的进度、设置。
import { T, errorText } from './i18n.js';
import {
    getSettings, saveSettings, getJobs, isOpen, listNotebooks, readPlayer, fmtTime,
    transcriptUrl, openVerbatim, isWebPage,
} from './verbatim.js';

const $ = id => document.getElementById(id);
let tab = null;
let player = null;

document.querySelectorAll('[data-t]').forEach(el => { el.textContent = T(el.dataset.t); });
$('nb-label').textContent = T('popup.notebook');
$('recent-h').textContent = T('popup.recent');
$('open-app').textContent = T('popup.openApp');
$('whole-btn').textContent = T('popup.whole');
$('settings-btn').title = T('popup.settings');

function showMsg(text, kind) {
    const m = $('msg');
    m.textContent = text;
    m.className = 'msg ' + kind;
}

async function loadNotebooks() {
    const sel = $('notebook');
    sel.innerHTML = '';
    sel.add(new Option(T('popup.noNotebook'), ''));
    try {
        for (const nb of await listNotebooks()) sel.add(new Option(nb.name, nb.id));
        const { lastNotebook } = await chrome.storage.local.get('lastNotebook');
        if (lastNotebook && [...sel.options].some(o => o.value === lastNotebook)) sel.value = lastNotebook;
        return true;
    } catch (e) {
        showMsg(errorText(e, (await getSettings()).server), 'bad');
        return false;
    }
}

async function renderJobs() {
    const ul = $('jobs');
    const jobs = await getJobs();
    ul.innerHTML = '';
    if (!jobs.length) {
        const li = document.createElement('li');
        li.className = 'empty';
        li.textContent = T('popup.none');
        ul.append(li);
        return;
    }
    for (const job of jobs.slice(0, 12)) {
        const li = document.createElement('li');
        li.className = job.status;
        const t = document.createElement('span');
        t.className = 't';
        t.textContent = (job.start ? `[${fmtTime(job.start)}–] ` : '') + job.title;
        t.title = job.error || job.url;
        const b = document.createElement('span');
        b.className = 'badge ' + job.status;
        b.textContent = T('status.' + job.status) + (job.status === 'running' && job.progress != null ? ` ${job.progress}%` : '');
        li.append(t, b);
        if (job.status === 'done') li.addEventListener('click', async () => chrome.tabs.create({ url: await transcriptUrl(job.task_id) }));
        ul.append(li);
    }
}

async function send(startSec) {
    const btns = [$('whole-btn'), $('here-btn')];
    btns.forEach(b => { b.disabled = true; });
    const projectId = $('notebook').value;
    await chrome.storage.local.set({ lastNotebook: projectId });
    const res = await chrome.runtime.sendMessage({
        type: 'submit', payload: { url: tab.url, title: tab.title, startSec, projectId },
    });
    btns.forEach(b => { b.disabled = false; });
    if (res && res.ok) showMsg(T('popup.sent'), 'ok');
    else showMsg((res && res.error) || 'Error', 'bad');
    renderJobs();
}

async function init() {
    [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const ok = tab && isWebPage(tab.url);
    $('page-title').textContent = ok ? tab.title : '';
    if (!ok) {
        $('not-video').textContent = T('popup.notVideo');
        $('not-video').classList.remove('hidden');
        $('actions').classList.add('hidden');
    } else {
        loadNotebooks();
        player = await readPlayer(tab.id);
        // 看到一半才想转：给一个「从这里开始」（开头几秒、快放完的不给）
        if (player && player.current > 5 && player.duration - player.current > 10) {
            $('here-btn').textContent = T('popup.fromHere', { t: fmtTime(player.current) });
            $('here-btn').classList.remove('hidden');
        }
    }
    renderJobs();
    if ((await getJobs()).some(isOpen)) chrome.runtime.sendMessage({ type: 'poll' }).then(renderJobs);
}

$('whole-btn').addEventListener('click', () => send(null));
$('here-btn').addEventListener('click', () => send(player && player.current));
$('open-app').addEventListener('click', () => openVerbatim('tab/library'));

$('settings-btn').addEventListener('click', async () => {
    const open = $('settings').classList.toggle('hidden') === false;
    $('main').classList.toggle('hidden', open);
    $('settings-btn').classList.toggle('on', open);
    if (open) {
        const s = await getSettings();
        $('s-server').value = s.server;
        $('s-password').value = s.password;
        $('s-engine').value = s.engine;
        $('s-subs').value = s.subs;
    }
});

$('s-save').addEventListener('click', async () => {
    await saveSettings({
        server: $('s-server').value.trim() || 'http://localhost:5001',
        password: $('s-password').value,
        engine: $('s-engine').value,
        subs: $('s-subs').value,
    });
    $('settings').classList.add('hidden');
    $('main').classList.remove('hidden');
    $('settings-btn').classList.remove('on');
    $('msg').classList.add('hidden');
    if (tab && isWebPage(tab.url)) loadNotebooks();
});

// 弹窗开着时每 3 秒刷新一下进度（后台在查，这里只读存下来的状态）
chrome.storage.onChanged.addListener(changes => { if (changes.jobs) renderJobs(); });
setInterval(() => chrome.runtime.sendMessage({ type: 'poll' }), 3000);

init();
