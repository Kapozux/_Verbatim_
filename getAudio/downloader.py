"""
yt-dlp 下载模块：URL（单视频 / 播放列表 / 频道）→ 音频文件。

调用系统 yt-dlp 二进制（brew 版，保持最新，能吃到用户全局配置/cookies），
而不是 venv 里的 python 包——后者被 Python 3.9 锁死在旧版本，
YouTube 的反爬会让旧版直接解析失败。

对外暴露：
  - probe(url, max_videos) -> [{'video_url', 'title', 'video_id', 'thumbnail'}, ...]
  - download_one(target, dest_dir) -> {'path', 'title', 'video_id', 'thumbnail'} 或 None
  - download_audios(...) 仍保留（串行全下），供非链条场景/兼容使用。
"""

import json
import os
import time
import re
import shutil
import subprocess

from config import YTDLP_LANG, YTDLP_COOKIES_FROM_BROWSER

_PROBE_TIMEOUT = 120        # 元数据解析超时（秒）
_DOWNLOAD_TIMEOUT = 1800    # 单个视频音频下载超时（秒）

_YT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def _resolve_ytdlp():
    # brew 版优先：venv 的 PATH 里可能残留 pip 装的旧版 yt-dlp（py3.9 锁旧版），
    # 旧版会被 YouTube 反爬挡掉，必须避开。
    candidates = ['/opt/homebrew/bin/yt-dlp', shutil.which('yt-dlp')]
    for binary in candidates:
        if binary and os.path.exists(binary):
            return binary
    raise RuntimeError('yt-dlp not found. Install it: brew install yt-dlp')


def _lang_args():
    return ['--extractor-args', f'youtube:lang={YTDLP_LANG}'] if YTDLP_LANG else []


def _cookie_args():
    # 借浏览器登录态：绕过 B站 412 风控、抬高 YouTube 限额
    return (['--cookies-from-browser', YTDLP_COOKIES_FROM_BROWSER]
            if YTDLP_COOKIES_FROM_BROWSER else [])


# YouTube 频道主页（无栏目）→ 补 /videos，否则 yt-dlp 会把"视频/直播/短视频"
# 各个栏目 tab 当成条目返回，下载器再拿 tab 去下 = 下载整个频道，永远下不完。
_YT_CHANNEL_ROOT = re.compile(
    r'^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(?:[?#].*)?$'
)

# B站 UP主空间页：砍掉 /video 等子路径和 query，回到裸空间页。
# space.bilibili.com/<uid>/video 会走 BilibiliSpaceVideo 提取器，412 风控更凶；
# 裸 space.bilibili.com/<uid> 更稳。单个视频 www.bilibili.com/video/BV... 不受影响。
_BILI_SPACE = re.compile(r'^(https?://space\.bilibili\.com/\d+)(?:/.*)?$')


def _normalize_url(url):
    url = url or ''
    m = _YT_CHANNEL_ROOT.match(url)
    if m:
        return m.group(1) + '/videos'
    b = _BILI_SPACE.match(url)
    if b:
        return b.group(1)
    return url


def _is_video_entry(e):
    """判断 flat 条目是不是"单个视频"——排除频道 tab / 嵌套播放列表。"""
    if e.get('_type') == 'playlist':
        return False
    if e.get('ie_key') in ('YoutubeTab', 'YoutubeChannel'):
        return False
    vid = e.get('id') or ''
    if vid.startswith('UC') and len(vid) == 24:   # YouTube 频道 ID，不是视频
        return False
    return True


def _thumbnail_for(entry):
    """从 flat 条目里取封面 URL；YouTube 用稳定的 ytimg 兜底。"""
    thumbs = entry.get('thumbnails')
    if isinstance(thumbs, list) and thumbs:
        for t in reversed(thumbs):  # 最后一个通常分辨率最高
            if t.get('url'):
                return t['url']
    if entry.get('thumbnail'):
        return entry['thumbnail']
    vid = entry.get('id') or ''
    if _YT_ID_RE.match(vid):
        return f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
    return ''


