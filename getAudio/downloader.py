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
import config

_PROBE_TIMEOUT = 120        # 元数据解析超时（秒）
_PROBE_ATTEMPTS = 3         # B站 412 等间歇风控：解析链接也退避重试
_DOWNLOAD_TIMEOUT = 1800    # 单个视频音频下载超时（秒）

_YT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def _resolve_ytdlp():
    # brew 版优先：venv 的 PATH 里可能残留 pip 装的旧版 yt-dlp（py3.9 锁旧版），
    # 旧版会被 YouTube 反爬挡掉，必须避开。
    candidates = [config.YTDLP_BIN, shutil.which('yt-dlp')]
    for binary in candidates:
        if binary and os.path.exists(binary):
            return binary
    raise RuntimeError('yt-dlp not found. Install it: brew install yt-dlp')


def _lang_args():
    return ['--extractor-args', f'youtube:lang={YTDLP_LANG}'] if YTDLP_LANG else []


def _ffmpeg_location_args():
    # 打包成 App 后 ffmpeg 是内置的，不一定在 PATH 上（收件人机器大概率没装
    # Homebrew）；显式告诉 yt-dlp 去哪找，供切片/转封装/字幕转换等后处理步骤用。
    return ['--ffmpeg-location', os.path.dirname(config.FFMPEG_BIN)]


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


def _normalize_bili_list(url):
    """B站合集/系列链接 → yt-dlp 认识的形式；不是合集则返回 None。

    浏览器地址栏给的是新版 `space.bilibili.com/<uid>/lists?sid=<sid>`，yt-dlp 报
    Unsupported URL；而 _BILI_SPACE 又会把 ?sid= 连同路径一起砍掉、退化成整个空间页
    （变成下这个 UP 的全部视频，不是这个合集）。所以要在它之前转成
    channel/collectiondetail?sid=（合集）或 channel/seriesdetail?sid=（系列）。
    """
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(url or '')
    except ValueError:
        return None
    if 'space.bilibili.com' not in (u.netloc or ''):
        return None
    parts = [p for p in (u.path or '').split('/') if p]
    if not parts or not parts[0].isdigit():
        return None
    uid = parts[0]
    rest = parts[1:]
    if not rest or rest[0] not in ('lists', 'channel'):
        return None
    q = parse_qs(u.query or '')
    sid = (q.get('sid') or [''])[0]
    if not sid:                       # /lists/<sid> 这种把 sid 放在路径里的
        for seg in rest[1:]:
            if seg.isdigit():
                sid = seg
                break
    if not sid:
        return None
    kind = (q.get('type') or [''])[0].lower()
    if 'seriesdetail' in rest or kind == 'series':
        page = 'seriesdetail'
    else:
        page = 'collectiondetail'     # 合集（season）是常见情形，默认它
    return f'https://space.bilibili.com/{uid}/channel/{page}?sid={sid}'


def _normalize_url(url):
    url = url or ''
    m = _YT_CHANNEL_ROOT.match(url)
    if m:
        return m.group(1) + '/videos'
    bl = _normalize_bili_list(url)     # 合集/系列要先认，否则会被下面这条砍成整个空间页
    if bl:
        return bl
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
    """从 yt-dlp 信息里取频道名 + 头像 + 订阅数（尽力而为，取不到留空/0）。"""
    name = (info.get('channel') or info.get('uploader')
            or info.get('playlist_uploader') or '')
    avatar = ''
    thumbs = info.get('thumbnails')
    if isinstance(thumbs, list):
        for t in thumbs:                     # 频道头像的 thumbnail id 通常含 'avatar'
            if 'avatar' in str(t.get('id', '')).lower() and t.get('url'):
                avatar = t['url']
                break
        if not avatar and thumbs and thumbs[0].get('url'):
            avatar = thumbs[0]['url']        # 兜底：第一张缩略图
    followers = (info.get('channel_follower_count')
                 or info.get('subscriber_count') or 0)
    return {'name': (name or '').strip(), 'avatar': avatar,
            'followers': int(followers) if followers else 0}


