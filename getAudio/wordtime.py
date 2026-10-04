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
    out, pos = _index(raw)
    if len(_cache) > 200:
        _cache.clear()
    _cache[task_id] = (mt, out, pos)
    return out, pos


def _index(raw):
    """[(秒, 显示词)] → ([(秒, 比对用的词)], {词: [位置…]})。一个显示词拆出几个（中文一串字）时，时间顺着往后铺。"""
    out = []
    for k, (s, w) in enumerate(raw):
        ts = tokens(w)
        nxt = raw[k + 1][0] if k + 1 < len(raw) else s + 0.3 * len(ts)
        for i, tk in enumerate(ts):
            out.append((s + (nxt - s) * i / len(ts), tk))
    pos = {}                                  # 词 → 出现的位置（升序）：locate 用少见的词当锚点
    for i, (_, tk) in enumerate(out):
        pos.setdefault(tk, []).append(i)
    return out, pos


def locate(task_id, quote, sec=None, before=180, after=240, min_cover=0.6):
    """原话第一个词出现的秒数（一位小数）；找不到 None。
    在卡片时间点前 3 分钟、后 4 分钟里模糊比对原话开头十来个词——自动字幕会听错词（hard → heart），
    不要求逐字一样；原话用「…」拼的按第一截。"""
    if not task_id:
        return None
    toks, pos = _load(task_id)
    return _locate(toks, pos, quote, sec, before, after, min_cover)


def locate_words(words, quote, sec=None, min_cover=0.6):
    """同 locate，但在给定的一小段 [(秒, 词)] 里找（点出处时现场对齐的那 90 秒）。"""
    toks, pos = _index(words)
    return _locate(toks, pos, quote, sec, 10 ** 9, 10 ** 9, min_cover)


def _locate(toks, pos, quote, sec, before, after, min_cover):
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


# ---- Gemini 转写的期：本机 Whisper 补（10-04 实测中位数差 0.24 秒、约 11 倍实时）----
# 音频转写完默认不留（KEEP_AUDIO），所以排队时先硬链接一份到 uploads/_align/，Whisper 跑完就删；
# 服务重启后队列没了，下次排队时把 _align/ 里剩下的重新排上。GPU 跟 Whisper 转写共用一把锁（mlx 不是线程安全的）。
_wq = _queue.Queue()
_wworker = None
_wlock = threading.Lock()
_wresumed = False


def align_dir():
    d = os.path.join(config.UPLOAD_FOLDER, '_align')
    os.makedirs(d, exist_ok=True)
    return d


def is_youtube(url):
    return bool(_YT_RE.search(str(url or '')))


def whisper_words(path):
    """本机 Whisper 带逐词时间转一遍 → [(秒, 词)]。语言自动判断（中文 / 英文都行）。"""
    import transcribe_whisper as tw
    model, kind = tw._get_model()
    if kind == 'mlx':
        import mlx_whisper
        with tw._mlx_gpu_lock:
            r = mlx_whisper.transcribe(path, path_or_hf_repo=model, word_timestamps=True)
        segs = r.get('segments') or []
        return [(round(float(w['start']), 2), w['word'].strip()) for s in segs for w in s.get('words') or [] if w['word'].strip()]
    if kind == 'faster':
        segs, _ = model.transcribe(path, word_timestamps=True, vad_filter=True)
        return [(round(float(w.start), 2), w.word.strip()) for s in segs for w in (s.words or []) if w.word.strip()]
    r = model.transcribe(path, word_timestamps=True)
    return [(round(float(w['start']), 2), w['word'].strip()) for s in r.get('segments') or [] for w in s.get('words') or []]


def queue_whisper(task_id, audio_path, offset_sec=0):
    """转写存好后调（Gemini 引擎、来源不是 YouTube）：留一份音频排队补逐词时间轴。WORDTIME_WHISPER=0 时不做。"""
    global _wworker
    if os.environ.get('WORDTIME_WHISPER', '1') != '1' or not task_id or has(task_id) or not os.path.isfile(audio_path or ''):
        return False
    keep = os.path.join(align_dir(), f'{task_id}__{int(offset_sec or 0)}{os.path.splitext(audio_path)[1].lower()}')
    try:
        if not os.path.exists(keep):
            try:
                os.link(audio_path, keep)
            except OSError:
                import shutil
                shutil.copyfile(audio_path, keep)
    except OSError:
        return False
    _wq.put(keep)
    _ensure_whisper_worker()
    return True


def _ensure_whisper_worker():
    global _wworker, _wresumed
    with _wlock:
        if not _wresumed:                       # 上次没跑完的（服务重启）重新排上
            _wresumed = True
            queued = set(_wq.queue)
            for f in sorted(os.listdir(align_dir())):
                p = os.path.join(align_dir(), f)
                if '__' in f and p not in queued:
                    _wq.put(p)
        if _wworker is None or not _wworker.is_alive():
            _wworker = threading.Thread(target=_run_whisper_queue, name='wordtime-whisper', daemon=True)
            _wworker.start()


