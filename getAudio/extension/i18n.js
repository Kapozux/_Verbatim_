// 插件的中英文案（跟着浏览器语言走；跟 static/i18n.js 一样，界面上不硬编码文案）。
const DICT = {
    zh: {
        'menu.page': '用 Verbatim 转写这个视频',
        'menu.link': '用 Verbatim 转写这个链接',
        'popup.notVideo': '这一页不是网页视频，打开一个视频页再点。',
        'popup.whole': '转写整个视频',
        'popup.fromHere': '从 {t} 开始转',
        'popup.notebook': '转完放进',
        'popup.noNotebook': '只放资料库',
        'popup.recent': '最近提交',
        'popup.none': '还没提交过。',
        'popup.openApp': '打开 Verbatim',
        'popup.settings': '设置',
        'popup.server': 'Verbatim 地址',
        'popup.password': '密码 / 令牌（没设就空着）',
        'popup.engine': '转写引擎',
        'popup.engine.default': '跟 Verbatim 的默认走（现在是订阅里的 Gemini Flash）',
        'popup.engine.agy_flash': 'Gemini Flash（订阅）',
        'popup.engine.agy_argon': 'Gemini Argon（订阅 · 最准）',
        'popup.engine.gemini35': 'Gemini 3.5',
        'popup.engine.gemini': 'Gemini',
        'popup.engine.qwenasr': 'Qwen（分说话人）',
        'popup.engine.whisper': '本机 Whisper（免费、慢）',
        'popup.subs': '视频自带字幕',
        'popup.subs.auto': '有字幕就直接用（几秒、不花钱）',
        'popup.subs.off': '一律听音频转写',
        'popup.save': '保存',
        'popup.saved': '已保存',
        'popup.sent': '已交给 Verbatim，后台转写中',
        'status.pending': '排队中',
        'status.running': '转写中',
        'status.done': '转好了',
        'status.failed': '失败',
        'err.offline': '连不上 Verbatim：先在电脑上把它打开（{server}）。',
        'err.auth': 'Verbatim 设了密码：在插件设置里填上。',
        'note.done': '转好了',
        'note.failed': '转写失败',
        'note.offline': '连不上 Verbatim',
    },
    en: {
        'menu.page': 'Transcribe this video with Verbatim',
        'menu.link': 'Transcribe this link with Verbatim',
        'popup.notVideo': "This tab isn't a web video. Open a video page and click again.",
        'popup.whole': 'Transcribe the whole video',
        'popup.fromHere': 'Start from {t}',
        'popup.notebook': 'Add to',
        'popup.noNotebook': 'Library only',
        'popup.recent': 'Recent',
        'popup.none': 'Nothing sent yet.',
        'popup.openApp': 'Open Verbatim',
        'popup.settings': 'Settings',
        'popup.server': 'Verbatim address',
        'popup.password': 'Password / token (leave empty if none)',
        'popup.engine': 'Engine',
        'popup.engine.default': 'Verbatim default (Gemini Flash on your plan)',
        'popup.engine.agy_flash': 'Gemini Flash (subscription)',
        'popup.engine.agy_argon': 'Gemini Argon (subscription · most accurate)',
        'popup.engine.gemini35': 'Gemini 3.5',
        'popup.engine.gemini': 'Gemini',
        'popup.engine.qwenasr': 'Qwen (speakers)',
        'popup.engine.whisper': 'Local Whisper (free, slow)',
        'popup.subs': "Video's own subtitles",
        'popup.subs.auto': 'Use them when present (seconds, free)',
        'popup.subs.off': 'Always transcribe the audio',
        'popup.save': 'Save',
        'popup.saved': 'Saved',
        'popup.sent': 'Sent to Verbatim. Transcribing in the background.',
        'status.pending': 'Queued',
        'status.running': 'Transcribing',
        'status.done': 'Done',
        'status.failed': 'Failed',
        'err.offline': "Can't reach Verbatim. Start it on this computer first ({server}).",
        'err.auth': 'Verbatim has a password set. Enter it in the extension settings.',
        'note.done': 'Transcript ready',
        'note.failed': 'Transcription failed',
        'note.offline': "Can't reach Verbatim",
    },
};

const LANG = (navigator.language || 'en').toLowerCase().startsWith('zh') ? 'zh' : 'en';

export function T(key, vars = {}) {
    const s = DICT[LANG][key] ?? DICT.en[key] ?? key;
    return s.replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? '');
}

export function errorText(e, server) {
    if (e && e.code === 'offline') return T('err.offline', { server });
    if (e && e.code === 'auth') return T('err.auth');
    return String((e && e.message) || e);
}