def _channel_from_info(info):
    """从 yt-dlp 信息里取频道名 + 头像 URL（尽力而为，取不到就空字符串）。"""
    name = (info.get('channel') or info.get('uploader')
            or info.get('playlist_uploader') or '')
    avatar = ''
    thumbs = info.get('thumbnails')
    if isinstance(thumbs, list):
        for t in thumbs:                     # 频道头像的 thumbnail id 通常含 'avatar'
            if 'avatar' in str(t.get('id', '')).lower() and t.get('url'):
                avatar = t['url']
                break
    return {'name': (name or '').strip(), 'avatar': avatar}


def probe(url, max_videos=None):
    """解析 URL 元数据（不下载）。

    返回 (targets, channel)：
      targets = [{'video_url','title','video_id','thumbnail'}, ...]
      channel = {'name', 'avatar'}
    """
    binary = _resolve_ytdlp()
    url = _normalize_url(url)          # 频道主页 → /videos，避免下成整个频道
    cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
           *_lang_args(), *_cookie_args()]
    if max_videos:
        cmd += ['--playlist-end', str(max_videos)]
    cmd.append(url)

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        err = (result.stderr or '').strip().splitlines()
        detail = err[-1][:200] if err else 'unknown error'
        raise RuntimeError(f'Could not parse link: {detail}')

    info = json.loads(result.stdout)
    channel = _channel_from_info(info)

    if info.get('_type') == 'playlist':
        # 只保留真正的视频条目，滤掉频道 tab / 嵌套播放列表（防跑飞）
        entries = [e for e in (info.get('entries') or []) if e and _is_video_entry(e)]
        if max_videos:
            entries = entries[:max_videos]
        targets = []
        for e in entries:
            targets.append({
                'video_url': e.get('url') or e.get('webpage_url') or e.get('id'),
                'title': e.get('title', ''),
                'video_id': e.get('id', ''),
                'thumbnail': _thumbnail_for(e),
            })
        return targets, channel

    return [{
        'video_url': url,
        'title': info.get('title', ''),
        'video_id': info.get('id', ''),
        'thumbnail': _thumbnail_for(info),
    }], channel


_DOWNLOAD_ATTEMPTS = 3        # B站 412 等间歇性风控：退避重试，绝大多数第二次就过