def _run_whisper_queue():
    while True:
        try:
            p = _wq.get(timeout=600)
        except _queue.Empty:
            return
        if not os.path.isfile(p):
            continue
        tid, _, rest = os.path.basename(p).partition('__')
        try:
            off = int(os.path.splitext(rest)[0] or 0)
        except ValueError:
            off = 0
        try:
            if not has(tid) and os.path.isdir(os.path.join(config.RESULTS_FOLDER, tid)):
                words = whisper_words(p)
                if words:
                    save(tid, [(s + off, w) for s, w in words], source='whisper')
                    if _engine(tid) == 'gemini35':      # 它的段落时间不可信：按时间轴改
                        apply_retime(tid)
        except Exception:
            pass
        finally:
            try:
                os.remove(p)
            except OSError:
                pass


# ================= 段落时间按逐词时间轴改（gemini35 的段落时间不可信，10-04 查实）=================
# 资料库 920 期 gemini35 短文件「最后一段时间 ÷ 时长」中位数 0.337，转成 16k 单声道再送一遍还是 0.337；
# 看着正常的几期也前后乱跳。所以有了逐词时间轴后，拿每段开头的话按顺序去时间轴里找，把段落时间换成真的。

_SPK_RE = re.compile(r'^\s*说话人[\d?？]+\s*[:：]\s*')


def _find_from(toks, pos, q, cursor, min_cover):
    """从 cursor 往后找 q（比对用的词）最近的一处；→ (位置, 秒) 或 None。"""
    head = list(dict.fromkeys(q[:8]))
    rare = sorted((w for w in head if w in pos), key=lambda w: len(pos[w]))[:3]
    starts = set()
    for w in rare:
        j = q.index(w)
        for p in pos[w]:
            if p - j >= cursor:
                starts.add(max(0, p - j))
    for i in sorted(starts):
        win = [x[1] for x in toks[i:i + len(q) + 3]]
        sm = difflib.SequenceMatcher(None, q, win, autojunk=False)
        blocks = sm.get_matching_blocks()
        if sum(b.size for b in blocks) / len(q) >= min_cover:
            blk = next(b for b in blocks if b.size)
            return i, round(max(0.0, toks[i + blk.b][0] - 0.3 * blk.a), 1)
    return None


def retime_segments(segments, raw_words, min_cover=0.6):
    """[{timestamp, text}] + 逐词时间轴 [(秒, 词)] → (新段落列表, 找到的段数)。
    按顺序找（同一句话出现两次时后面的段落落在后一次）；原来的时间留在 model_timestamp；
    找不到的段落夹在前后找到的两段之间、标 approx。一段都找不到就原样返回。传进来的列表不改。"""
    import timecode
    toks, pos = _index(raw_words or [])
    if not toks:
        return list(segments or []), 0
    found, cursor = [], 0
    for seg in segments or []:
        q = tokens(_SPK_RE.sub('', str(seg.get('text') or '')))[:12]
        hit = _find_from(toks, pos, q, cursor, min_cover) if len(q) >= 3 else None
        if hit:
            cursor = hit[0] + 1
        found.append(hit[1] if hit else None)
    n = sum(x is not None for x in found)
    if not n:
        return list(segments or []), 0
    out = []
    for k, seg in enumerate(segments):
        s = dict(seg)
        s.setdefault('model_timestamp', seg.get('timestamp'))
        sec = found[k]
        if sec is None:
            prev = next(((j, found[j]) for j in range(k - 1, -1, -1) if found[j] is not None), None)
            nxt = next(((j, found[j]) for j in range(k + 1, len(found)) if found[j] is not None), None)
            if prev and nxt:
                sec = prev[1] + (nxt[1] - prev[1]) * (k - prev[0]) / (nxt[0] - prev[0])
            else:
                sec = (prev or nxt)[1]
            s['approx'] = True
        else:
            s.pop('approx', None)
        s['timestamp'] = timecode.hms(sec)
        out.append(s)
    return out, n


def _engine(task_id):
    try:
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'), encoding='utf-8') as f:
            return json.load(f).get('engine') or ''
    except (OSError, ValueError):
        return ''


def apply_retime(task_id):
    """有 words.json 时把这一期 transcript.json 的段落时间按时间轴改好（原时间留在 model_timestamp）；→ 找到的段数。"""
    if not has(task_id):
        return 0
    tpath = os.path.join(config.RESULTS_FOLDER, task_id, 'transcript.json')
    try:
        with open(tpath, encoding='utf-8') as f:
            segs = json.load(f)
        with open(_path(task_id), encoding='utf-8') as f:
            raw = json.load(f).get('words') or []
    except (OSError, ValueError):
        return 0
    if not isinstance(segs, list) or not segs:
        return 0
    base = [dict(s, timestamp=s.get('model_timestamp') or s.get('timestamp')) for s in segs]
    out, n = retime_segments(base, raw)
    if not n:
        return 0
    tmp = tpath + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, tpath)
    try:
        import library
        library.changed(task_id)
    except Exception:  # noqa: BLE001  索引晚一点更新不要紧（最多 60 秒）
        pass
    return n
