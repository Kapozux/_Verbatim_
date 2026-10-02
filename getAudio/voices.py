"""声纹：认出录音里是谁在说话——在本机算，音频不出这台电脑。

为什么要它：转写引擎给的「说话人1 / 说话人2」只在一份文件里有意义，下一节课的说话人1 未必是同一个人；
长录音切块转写时，连同一份文件里都会重新编号。让音频大模型判断「两段是不是同一个人」接近瞎猜。
这里用专门的声纹模型（3D-Speaker CAM++ 中文版，经 sherpa-onnx 在本机跑）：

  fingerprint(task_id, 音频)    一条录音转完就做：切出每一段话（谁在什么时候说）、每段算一个声纹向量，
                                连同一份压缩过的单声道音频（试听用）存进 results/<tid>/。一小时音频约 2–3 分钟 CPU。
  identify(项目目录)            把项目里所有录音的声纹放在一起认说话人：说得最多的是主讲（课上就是老师）；
                                其余的声音先在每节课里按相似度分组，跨课很像的连成同一个说话人，有点像的只提示
                                「可能是」。结果和用户的改名 / 合并 / 拆开都存在项目的 voices.json，重算时保留。
  speaker_segments(项目, tid)   每句转写是谁说的（ask.build_speakers 给证据卡标「谁说的」、转写阅读器显示名字）。

认人的单位是「组」：一节课里声音相近的一串话。用户纠错也按组——「这组不是 TA」「这两个是同一个人」——
纠错既是约束（重算之后仍成立），也是校准数据：否掉过的最像的一对，决定以后跨课自动连人的门槛。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import config

MIN_TURN = 0.5       # 短于半秒的片段不算声纹（算不准）
LONG_TURN = 1.5      # 组的声纹只用够长的片段算
T_GROUP = 0.6        # 同一节课里，两组声音平均相似度 ≥ 这个就是同一个人
T_LINK = 0.75        # 跨课连成同一个说话人的默认门槛（用户否掉过更像的会自动调高）
T_LINK_MAX = 0.9
T_SUGGEST = 0.6      # 门槛以下、这个以上：只提示「可能是」，等用户听了确认
MIN_SPEAKER = 10.0   # 一组里够长的话加起来不到 10 秒：不单独编号（很像已有的人就并过去），否则归「未区分」。
                     # 真实课堂录音里 3 秒的门槛会冒出二三十个「学生」，大多是几句插话或杂音
CLIP_MAX = 8.0       # 试听片段最长几秒

REC_FILE = 'voices.npz'
REC_META = 'voices.json'
AUDIO_FILE = 'voice.m4a'
STATE_FILE = 'voices.json'          # 项目目录里的那份（跟录音目录里的同名，各管各的）

ROLES = ('main', 'student', 'group', 'other')


class VoicesUnavailable(RuntimeError):
    """声纹模型没装（或 sherpa-onnx 没装）。"""


# ================= 引擎（模型边界；测试换成假的）=================

class SherpaEngine:
    """pyannote segmentation-3.0 切段 + CAM++ 中文声纹，都是 ONNX，经 sherpa-onnx 在本机 CPU 上跑。"""
    sample_rate = 16000
    model_id = 'campplus-zh-16k+pyannote-seg-3.0'
    FILES = ('campplus_zh.onnx', 'sherpa-onnx-pyannote-segmentation-3-0/model.onnx')

    def __init__(self, models_dir):
        import sherpa_onnx
        self._so = sherpa_onnx
        self.dir = models_dir
        self.threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        self._sd = None
        self._ext = None

    @classmethod
    def installed(cls, models_dir):
        if not all(os.path.isfile(os.path.join(models_dir, f)) for f in cls.FILES):
            return False
        try:
            import sherpa_onnx  # noqa: F401
            return True
        except ImportError:
            return False

    def diarize(self, samples):
        """→ [(起秒, 止秒, 本地编号)]。阈值故意偏严：同一个人被拆成几类没关系，后面按声纹再合；混在一起就分不开了。"""
        so = self._so
        if self._sd is None:
            cfg = so.OfflineSpeakerDiarizationConfig(
                segmentation=so.OfflineSpeakerSegmentationModelConfig(
                    pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=os.path.join(self.dir, self.FILES[1])),
                    num_threads=self.threads),
                embedding=so.SpeakerEmbeddingExtractorConfig(model=os.path.join(self.dir, self.FILES[0]),
                                                             num_threads=self.threads),
                clustering=so.FastClusteringConfig(num_clusters=-1, threshold=0.5),
                min_duration_on=0.3, min_duration_off=0.5)
            self._sd = so.OfflineSpeakerDiarization(cfg)
        return [(r.start, r.end, r.speaker) for r in self._sd.process(samples).sort_by_start_time()]

    def embed(self, samples):
        so = self._so
        if self._ext is None:
            self._ext = so.SpeakerEmbeddingExtractor(so.SpeakerEmbeddingExtractorConfig(
                model=os.path.join(self.dir, self.FILES[0]), num_threads=self.threads))
        st = self._ext.create_stream()
        st.accept_waveform(self.sample_rate, samples)
        st.input_finished()
        return np.array(self._ext.compute(st), dtype=np.float32) if self._ext.is_ready(st) else None


_engine = None
_engine_lock = threading.Lock()


def set_engine(e):
    global _engine
    _engine = e


def models_dir():
    return os.environ.get('VERBATIM_VOICE_MODELS') or os.path.join(config.DATA_DIR, 'models', 'voices')


def engine():
    """装好了就返回引擎，没装返回 None（声纹是可选功能，没有它一切照旧）。"""
    global _engine
    with _engine_lock:
        if _engine is None and SherpaEngine.installed(models_dir()):
            try:
                _engine = SherpaEngine(models_dir())
            except Exception as e:  # noqa: BLE001
                print(f'[voices] engine failed to load: {e}')
        return _engine


def available():
    return engine() is not None


# ================= 一条录音：声纹 =================

class Recording:
    """一条录音切出的每段话：起止（已加上转写的时间偏移）、本地编号、单位化的声纹。"""

    def __init__(self, tid, model, start, end, local, emb):
        self.tid = tid
        self.model = model
        self.start = np.asarray(start, dtype=np.float64)
        self.end = np.asarray(end, dtype=np.float64)
        self.local = np.asarray(local, dtype=np.int64)
        emb = np.asarray(emb, dtype=np.float32)
        n = np.linalg.norm(emb, axis=1, keepdims=True) if len(emb) else np.ones((0, 1))
        self.emb = emb / np.where(n > 0, n, 1)

    @property
    def length(self):
        return self.end - self.start


def _rdir(tid):
    return os.path.join(config.RESULTS_FOLDER, tid)


def _read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = f'{path}.{threading.get_ident()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def rec_meta(tid):
    return _read_json(os.path.join(_rdir(tid), REC_META))


def recording(tid):
    """→ Recording；没做过声纹返回 None。"""
    meta = rec_meta(tid)
    path = os.path.join(_rdir(tid), REC_FILE)
    if not meta or not os.path.isfile(path):
        return None
    try:
        z = np.load(path)
        return Recording(tid, meta.get('model'), z['start'], z['end'], z['local'], z['emb'].astype(np.float32))
    except (OSError, ValueError, KeyError):
        return None


def has_audio(tid):
    return os.path.isfile(os.path.join(_rdir(tid), AUDIO_FILE))


def audio_path(tid):
    return os.path.join(_rdir(tid), AUDIO_FILE)


def decode(path, sample_rate):
    """任意音视频 → 单声道 float32（ffmpeg）。"""
    raw = subprocess.run([config.FFMPEG_BIN, '-v', 'error', '-nostdin', '-i', path, '-vn', '-ac', '1',
                          '-ar', str(sample_rate), '-f', 's16le', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768


def keep_audio(tid, src):
    """留一份单声道 32k AAC（每小时约 15 MB）在 results/<tid>/：试听片段用；换声纹模型时也能拿它重算。"""
    out = audio_path(tid)
    if not os.path.isdir(_rdir(tid)):
        return None
    tmp = out + '.part.m4a'
    try:
        subprocess.run([config.FFMPEG_BIN, '-v', 'error', '-nostdin', '-y', '-i', src, '-vn', '-ac', '1', '-ar', '16000',
                        '-c:a', 'aac', '-b:a', '32k', '-movflags', '+faststart', tmp], capture_output=True, check=True)
        os.replace(tmp, out)
        return out
    except (OSError, subprocess.CalledProcessError) as e:
        print(f'[voices] keep_audio {tid[:8]}: {e}')
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def fingerprint_samples(tid, samples, offset=0.0):
    """切段 + 每段一个声纹，存进 results/<tid>/。同一个模型做过的直接返回。"""
    eng = engine()
    if eng is None:
        raise VoicesUnavailable('voice models are not installed')
    meta = rec_meta(tid)
    if meta and meta.get('model') == eng.model_id and os.path.isfile(os.path.join(_rdir(tid), REC_FILE)):
        return meta
    sr = eng.sample_rate
    rows, embs = [], []
    for s, e, k in eng.diarize(samples):
        if e - s < MIN_TURN:
            continue
        v = eng.embed(samples[int(s * sr):int(e * sr)])
        if v is None:
            continue
        v = np.asarray(v, dtype=np.float32)
        n = float(np.linalg.norm(v))
        if not np.isfinite(n) or n == 0:
            continue
        rows.append((s + offset, e + offset, int(k)))
        embs.append(v / n)
    d = _rdir(tid)
    os.makedirs(d, exist_ok=True)
    arr = np.array(rows, dtype=np.float64).reshape(-1, 3)
    tmp = os.path.join(d, REC_FILE + '.part.npz')
    np.savez(tmp, start=arr[:, 0], end=arr[:, 1], local=arr[:, 2].astype(np.int64),
             # 一句话都没切出来（静音 / 噪声 / 纯音乐）：存一个空表，别在 reshape 上崩
             emb=np.array(embs, dtype=np.float16).reshape(len(embs), -1) if embs else np.zeros((0, 0), np.float16))
    os.replace(tmp, os.path.join(d, REC_FILE))
    meta = {'model': eng.model_id, 'turns': len(rows), 'offset': offset,
            'duration': round(len(samples) / sr, 1), 'created_at': time.strftime('%Y-%m-%d %H:%M:%S')}
    _write_json(os.path.join(d, REC_META), meta)
    return meta


_fp_lock = threading.Lock()


def fingerprint(tid, src, offset=0.0):
    """一条录音的声纹（顺带留试听音频）。很吃 CPU，同一时间只跑一条。"""
    eng = engine()
    if eng is None:
        raise VoicesUnavailable('voice models are not installed')
    with _fp_lock:
        if not has_audio(tid):
            keep_audio(tid, src)
        return fingerprint_samples(tid, decode(src, eng.sample_rate), offset)


_jobs = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='voices')


def queue(tid, src, offset=0.0, cleanup=False, then=None):
    """排进后台（一次一条）。cleanup=True：做完删掉 src（上传来的临时文件）。then(tid)：做完之后回调。"""
    _jobs[tid] = {'state': 'queued'}

    def run():
        _jobs[tid] = {'state': 'running'}
        try:
            (_fingerprint_isolated if _isolated() else fingerprint)(tid, src, offset)
            _jobs[tid] = {'state': 'done'}
            if then:
                then(tid)
        except Exception as e:  # noqa: BLE001
            print(f'[voices] fingerprint {tid[:8]} failed: {e}')
            _jobs[tid] = {'state': 'failed', 'error': str(e)[:300]}
        finally:
            if cleanup:
                try:
                    os.remove(src)
                except OSError:
                    pass
    _pool.submit(run)


def job(tid):
    return _jobs.get(tid)


def _isolated():
    """真声纹引擎放到子进程里跑。sherpa-onnx 的切段整段攥着 GIL：实测 4 分钟音频把主线程卡住 7.2 秒，
    一小时的课约 2 分钟、两小时的节目约 4 分钟——在服务进程里跑，网页和所有接口一起没反应（2026-10-02 外联会话报的
    「服务卡死好几分钟」就是转完长节目紧接着做声纹）。打包版（frozen）没有能 -c 起的 python，也不带声纹模型，照旧本进程；
    测试用的假引擎也在本进程跑。"""
    return isinstance(engine(), SherpaEngine) and not getattr(sys, 'frozen', False)


def _child(tid, src, offset):
    """子进程入口：降一点优先级（别跟正在跑的转写抢 CPU），然后照常做声纹。"""
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    fingerprint(tid, src, offset)


def _fingerprint_isolated(tid, src, offset=0.0):
    code = 'import sys, voices; voices._child(sys.argv[1], sys.argv[2], float(sys.argv[3]))'
    r = subprocess.run([sys.executable, '-c', code, tid, src, str(offset)],
                       cwd=os.path.dirname(os.path.abspath(__file__)), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(((r.stderr or '').strip().splitlines() or [f'exit {r.returncode}'])[-1][:300])


SEARCH_DIRS = ('~/Desktop', '~/Downloads', '~/Music', '~/Movies', '~/Documents')
_AUDIO_EXT = re.compile(r'\.(m4a|mp3|wav|aac|flac|ogg|opus|mp4|mov|m4v|webm|mkv)$', re.I)


def find_original(tid):
    """以前转写的录音没留音频：按 meta.json 里的原文件名，在桌面 / 下载 / 音乐 / 影片 / 文稿（含下一层）里找。
    时长和转写对得上才算（同名不同录音的不要）。"""
    meta = _read_json(os.path.join(_rdir(tid), 'meta.json'), {}) or {}
    name = meta.get('filename') or ''
    if not name or not _AUDIO_EXT.search(name):
        return None
    want = meta.get('duration_seconds')
    for base in SEARCH_DIRS:
        base = os.path.expanduser(base)
        cands = [os.path.join(base, name)]
        try:
            cands += [os.path.join(base, d, name) for d in os.listdir(base)
                      if not d.startswith('.') and os.path.isdir(os.path.join(base, d))]
        except OSError:
            continue
        for p in cands:
            if os.path.isfile(p) and (not want or _close(_probe_duration(p), want)):
                return p
    return None


def _probe_duration(path):
    try:
        out = subprocess.run([config.FFPROBE_BIN, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _close(a, b):
    return a is not None and abs(a - float(b)) <= max(5.0, 0.03 * float(b))


# ================= 一个项目：认说话人 =================

class _Group:
    """一节课里声音相近的一串话。ref = 「tid:最长那段的下标」，跨重算稳定（同一份声纹不变）。"""
    __slots__ = ('tid', 'idx', 'vec', 'seconds', 'long_seconds', 'ref', 'first', 'speaker', 'link', 'suggest')

    def __init__(self, rec, idx):
        self.tid = rec.tid
        self.idx = idx
        L = rec.length[idx]
        use = [i for i in idx if rec.length[i] >= LONG_TURN] or idx
        v = (rec.emb[use] * rec.length[use][:, None]).sum(0)
        self.vec = v / (np.linalg.norm(v) or 1)
        self.seconds = float(L.sum())
        self.long_seconds = float(sum(rec.length[i] for i in idx if rec.length[i] >= LONG_TURN))
        self.ref = f'{rec.tid}:{idx[int(np.argmax(L))]}'
        self.first = float(rec.start[idx].min())
        self.speaker = None
        self.link = None             # 连进这个说话人时的相似度（校准用）
        self.suggest = None

    def top(self, n=3):
        return self.idx[:n]


def _groups(rec):
    """本地编号（引擎分的类，偏碎）→ 按声纹平均相似度合并成组（平均连接聚类，门槛 T_GROUP）。"""
    by_local = defaultdict(list)
    for i in range(len(rec.start)):
        by_local[int(rec.local[i])].append(i)
    keys = list(by_local)
    if not keys:
        return []
    cents = []
    for k in keys:
        idx = by_local[k]
        use = [i for i in idx if rec.length[i] >= LONG_TURN] or idx
        v = (rec.emb[use] * rec.length[use][:, None]).sum(0)
        cents.append(v / (np.linalg.norm(v) or 1))
    C = np.array(cents)
    with np.errstate(all='ignore'):        # numpy 2.0 + macOS Accelerate 的 matmul 会报假的浮点警告
        S = C @ C.T
    members = [[i] for i in range(len(keys))]
    alive = list(range(len(keys)))
    np.fill_diagonal(S, -np.inf)
    while len(alive) > 1:
        sub = S[np.ix_(alive, alive)]
        a, b = np.unravel_index(np.argmax(sub), sub.shape)
        if sub[a, b] < T_GROUP:
            break
        a, b = alive[a], alive[b]
        na, nb = len(members[a]), len(members[b])
        S[a, :] = (S[a, :] * na + S[b, :] * nb) / (na + nb)
        S[:, a] = S[a, :]
        S[a, a] = -np.inf
        members[a] += members[b]
        alive.remove(b)
    out = []
    for a in alive:
        idx = sorted((i for m in members[a] for i in by_local[keys[m]]), key=lambda i: -rec.length[i])
        out.append(_Group(rec, idx))
    return out


def _project_tids(chain_dir):
    st = _read_json(os.path.join(chain_dir, 'chain.json'), {}) or {}
    vids = sorted((v for v in st.get('videos') or [] if v.get('task_id')),
                  key=lambda v: v.get('index') if v.get('index') is not None else 10 ** 6)
    return [v['task_id'] for v in vids], st


def _state(chain_dir):
    return _read_json(os.path.join(chain_dir, STATE_FILE), {}) or {}


def _cjk(s):
    return bool(re.search(r'[一-鿿]', s or ''))


def _teacher_name(project_name):
    m = re.search(r'([一-鿿])老师', project_name or '')
    return m.group(0) if m else ''


def _wording(st):
    """默认叫法：中文 / 英文，课堂 / 一般录音。"""
    zh = st.get('lang', 'zh') == 'zh'
    cls = st.get('classroom', False)
    if zh:
        return {'main': '老师' if cls else '主讲', 'student': '学生{n}' if cls else '发言人{n}',
                '?': '学生（未区分）' if cls else '其他人（未区分）', 'group': '全班' if cls else '多人', 'other': '其他声音'}
    return {'main': 'Teacher' if cls else 'Main speaker', 'student': 'Student {n}' if cls else 'Speaker {n}',
            '?': 'Student (unclear)' if cls else 'Unclear', 'group': 'Whole class' if cls else 'Several people',
            'other': 'Other sound'}


def _display(sp, words):
    if sp.get('name'):
        return sp['name']
    if sp.get('role') == 'student':
        return words['student'].format(n=sp.get('n') or '?')
    return words.get(sp.get('role'), words['student'].format(n=sp.get('n') or '?'))


def _t_link(pairs):
    """否掉过的最像的一对决定门槛：比它高一点，夹在 [T_LINK, T_LINK_MAX]。确认过的不往下调（宁可多分、让人确认，不乱合）。"""
    neg = [p['sim'] for p in pairs or [] if not p.get('same')]
    return round(min(T_LINK_MAX, max([T_LINK] + [s + 0.01 for s in neg])), 3)


_locks = {}
_locks_guard = threading.Lock()


def _lock(chain_dir):
    with _locks_guard:
        return _locks.setdefault(os.path.abspath(chain_dir), threading.RLock())


def identify(chain_dir):
    """项目里所有做过声纹的录音一起认说话人，写 voices.json。纯计算、很快，可以随时重跑；用户的决定都保留。"""
    with _lock(chain_dir):
        order, chain = _project_tids(chain_dir)
        recs = {tid: r for tid in order for r in [recording(tid)] if r is not None and len(r.start)}
        st = _state(chain_dir)
        if not recs:
            if st:
                st.update({'lessons': {}, 'built_at': time.strftime('%Y-%m-%d %H:%M:%S')})
                _write_json(os.path.join(chain_dir, STATE_FILE), st)
            return st
        model = Counter(r.model for r in recs.values()).most_common(1)[0][0]
        recs = {k: r for k, r in recs.items() if r.model == model}
        if st.get('model') and st['model'] != model:          # 换了声纹模型：旧的约束对不上了，从头来
            st = {}
        name = chain.get('author') or ''
        st.setdefault('lang', 'zh' if _cjk(name) or _cjk(_first_text(order)) else 'en')
        teacher = _teacher_name(name)
        st['classroom'] = bool(teacher) or bool(re.search(r'课|老师|class|lesson|lecture', name, re.I))
        old = {s['id']: s for s in st.get('speakers') or []}
        t_link = _t_link(st.get('pairs'))

        groups = [g for tid in order if tid in recs for g in _groups(recs[tid])]
        length = {(r.tid, i): float(r.length[i]) for r in recs.values() for i in range(len(r.start))}
        pins = {tuple(k): s['id'] for s in old.values() for k in s.get('pins') or []}
        nots = defaultdict(set)
        for s in old.values():
            for k in s.get('nots') or []:
                nots[tuple(k)].add(s['id'])

        def pin_of(g):
            votes = Counter()
            for i in g.idx:
                sid = pins.get((g.tid, i))
                if sid:
                    votes[sid] += length[(g.tid, i)]
            return votes.most_common(1)[0][0] if votes else None

        # 两簇有多像 = 两边各取一组、所有组合的相似度按时长加权的平均（平均连接）。
        # 不用质心：质心会被「两边都有点像」的一组串起来，把两个人连成一个（真实课堂数据里见过）
        # 1. 每个够长的组先自成一簇；用户钉在同一个说话人上的组先并在一起
        clusters = []                               # {'groups': [...], 'sum': 加权向量和, 'w': 总时长, 'pin': 说话人 id 或 None}
        by_pin = {}
        small = []
        for g in groups:
            pin = pin_of(g)
            if pin is None and g.long_seconds < MIN_SPEAKER:
                small.append(g)
                continue
            if pin in by_pin:
                c = by_pin[pin]
            else:
                c = {'groups': [], 'sum': np.zeros_like(g.vec), 'w': 0.0, 'pin': pin}
                clusters.append(c)
                if pin:
                    by_pin[pin] = c
            c['groups'].append(g)
            c['sum'] = c['sum'] + g.vec * g.seconds
            c['w'] += g.seconds

        def mean(c):
            return c['sum'] / (c['w'] or 1)

        def banned(a, b):
            """不能并：两个都是用户认过的不同的人；或者一边有组被用户说过「不是」另一边那个人。"""
            if a['pin'] and b['pin'] and a['pin'] != b['pin']:
                return True
            for x, y in ((a, b), (b, a)):
                if y['pin'] and any(y['pin'] in nots.get((g.tid, i), ()) for g in x['groups'] for i in g.idx[:3]):
                    return True
            return False

        # 2. 每次把最像的两簇并起来（质心相似度），直到最像的一对也不到门槛。跟先后顺序无关：
        #    给一个人改名 / 拆开不会顺带改变别人的归属
        while len(clusters) > 1:
            V = np.array([mean(c) for c in clusters])
            with np.errstate(all='ignore'):
                S = V @ V.T
            np.fill_diagonal(S, -np.inf)
            done = False
            while not done:
                a, b = np.unravel_index(np.argmax(S), S.shape)
                if S[a, b] < t_link:
                    done = True
                    break
                if banned(clusters[a], clusters[b]):
                    S[a, b] = S[b, a] = -np.inf
                    continue
                break
            if done:
                break
            ca, cb = clusters[a], clusters[b]
            ca['groups'] += cb['groups']
            ca['sum'] = ca['sum'] + cb['sum']
            ca['w'] += cb['w']
            ca['pin'] = ca['pin'] or cb['pin']
            clusters.pop(b)

        # 3. 太短的组：很像某个说话人（比门槛再高 0.05）就并过去，否则归「未区分」
        for g in small:
            cands = [(float(g.vec @ mean(c)), k) for k, c in enumerate(clusters)
                     if not (c['pin'] and any(c['pin'] in nots.get((g.tid, i), ()) for i in g.idx[:3]))]
            best = max(cands) if cands else (0.0, None)
            if best[1] is not None and best[0] >= t_link + 0.05:
                c = clusters[best[1]]
                c['groups'].append(g)
                c['sum'] = c['sum'] + g.vec * g.seconds
                c['w'] += g.seconds
            else:
                g.speaker = '?'

        live, cl = {}, {}
        for k, c in enumerate(clusters):
            sid = c['pin'] or f'~{k + 1}'
            live[sid] = {'sum': c['sum'], 'w': c['w'], 'groups': c['groups']}
            cl[sid] = c
            for g in c['groups']:
                g.speaker = sid

        # 每组跟「同一个说话人的其余部分」有多像（界面显示、拆开时记作校准数据）
        for sid, e in live.items():
            for g in e['groups']:
                w = e['w'] - g.seconds
                g.link = round(float(g.vec @ (e['sum'] - g.vec * g.seconds) / w), 3) \
                    if len(e['groups']) > 1 and w > 1e-9 else None
        # 「可能是」：两个说话人质心介于 T_SUGGEST 和门槛之间，挂在说得少的那个上
        sids = list(live)
        suggest = {}
        for i, a_ in enumerate(sids):
            for b_ in sids[i + 1:]:
                if banned(cl[a_], cl[b_]):
                    continue
                sim = float(mean(cl[a_]) @ mean(cl[b_]))
                if T_SUGGEST <= sim < t_link:
                    lo, hi = sorted((a_, b_), key=lambda x: sum(g.seconds for g in live[x]['groups']))
                    if lo not in suggest or suggest[lo][1] < sim:
                        suggest[lo] = (hi, round(sim, 3))

        # 3. 新算出来的说话人对回上一次的（看上次记的代表片段落在谁身上），继承 id、名字、编号、角色
        used = {sid for sid in live if not sid.startswith('~')}
        overlap = defaultdict(Counter)
        for s in old.values():
            if s['id'] in used:
                continue
            for k in s.get('anchors') or []:
                k = tuple(k)
                for sid, e in live.items():
                    if sid.startswith('~') and any(g.tid == k[0] and k[1] in g.idx for g in e['groups']):
                        overlap[sid][s['id']] += length.get(k, 0)
        rename = {}
        for sid, cnt in sorted(overlap.items(), key=lambda x: -max(x[1].values())):
            for oid, _ in cnt.most_common():
                if oid not in used:
                    rename[sid] = oid
                    used.add(oid)
                    break
        next_id = 1 + max([int(s[1:]) for s in list(old) + list(used) if re.fullmatch(r'p\d+', s)] or [0])
        for sid in sorted((s for s in live if s.startswith('~')), key=lambda s: int(s[1:])):
            if sid not in rename:
                rename[sid] = f'p{next_id}'
                next_id += 1
        for sid in list(live):
            if sid in rename:
                live[rename[sid]] = live.pop(sid)
        for g in groups:
            g.speaker = rename.get(g.speaker, g.speaker)
        suggest = {rename.get(k, k): (rename.get(v[0], v[0]), v[1]) for k, v in suggest.items()}

        # 4. 汇总每个说话人
        pos = {tid: i for i, tid in enumerate(order)}
        speakers = []
        for sid, e in live.items():
            o = old.get(sid, {})
            gs = sorted(e['groups'], key=lambda g: (pos[g.tid], g.first))
            turns = sorted(((length[(g.tid, i)], g.tid, i) for g in gs for i in g.idx), reverse=True)
            sug = suggest.get(sid)
            speakers.append({
                'id': sid, 'name': o.get('name', ''), 'named': o.get('named', ''), 'role': o.get('role', ''),
                'n': o.get('n'), 'hint': o.get('hint'), 'hint_groups': o.get('hint_groups'),
                'pins': o.get('pins') or [], 'nots': o.get('nots') or [],
                'anchors': [[tid, int(i)] for _, tid, i in turns[:5]],
                'seconds': round(sum(g.seconds for g in gs), 1),
                'lessons': len({g.tid for g in gs}),
                'groups': [{'ref': g.ref, 'tid': g.tid, 'seconds': round(g.seconds, 1), 'sim': g.link} for g in gs],
                'suggest': {'id': sug[0], 'sim': sug[1]} if sug else None,
                'first': (pos[gs[0].tid], gs[0].first),
                'mean': [round(float(x), 4) for x in e['sum'] / (e['w'] or 1)],   # 平均连接用（合并 / 否掉时记相似度）
            })
        if not any(s['role'] == 'main' for s in speakers):
            max(speakers, key=lambda s: s['seconds'])['role'] = 'main'
        for s in speakers:
            s['role'] = s['role'] or 'student'
            if s['role'] == 'main' and not s['name'] and teacher:
                s['name'], s['named'] = teacher, 'project'
        n_used = {s['n'] for s in speakers if s.get('n')}
        nxt = 1
        for s in sorted(speakers, key=lambda s: s['first']):
            if s['role'] == 'student' and not s.get('n'):
                while nxt in n_used:
                    nxt += 1
                s['n'] = nxt
                n_used.add(nxt)
        # 片段：每个说话人挑几段最长的，尽量来自不同的课
        for s in speakers:
            seen, clips = set(), []
            for _, tid, i in sorted(((length[(g.tid, i)], g.tid, i) for g in live[s['id']]['groups'] for i in g.idx),
                                    reverse=True):
                if tid in seen and len(clips) < len({g.tid for g in live[s['id']]['groups']}):
                    continue
                r = recs[tid]
                clips.append([tid, round(float(r.start[i]), 2), round(float(min(r.end[i], r.start[i] + CLIP_MAX)), 2)])
                seen.add(tid)
                if len(clips) >= 3:
                    break
            s['clips'] = clips
            s.pop('first')
        lessons = {}
        for tid, r in recs.items():
            lab = [''] * len(r.start)
            for g in groups:
                if g.tid == tid:
                    for i in g.idx:
                        lab[i] = g.speaker
            lessons[tid] = {'turns': lab, 'unclear': round(sum(g.seconds for g in groups
                                                               if g.tid == tid and g.speaker == '?'), 1)}
        sig = hashlib.sha1(json.dumps([speakers, lessons, _wording(st)], sort_keys=True, ensure_ascii=False)
                           .encode()).hexdigest()[:16]
        if sig != st.get('sig') or not st.get('built_at'):      # 结果没变就不动时间戳（卡片据此判断要不要重标）
            st['built_at'] = time.strftime('%Y-%m-%d %H:%M:%S') + f'.{int(time.time() * 1000) % 1000:03d}'
        st.update({'model': model, 't_link': t_link, 'speakers': speakers, 'lessons': lessons, 'sig': sig})
        st.setdefault('pairs', [])
        _write_json(os.path.join(chain_dir, STATE_FILE), st)
        return st


def _first_text(order):
    for tid in order[:3]:
        segs = _read_json(os.path.join(_rdir(tid), 'transcript.json'), []) or []
        if isinstance(segs, list) and segs:
            return ' '.join(str(s.get('text') or '') for s in segs[:5] if isinstance(s, dict))
    return ''


# ================= 读：每句是谁说的 =================

def names(chain_dir):
    """说话人 id → 显示的名字（含「?」→ 未区分）。卡片上存的是 id，改名立刻生效。"""
    st = _state(chain_dir)
    if not st.get('speakers'):
        return {}
    words = _wording(st)
    out = {s['id']: _display(s, words) for s in st['speakers']}
    out['?'] = words['?']
    return out


_SPK_PREFIX = re.compile(r'^\s*说话人\s*\d+\s*[：:]\s*')


def _seg_times(segs, rec_end):
    from timecode import seconds
    starts = [seconds(s.get('timestamp')) if isinstance(s, dict) else None for s in segs]
    out = []
    for i, s in enumerate(starts):
        nxt = next((x for x in starts[i + 1:] if x is not None and s is not None and x > s), None)
        out.append((s, nxt if nxt is not None else (max(rec_end, (s or 0) + 1) if s is not None else None)))
    return out


def speaker_segments(chain_dir, tid):
    """→ [(秒, 显示名, 说话人 id, 去掉「说话人N：」的正文)]，一句转写一条；这条录音没声纹返回 []。
    一句话的起止 = 这句的时间点到下一句的时间点；跟哪个说话人的话重叠最多就算谁的。"""
    st = _state(chain_dir)
    lesson = (st.get('lessons') or {}).get(tid)
    rec = recording(tid) if lesson else None
    if not rec or len(lesson.get('turns') or []) != len(rec.start):
        return []
    nm = names(chain_dir)
    segs = _read_json(os.path.join(_rdir(tid), 'transcript.json'), []) or []
    if not isinstance(segs, list):
        return []
    labs = lesson['turns']
    out = []
    for sg, (a, b) in zip(segs, _seg_times(segs, float(rec.end.max()) if len(rec.end) else 0)):
        text = _SPK_PREFIX.sub('', str((sg or {}).get('text') or ''))
        if a is None:
            out.append((None, '', '', text))
            continue
        ov = Counter()
        lo = np.maximum(rec.start, a)
        hi = np.minimum(rec.end, b)
        for i in np.nonzero(hi > lo)[0]:
            if labs[i]:
                ov[labs[i]] += float(hi[i] - lo[i])
        if ov:
            sid, sec = ov.most_common(1)[0]
            if sec >= min(1.0, 0.3 * (b - a)):
                out.append((a, nm.get(sid, ''), sid, text))
                continue
        out.append((a, '', '', text))
    return out


# ================= 名单与用户操作 =================

def roster(chain_dir):
    """给界面：说话人（主讲在前，再按说话时长）、覆盖了几节课、哪些课还没声纹。"""
    order, chain = _project_tids(chain_dir)
    st = _state(chain_dir)
    words = _wording(st)
    nm = names(chain_dir)
    sps = []
    for s in st.get('speakers') or []:
        sug = s.get('suggest')
        sps.append({k: s.get(k) for k in ('id', 'role', 'n', 'named', 'seconds', 'lessons', 'groups', 'clips', 'hint')}
                   | {'name': _display(s, words),
                      'suggest': {'id': sug['id'], 'name': nm.get(sug['id'], ''), 'sim': sug['sim']}
                      if sug and sug['id'] in nm else None})
    sps.sort(key=lambda s: (s['role'] != 'main', s['role'] == 'other', -s['seconds']))
    have = [tid for tid in order if rec_meta(tid)]
    titles = {v.get('task_id'): v.get('title') or '' for v in chain.get('videos') or []}
    return {'speakers': sps, 't_link': st.get('t_link', T_LINK), 'available': available(),
            'lessons_total': len(order), 'lessons_with_voices': len(have),
            'unclear_seconds': round(sum((x.get('unclear') or 0) for x in (st.get('lessons') or {}).values()), 1),
            'unclear_label': words['?'],
            'order': [{'task_id': tid, 'title': titles.get(tid, '')} for tid in order],
            'missing': [{'task_id': tid, 'title': titles.get(tid, ''), 'job': job(tid), 'audio': has_audio(tid)}
                        for tid in order if tid not in have],
            'built_at': st.get('built_at')}


def act(chain_dir, action, speaker=None, **kw):
    """用户的决定：rename / role / merge / detach / not_same / accept_hint。存成约束，然后重算。"""
    with _lock(chain_dir):
        st = _state(chain_dir)
        sps = {s['id']: s for s in st.get('speakers') or []}
        sp = sps.get(speaker)
        if sp is None:
            raise ValueError('unknown speaker')

        def pin_all(s):
            have = {tuple(k) for k in s.get('pins') or []}
            for g in s.get('groups') or []:
                tid, i = g['ref'].split(':')
                if (tid, int(i)) not in have:
                    s.setdefault('pins', []).append([tid, int(i)])

        if action == 'rename':
            name = str(kw.get('name') or '').strip()[:40]
            sp['name'], sp['named'] = name, 'user' if name else ''
            pin_all(sp)
        elif action == 'accept_hint':
            if not sp.get('hint'):
                raise ValueError('no hint')
            sp['name'], sp['named'] = sp['hint']['name'], 'user'
            pin_all(sp)
        elif action == 'role':
            role = kw.get('role')
            if role not in ROLES:
                raise ValueError('bad role')
            if role == 'main':
                for o in sps.values():
                    if o['role'] == 'main' and o is not sp:
                        o['role'] = 'student'
                        pin_all(o)
            sp['role'] = role
            if sp.get('named') == 'project' and role != 'main':
                sp['name'], sp['named'] = '', ''
            pin_all(sp)
        elif action == 'merge':
            into = sps.get(kw.get('into'))
            if into is None or into is sp:
                raise ValueError('unknown target')
            st.setdefault('pairs', []).append({'sim': round(_cos(sp, into), 3), 'same': True})
            pin_all(into)
            for g in sp.get('groups') or []:
                tid, i = g['ref'].split(':')
                into.setdefault('pins', []).append([tid, int(i)])
            into['nots'] = (into.get('nots') or []) + [k for k in sp.get('nots') or []]
            if not into.get('named') and sp.get('named') == 'user':
                into['name'], into['named'] = sp['name'], 'user'
            st['speakers'] = [s for s in st['speakers'] if s is not sp]
        elif action == 'detach':
            g = next((g for g in sp.get('groups') or [] if g['ref'] == kw.get('group')), None)
            if g is None:
                raise ValueError('unknown group')
            tid, idx = _group_turns(g['ref'])
            inside = set(idx)
            sp['pins'] = [k for k in sp.get('pins') or [] if k[0] != tid or int(k[1]) not in inside]
            sp.setdefault('nots', []).extend([tid, int(i)] for i in idx[:3])
            sp['groups'] = [x for x in sp.get('groups') or [] if x['ref'] != g['ref']]
            pin_all(sp)                    # TA 剩下的组钉住：「不是 TA」才有一个确定的 TA
            if g.get('sim') is not None:
                st.setdefault('pairs', []).append({'sim': g['sim'], 'same': False})
        elif action == 'not_same':
            other = sps.get(kw.get('other'))
            if other is None:
                raise ValueError('unknown target')
            st.setdefault('pairs', []).append({'sim': round(_cos(sp, other), 3), 'same': False})
            for g in sp.get('groups') or []:
                tid, i = g['ref'].split(':')
                other.setdefault('nots', []).append([tid, int(i)])
            pin_all(sp)
            pin_all(other)
            if (sp.get('suggest') or {}).get('id') == other['id']:
                sp['suggest'] = None
        else:
            raise ValueError('unknown action')
        _write_json(os.path.join(chain_dir, STATE_FILE), st)
        identify(chain_dir)
        return roster(chain_dir)


def _cos(a, b):
    """两个说话人的平均连接相似度（跟认人时用的同一个量，校准门槛才对得上）。"""
    va, vb = np.array(a.get('mean') or [0.0]), np.array(b.get('mean') or [0.0])
    return float(va @ vb) if va.shape == vb.shape else 0.0


def _group_turns(ref):
    """组的 ref → (tid, 这组的段下标，最长的在前)。分组是确定的（同一份声纹分出来一样），重算一遍就知道。"""
    tid, i = ref.split(':')
    rec = recording(tid)
    g = next((g for g in _groups(rec) if g.ref == ref), None) if rec is not None else None
    return tid, (g.idx if g else [int(i)])


# ================= 起名字：只认原话里真点到的名字 =================

NAME_PROMPT = """Below are excerpts from a recording{title}. In each excerpt, the lines before ">>" are said by other people
(usually the teacher / host), and the lines starting with ">>" are all said by ONE person we want to name.
If someone clearly calls that person by name right before they speak (e.g. "小明，你来说说" / "Tom, go ahead"),
give the name and copy that sentence exactly as written. If there is no such sentence, return empty strings.
Return JSON only: {{"name": "...", "quote": "..."}}

