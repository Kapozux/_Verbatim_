"""
转写「证伪层」——不相信任何一段文字，除非它站得住。

ASR（Whisper / Gemini）在静音、杂乱、低置信处有三种通病：
  1. 静音幻听：对着没人说话的时段编出成片的「嗯。嗯。嗯。/ 然後。我。我。」
  2. 复读死循环：把同一句话逐字反复吐十几遍
  3. 坏时间戳：混进 [0m3s30m433ms] 之类的畸形 token

本模块在转写「之后」做后处理，把这些清掉。核心原则：
  **只有多个信号都指向「假」才删；单一信号存疑就保留。**
  绝不删除有独特实义的内容——哪怕它很短（「OK。」「对。」照样留）。

可选：传入音频的静音时间轴（detect_silence），落在静音里的可疑短段
才删——这是最硬的「证伪」证据（你说的「那 4-6 分钟大概率是空的」），
避免仅凭文字误杀真人的低声细语。

输入 / 输出都是 [{'timestamp': 'HH:MM:SS', 'text': str}, ...]（Gemini 系稿）。
"""

import os
import re
import shutil
import subprocess
from collections import Counter

# —— 阈值（保守）——
_MICRO_MAX = 4        # 「核心字数 <= 4」算微段（嗯/然後/我/对…）
_SHORT_MAX = 12       # 「核心字数 <= 12」算短句（含 `我再拍一次` 这种 5~11 字的循环词）
_FILLER_RUN_MIN = 6   # 连续微段成片 >= 6 且只在少数几个词里打转 → 整段丢
_DUP_COLLAPSE_MIN = 2 # 连续「完全相同的短核心」>= 2 → 只留 1 条
_LOOP_MIN_CORE = 12   # 长句（核心 >= 12 字）
_LOOP_MIN_COUNT = 3   # 逐字重复 >= 3 次 → 只留首次
# 「短句循环」判定：一段连续短句里，不同核心种类占比极低 = 死循环打转
_LOOP_RUN_MIN = 8         # 连续短句成片至少这么长才考虑
_LOOP_DIVERSITY = 0.35    # 不同核心数 / 段数 <= 此值 → 判为循环
_LOOP_DROP_ALL = 5        # 循环片里，某短句重复 >= 此次数 → 全删（含首条）

# 常见语气词/口水词（仅在「成片重复」时才据此判假，单独出现一律保留）
_FILLER_CHARS = set('嗯呃啊哦唔呐呗哈嘛呀么呢哎诶嗨欸')

_TS_RE = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?$')
# 文字里漏出来的畸形时间戳残片，如 [0m3s30m433ms]、[00:1a]
_BROKEN_TS_IN_TEXT = re.compile(r'\[\s*\d[0-9a-zA-Z:.\s]*m?s?\]')


def _core(text):
    """去掉标点和空白，留下判断用的核心串。"""
    return re.sub(r'[\s\W_]+', '', text or '', flags=re.UNICODE)


def _is_micro(seg):
    return len(_core(seg['text'])) <= _MICRO_MAX


def _looks_filler(core):
    """核心串是否全由语气词（可含少量『然後/我/好/对』这类打转词）构成。"""
    if not core:
        return True
    return all(ch in _FILLER_CHARS for ch in core)


def _clean_text(text):
    """剥掉文字里漏出的畸形时间戳残片；首尾清一下。"""
    return _BROKEN_TS_IN_TEXT.sub('', text or '').strip()


def _ts_to_seconds(ts):
    parts = (ts or '').split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def _in_silence(sec, silence_intervals):
    if sec is None or not silence_intervals:
        return False
    for a, b in silence_intervals:
        if a <= sec <= b:
            return True
    return False