def channel_followers(url):
    """单独取订阅数。YouTube 在 lang=zh-CN 下会把 channel_follower_count 抹成 None，
    所以这里用**不带 lang**的干净命令探一次（cookies 保留）。取不到返回 0。"""
    try:
        binary = _resolve_ytdlp()
        cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
               *_cookie_args(), *_ffmpeg_location_args(), '--playlist-end', '1', _normalize_url(url)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            fc = json.loads(r.stdout).get('channel_follower_count') or 0
            return int(fc) if fc else 0
    except Exception:  # noqa: BLE001
        pass
    return 0


def probe(url, max_videos=None):
    """解析 URL 元数据（不下载）。

    返回 (targets, channel)：
      targets = [{'video_url','title','video_id','thumbnail'}, ...]
      channel = {'name', 'avatar'}
    """
    binary = _resolve_ytdlp()
    url = _normalize_url(url)          # 频道主页 → /videos，避免下成整个频道
    cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
           *_lang_args(), *_cookie_args(), *_ffmpeg_location_args()]
    if max_videos:
        cmd += ['--playlist-end', str(max_videos)]
    cmd.append(url)

    # B站 412 / 网络抖动这类间歇性风控：退避重试（download_one 早就这么干，
    # probe 之前漏了，一次 412 就把整条链判死）。
    result = None
    for attempt in range(1, _PROBE_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            break
        if attempt < _PROBE_ATTEMPTS:
            time.sleep(4 * attempt)     # 4s, 8s 退避
    if result is None or result.returncode != 0 or not result.stdout.strip():
        err = (result.stderr or '').strip().splitlines() if result else []
        detail = err[-1][:200] if err else 'timeout / unknown error'
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
                'view_count': int(e.get('view_count') or 0),  # flat 常为 0，B站等有时给
            })
        # B站空间页等 flat 探测拿不到频道名（entries 连 title 都是空的）——
        # 兜底：对第一个视频做一次全量探测，用它的 uploader 当频道名/头像。
        if not channel.get('name') and targets:
            try:
                _, ch2 = probe(targets[0]['video_url'])   # 单视频 → 走下面的全量分支
                if ch2.get('name'):
                    channel['name'] = ch2['name']
                    if not channel.get('avatar') and ch2.get('avatar'):
                        channel['avatar'] = ch2['avatar']
            except Exception:  # noqa: BLE001  探测失败不影响主流程
                pass
        return targets, channel

    return [{
        'video_url': url,
        'title': info.get('title', ''),
        'video_id': info.get('id', ''),
        'thumbnail': _thumbnail_for(info),
    }], channel


_DOWNLOAD_ATTEMPTS = 3        # B站 412 等间歇性风控：退避重试，绝大多数第二次就过

# 每次重试换一档音质，而不是原样重试同一条命令。
#
# 踩过的真实案例：某条 B 站视频，yt-dlp 默认挑的「最佳音质」那一档（175kbps）
# 被分配到的 CDN 边缘节点直接 404——节点本身挂了，跟账号/网络/cookie 都无关，
# 同一视频的其它音质档（66k/96k）分到的是别的节点，一切正常。yt-dlp 的格式
# 回退语法 `-f "bestaudio/worstaudio"` 在这种情况下**不会生效**：那只在「格式
# 不存在」时回退，格式存在、只是下载 URL 本身挂了不算数，照样直接报错退出。
# 所以只能在重试循环里手动换一次 -f，逼它问一条不同的音轨（大概率分到别的
# CDN 节点）。转写用途对音质要求很低，worstaudio 完全够用。
#
# 首选 m4a 音轨：YouTube / B站 都有现成的 m4a（AAC），拿到就是最终文件、零转码；
# 没有 m4a 才退到平台给的最佳音轨（多半是 opus，落成 .opus，下游同样直接认）。
_FORMAT_ATTEMPTS = ('bestaudio[ext=m4a]/bestaudio', 'worstaudio',
                    'bestaudio[ext=m4a]/bestaudio')


