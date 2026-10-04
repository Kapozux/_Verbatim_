"""逐词时间轴（ADR 0012）：出处跳到原话第一个词真正出现的那一刻。

转写正文（Gemini 写的）读起来最好，但时间点是模型写的、一段二三十秒，原话在段中间时跳过去要先听十几秒别的；
所以每期另存一条「逐词时间轴」只管定位，正文不动：

    results/<tid>/words.json  {"source": "youtube" | "qwen" | "whisper", "lang": "en", "words": [[秒, "词"], ...]}

来源按准度：YouTube 字幕（json3 每个词带毫秒偏移）> 阿里云 Qwen 自带的句 / 词时间 > 本机 Whisper 对齐。
找不到原话就返回 None，调用方保留原来的时间——不估算。
"""
import difflib
import json
import os
import queue as _queue
import random
import re
import threading
import time

import config

_TOKEN_RE = re.compile(r"[一-鿿㐀-䶿]|[a-z0-9]+(?:'[a-z]+)?")
_FRAG_RE = re.compile(r'…+|\.{3,}|⋯+')
_cache = {}


def tokens(text):
    """→ 比对用的小写词；中文按字。撇号、标点、% 都不算（8% → 8）。"""
    return [w.replace("'", '') for w in _TOKEN_RE.findall(str(text or '').lower())]


def _path(task_id):
    return os.path.join(config.RESULTS_FOLDER, task_id, 'words.json')


def parse_json3(data):
    """YouTube json3 字幕 → [(秒, 词)]。自动字幕每个词一个 seg（带 tOffsetMs）；
    人工字幕一行一个 seg，没有词级偏移，就把这一行的词均匀铺在这一行的显示时长里。"""
    out = []
    for ev in (data or {}).get('events') or []:
        segs = [s for s in ev.get('segs') or [] if str(s.get('utf8') or '').strip()]
        if not segs:
            continue
        t0 = (ev.get('tStartMs') or 0) / 1000
        dur = (ev.get('dDurationMs') or 2000) / 1000
        for k, sg in enumerate(segs):
            ws = str(sg['utf8']).split()
            st = t0 + (sg.get('tOffsetMs') or 0) / 1000
            if len(segs) == 1:
                span = dur
            else:
                nxt = t0 + (segs[k + 1].get('tOffsetMs') or 0) / 1000 if k + 1 < len(segs) else st + 0.4 * len(ws)
                span = max(0.0, nxt - st)
            for i, w in enumerate(ws):
                out.append((round(st + span * i / len(ws), 2), w))
    return out


_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')


def merge_pieces(pieces):
    """阿里云 enable_words 返回的词片段 → [(秒, 整词)]。英文新词开头带空格（' bit'），一个词可能拆成几片
    （'Frank' + 'ly'），没空格的片段接到上一片；上一片带了标点就断开。中文每片各算一个（不带空格，不然整句粘成一个）。"""
    out, prev_punct, prev_cjk = [], True, False
    for w in pieces or []:
        p = str(w.get('text') or '')
        cjk = bool(_CJK_RE.search(p))
        if not out or p.startswith(' ') or prev_punct or cjk or prev_cjk:
            out.append([(w.get('begin_time') or 0) / 1000, p.strip()])
        else:
            out[-1][1] += p
        prev_punct, prev_cjk = bool(str(w.get('punctuation') or '').strip()), cjk
    return [(round(s, 2), x) for s, x in out if x]


def save(task_id, words, source, lang=''):
    path = _path(task_id)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'source': source, 'lang': lang, 'words': [[round(s, 2), w] for s, w in words]}, f, ensure_ascii=False)
    os.replace(tmp, path)
    _cache.pop(task_id, None)


def has(task_id):
    return bool(task_id) and os.path.isfile(_path(task_id))


def _load(task_id):
    """→ [(秒, 比对用的词)]；一个显示词拆出几个（中文一串字）时，时间顺着往后铺。"""
    path = _path(task_id)
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return [], {}
    hit = _cache.get(task_id)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f).get('words') or []
    except (OSError, ValueError):
        return [], {}
    out = []
    for k, (s, w) in enumerate(raw):
        ts = tokens(w)
        nxt = raw[k + 1][0] if k + 1 < len(raw) else s + 0.3 * len(ts)
        for i, tk in enumerate(ts):
            out.append((s + (nxt - s) * i / len(ts), tk))
    pos = {}                                  # 词 → 出现的位置（升序）：locate 用少见的词当锚点
    for i, (_, tk) in enumerate(out):
        pos.setdefault(tk, []).append(i)
    if len(_cache) > 200:
        _cache.clear()
    _cache[task_id] = (mt, out, pos)
    return out, pos