{text}"""

_NORM = re.compile(r'[\s，。！？、,.!?:：；;“”"\'（）()《》…—-]+')


def _norm(s):
    return _NORM.sub('', (s or '').lower())


def suggest_names(chain_dir, llm=None, min_seconds=MIN_SPEAKER):
    """没名字的说话人：把 TA 开口前别人说的话给便宜模型看，问有没有点名。模型给的原话必须在转写里找得到、
    且包含那个名字，才作为建议（hint）显示；从不自动改名。每个说话人只问一次（组变多了再问）。"""
    if llm is None:
        import ask

        def llm(p):
            return ask._llm(p, model=ask.TAG_MODEL, purpose='voices')
    st = _state(chain_dir)
    found = {}
    todo = [s for s in st.get('speakers') or [] if s.get('role') == 'student' and s.get('named') != 'user'
            and s.get('seconds', 0) >= min_seconds and s.get('hint_groups') != len(s.get('groups') or [])]
    for s in todo:
        ctx, before = [], []
        for g in s.get('groups') or []:
            segs = speaker_segments(chain_dir, g['tid'])
            for j, (sec, _, sid, text) in enumerate(segs):
                if sid != s['id'] or (j and segs[j - 1][2] == s['id']):
                    continue
                prev = [x[3] for x in segs[max(0, j - 2):j] if x[2] != s['id']]
                mine = [x[3] for x in segs[j:j + 3] if x[2] == s['id']]
                if prev and mine:
                    ctx.append('\n'.join(prev + ['>> ' + m for m in mine]))
                    before += [(p, g['tid'], sec) for p in prev]
                if len(ctx) >= 12:
                    break
            if len(ctx) >= 12:
                break
        hint = None
        if ctx:
            try:
                raw = llm(NAME_PROMPT.format(title='', text='\n\n---\n'.join(ctx)[:6000]))
                obj = raw if isinstance(raw, dict) else _json(raw)
                name, quote = str(obj.get('name') or '').strip()[:20], str(obj.get('quote') or '').strip()[:200]
                if name and quote and _norm(name) in _norm(quote):
                    hit = next(((tid, sec) for p, tid, sec in before if _norm(quote) in _norm(p)), None)
                    if hit:
                        hint = {'name': name, 'quote': quote, 'tid': hit[0], 'sec': hit[1]}
            except Exception as e:  # noqa: BLE001  起名字是锦上添花
                print(f'[voices] name hint failed: {e}')
                continue
        s['hint'], s['hint_groups'] = hint, len(s.get('groups') or [])
        if hint:
            found[s['id']] = hint
    if todo:
        with _lock(chain_dir):
            cur = _state(chain_dir)
            by = {s['id']: s for s in todo}
            for s in cur.get('speakers') or []:
                if s['id'] in by:
                    s['hint'], s['hint_groups'] = by[s['id']]['hint'], by[s['id']]['hint_groups']
            _write_json(os.path.join(chain_dir, STATE_FILE), cur)
    return found


def _json(raw):
    m = re.search(r'\{.*\}', raw or '', re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}