def clean_transcript(segments, silence_intervals=None):
    """清洗一份转写稿，返回 (clean_segments, report)。

    report = {'removed': int, 'filler_runs': int, 'loops': int,
              'bad_ts': int, 'silence_dropped': int}
    绝不原地改传入的 list。
    """
    report = {'removed': 0, 'filler_runs': 0, 'loops': 0,
              'bad_ts': 0, 'silence_dropped': 0}

    # —— 0) 逐段清文字 + 修时间戳 ——
    segs = []
    for s in segments:
        text = _clean_text(s.get('text', ''))
        if not text:
            report['removed'] += 1
            continue
        ts = s.get('timestamp', '')
        if not _TS_RE.match(ts or ''):
            report['bad_ts'] += 1
            # 时间戳坏了不丢内容：继承上一条的时间戳
            ts = segs[-1]['timestamp'] if segs else '00:00:00'
        segs.append({'timestamp': ts, 'text': text})

    n = len(segs)
    drop = [False] * n

    # —— 1) 静音掩码：落在静音里的「微段」大概率是幻听 ——
    if silence_intervals:
        for i, s in enumerate(segs):
            if _is_micro(s) and _in_silence(_ts_to_seconds(s['timestamp']), silence_intervals):
                drop[i] = True
                report['silence_dropped'] += 1

    # —— 2) 成片幻听 / 短句死循环：在「够长的连续短句片」里逐段判 ——
    #    片内多样性极低（翻来覆去就那几个短词）才动手：
    #      · 纯语气词（嗯/啊…）             → 丢
    #      · 高频重复（>= _LOOP_DROP_ALL 次）→ 全丢（含首条，`我再拍一次`×几百就是它）
    #      · 中频重复（2~4 次）             → 只留首条
    #    片内唯一出现的实义短句（如「好，青和」「那要評評」）一律保留。
    i = 0
    while i < n:
        if len(_core(segs[i]['text'])) > _SHORT_MAX:
            i += 1
            continue
        j = i
        while j < n and len(_core(segs[j]['text'])) <= _SHORT_MAX:
            j += 1
        run = range(i, j)
        run_len = j - i
        cnt = Counter(_core(segs[k]['text']) for k in run)
        is_loop = (run_len >= _LOOP_RUN_MIN
                   and len(cnt) / run_len <= _LOOP_DIVERSITY)
        # 旧规则兜底：短小的纯微段片（长度 6~7、多样性没那么低）
        is_micro_run = (run_len >= _FILLER_RUN_MIN
                        and all(len(_core(segs[k]['text'])) <= _MICRO_MAX for k in run))
        if is_loop or is_micro_run:
            seen = set()
            fired = False
            for k in run:
                c = _core(segs[k]['text'])
                drop_it = False
                if _looks_filler(c):
                    drop_it = True
                elif cnt[c] >= _LOOP_DROP_ALL:
                    drop_it = True
                elif cnt[c] >= 2:
                    drop_it = c in seen
                    seen.add(c)
                if drop_it and not drop[k]:
                    drop[k] = True
                    report['removed'] += 1
                    fired = True
            if fired:
                report['filler_runs'] += 1
        i = j

    # —— 3) 连续相同短核心折叠：留 1 条 ——
    run_start = 0
    for i in range(1, n + 1):
        same = (i < n and not drop[i] and not drop[run_start]
                and _core(segs[i]['text']) == _core(segs[run_start]['text'])
                and len(_core(segs[i]['text'])) <= _MICRO_MAX)
        if not same:
            if i - run_start >= _DUP_COLLAPSE_MIN:
                for k in range(run_start + 1, i):
                    if not drop[k]:
                        drop[k] = True
                        report['removed'] += 1
            run_start = i

    # —— 4) 长句复读去重：同一长核心出现 >= N 次 → 只留首次 ——
    seen = {}
    for i, s in enumerate(segs):
        if drop[i]:
            continue
        c = _core(s['text'])
        if len(c) >= _LOOP_MIN_CORE:
            seen.setdefault(c, []).append(i)
    for c, idxs in seen.items():
        if len(idxs) >= _LOOP_MIN_COUNT:
            for i in idxs[1:]:
                if not drop[i]:
                    drop[i] = True
                    report['removed'] += 1
            report['loops'] += 1

    clean = [segs[i] for i in range(n) if not drop[i]]
    return clean, report


def detect_silence(filepath, noise_db=-35, min_silence=2.0):
    """用 ffmpeg silencedetect 返回静音区间 [(start_sec, end_sec), ...]。

    best-effort：ffmpeg 缺失或出错就返回 []（退化为纯文字证伪）。
    noise_db 以下、持续 min_silence 秒以上判为静音。
    """
    ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
    if not os.path.exists(ffmpeg) or not os.path.isfile(filepath):
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, '-nostats', '-i', filepath,
             '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=600,
        )
    except Exception:
        return []
    intervals = []
    start = None
    for line in (proc.stderr or '').splitlines():
        m = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m and start is not None:
            intervals.append((max(0.0, start), float(m.group(1))))
            start = None
    return intervals
