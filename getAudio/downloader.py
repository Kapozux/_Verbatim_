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


def _normalize_url(url):
    m = _YT_CHANNEL_ROOT.match(url or '')
    return m.group(1) + '/videos' if m else url


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


def probe(url, max_videos=None):
    """解析 URL 元数据（不下载），返回目标视频列表。

    每个元素：{'video_url', 'title', 'video_id', 'thumbnail'}
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
        return targets

    return [{
        'video_url': url,
        'title': info.get('title', ''),
        'video_id': info.get('id', ''),
        'thumbnail': _thumbnail_for(info),
    }]


def download_one(target, dest_dir):
    """下载单个目标的音频，返回 {'path','title','video_id','thumbnail'} 或 None。"""
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
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    path, real_title, video_id = lines[0], lines[1], lines[2]
    if not os.path.isfile(path):
        return None
    return {
        'path': path,
        'title': real_title or target.get('title') or 'untitled',
        'video_id': video_id or target.get('video_id', ''),
        'thumbnail': target.get('thumbnail', ''),
    }


def download_audios(url, dest_dir, max_videos=None, progress_cb=None):
    """（兼容旧接口）串行下载 URL 指向的所有音频，返回成功项列表。"""
    targets = probe(url, max_videos)
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
