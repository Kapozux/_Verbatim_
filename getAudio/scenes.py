"""画面描述（2026-11-16，用户：「转写里加一个画面描述……按每个变化帧丢给 AI 读……有些时候视频没有音频」）。

跟画面证据卡（frames.py）的区别：证据卡只留「屏幕上有字」的帧、抄字当证据；这里每一段画面都留，
写「画面里在发生什么」（人、动作、场景、屏幕上的字），按时间排成一条跟转写并排的画面轨。

    scene_times(g, fps)            → 纯数值挑帧：整幅画面（不剔除在动的人）变了就开新的一段，一段取中间那帧；
                                     一段太长（镜头不动但人在做事）每 MAX_GAP_S 秒补一帧；最多 MAX_SCENES 帧
    build(results_dir, tid, ...)   → 下视频 / 用给的视频 → 挑帧 → 模型一批批描述 → visual/scenes.json + 小缩略图
    as_segments(d)                 → 没有声音的视频：画面描述直接当转写正文（transcript.json 的段）
    queue(tid, video_path, url)    → 有声音的视频：转写完在后台排队做（一次一期），status(tid) 看进度

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
from datetime import datetime

import numpy as np

import config
import frames
import usage

VERSION = 1
SCENE_DIFF = 9.0          # 跟这一段第一帧比，整幅画面平均灰度差超过这个 → 新的一段
MIN_SCENE_S = 1.5         # 比这短的段并进前一段（闪一下的转场）
MAX_GAP_S = 20.0          # 一段超过这么长，每这么多秒补一帧（镜头不动但人在做事）
MAX_SCENES = 150          # 一期最多描述这么多帧（多了均匀抽）
READ_W = 768              # 送去描述的图宽
THUMB_W = 320             # 留在磁盘上的缩略图宽
BATCH = 8                 # 一次调用描述几帧
# 不给模型「那时说的话」：11-16 实测 Haiku 拿到话就跟着话写（幻灯片被写成「还是那个人在雪山前」），
# 不给反而每帧都对。说的话转写里已经有了，画面轨只管看得见的。

PROMPT = """You are writing the visual track of a video transcript, for a reader who cannot see the video.
The frames below are in time order (image 1 is the first frame listed, and so on); each comes with the time span it represents.
Describe each image on its own: look at that image, not at the neighbours.

For each frame write one or two plain sentences on what is visible: who or what is on screen, what they are doing,
the setting, and any readable on-screen text (copy short text exactly, keep its original language).
If a frame continues the previous one, only say what changed (e.g. "She picks up the cup."), do not repeat the setting.
Do not guess names, intentions or sounds that are not visible. Write in {lang}.
{prev}
Reply with JSON only: {{"scenes": [{{"i": number, "text": "..."}}]}}