def locate(task_id, quote, sec=None, before=180, after=240, min_cover=0.6):
    """原话第一个词出现的秒数（一位小数）；找不到 None。
    在卡片时间点前 3 分钟、后 4 分钟里模糊比对原话开头十来个词——自动字幕会听错词（hard → heart），
    不要求逐字一样；原话用「…」拼的按第一截。"""
    if not task_id:
        return None
    toks, pos = _load(task_id)
    if not toks:
        return None
    frag = next((f for f in _FRAG_RE.split(quote or '') if len(tokens(f)) >= 3), '')
    q = tokens(frag)[:12]
    if len(q) < 3:
        return None
    # 锚点：原话开头 8 个词里这一期最少见的 3 个（the / and 到处都是，拿它们当锚要比几百次）；
    # 取 3 个是给听错留余地——其中一个被字幕写错了，另外两个还能把位置找回来
    head = list(dict.fromkeys(q[:8]))
    rare = sorted((w for w in head if w in pos), key=lambda w: len(pos[w]))[:3]
    starts = set()
    for w in rare:
        j = q.index(w)
        for p in pos[w]:
            if sec is None or sec - before <= toks[p][0] <= sec + after:
                starts.add(max(0, p - j))
    best = None
    for i in sorted(starts):
        st = toks[i][0]
        win = [x[1] for x in toks[i:i + len(q) + 3]]
        sm = difflib.SequenceMatcher(None, q, win, autojunk=False)
        r = sum(b.size for b in sm.get_matching_blocks()) / len(q)      # 原话开头的词有几成按顺序对上
        key = (r, -abs(st - sec) if sec is not None else 0)
        if r >= min_cover and (best is None or key > best[0]):
            best = (key, i, sm)
    if not best:
        return None
    _, i, sm = best
    blk = next(b for b in sm.get_matching_blocks() if b.size)
    # 原话开头几个词字幕里没对上（听错了）：从第一个对上的词往前推，每个词约 0.3 秒
    return round(max(0.0, toks[i + blk.b][0] - 0.3 * blk.a), 1)


def fetch_youtube(task_id, url, lang='auto'):
    """从 YouTube 字幕建这一期的逐词时间轴。返回 (词数, None) 或 (0, 原因)；原因含 '429' 时调用方该停手。"""
    import shutil
    import tempfile
    import downloader
    tmp = tempfile.mkdtemp(prefix='words_')
    try:
        path, kind, got = downloader.fetch_word_captions(url, tmp, lang)
        if not path:
            return 0, kind
        with open(path, encoding='utf-8') as f:
            words = parse_json3(json.load(f))
        if len(words) < 20:
            return 0, 'captions too short'
        save(task_id, words, source='youtube-' + kind, lang=got or '')
        return len(words), None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- 新转写顺手补：一条后台线程排队拉，每次间隔 20 秒起；被 429 就歇 10 分钟 ----
_YT_RE = re.compile(r'(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/)|youtu\.be/)[\w-]{11}')
_q = _queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def queue(task_id, url):
    """转写存好后调：YouTube 的期排进队列补逐词时间轴；别的来源、已经有了的、WORDTIME_AUTO=0 时不做。"""
    global _worker
    if os.environ.get('WORDTIME_AUTO', '1') != '1' or not _YT_RE.search(str(url or '')) or has(task_id):
        return False
    _q.put((task_id, url))
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_queue, name='wordtime', daemon=True)
            _worker.start()
    return True


def _run_queue():
    while True:
        try:
            tid, url = _q.get(timeout=600)
        except _queue.Empty:
            return
        if has(tid):
            continue
        try:
            n, why = fetch_youtube(tid, url)
        except Exception as e:
            n, why = 0, str(e)
        if not n and why and '429' in why:
            _q.put((tid, url))
            time.sleep(600)
        time.sleep(20 + random.uniform(0, 10))