def download_one(target, dest_dir, section=None):
    """下载单个目标的音频，返回 {'path','title','video_id','thumbnail'} 或 None。

    带退避重试：B站 412 / 网络抖动这类间歇失败，隔几秒重试常能过；
    也会在重试时换一档音质，应对「格式存在但 CDN 节点挂了」这种单纯换个
    格式就能绕过、重试同一格式却怎么都过不去的情况（见 _FORMAT_ATTEMPTS）。
    section: 可选时间段（yt-dlp --download-sections 的值，如 '*600-1500'），
             只下载/切出那一段音频，转写成本随之下降。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    outtmpl = os.path.join(dest_dir, '%(title)s [%(id)s].%(ext)s')
    section_args = ['--download-sections', section] if section else []

    def build_cmd(fmt):
        return [
            binary,
            *(['-f', fmt] if fmt else []),
            # -x 不带 --audio-format：默认 best = 只抽音轨、不转码。下载到的 opus/m4a
            # 本来就是压缩好的，之前强制转 mp3 128K 是每个视频白跑一遍 ffmpeg，体积还不降。
            '-x',
            '-N', '4',                      # 分片并发下载，长视频的 DASH 分片明显更快
            '-o', outtmpl,
            '--no-playlist', '--no-warnings', '--quiet',
            *section_args,
            *_lang_args(), *_cookie_args(), *_ffmpeg_location_args(),
            # 下载+后处理完成后打印最终文件路径和元信息，逐行读取
            '--print', 'after_move:filepath',
            '--print', 'after_move:title',
            '--print', 'after_move:id',
            '--print', 'after_move:view_count',
            # 博主名：channel 优先、uploader 兜底；都没有打印 '-' 占位（空行会被过滤掉，打乱行序）
            '--print', 'after_move:%(channel,uploader|-)s',
            '--no-simulate',
            target['video_url'],
        ]

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        fmt = _FORMAT_ATTEMPTS[(attempt - 1) % len(_FORMAT_ATTEMPTS)]
        cmd = build_cmd(fmt)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
            if len(lines) >= 3 and os.path.isfile(lines[0]):
                vc = 0
                if len(lines) >= 4:
                    try:
                        vc = int(lines[3])
                    except (ValueError, TypeError):
                        vc = 0
                uploader = lines[4].strip() if len(lines) >= 5 else ''
                if uploader in ('-', 'NA'):
                    uploader = ''
                return {
                    'path': lines[0],
                    'title': lines[1] or target.get('title') or 'untitled',
                    'video_id': lines[2] or target.get('video_id', ''),
                    'thumbnail': target.get('thumbnail', ''),
                    'view_count': vc or int(target.get('view_count') or 0),
                    'uploader': uploader,
                }
        if attempt < _DOWNLOAD_ATTEMPTS:
            time.sleep(4 * attempt)      # 4s, 8s 退避
    # B站「活动页」视频（入选盛典/榜单等）：普通 /video/BVxxx 页 yt-dlp 解析不了
    # （Unable to extract initial state），但同一支片子的活动页 URL 可以。换它再试一次。
    fest = _bili_festival_url(target.get('video_url'))
    if fest and fest != target.get('video_url'):
        return download_one({**target, 'video_url': fest}, dest_dir, section=section)
    return None


_BV_RE = re.compile(r'(BV[0-9A-Za-z]{10})')


def _bili_festival_url(url):
    """查这支 B站视频的活动页地址（官方 API 的 festival_jump_url）；没有则 None。"""
    if 'bilibili.com' not in (url or ''):
        return None
    m = _BV_RE.search(url or '')
    if not m:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f'https://api.bilibili.com/x/web-interface/view?bvid={m.group(1)}',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return ((data.get('data') or {}).get('festival_jump_url') or '').strip() or None
    except Exception:  # noqa: BLE001
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


# 字幕语言匹配：用户/自动选定的「想要的语言」（zh / en / ja …）对平台字幕轨代码做宽松匹配。
# zh 要能匹配 zh-Hans / zh-CN / zh-Hant / ai-zh（B站 AI 字幕）；en 匹配 en-US / en-orig 等。
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_KANA_RE = re.compile(r'[\u3040-\u30ff]')
_HANGUL_RE = re.compile(r'[\uac00-\ud7af]')


def _lang_matches(code, want):
    c = (code or '').lower()
    w = (want or '').lower()
    if not c or not w:
        return False
    if c.startswith('ai-'):            # B站等平台的 AI 自动字幕：ai-zh / ai-en
        c = c[3:]
    if c.endswith('-orig'):            # YouTube 原声自动字幕：en-orig / zh-Hans-orig
        c = c[:-5]
    return c == w or c.split('-')[0] == w


def _detect_orig_lang(info):
    """猜视频的原始语言：平台标注 > YouTube 自动字幕里的 -orig 轨 > 标题文字系统。

    返回 'zh' / 'en' / 'ja' … 或 ''（实在猜不到）。猜不到时调用方**不选字幕、直接转写**，
    绝不再随便挑一条别的语言——之前的做法是在语言未知时先挑 en，YouTube 的自动字幕
    清单里有一百多种机翻语言，中文视频就这样拿到了英文机翻。
    """
    lang = (info.get('language') or '').split('-')[0].lower()
    if lang:
        return lang
    for code in (info.get('automatic_captions') or {}):
        if str(code).endswith('-orig'):
            return str(code)[:-5].split('-')[0].lower()
    title = info.get('title') or ''
    if _KANA_RE.search(title):
        return 'ja'
    if _HANGUL_RE.search(title):
        return 'ko'
    if _CJK_RE.search(title):
        return 'zh'
    return ''


def _choose_track(manual, autos, want):
    """在人工字幕 / 自动字幕里找「想要的语言」那一条。返回 (kind, code) 或 (None, None)。

    人工 > 自动；自动里优先 -orig（真正的语音识别轨，不是机翻）。
    找不到该语言就返回空——调用方落回音频转写，而不是换一种语言凑合。
    """
    for code in manual:
        if _lang_matches(code, want):
            return 'manual', code
    for code in autos:
        if str(code).endswith('-orig') and _lang_matches(code, want):
            return 'auto', code
    for code in autos:
        if _lang_matches(code, want):
            return 'auto', code
    return None, None


def fetch_subtitle(target, dest_dir, lang='auto'):
    """抓取视频已有字幕并转 srt。返回 (srt路径, 类型, meta) 或 (None, None, meta)。

    lang：'auto' = 只要视频原始语言那一条（猜不出原语言就不用字幕）；
          'zh' / 'en' / 'ja' … = 只要这种语言（人工优先，其次平台自动/机翻字幕）；
          都找不到 → 返回 None，让调用方走下载 + 转写。**绝不返回别的语言的字幕。**

    先用一次 -J 拿到视频语言 + 可用字幕清单，只下选中的那一条轨——
    避免请求多语言触发 429。

    meta = {'title', 'video_id', 'sub_lang', 'want'}：顺手把这次元数据调用里的标题/id
    一并带回去——调用方（如直接贴链接转写）事先并不知道这些，省得再单独探测一次。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    url = target['video_url']

    # 1) 一次元数据调用，拿语言 + 字幕清单（不下载、不强制 lang，免得偏向翻译轨）
    try:
        r = subprocess.run(
            [binary, '-J', '--skip-download', '--no-playlist', '--no-warnings',
             *_cookie_args(), *_ffmpeg_location_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
        info = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return None, None, None

    meta = {'title': info.get('title'), 'video_id': info.get('id') or target.get('video_id', ''),
            'duration': info.get('duration')}       # 秒；给字幕覆盖率门槛用
    manual = info.get('subtitles') or {}
    autos = info.get('automatic_captions') or {}

    want = _detect_orig_lang(info) if (lang or 'auto') == 'auto' else str(lang).lower()
    meta['want'] = want
    if not want:
        return None, None, meta          # 原语言猜不到 → 不赌，去转写
    kind, chosen = _choose_track(manual, autos, want)
    if not chosen:
        return None, None, meta
    meta['sub_lang'] = chosen

    # 2) 只下这一条轨
    outtmpl = os.path.join(dest_dir, 'sub_%(id)s.%(ext)s')
    flag = '--write-subs' if kind == 'manual' else '--write-auto-subs'
    try:
        subprocess.run(
            [binary, '--skip-download', flag, '--sub-langs', chosen,
             '--convert-subs', 'srt', '-o', outtmpl,
             '--no-playlist', '--no-warnings', '--quiet', *_cookie_args(), *_ffmpeg_location_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, None, meta
    path = _collect_srt(dest_dir, meta['video_id'])
    return (path, kind, meta) if path else (None, None, meta)


def _fmt_ts(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


_SRT_TS = re.compile(
    r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


_CUE_JUNK = re.compile(r'\[(?:\\h|\s|_)*\]|\\h')       # WebVTT 的 \h 转义、[__] 残留
_SENT_END = re.compile(r'[.!?。！？…]["”’)）]?$')


def _ts_to_sec(ts):
    try:
        parts = [int(x) for x in str(ts).split(':')]
    except ValueError:
        return 0
    sec = 0
    for x in parts:
        sec = sec * 60 + x
    return sec


_TAG_ONLY = re.compile(r'^[\s\[\(（【]*[^\]\)）】]{0,20}[\]\)）】][\s♪]*$')   # [Music] / [Applause] / （音乐）


def subtitle_usable(segs, duration):
    """字幕够不够格替代转写。返回 (是否可用, 原因字符串)。

    踩过的坑：自动字幕中途断掉、或下载被限流截断，只拿到开头一小段；之前只判断
    "有没有"，残缺字幕就被当成完整转写存了（昆明火车站那条 1087 字、长沙饭店那条
    7550 字对应完整转写 34363 字）。门槛：
      - 覆盖率：最后一条 cue 的结束时间 ≥ 视频时长的 80%（时长未知则跳过这条）；
      - 密度：每分钟 ≥ 40 字（时长未知则总字数 ≥ 200）；[Music] 这类纯标签不算字。
    """
    if not segs:
        return False, 'no cues'
    text = ''.join(s.get('text', '') for s in segs if not _TAG_ONLY.match(s.get('text', '') or ''))
    chars = len(re.sub(r'\s+', '', text))
    last_end = max(_ts_to_sec(s.get('end') or s.get('timestamp')) for s in segs)
    if duration and duration > 0:
        cover = last_end / float(duration)
        if cover < 0.8:
            return False, f'covers only {int(cover * 100)}% of the video'
        if chars / (float(duration) / 60.0) < 40:
            return False, f'too sparse ({chars} chars for {int(duration // 60)} min)'
        return True, f'covers {int(cover * 100)}%'
    if chars < 200:
        return False, f'too short ({chars} chars)'
    return True, 'ok'


def merge_caption_cues(segs, max_chars=200, max_span=15):
    """把两三秒一条的字幕 cue 合并成句子级片段，读起来像转写稿而不是 248 行半句话。

    合并规则：一直往当前片段里追加，直到句末标点、累计超过 max_chars、
    或时间跨度超过 max_span 秒。时间戳取第一条 cue 的。顺手清掉 \h、[__] 这类字幕格式残留。
    """
    out, buf, start = [], None, 0
    for seg in segs or []:
        text = re.sub(r'\s+', ' ', _CUE_JUNK.sub(' ', seg.get('text') or '')).strip()
        if not text:
            continue
        if buf is None:
            buf = {'timestamp': seg.get('timestamp'), 'end': seg.get('end'), 'text': text}
            start = _ts_to_sec(seg.get('timestamp'))
        else:
            joiner = '' if ord(buf['text'][-1]) > 0x2E7F else ' '     # 中日韩不加空格
            buf['text'] += joiner + text
            buf['end'] = seg.get('end') or buf['end']
        span = _ts_to_sec(seg.get('end') or seg.get('timestamp')) - start
        if _SENT_END.search(buf['text']) or len(buf['text']) >= max_chars or span >= max_span:
            out.append(buf)
            buf = None
    if buf:
        out.append(buf)
    return out


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