{frames}"""


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

def scene_times(g, fps):
    """g: (n, GH, GW) 灰度 → [{'t': 取帧的秒, 'start', 'end'}]，按时间排。"""
    n = len(g)
    if not n:
        return []
    gi = g.astype(np.int16)
    cuts, start = [], 0
    for i in range(1, n):
        if np.abs(gi[i] - gi[start]).mean() > SCENE_DIFF:
            cuts.append((start, i))
            start = i
    cuts.append((start, n))
    min_len = max(1, int(round(MIN_SCENE_S * fps)))
    segs = []
    for a, b in cuts:                          # 太短的段并进前一段
        if segs and b - a < min_len:
            segs[-1] = (segs[-1][0], b)
        else:
            segs.append((a, b))
    if len(segs) > 1 and segs[0][1] - segs[0][0] < min_len:
        segs[1] = (segs[0][0], segs[1][1])
        segs.pop(0)
    out = []
    gap = max(1, int(round(MAX_GAP_S * fps)))
    for a, b in segs:
        parts = max(1, -(-(b - a) // gap))     # 向上取整
        step = (b - a) / parts
        for k in range(parts):
            pa, pb = a + step * k, a + step * (k + 1)
            mid = min(n - 1, int((pa + pb) / 2))
            out.append({'t': round(mid / fps, 2), 'start': round(pa / fps, 1), 'end': round(pb / fps, 1)})
    if len(out) > MAX_SCENES:                  # 均匀抽，相邻的合并时间范围
        idx = np.linspace(0, len(out), MAX_SCENES + 1).astype(int)
        out = [{'t': out[(i + j) // 2]['t'], 'start': out[i]['start'], 'end': out[j - 1]['end']}
               for i, j in zip(idx[:-1], idx[1:]) if j > i]
    return out


# ================= 模型 =================

def describe(items, lang):
    """items: [{'i', 'start', 'end', 'img'}] → {i: 描述}。一批读失败就跳过那一批（别的照常）。"""
    out, prev = {}, ''
    for s in range(0, len(items), BATCH):
        batch = items[s:s + BATCH]
        lines = []
        for k, it in enumerate(batch):
            lines.append(f"Frame {it['i']} (image {k + 1}), {frames._clock(it['start'])}-{frames._clock(it['end'])}.")
        prompt = PROMPT.format(
            lang=lang, frames='\n'.join(lines),
            prev=f'\nThe frame just before this batch was described as: "{prev}"\n' if prev else '')
        try:
            d = frames._json(frames._call(prompt, [it['img'] for it in batch]))
        except Exception:  # noqa: BLE001
            continue
        ids = {it['i'] for it in batch}
        for c in d.get('scenes') or []:
            try:
                i = int(c.get('i'))
            except (TypeError, ValueError):
                continue
            text = str(c.get('text') or '').strip()
            if i in ids and text:
                out[i] = text[:400]
        last = max((i for i in ids if i in out), default=None)
        prev = out[last][:200] if last is not None else prev
    return out


# ================= 一期 =================

def _lang(segs):
    """有转写跟转写的语言；没声音的视频默认中文（SCENES_LANG=English 可改）。"""
    if segs:
        return frames._lang(segs)
    return os.environ.get('SCENES_LANG') or 'Chinese (Simplified)'


def build(results_dir, tid, video_path=None, url=None, progress=None, lang=None):
    """做一期的画面描述，存进 results/<tid>/visual/scenes.json。video_path 没给就按 url 重新下（下完删）。"""
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
        picks = scene_times(g, fps)
        del g
        segs = frames._segments(results_dir, tid)
        items = []
        for k, p in enumerate(picks):
            img = os.path.join(tmp, f'r{k:03d}.jpg')
            if frames.grab(video_path, p['t'], img, width=READ_W):
                items.append({'i': k, 'start': p['start'], 'end': p['end'], 't': p['t'], 'img': img})
        if not items:
            raise RuntimeError('No frames could be read from the video')
        say('describe')
        read = describe(items, lang or _lang(segs))
        vdir = frames.visual_dir(results_dir, tid)
        os.makedirs(vdir, exist_ok=True)
        for old in glob.glob(os.path.join(vdir, 's_*.jpg')):
            os.remove(old)
        scenes = []
        for it in items:
            text = read.get(it['i'])
            if not text:
                continue
            name = f's_{int(round(it["t"] * 10)):06d}.jpg'
            if not frames.grab(video_path, it['t'], os.path.join(vdir, name), width=THUMB_W):
                name = ''
            scenes.append({'start': it['start'], 'end': it['end'], 'text': text, 'img': name})
        if not scenes:
            raise RuntimeError('The model returned no descriptions')
        out = {'version': VERSION, 'task_id': tid, 'made_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'model': config.GEMINI_VISUAL_MODEL, 'duration': round(dur, 1), 'picked': len(items),
               'scenes': scenes}
        tmp_json = scenes_path(results_dir, tid) + '.tmp'
        with open(tmp_json, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp_json, scenes_path(results_dir, tid))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def as_segments(d):
    """没有声音的视频：画面描述当转写正文。"""
    import timecode
    return [{'timestamp': timecode.clock(s['start']), 'end': timecode.clock(s['end']), 'text': s['text']}
            for s in (d or {}).get('scenes') or []]


def estimate_frames(duration_s):
    return int(min(MAX_SCENES, max(3, duration_s / 8)))


# ================= 后台队列（有声音的视频：转写完再做） =================

_q = _queue.Queue()
_state = {}               # tid -> {'status': queued/running/done/failed, 'error'?}
_lock = threading.Lock()
_worker = None


def status(results_dir, tid):
    if load(results_dir, tid):
        return {'status': 'done'}
    with _lock:
        return dict(_state.get(tid) or {})


def queue(results_dir, tid, video_path=None, url=None, on_done=None):
    """排进后台队列；video_path 是专门留给这一步的视频（做完删掉）。已经做过 / 已在排队 → 不重复排。"""
    global _worker
    if load(results_dir, tid):
        _cleanup(video_path)
        return False
    with _lock:
        if (_state.get(tid) or {}).get('status') in ('queued', 'running'):
            return False
        _state[tid] = {'status': 'queued'}
        _q.put((results_dir, tid, video_path, url, on_done))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name='scenes', daemon=True)
            _worker.start()
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
            results_dir, tid, video_path, url, on_done = _q.get(timeout=60)
        except _queue.Empty:
            return
        with _lock:
            _state[tid] = {'status': 'running'}
        try:
            with usage.scope(ref=tid):
                build(results_dir, tid, video_path=video_path, url=url)
            st = {'status': 'done'}
        except Exception as e:  # noqa: BLE001
            print(f'[scenes] {tid[:8]} failed: {e}')
            st = {'status': 'failed', 'error': str(e)[:200]}
        finally:
            _cleanup(video_path)
        with _lock:
            _state[tid] = st
        if on_done:
            try:
                on_done(tid, st)
            except Exception:  # noqa: BLE001
                pass
