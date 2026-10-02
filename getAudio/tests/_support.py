"""测试公用：隔离的数据目录、合成数据、模型边界的桩、结果汇总。

用法（每个 test_*.py 的开头，必须在 import app / config 之前）：

    from _support import isolate, Checks
    TMP = isolate('projects')          # 临时数据目录、假 key、禁止外网
    import app as A                    # 之后才 import 生产代码

为什么每个测试文件单独一个进程（tests/run.py 负责）：config 在 import 时就定下数据目录和 key，
app 在 import 时注册路由、建线程池——同一个进程里没法干净地重来。

三条安全规矩（防止测试花钱、碰真实数据）：
  · 数据目录是临时目录，跑完删掉；
  · API key 换成假的（.env 不覆盖已有的环境变量，所以要在 import config 之前设）；
  · 除本机（墨页 127.0.0.1:8765 之类）以外一律不许连网——漏打桩的模型调用会当场报错，而不是悄悄花钱。
"""
import atexit
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCAL = {'127.0.0.1', 'localhost', '::1', '0.0.0.0'}


class NetworkBlocked(RuntimeError):
    pass


def _guard_network():
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def host_of(addr):
        return addr[0] if isinstance(addr, tuple) else str(addr)

    def connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and host_of(addr) not in _LOCAL:
            raise NetworkBlocked(f'test tried to reach {addr} — stub this call')
        return real_connect(self, addr)

    def create_connection(addr, *a, **k):
        if host_of(addr) not in _LOCAL:
            raise NetworkBlocked(f'test tried to reach {addr} — stub this call')
        return real_create(addr, *a, **k)

    socket.socket.connect = connect
    socket.create_connection = create_connection


def isolate(prefix, cheap_route=False):
    """临时数据目录 + 假 key + 禁外网；返回数据目录。必须在 import 任何生产模块之前调用。"""
    tmp = tempfile.mkdtemp(prefix=f'verbatim_{prefix}_')
    os.environ['GETAUDIO_DATA_DIR'] = tmp
    os.environ['GEMINI_API_KEY'] = 'test-not-a-real-key'
    os.environ['DASHSCOPE_API_KEY'] = 'test-key' if cheap_route else ''
    os.environ['OPENROUTER_API_KEY'] = ''
    os.environ['CHEAP_TEXT_ROUTE'] = 'on' if cheap_route else 'off'
    os.makedirs(os.path.join(tmp, 'results', '_chains'))
    # 本机开着代理（Clash 之类）时，HTTP 客户端会先连 127.0.0.1 的代理、由代理转发出去——
    # 下面的「只许连本机」挡不住这一跳，所以代理变量一律清掉
    for k in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ.pop(k, None)
    if SRC not in sys.path:
        sys.path.insert(0, SRC)
    _guard_network()
    atexit.register(shutil.rmtree, tmp, True)
    return tmp


# ---------------- 合成数据（不用真实资料库里的东西）----------------

def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)


def make_transcript(results_dir, segments, title='Episode', creator='', url='', engine='gemini35', tid=None):
    """results/<tid>/transcript.json + meta.json。segments: [(timestamp, text), ...]。返回 tid。"""
    tid = tid or os.urandom(16).hex()
    d = os.path.join(results_dir, tid)
    write_json(os.path.join(d, 'transcript.json'), [{'timestamp': ts, 'text': tx} for ts, tx in segments])
    write_json(os.path.join(d, 'meta.json'), {
        'id': tid, 'ai_title': title, 'filename': title + '.m4a', 'creator': creator, 'source_url': url,
        'engine': engine, 'date': '2026-09-01 10:00:00', 'duration_seconds': 60 * len(segments),
        'segment_count': len(segments), 'char_count': sum(len(t) for _, t in segments)})
    return tid


def make_creator(app_module, name, handle, episodes, portrait=True, transcripts=False, kind=None):
    """一个分析过的博主链条：每期一份 cards_NNN.json。episodes: [[quote, ...], ...]。
    transcripts=True 时同时写出每期的转写（问答「转写全文入索引」和项目加录音要用）。返回链条 id。"""
    A = app_module
    cid = os.urandom(16).hex()
    d = A._chain_dir(cid)
    os.makedirs(d)
    vids = []
    for i, quotes in enumerate(episodes):
        title = f'{name} episode {i + 1}'
        tid = (make_transcript(A.config.RESULTS_FOLDER, [(f'00:0{j}:10', q) for j, q in enumerate(quotes)],
                               title=title, creator=name) if transcripts else os.urandom(16).hex())
        vids.append({'index': i, 'title': title, 'task_id': tid, 'status': 'done',
                     'video_url': f'https://www.youtube.com/watch?v={tid[:11]}', 'upload_date': f'2026090{i + 1}'})
        write_json(os.path.join(d, f'cards_{i:03d}.json'), {
            'task_id': tid, 'title': title,
            'cards': [{'quote': q, 'obs': f'{name} says {q}', 'timestamp': f'00:0{j}:10', 'layer': '他的主张'}
                      for j, q in enumerate(quotes)]})
    state = {'id': cid, 'url': f'https://www.youtube.com/@{handle}' if handle else '', 'author': name,
             'stage': 'done', 'videos': vids, 'created_at': '2026-09-01 00:00:00'}
    if kind:
        state['kind'] = kind
    if portrait:
        state['final_doc'] = '总分析.md'
        with open(os.path.join(d, '总分析.md'), 'w', encoding='utf-8') as f:
            f.write(f'# {name}\n\n## 他怎么看\n{name} believes in small teams.\n')
    A._save_chain(state)
    return cid


def fake_embed(texts, task=None):
    """确定性的向量：同一段文字永远同一个向量，不调模型。"""
    import numpy as np
    out = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest()
        v = np.frombuffer((h * 96)[:768 * 4], dtype=np.uint8)[:768].astype(np.float32) - 128
        out.append(v / np.linalg.norm(v))
    return np.stack(out)


def moye_alive():
    try:
        import sources
        return sources.moye_alive(timeout=1)
    except Exception:  # noqa: BLE001
        return False


# ---------------- 结果汇总 ----------------

class Checks:
    """收集每一项检查；finish() 打印汇总并以退出码报告（run.py 看退出码）。
    skip(name, why)：依赖的本机服务没开时跳过，不算失败。"""

    def __init__(self):
        self.results = {}
        self.skipped = {}

    def check(self, name, ok, detail=''):
        self.results[name] = bool(ok)
        mark = '通过' if ok else '失败'
        print(f'  [{mark}] {name}' + (f' — {detail}' if detail and not ok else ''))
        return bool(ok)

    def skip(self, name, why):
        self.skipped[name] = why
        print(f'  [跳过] {name} — {why}')

    def finish(self):
        bad = [k for k, v in self.results.items() if not v]
        print(f'\n结论：{len(self.results) - len(bad)} 通过，{len(bad)} 失败，{len(self.skipped)} 跳过'
              + (f'；失败：{", ".join(bad)}' if bad else ''))
        sys.stdout.flush()
        # 有后台线程（索引、生成）还在跑时 sys.exit 会等它们；os._exit 直接收工（临时目录由 atexit 外的这一步清掉）
        shutil.rmtree(os.environ.get('GETAUDIO_DATA_DIR', ''), ignore_errors=True)
        os._exit(1 if bad else 0)
