"""画面描述（2026-11-16，用户：「转写里加一个画面描述……按每个变化帧丢给 AI 读……有些时候视频没有音频」）。

跟画面证据卡（frames.py）的区别：证据卡只留「屏幕上有字」的帧、抄字当证据；这里每一段画面都留，
写「画面里在发生什么」（人、动作、场景、屏幕上的字），按时间排成一条跟转写并排的画面轨。

    scene_times(g, fps)            → 纯数值挑帧：整幅画面（不剔除在动的人）变了就开新的一段，一段取中间那帧；
                                     一段太长（镜头不动但人在做事）每 MAX_GAP_S 秒补一帧；最多 MAX_SCENES 帧
    build(results_dir, tid, ...)   → 下视频 / 用给的视频 → 挑帧 → 模型一批批描述 → visual/scenes.json + 小缩略图
    as_segments(d)                 → 没人说话的视频：画面字幕 + 画面描述直接当转写正文（transcript.json 的段）
    queue(tid, video_path, url)    → 有声音的视频：转写完在后台排队做（一次一期），status(tid) 看进度
    speech_sparse(segs, dur)       → 转出来的话太少（配乐 + 画面字幕的视频）→ 自动加画面轨

字幕模式（captions=True，用户 11-16：「大部分是有声音但不是人说话的（画面有字幕，声音里不会读）」）：
画面横切 6 条、任何一条变了就切（换一行字幕也算）；模型先原样抄画面上的字、再一句画面；同一句字幕连着的并成一条。
用在：没音轨的视频、转出白卷 / 引擎回空文本的视频（字幕当正文）、话太少的视频（正文照存，自动加画面轨）。

模型走 frames._call → analyze._call_gemini_mm：TEXT_ENGINE=claude 时先给本机 Claude Haiku（订阅，记 $0），
失败才落 Gemini flash-lite（约每帧 $0.0004，一期最多 MAX_SCENES 帧 ≈ 6 美分）。
存在 results/<tid>/visual/：scenes.json + s_<十分之一秒>.jpg（320 宽缩略图）。视频不留。
"""
import glob
import json
import os
import queue as _queue
import re
import shutil
import tempfile
import threading
from datetime import datetime, timedelta

import numpy as np

import config
import frames
import usage

VERSION = 1
SCENE_DIFF = 9.0          # 跟这一段第一帧比，整幅画面平均灰度差超过这个 → 新的一段
MIN_SCENE_S = 1.5         # 比这短的段并进前一段（闪一下的转场）
MAX_GAP_S = 20.0          # 一段超过这么长，每这么多秒补一帧（镜头不动但人在做事）
MAX_SCENES = 150          # 一期最多描述这么多帧（多了均匀抽）
# 字幕模式（captions=True）：有声音但没人说话、话写在画面字幕里的视频（11-16 用户：「大部分是有声音但不是人说话的」）。
# 换一行字幕只动画面底下一小条，整幅平均差不到 2，上面的判据一段都切不开（实测 15 秒换 6 句只挑出 1 帧）。
# 所以把画面横切 BANDS 条，任何一条变了就切；背景在动的视频会退成约每 1 秒一帧，字幕重复的由代码去掉。
BANDS = 6
BAND_DIFF = 4.0
CAP_MIN_S = 1.0
CAP_GAP_S = 6.0
CAP_MAX = 300
# 11-16 用户定：单图读（Haiku），一次 9 张、每张 768 宽。试过 3x3 拼图（每格 512）：省约 40%，但漏字 4.7% vs 单图 1.1%
# （标题、开头小字最常丢）；用户：「弄得少，那还不如保证点准确性」。实测见 docs/handoff/activity.md。
READ_W = 768              # 送去读的图宽
THUMB_W = 320             # 留在磁盘上的缩略图宽
BATCH = 9                 # 一次调用读几帧
# 所有批进 frames 的共用读图池同时读（VISION_CALLS）；批之间不接话，连着的同一句字幕在代码里去重
# 不给模型「那时说的话」：11-16 实测 Haiku 拿到话就跟着话写（幻灯片被写成「还是那个人在雪山前」），
# 不给反而每帧都对。说的话转写里已经有了，画面轨只管看得见的。