def download_one(target, dest_dir):
    """下载单个目标的音频，返回 {'path','title','video_id','thumbnail'} 或 None。

    带退避重试：B站 412 / 网络抖动这类间歇失败，隔几秒重试常能过。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    outtmpl = os.path.join(dest_dir, '%(title)s [%(id)s].%(ext)s')
    cmd = [
        binary,
        '-x', '--audio-format', 'mp3', '--audio-quality', '128K',
        '-o', outtmpl,
        '--no-playlist', '--no-warnings', '--quiet',
        *_lang_args(), *_cookie_args(),
        # 下载+后处理完成后打印最终文件路径和元信息，逐行读取
        '--print', 'after_move:filepath',
        '--print', 'after_move:title',
        '--print', 'after_move:id',
        '--no-simulate',
        target['video_url'],
    ]
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
            if len(lines) >= 3 and os.path.isfile(lines[0]):
                return {
                    'path': lines[0],
                    'title': lines[1] or target.get('title') or 'untitled',
                    'video_id': lines[2] or target.get('video_id', ''),
                    'thumbnail': target.get('thumbnail', ''),
                }
        if attempt < _DOWNLOAD_ATTEMPTS:
            time.sleep(4 * attempt)      # 4s, 8s 退避
    return None


_SUB_TIMEOUT = 120


def _collect_srt(dest_dir, vid):
    try:
        names = [n for n in os.listdir(dest_dir)
                 if n.startswith('sub_') and n.endswith('.srt')
                 and (not vid or vid in n)]
    except OSError:
        return None
    return os.path.join(dest_dir, names[0]) if names else None


def _pick_sub_lang(tracks, orig):
    """从可用字幕轨里挑一条：原语言 > 英 > 中(含B站 AI字幕 ai-zh) > 任意。避免下成机翻。"""
    if not tracks:
        return None
    pref = [orig, 'en', 'en-US', 'zh-Hans', 'zh', 'zh-CN', 'zh-Hant',
            'ai-zh', 'ai-en']          # ai-zh/ai-en：B站等平台的 AI 自动字幕
    for code in [c for c in pref if c]:
        if code in tracks:
            return code
    # 兜底：任何 zh 开头(含 ai-zh)的轨优先，再不行取第一条
    for code in tracks:
        if str(code).lower().startswith(('zh', 'ai-zh')):
            return code
    return next(iter(tracks))


def fetch_subtitle(target, dest_dir):
    """抓取视频已有字幕并转 srt。返回 (srt路径, 类型) 或 (None, None)。

    先用一次 -J 拿到视频语言 + 可用字幕清单，只下『原语言那一条轨』——
    避免请求多语言触发 429，也避免下成机器翻译。人工字幕优先，其次自动字幕。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    url = target['video_url']

    # 1) 一次元数据调用，拿语言 + 字幕清单（不下载、不强制 lang，免得偏向翻译轨）
    try:
        r = subprocess.run(
            [binary, '-J', '--skip-download', '--no-playlist', '--no-warnings',
             *_cookie_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
        info = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return None, None

    orig = (info.get('language') or '').split('-')[0]
    manual = info.get('subtitles') or {}
    autos = info.get('automatic_captions') or {}

    kind = chosen = None
    m = _pick_sub_lang(manual, orig)
    if m:
        kind, chosen = 'manual', m
    else:
        a = orig if (orig and orig in autos) else _pick_sub_lang(autos, orig)
        if a:
            kind, chosen = 'auto', a
    if not chosen:
        return None, None

    # 2) 只下这一条轨
    outtmpl = os.path.join(dest_dir, 'sub_%(id)s.%(ext)s')
    flag = '--write-subs' if kind == 'manual' else '--write-auto-subs'
    try:
        subprocess.run(
            [binary, '--skip-download', flag, '--sub-langs', chosen,
             '--convert-subs', 'srt', '-o', outtmpl,
             '--no-playlist', '--no-warnings', '--quiet', *_cookie_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, None
    path = _collect_srt(dest_dir, target.get('video_id', ''))
    return (path, kind) if path else (None, None)


def _fmt_ts(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


_SRT_TS = re.compile(
    r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def parse_srt(path):
    """SRT → [{'timestamp','end','text'}]，格式对齐转写输出。

    YouTube 自动字幕是"滚动累积"格式（同一句反复出现、逐步补全），逐行去重：
    与上一行相同则跳过；是上一行的延伸则替换（保留最长的那版）。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read()
    except OSError:
        return []
    segs = []
    last = None
    for block in re.split(r'\n\s*\n', raw.strip()):
        m = _SRT_TS.search(block)
        if not m:
            continue
        sh, sm, ss, _, eh, em, es, _ = m.groups()
        ts, end = _fmt_ts(int(sh) * 3600 + int(sm) * 60 + int(ss)), \
            _fmt_ts(int(eh) * 3600 + int(em) * 60 + int(es))
        for ln in block.splitlines():
            if _SRT_TS.search(ln) or ln.strip().isdigit():
                continue
            text = re.sub(r'<[^>]+>', '', ln).strip()
            if not text or text == last:
                continue
            if last and text.startswith(last):           # 滚动补全：延伸上一行
                segs[-1] = {'timestamp': segs[-1]['timestamp'], 'end': end, 'text': text}
                last = text
                continue
            if last and last.startswith(text):           # 上一行已包含它
                continue
            segs.append({'timestamp': ts, 'end': end, 'text': text})
            last = text
    return segs


def download_audios(url, dest_dir, max_videos=None, progress_cb=None):
    """（兼容旧接口）串行下载 URL 指向的所有音频，返回成功项列表。"""
    targets, _ = probe(url, max_videos)
    if not targets:
        raise RuntimeError('No downloadable videos at this link')

    total = len(targets)
    results = []
    for idx, target in enumerate(targets):
        if progress_cb:
            progress_cb(idx, total, target.get('title') or target['video_url'])
        item = download_one(target, dest_dir)
        if item:
            results.append(item)
    if progress_cb:
        progress_cb(total, total, '')
    return results
