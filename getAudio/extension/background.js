// 后台：右键菜单、快捷键提交；没完的任务每 30 秒查一次，转好 / 失败弹系统通知，点通知打开那份稿子。
import { T, errorText } from './i18n.js';
import { submit, refreshJobs, getJobs, isOpen, getSettings, transcriptUrl, isWebPage } from './verbatim.js';

const POLL_ALARM = 'verbatim-poll';

chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.removeAll(() => {
        chrome.contextMenus.create({ id: 'page', title: T('menu.page'), contexts: ['page', 'video'] });
        chrome.contextMenus.create({ id: 'link', title: T('menu.link'), contexts: ['link'] });
    });
});

function notify(id, title, message) {
    chrome.notifications.create(id, { type: 'basic', iconUrl: 'icons/icon128.png', title, message });
}

async function startPolling() {
    await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5 });
    // 有字幕的几秒就完：头一分钟密一点查，不等 30 秒的闹钟（service worker 活着就查，被回收了靠闹钟）
    for (let i = 0; i < 12; i++) {
        await new Promise(r => setTimeout(r, 5000));
        if (!(await poll())) return;
    }
}

// 查一轮；返回还有没有没完的
async function poll() {
    for (const job of await refreshJobs()) {
        if (job.status === 'done') notify('done:' + job.task_id, T('note.done'), job.title);
        else notify('failed:' + job.task_id, T('note.failed'), `${job.title}\n${job.error || ''}`.trim());
    }
    const open = (await getJobs()).some(isOpen);
    if (!open) await chrome.alarms.clear(POLL_ALARM);
    return open;
}

async function send(payload) {
    try {
        const jobs = await submit(payload);
        startPolling();
        return { ok: true, jobs };
    } catch (e) {
        const { server } = await getSettings();
        return { ok: false, error: errorText(e, server) };
    }
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    const url = info.menuItemId === 'link' ? info.linkUrl : (tab && tab.url);
    if (!isWebPage(url)) return;
    const res = await send({ url, title: info.menuItemId === 'link' ? (info.selectionText || url) : tab.title });
    if (!res.ok) notify('err:' + Date.now(), T('note.offline'), res.error);
    else notify('sent:' + Date.now(), 'Verbatim', T('popup.sent'));
});

chrome.commands.onCommand.addListener(async (cmd, tab) => {
    if (cmd !== 'transcribe-page' || !tab || !isWebPage(tab.url)) return;
    const res = await send({ url: tab.url, title: tab.title });
    notify('sent:' + Date.now(), res.ok ? 'Verbatim' : T('note.offline'), res.ok ? T('popup.sent') : res.error);
});

chrome.alarms.onAlarm.addListener(a => { if (a.name === POLL_ALARM) poll(); });

chrome.notifications.onClicked.addListener(async id => {
    const [kind, taskId] = id.split(':');
    if (kind === 'done' && taskId) chrome.tabs.create({ url: await transcriptUrl(taskId) });
    chrome.notifications.clear(id);
});

// 弹窗里点按钮也走这里，这样弹窗关了后台照样接着查
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg && msg.type === 'submit') { send(msg.payload).then(reply); return true; }
    if (msg && msg.type === 'poll') { poll().then(() => reply(true)); return true; }
    return false;
});