PROMPT = """You are writing the visual track of a video transcript, for a reader who cannot see the video.
The {n} images are video frames in time order; image 1 is tile 1, image 2 is tile 2, and so on.
Look at each image on its own, not at its neighbours.

For each tile return:
- "caption": every piece of text shown on screen in that tile — titles, headings, bullet points, slide and chart text,
  subtitles, captions, text overlays — in reading order (top to bottom, left to right), copied exactly as it appears,
  character for character, in its original language, pieces joined with a space. Do not correct, translate, reword,
  shorten or complete it, even if it looks wrong. Leave out only watermarks, channel names/logos and platform UI.
  "" if the tile shows no such text.
- "text": a complete description in {lang} of what the picture shows: who or what is on screen, what they are doing,
  the setting, the objects, the layout, and what any chart or diagram shows. Refer to the on-screen text by role
  ("the title", "two bullet points") instead of copying it again. Do not mention what you left out.
Do not guess names, intentions or sounds that are not visible.

Reply with JSON only: {{"scenes": [{{"n": tile number, "caption": "...", "text": "..."}}]}}

{tiles}"""


def scenes_path(results_dir, tid):
    return os.path.join(frames.visual_dir(results_dir, tid), 'scenes.json')


def load(results_dir, tid):
    try:
        with open(scenes_path(results_dir, tid), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get('version') == VERSION else None


def thumb_path(results_dir, tid, name):
    """缩略图只认 s_<数字>.jpg。"""
    if not re.fullmatch(r's_\d+\.jpg', name or ''):
        return None
    p = os.path.join(frames.visual_dir(results_dir, tid), name)
    return p if os.path.isfile(p) else None


# ================= 挑帧（纯数值，不花钱） =================

def scene_times(g, fps, captions=False):
    """g: (n, GH, GW) 灰度 → [{'t': 取帧的秒, 'start', 'end'}]，按时间排。captions=True 走字幕模式。"""
    n = len(g)
    if not n:
        return []
    gi = g.astype(np.int16)
    bands = np.array_split(np.arange(g.shape[1]), BANDS)

    def changed(i, start):
        d = np.abs(gi[i] - gi[start])
        if d.mean() > SCENE_DIFF:
            return True
        return captions and max(d[b].mean() for b in bands) > BAND_DIFF

    cuts, start = [], 0
    for i in range(1, n):
        if changed(i, start):
            cuts.append((start, i))
            start = i
    cuts.append((start, n))
    min_len = max(1, int(round((CAP_MIN_S if captions else MIN_SCENE_S) * fps)))
    segs, pend = [], None
    for a, b in cuts:                          # 太短的段攒着，跟后面的连起来够长了再算一段（闪一下的转场并进下一段）
        pend = (pend[0], b) if pend else (a, b)
        if pend[1] - pend[0] >= min_len:
            segs.append(pend)
            pend = None
    if pend:                                   # 结尾剩一点：并进最后一段
        if segs:
            segs[-1] = (segs[-1][0], pend[1])
        else:
            segs.append(pend)
    out = []
    gap = max(1, int(round((CAP_GAP_S if captions else MAX_GAP_S) * fps)))
    for a, b in segs:
        parts = max(1, -(-(b - a) // gap))     # 向上取整
        step = (b - a) / parts
        for k in range(parts):
            pa, pb = a + step * k, a + step * (k + 1)
            # 字幕模式取一段偏后的那帧：字幕常常淡入，开头几帧还没显示全
            pos = 0.7 if captions else 0.5
            mid = min(n - 1, int(pa + (pb - pa) * pos))
            out.append({'t': round(mid / fps, 2), 'start': round(pa / fps, 1), 'end': round(pb / fps, 1)})
    cap = CAP_MAX if captions else MAX_SCENES
    if len(out) > cap:                         # 均匀抽，相邻的合并时间范围
        idx = np.linspace(0, len(out), cap + 1).astype(int)
        out = [{'t': out[(i + j) // 2]['t'], 'start': out[i]['start'], 'end': out[j - 1]['end']}
               for i, j in zip(idx[:-1], idx[1:]) if j > i]
    return out


# ================= 模型 =================

def _read_batch(batch, lang):
    """一批（最多 BATCH 帧）一次读完（每帧一张图）→ {i: {'text', 'caption'}}。读失败 → {}。"""
    tiles = '\n'.join(f"Tile {k + 1} = frame {it['i']}, {frames._clock(it['start'])}-{frames._clock(it['end'])}."
                      for k, it in enumerate(batch))
    prompt = PROMPT.format(n=len(batch), lang=lang, tiles=tiles)
    try:
        d = frames._json(frames._call(prompt, [it['img'] for it in batch]))
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    rows = d.get('scenes') if isinstance(d, dict) else None
    for c in rows if isinstance(rows, list) else []:
        if not isinstance(c, dict):
            continue
        try:
            k = int(c.get('n'))
        except (TypeError, ValueError, OverflowError):      # 1e999 解析成 inf，int(inf) 抛 OverflowError
            continue
        if not 1 <= k <= len(batch):
            continue
        text = str(c.get('text') or '').strip()[:1000]
        cap = re.sub(r'\s+', ' ', str(c.get('caption') or '')).strip()[:600]
        if text or cap:
            out[batch[k - 1]['i']] = {'text': text, 'caption': cap}
    return out


def describe(items, lang):
    """items: [{'i', 'start', 'end', 'img'}] → {i: {'text', 'caption'}}。每 BATCH 帧一次调用；所有批进共用读图池
    （frames.read_all）同时读。一批读失败就跳过那一批（别的照常）。"""
    batches = [items[s:s + BATCH] for s in range(0, len(items), BATCH)]
    out = {}
    for got in frames.read_all(lambda b: _read_batch(b, lang), batches):
        out.update(got)
    return out


def _norm(s):
    return re.sub(r'[\s\W_]+', '', s or '').lower()


def merge_captions(scenes):
    """字幕模式：同一句字幕连着好几帧（背景在动的视频每秒一帧）→ 并成一条，时间连起来；
    画面那句留第一帧的。没有字幕的帧照留（只有画面描述）。"""
    out = []
    for s in scenes:
        prev = out[-1] if out else None
        if prev and s.get('caption') and _norm(s['caption']) == _norm(prev.get('caption')):
            prev['end'] = s['end']
            continue
        out.append(dict(s))
    return out


# ================= 一期 =================

def _lang(segs):
    """有转写跟转写的语言；没声音的视频默认中文（SCENES_LANG=English 可改）。"""
    if segs:
        return frames._lang(segs)
    return os.environ.get('SCENES_LANG') or 'Chinese (Simplified)'


def build(results_dir, tid, video_path=None, url=None, progress=None, lang=None, captions=False):
    """做一期的画面描述，存进 results/<tid>/visual/scenes.json。video_path 没给就按 url 重新下（下完删）。
    captions=True：字幕模式（有声音但没人说话的视频，话在画面字幕里）。"""
    done = load(results_dir, tid)
    if done:
        return done
    say = progress or (lambda msg: None)
    tmp = tempfile.mkdtemp(prefix='scn_')
    try:
        if not video_path:
            if not url:
                raise RuntimeError('No video source for this recording')
            say('download')
            video_path = frames.download_video(url, tmp)
        say('scan')
        dur, native = frames.probe(video_path)
        fps = min(native, frames.SAMPLE_FPS if dur <= 3600 else 1.0)
        g = frames.sample_gray(video_path, fps)
        picks = scene_times(g, fps, captions=captions)
        del g
        segs = frames._segments(results_dir, tid)
        imgs = [os.path.join(tmp, f'r{k:03d}.jpg') for k in range(len(picks))]
        ok = frames.grab_many(video_path, [(p['t'], img) for p, img in zip(picks, imgs)], width=READ_W)
        items = [{'i': k, 'start': p['start'], 'end': p['end'], 't': p['t'], 'img': img}
                 for k, (p, img, good) in enumerate(zip(picks, imgs, ok)) if good]
        if not items:
            raise RuntimeError('No frames could be read from the video')
        say('describe')
        read = describe(items, lang or _lang(segs))
        scenes = []
        for it in items:
            r = read.get(it['i'])
            if r:
                scenes.append({'start': it['start'], 'end': it['end'], 'text': r['text'], 'img': '', '_t': it['t'],
                               **({'caption': r['caption']} if r.get('caption') else {})})
        if captions:
            scenes = merge_captions(scenes)
        if not scenes:
            raise RuntimeError('The model returned no descriptions')
        vdir = frames.visual_dir(results_dir, tid)
        os.makedirs(vdir, exist_ok=True)
        for old in glob.glob(os.path.join(vdir, 's_*.jpg')):
            os.remove(old)
        names = [f"s_{int(round(s['_t'] * 10)):06d}.jpg" for s in scenes]
        ok = frames.grab_many(video_path, [(s.pop('_t'), os.path.join(vdir, n)) for s, n in zip(scenes, names)],
                              width=THUMB_W)
        for s, n, good in zip(scenes, names, ok):
            if good:
                s['img'] = n
        out = {'version': VERSION, 'task_id': tid, 'made_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'model': config.GEMINI_VISUAL_MODEL, 'duration': round(dur, 1), 'picked': len(items),
               'captions': bool(captions), 'layout': 'single', 'scenes': scenes}
        tmp_json = scenes_path(results_dir, tid) + '.tmp'
        with open(tmp_json, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp_json, scenes_path(results_dir, tid))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scene_line(s):
    """一条画面描述写成正文的一行：有字幕先字幕，画面放在「［画面］」后面。"""
    cap, text = (s.get('caption') or '').strip(), (s.get('text') or '').strip()
    if cap and text:
        return f'{cap}　［画面］{text}'
    return cap or f'［画面］{text}'


def as_segments(d):
    """没人说话的视频：画面描述 / 画面字幕当转写正文。"""
    import timecode
    return [{'timestamp': timecode.clock(s['start']), 'end': timecode.clock(s['end']), 'text': scene_line(s)}
            for s in (d or {}).get('scenes') or []]


def speech_sparse(segments, duration_s):
    """转出来的话太少（每分钟不到 SPARSE_CPM 个字，标注 [音乐] 这类不算）→ 这期多半是配乐 + 画面字幕。"""
    if not duration_s or duration_s < 15:
        return False
    body = ''.join(re.sub(r'[\[（(【].{0,12}?[\]）)】]', '', str(s.get('text') or '')) for s in segments or [])
    body = re.sub(r'^说话人\d+[：:]', '', body)
    chars = len(re.sub(r'\s', '', body))
    return chars / (duration_s / 60) < SPARSE_CPM


SPARSE_CPM = 40           # 正常说话每分钟中文两三百字、英文一百多词；配乐视频常常只有「[音乐]」或一两句


# ---- 转写引擎在纯配乐上编话（11-16 真事）----
# 一条讲 Google 的配乐 + 画面字幕视频：OpenRouter 上的 Gemini 前 5 分钟什么都没转，最后 2 分 40 秒编了 44 句情话；
# 本机 Whisper 听同一段判「没人说话」，画面字幕讲的是 AlphaFold / Gemini / TPU。话不少，speech_sparse 没拦住，
# 正文就成了编出来的东西。画面描述做完后再对一次：三条都中才换正文。
DOUBT_CAP_CPM = 40        # 画面上的字够多：每分钟至少这么多字
DOUBT_CAP_COVER = 0.5     # 带字的帧铺满至少这么多时长
DOUBT_SPAN = 0.5          # 话只覆盖不到这么多时长……
DOUBT_LEAD = 0.4          # ……或者开头空了这么多
DOUBT_OVERLAP = 0.15      # 话里的两字词出现在画面字里的比例低于这个 = 对不上


def _bigrams(text):
    t = _norm(text)
    return {t[k:k + 2] for k in range(len(t) - 1)}


def speech_doubtful(segments, duration_s, d):
    """转出来的话像不像引擎编的（不是真说的）。→ 原因（一句话）或 None。
    要同时：画面上的字多；话只集中在一小段（或开头大段空白）；话跟画面上的字基本对不上。"""
    import timecode
    scs = [x for x in (d or {}).get('scenes') or [] if (x.get('caption') or '').strip()]
    if not duration_s or duration_s < 30 or not scs or not segments:
        return None
    cap_text = ' '.join(x['caption'] for x in scs)
    cap_cpm = len(_norm(cap_text)) / (duration_s / 60)
    cap_cover = sum(max(0, x['end'] - x['start']) for x in scs) / duration_s
    if cap_cpm < DOUBT_CAP_CPM or cap_cover < DOUBT_CAP_COVER:
        return None
    times = [timecode.parse(str(x.get('timestamp') or ''), bare_seconds=True) for x in segments]
    times = [t for t in times if t is not None]
    if not times:
        return None
    first, last = min(times), max(times)
    span, lead = (last - first) / duration_s, first / duration_s
    if span >= DOUBT_SPAN and lead <= DOUBT_LEAD:
        return None
    said = _bigrams(' '.join(str(x.get('text') or '') for x in segments))
    if not said:
        return None
    overlap = len(said & _bigrams(cap_text)) / len(said)
    if overlap >= DOUBT_OVERLAP:
        return None
    return (f'speech covers {span:.0%} of the video (starts at {lead:.0%}), on-screen text {cap_cpm:.0f} chars/min, '
            f'only {overlap:.0%} of the speech matches it')


def use_captions_as_body(results_dir, tid):
    """画面描述做完后：转出来的话可疑（speech_doubtful）→ 正文换成画面上的字（跟没人说话的视频一样），
    原来的转写另存 transcript.speech.json（不删），meta 记 speech_engine / speech_dropped，摘要和标题按新正文重写。
    → 换了 True，没换 False。"""
    tdir = os.path.join(results_dir, tid)
    try:
        with open(os.path.join(tdir, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
        with open(os.path.join(tdir, 'transcript.json'), encoding='utf-8') as f:
            segs = json.load(f)
    except (OSError, ValueError):
        return False
    d = load(results_dir, tid)
    if meta.get('engine') == 'scenes' or not d:
        return False
    why = speech_doubtful(segs, meta.get('duration_seconds') or d.get('duration'), d)
    if not why:
        return False
    body = as_segments(d)
    if not body:
        return False

    def write(name, obj):
        tmp = os.path.join(tdir, name + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(tdir, name))
    write('transcript.speech.json', segs)
    write('transcript.json', body)
    meta.update(speech_engine=meta.get('engine'), engine='scenes', speech_dropped=why,
                segment_count=len(body), char_count=sum(len(x['text']) for x in body))
    write('meta.json', meta)
    print(f'[scenes] {tid[:8]}: speech looks made up ({why}); on-screen text is the transcript now')
    full = '\n'.join(f"[{x['timestamp']}] {x['text']}" for x in body)
    try:
        from summarize import summarize_transcript
        with usage.scope(ref=tid):
            summary = summarize_transcript(full)
        if not summary:                     # 接口错 / 解析不了时它返回 None，不抛错
            raise RuntimeError('no summary returned')
        write('summary.json', summary)
        meta['has_summary'] = True
        write('meta.json', meta)
    except Exception as e:  # noqa: BLE001
        # 旧摘要写的是那段编出来的话：重写不成就删掉，别让历史页和起标题接着用它
        print(f'[scenes] {tid[:8]}: summary failed: {e}')
        try:
            os.remove(os.path.join(tdir, 'summary.json'))
        except OSError:
            pass
        meta['has_summary'] = False
        write('meta.json', meta)
    try:
        from enrich import enrich_task
        enrich_task(tdir)
    except Exception as e:  # noqa: BLE001
        print(f'[scenes] {tid[:8]}: title failed: {e}')
    return True


def estimate_frames(duration_s):
    return int(min(MAX_SCENES, max(3, duration_s / 8)))


# ================= 后台队列（有声音的视频：转写完再做） =================
# 同时做几期：VISION_EPISODES（画面证据卡 frames.run_batch 用同一个数）。每期都要重下一遍视频（B 站风控），
# 模型调用另有两道闸：claude_cli 的 CLAUDE_PARALLEL、三个应用共用的 claude_gate。

def episodes_at_once():
    return frames.episodes_at_once()


RECENT_S = 20 * 60        # 「进行中」列表里做完的留多久
_q = _queue.Queue()
_state = {}               # tid -> {'status': queued/running/done/failed, 'error'?, 'started', 'updated'}
_lock = threading.Lock()
_workers = []


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def status(results_dir, tid):
    if load(results_dir, tid):
        return {'status': 'done'}
    with _lock:
        return dict(_state.get(tid) or {})


def recent():
    """「进行中」列表：在排队 / 在做的，和最近 RECENT_S 秒里做完 / 失败的。→ [(tid, state)]"""
    cutoff = (datetime.now() - timedelta(seconds=RECENT_S)).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        return [(t, dict(st)) for t, st in _state.items()
                if st.get('status') in ('queued', 'running') or (st.get('updated') or '') >= cutoff]


def queue(results_dir, tid, video_path=None, url=None, on_done=None, captions=False):
    """排进后台队列；video_path 是专门留给这一步的视频（做完删掉）。已经做过 / 已在排队 → 不重复排。"""
    if load(results_dir, tid):
        _cleanup(video_path)
        return False
    with _lock:
        if (_state.get(tid) or {}).get('status') in ('queued', 'running'):
            return False
        _state[tid] = {'status': 'queued', 'started': _now(), 'updated': _now()}
        _q.put((results_dir, tid, video_path, url, on_done, captions))
        _workers[:] = [w for w in _workers if w.is_alive()]
        while len(_workers) < episodes_at_once():
            w = threading.Thread(target=_run, name=f'scenes-{len(_workers)}', daemon=True)
            w.start()
            _workers.append(w)
    return True


def _cleanup(path):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _run():
    while True:
        try:
            results_dir, tid, video_path, url, on_done, captions = _q.get(timeout=60)
        except _queue.Empty:
            return
        with _lock:
            _state[tid] = {**_state.get(tid, {}), 'status': 'running', 'updated': _now()}
        try:
            with usage.scope(ref=tid):
                build(results_dir, tid, video_path=video_path, url=url, captions=captions)
            st = {'status': 'done'}
        except Exception as e:  # noqa: BLE001
            print(f'[scenes] {tid[:8]} failed: {e}')
            st = {'status': 'failed', 'error': str(e)[:200]}
        finally:
            _cleanup(video_path)
        with _lock:
            _state[tid] = {**_state.get(tid, {}), **st, 'updated': _now()}
        if on_done:
            try:
                on_done(tid, st)
            except Exception:  # noqa: BLE001
                pass
