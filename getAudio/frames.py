"""画面证据卡（测试版）：从视频画面里抽「屏幕上写了什么」——幻灯片、图表、公式、代码、白板。

做法（照 ~/verbatim_outreach/video_understanding 的笔记）：模型看不了视频，只看图，每张图都要钱。
所以让代码看每一帧，模型只看要紧的几张：
  1. 重新下一份 ≤720p 的画面（转写时只下了音轨），不要声音。
  2. 数值过一遍：160×90 灰度、每秒 2 帧（不超过原生帧率）。只在「内容区域」里比较——经常在动的像素
     （讲的人、摄像头画面）不算。每帧跟「上一张关键帧」比，不跟上一帧比：逐条出现的要点、白板越写越多
     也能累积到阈值。一段画面取它**最后**一帧：内容最满、擦掉之前那一刻。
  3. 太短的段（转场、动画）不要；dHash 去重，同一页幻灯片翻回来只算一张，记下它出现过的几段时间。
  4. 模型两遍：先把所有关键帧拼成带编号的缩略图（一张图 30 格），问哪些有信息；
     再只把留下的那几张原图（1280 宽）连同那段时间里说的话送进去，抄字、写描述。
  5. 磁盘上只留留下的关键帧 JPG 和 cards.json，视频删掉。

存在 results/<task_id>/visual/：cards.json + f_<秒>.jpg。同一期在几个项目里共用，做过就直接用，不再花钱。
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

import numpy as np

import config
import usage

VERSION = 1
GW, GH = 160, 90             # 数值过一遍用的小图
SAMPLE_FPS = 2.0             # 讲课视频大多是静的，每秒 2 帧够找换页；超过 1 小时降到 1 帧
MOVING_FRAC = 0.15           # 一个像素在超过这个比例的相邻帧之间都在变 → 算「在动的」（人、摄像头），不参与比较
MOVE_DIFF = 12               # 相邻两帧灰度差超过这个算「变了」
KEY_DIFF = 7.0               # 跟上一张关键帧的平均灰度差（内容区域内）超过这个 → 新的一段
MIN_SEG_S = 2.0              # 比这短的段是转场 / 动画，不要
DHASH_SAME = 5               # dHash 汉明距离 ≤ 这个算同一张
MAX_KEYFRAMES = 90           # 一期最多这么多张关键帧（多了留显示最久的）
SHEET_COLS, SHEET_ROWS, TILE_W = 6, 5, 320
FULL_W = 1280                # 送去抄字的原图宽度
READ_BATCH = 6               # 一次调用读几张原图
SAID_CHARS = 700             # 每张图配多少字「那段时间说的话」


def visual_dir(results_dir, task_id):
    return os.path.join(results_dir, task_id, 'visual')


def load(results_dir, task_id):
    """做过的画面卡；没做过 / 版本旧了 → None。"""
    try:
        with open(os.path.join(visual_dir(results_dir, task_id), 'cards.json'), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get('version') == VERSION else None


def image_path(results_dir, task_id, name):
    """图片名只认 f_<数字>.jpg，别让前端拼出别的路径。"""
    if not re.fullmatch(r'f_\d+\.jpg', name or ''):
        return None
    p = os.path.join(visual_dir(results_dir, task_id), name)
    return p if os.path.isfile(p) else None


# ================= 视频 =================

def _ffmpeg():
    return shutil.which('ffmpeg') or 'ffmpeg'


def probe(path):
    """→ (时长秒, 原生帧率)。"""
    ffprobe = shutil.which('ffprobe') or 'ffprobe'
    r = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                        'stream=r_frame_rate:format=duration', '-of', 'json', path],
                       capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or '{}')
    dur = float((d.get('format') or {}).get('duration') or 0)
    fps = 30.0
    rate = ((d.get('streams') or [{}])[0].get('r_frame_rate') or '30/1').split('/')
    try:
        fps = float(rate[0]) / float(rate[1] if len(rate) > 1 else 1) or 30.0
    except (ValueError, ZeroDivisionError):
        pass
    return dur, fps


def download_video(url, dest_dir):
    """只下画面（≤720p，不要声音），返回文件路径；失败抛错。cookie / ffmpeg 位置跟下音频同一套。"""
    import downloader
    os.makedirs(dest_dir, exist_ok=True)
    cmd = [downloader._resolve_ytdlp(),
           '-f', 'bv*[height<=720][vcodec^=avc1]/bv*[height<=720]/b[height<=720]/b',
           '-N', '4', '-o', os.path.join(dest_dir, 'video.%(ext)s'),
           '--no-playlist', '--no-warnings', '--quiet',
           *downloader._cookie_args(), *downloader._ffmpeg_location_args(),
           '--print', 'after_move:filepath', '--no-simulate', url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    path = (r.stdout or '').strip().splitlines()[-1:] or ['']
    if r.returncode != 0 or not os.path.isfile(path[0]):
        raise RuntimeError('Video download failed: ' + (r.stderr or '').strip()[-200:])
    return path[0]


def sample_gray(path, fps):
    """整段视频 → (n, GH, GW) uint8 灰度。"""
    r = subprocess.run([_ffmpeg(), '-loglevel', 'error', '-i', path, '-an', '-vf',
                        f'fps={fps},scale={GW}:{GH}', '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                       capture_output=True, timeout=3600)
    raw = r.stdout
    n = len(raw) // (GW * GH)
    if not n:
        raise RuntimeError('Could not decode the video')
    return np.frombuffer(raw[:n * GW * GH], np.uint8).reshape(n, GH, GW)


# ================= 找关键帧（纯数值，不花钱） =================

def content_mask(g):
    """内容区域：不常动的像素。人在讲、镜头在晃的地方剔掉；剩下太少（整屏都在动）就全用。"""
    if len(g) < 3:
        return np.ones(g.shape[1:], bool)
    moving = (np.abs(np.diff(g.astype(np.int16), axis=0)) > MOVE_DIFF).mean(0)
    mask = moving < MOVING_FRAC
    return mask if mask.mean() > 0.2 else np.ones(g.shape[1:], bool)


def dhash(img):
    """灰度小图 → 64 位差值哈希。"""
    from PIL import Image
    a = np.asarray(Image.fromarray(img).resize((9, 8), Image.BILINEAR), np.int16)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int(''.join('1' if b else '0' for b in bits), 2)


def _hamming(a, b):
    return bin(a ^ b).count('1')


def keyframes(g, fps, mask=None):
    """→ [{'t': 取帧的秒, 'shown': [[从, 到], ...]}]，按第一次出现排。

    每帧跟这一段的第一帧比（内容区域内），差过 KEY_DIFF 就开新一段；一段取最后一帧。
    """
    mask = content_mask(g) if mask is None else mask
    n = len(g)
    if not n:
        return []
    gm = g[:, mask].astype(np.int16)
    segs, start = [], 0
    for i in range(1, n):
        if np.abs(gm[i] - gm[start]).mean() > KEY_DIFF:
            segs.append((start, i - 1))
            start = i
    segs.append((start, n - 1))
    min_len = max(1, int(round(MIN_SEG_S * fps)))
    segs = [s for s in segs if s[1] - s[0] + 1 >= min_len] or [max(segs, key=lambda s: s[1] - s[0])]
    kept = []                                  # [{'hash', 't', 'shown'}]
    for a, b in segs:
        h = dhash(g[b])
        span = [round(a / fps, 1), round((b + 1) / fps, 1)]
        same = next((k for k in kept if _hamming(k['hash'], h) <= DHASH_SAME), None)
        if same:
            same['shown'].append(span)
            if span[1] - span[0] > same['_len']:          # 同一张取显示最久那段的末帧（最完整）
                same.update(t=round(b / fps, 2), _len=span[1] - span[0])
        else:
            kept.append({'hash': h, 't': round(b / fps, 2), 'shown': [span], '_len': span[1] - span[0]})
    if len(kept) > MAX_KEYFRAMES:
        kept = sorted(kept, key=lambda k: -sum(s[1] - s[0] for s in k['shown']))[:MAX_KEYFRAMES]
        kept.sort(key=lambda k: k['shown'][0][0])
    return [{'t': k['t'], 'shown': k['shown']} for k in kept]


def grab(video, t, out, width=FULL_W):
    subprocess.run([_ffmpeg(), '-loglevel', 'error', '-y', '-ss', f'{max(0, t):.2f}', '-i', video,
                    '-frames:v', '1', '-vf', f"scale='min({width},iw)':-2", '-q:v', '3', out],
                   capture_output=True, timeout=120)
    return os.path.isfile(out)


def _font(size):
    from PIL import ImageFont
    for p in ('/System/Library/Fonts/Helvetica.ttc', 'C:\\Windows\\Fonts\\arial.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def contact_sheets(frames, out_prefix):
    """frames: [(编号, 秒, jpg 路径)] → 每 30 张拼一张带「#编号 mm:ss」的缩略图，返回 [(路径, [编号...])]。"""
    from PIL import Image, ImageDraw
    th = TILE_W * 9 // 16
    font = _font(max(14, TILE_W // 16))
    per = SHEET_COLS * SHEET_ROWS
    out = []
    for s in range(0, len(frames), per):
        chunk = frames[s:s + per]
        rows = (len(chunk) + SHEET_COLS - 1) // SHEET_COLS
        sheet = Image.new('RGB', (SHEET_COLS * TILE_W + (SHEET_COLS - 1) * 4, rows * th + (rows - 1) * 4), (40, 40, 40))
        for k, (idx, t, p) in enumerate(chunk):
            tile = Image.open(p).convert('RGB')
            tile.thumbnail((TILE_W, th))
            canvas = Image.new('RGB', (TILE_W, th), (0, 0, 0))
            canvas.paste(tile, ((TILE_W - tile.width) // 2, (th - tile.height) // 2))
            d = ImageDraw.Draw(canvas)
            label = f'#{idx}  {_clock(t)}'
            d.rectangle([0, 0, TILE_W // 2.3, TILE_W // 13], fill=(0, 0, 0))
            d.text((5, 2), label, fill=(255, 230, 0), font=font)
            sheet.paste(canvas, ((k % SHEET_COLS) * (TILE_W + 4), (k // SHEET_COLS) * (th + 4)))
        path = f'{out_prefix}_{s // per:02d}.jpg'
        sheet.save(path, quality=85)
        out.append((path, [i for i, _, _ in chunk]))
    return out


def _clock(sec):
    sec = int(sec)
    return f'{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}' if sec >= 3600 else f'{sec // 60}:{sec % 60:02d}'


# ================= 模型 =================

TRIAGE_PROMPT = """These are numbered keyframes from a video (each tile shows "#number time").
Pick the frames whose screen carries information worth reading: a slide with text, a chart, a table,
a formula, code, a diagram, a whiteboard, a document or web page, a screenshot of a paper or product.
Skip: a talking head or face with no text, a title card that only shows the channel or episode name,
blank or black frames, transitions, b-roll footage without readable content, a near repeat of a frame you already picked.

Reply with JSON only: {{"keep": [numbers]}}
Frames on this sheet: {ids}"""

READ_PROMPT = """You are reading frames from a video so they can be cited as evidence. For each frame below
you get its number, when it was on screen, and what the speaker was saying at that time.

For each frame write:
- "kind": one of slide, chart, table, code, formula, diagram, whiteboard, document, screen, other
- "title": the frame's heading if it has one, else a 3-8 word label
- "text": the key text on screen, copied exactly as shown (keep the original language, do not translate,
  keep numbers and symbols; formulas in LaTeX). Most important lines first, at most about 600 characters.
- "desc": one or two sentences on what the frame shows (for a chart: what is plotted and what stands out)
- "obs": one sentence on how it relates to what is being said, or "" if it does not
- "skip": true if the frame has nothing worth reading after all
Write "title", "desc" and "obs" in {lang}. Only describe what is visible; do not guess beyond the frame.

Reply with JSON only: {{"cards": [{{"i": number, "kind": "...", "title": "...", "text": "...", "desc": "...", "obs": "...", "skip": false}}]}}

{frames}"""

KINDS = ('slide', 'chart', 'table', 'code', 'formula', 'diagram', 'whiteboard', 'document', 'screen', 'other')


def _call(prompt, images):
    """图 + 文 → 文本。走 analyze 的多模态封装（重试、降级链、记账）。测试里替换这个函数。"""
    import analyze
    return analyze._call_gemini_mm(prompt, images, model=config.GEMINI_VISUAL_MODEL, purpose='visual')


def _json(raw):
    import analyze
    return analyze._parse_json_obj(raw) or {}


def triage(sheets):
    """sheets: [(图, [编号])] → 留下的编号集合。某张缩略图读失败就全留（宁可多花一点，不漏）。"""
    keep = set()
    for path, ids in sheets:
        try:
            d = _json(_call(TRIAGE_PROMPT.format(ids=', '.join(f'#{i}' for i in ids)), [path]))
            got = {int(x) for x in d.get('keep') or [] if str(x).lstrip('#').isdigit() or isinstance(x, int)}
        except Exception:  # noqa: BLE001
            got = set(ids)
        keep |= got & set(ids)
    return keep


def read_frames(items, lang):
    """items: [{'i', 'shown', 'said', 'img'}] → {i: card dict}。"""
    out = {}
    for s in range(0, len(items), READ_BATCH):
        batch = items[s:s + READ_BATCH]
        block = '\n\n'.join(
            f"Frame {it['i']} (image {k + 1}), on screen {', '.join(_clock(a) + '-' + _clock(b) for a, b in it['shown'])}.\n"
            f"Speaker at that time: {it['said'] or '(nothing)'}" for k, it in enumerate(batch))
        try:
            d = _json(_call(READ_PROMPT.format(lang=lang, frames=block), [it['img'] for it in batch]))
        except Exception:  # noqa: BLE001
            continue
        ids = {it['i'] for it in batch}
        for c in d.get('cards') or []:
            try:
                i = int(c.get('i'))
            except (TypeError, ValueError):
                continue
            if i in ids and not c.get('skip'):
                out[i] = c
    return out


# ================= 跟转写对上 =================

def _segments(results_dir, task_id):
    import timecode
    try:
        with open(os.path.join(results_dir, task_id, 'transcript.json'), encoding='utf-8') as f:
            segs = json.load(f)
    except (OSError, ValueError):
        return []
    segs = segs.get('segments') if isinstance(segs, dict) else segs
    out = []
    for s in segs or []:
        t = timecode.seconds(s.get('timestamp') or '')
        if t is not None:
            out.append((float(t), re.sub(r'^说话人\d+[：:]\s*', '', s.get('text') or '')))
    return out


def said_during(segs, shown, limit=SAID_CHARS):
    """画面显示的那几段时间里说的话，往前多算 5 秒（常常先说「我们看这张图」再翻页）。"""
    parts = []
    for i, (t, text) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else t + 30
        if any(t < b and nxt > a - 5 for a, b in shown):
            parts.append(text)
    s = ' '.join(parts)
    return s if len(s) <= limit else s[:limit] + '…'


def _lang(segs):
    sample = ''.join(t for _, t in segs[:20])
    cjk = len(re.findall(r'[\u4e00-\u9fff]', sample))
    return 'Chinese (Simplified)' if cjk > len(sample) * 0.2 else 'English'


# ================= 一期 =================

def build(results_dir, task_id, video_path=None, url=None, progress=None):
    """做一期的画面卡，存进 results/<id>/visual/。video_path 没给就按 url 重新下（下完删）。→ cards.json 的内容。"""
    done = load(results_dir, task_id)
    if done:
        return done
    say = progress or (lambda msg: None)
    vdir = visual_dir(results_dir, task_id)
    tmp = tempfile.mkdtemp(prefix='vis_')
    try:
        if not video_path:
            if not url:
                raise RuntimeError('No video source for this recording')
            say('download')
            video_path = download_video(url, tmp)
        say('scan')
        dur, native = probe(video_path)
        fps = min(native, SAMPLE_FPS if dur <= 3600 else 1.0)
        g = sample_gray(video_path, fps)
        keys = keyframes(g, fps)
        del g
        frames = []
        for k, kf in enumerate(keys):
            p = os.path.join(tmp, f'k{k:03d}.jpg')
            if grab(video_path, kf['t'], p):
                frames.append((k, kf['t'], p))
        if not frames:
            raise RuntimeError('No frames could be read from the video')
        say('triage')
        keep = triage(contact_sheets(frames, os.path.join(tmp, 'sheet')))
        segs = _segments(results_dir, task_id)
        items = [{'i': k, 'shown': keys[k]['shown'], 'said': said_during(segs, keys[k]['shown']), 'img': p}
                 for k, t, p in frames if k in keep]
        say('read')
        read = read_frames(items, _lang(segs))
        os.makedirs(vdir, exist_ok=True)
        for old in glob.glob(os.path.join(vdir, 'f_*.jpg')):
            os.remove(old)
        cards = []
        for it in items:
            c = read.get(it['i'])
            if not c:
                continue
            t = keys[it['i']]['t']
            name = f'f_{int(round(t * 10)):06d}.jpg'           # 十分之一秒，同一期里不会撞
            shutil.copyfile(it['img'], os.path.join(vdir, name))
            cards.append({'id': f"v{len(cards)}", 'at': it['shown'][0][0], 'shown': it['shown'], 'img': name,
                          'kind': c.get('kind') if c.get('kind') in KINDS else 'other',
                          'title': str(c.get('title') or '')[:120], 'text': str(c.get('text') or '')[:1200],
                          'desc': str(c.get('desc') or '')[:500], 'obs': str(c.get('obs') or '')[:300],
                          'said': it['said']})
        out = {'version': VERSION, 'task_id': task_id, 'made_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'model': config.GEMINI_VISUAL_MODEL, 'duration': round(dur, 1), 'keyframes': len(frames),
               'triaged': len(items), 'cards': cards}
        with open(os.path.join(vdir, 'cards.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def estimate_usd(duration_s):
    """粗估：费用跟关键帧数走，不跟时长走——实测每张留下的帧约 $0.00055（flash-lite，原图 + 那段话进、几百 token 出）。
    换页密的录屏大约每 8 秒一张，最多 MAX_KEYFRAMES 张，所以一期最多五美分左右。"""
    n = min(MAX_KEYFRAMES, max(3.0, duration_s / 8))
    return round(n * 0.00055 + 0.001, 3)


def run_batch(results_dir, rows, on_episode=None):
    """rows: [{'task_id', 'title', 'url'}]，一期一期做（对 B 站温和一点）。
    on_episode(i, row, status) 每期开始 / 结束时回调。→ 每期一条 {'task_id','title','n','error'?}。"""
    out = []
    for i, row in enumerate(rows):
        if on_episode:
            on_episode(i, row, 'start')
        rec = {'task_id': row['task_id'], 'title': row.get('title') or ''}
        if load(results_dir, row['task_id']):
            rec['cached'] = True                 # 别的项目里做过：不花钱
        try:
            with usage.scope(ref='visual:' + row['task_id']):
                d = build(results_dir, row['task_id'], url=row.get('url'))
            rec['n'] = len(d.get('cards') or [])
        except Exception as e:  # noqa: BLE001
            rec.update(n=0, error=str(e)[:200])
        out.append(rec)
        if on_episode:
            on_episode(i, rec, 'done')
    return out
