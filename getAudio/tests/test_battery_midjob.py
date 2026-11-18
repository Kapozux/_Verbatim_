"""本地 Whisper 转到一半拔电（GitHub #4）：以前只在开跑前查一次电源，插着电开一个几小时的长文件、
中途拔掉，会一直满载跑到完。现在转写过程中隔一阵再查：拔了就原地暂停等插电，等太久才放弃。
另外 mlx（Apple Silicon 默认）整段一次调完、以前根本不报中间进度，要接上它自己的 tqdm 才查得到；
进度回调里抛的错不能被「mlx 出错 → 退回 CPU 重跑」那条兜底吞掉。

跑生产代码：app.run_transcription（engine=whisper）、transcribe_whisper.transcribe_audio（mlx 分支）。
桩：app._on_battery（假电源）、transcribe_whisper.transcribe_audio / _get_model、假的 mlx_whisper 模块
（照真库的写法：transcribe 里用模块级 `tqdm.tqdm(total=…)` 报帧数）。
"""
import json
import os
import queue
import sys
import types
import uuid

from _support import Checks, isolate

TMP = isolate('battery_midjob')
os.environ['VOICES_AUTO'] = 'off'
import app as A  # noqa: E402
import taskdb  # noqa: E402
import transcribe_whisper as W  # noqa: E402

t = Checks()
A._BATTERY_RECHECK_S = 0      # 每次进度回调都查
A._POWER_POLL_S = 0           # 等插电时不真睡

power = {'battery': False, 'checks': 0, 'plug_back_after': None}


def fake_on_battery():
    power['checks'] += 1
    if power['plug_back_after'] is not None and power['battery']:
        power['plug_back_after'] -= 1
        if power['plug_back_after'] < 0:
            power['battery'] = False
    return power['battery']


A._on_battery = fake_on_battery
progress_seen = []
real_transcribe_audio = W.transcribe_audio


def fake_whisper(path, progress_callback=None):
    progress_callback(10)
    power['battery'] = True          # 转到一成拔电
    progress_callback(50)
    progress_seen.append(50)
    progress_callback(99)
    return [{'start': 1, 'end': 2, 'text': '你好'}]


W.transcribe_audio = fake_whisper


def run(name):
    tid = str(uuid.uuid4())
    src = os.path.join(TMP, name)
    open(src, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, name, 'whisper', None, src)
    q = queue.Queue()
    A.run_transcription(tid, src, 'whisper', name, q, summarize=False)
    msgs = []
    while not q.empty():
        msgs.append(json.loads(q.get()))
    return taskdb.get(tid) or {}, msgs


# 1) 中途拔电、过两轮又插回 → 暂停后接着跑完
power.update(battery=False, checks=0, plug_back_after=2)
r, msgs = run('a.mp3')
t.check('中途拔电又插回：任务照样完成', r.get('status') == 'done', f"{r.get('status')} {str(r.get('error'))[:200]}")
t.check('转写过程中查过电源（不只开头一次）', power['checks'] >= 3, power['checks'])
t.check('拔电时给前端发了「暂停等插电」', any(m.get('type') == 'queued' and 'battery' in m.get('message', '').lower()
                                         for m in msgs), [m.get('message') for m in msgs][:8])
t.check('暂停事件带原因码，页面靠它显示在等电源', any(m.get('reason') == 'battery_paused' for m in msgs),
        [m.get('reason') for m in msgs if m.get('type') == 'queued'])
_js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'app.js'), encoding='utf-8').read()
_i18n = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'i18n.js'), encoding='utf-8').read()
t.check('页面按原因码换文案，两种语言都有', all(
    f"'{r}'" in _js and f"T('rowStatus.{k}')" in _js and _i18n.count(f"'rowStatus.{k}'") == 2
    for r, k in (('battery', 'waitingPower'), ('battery_paused', 'pausedOnBattery'))))

# 2) 中途拔电、一直没插回 → 等满时限放弃，任务失败、说清原因
A._POWER_WAIT_S = 0
power.update(battery=False, checks=0, plug_back_after=None)
progress_seen.clear()
r, msgs = run('b.mp3')
t.check('一直没插回：任务失败', r.get('status') == 'failed', r.get('status'))
t.check('失败原因说是电池', '电池' in str(r.get('error')), str(r.get('error'))[:200])
t.check('拔电之后没接着往下转', not progress_seen, progress_seen)

# 3) mlx 分支：接上 tqdm 报中间进度；进度回调抛错不触发「退回 CPU 重跑」
W.transcribe_audio = real_transcribe_audio
fake_mlx = types.ModuleType('mlx_whisper')
fake_mlx_t = types.ModuleType('mlx_whisper.transcribe')
import tqdm as _tqdm  # noqa: E402
fake_mlx_t.tqdm = _tqdm


def mlx_transcribe(path, **kw):
    with sys.modules['mlx_whisper.transcribe'].tqdm.tqdm(total=1000, unit='frames', disable=True) as pbar:
        for _ in range(4):
            pbar.update(250)
    return {'segments': [{'start': 0, 'end': 1, 'text': 'hi'}]}


fake_mlx.transcribe = mlx_transcribe
fake_mlx_t.transcribe = mlx_transcribe
sys.modules['mlx_whisper'] = fake_mlx
sys.modules['mlx_whisper.transcribe'] = fake_mlx_t
W._get_model = lambda: ('fake-repo', 'mlx')
W.free_gpu = lambda: None
fallback = []
W._transcribe_faster = lambda *a, **k: fallback.append('faster') or []

pcts = []
segs = W.transcribe_audio('x.wav', progress_callback=pcts.append)
t.check('mlx 也报中间进度', any(5 < p < 99 for p in pcts), pcts)
t.check('mlx 跑完把 tqdm 还原', sys.modules['mlx_whisper.transcribe'].tqdm is _tqdm)
t.check('mlx 正常出字', segs and segs[0]['text'] == 'hi', segs)

# 别的线程同时在跑 mlx_whisper.transcribe（podcast._words 用的是另一把锁）：它的进度不算到这个任务头上
import threading  # noqa: E402
other = []


def mlx_with_bystander(path, **kw):
    th = threading.Thread(target=lambda: other.append(mlx_transcribe('other.wav')))
    th.start()
    th.join(10)
    return mlx_transcribe(path)


fake_mlx.transcribe = mlx_with_bystander
pcts2 = []
W.transcribe_audio('x.wav', progress_callback=pcts2.append)
fake_mlx.transcribe = mlx_transcribe
t.check('别的线程的 mlx 进度不进这个任务的回调', other and pcts2 == pcts, f'{pcts2} vs {pcts}')


class Stop(Exception):
    pass


def boom(p):
    if p > 5:
        raise Stop('battery')


os.environ.pop('WHISPER_DISABLE_MLX', None)
try:
    W.transcribe_audio('x.wav', progress_callback=boom)
    t.check('进度回调抛的错原样抛出', False, '没抛')
except Stop:
    t.check('进度回调抛的错原样抛出', True)
t.check('不因此退回 CPU 重跑', not fallback and not os.environ.get('WHISPER_DISABLE_MLX'), fallback)
t.check('抛错后 tqdm 也还原', sys.modules['mlx_whisper.transcribe'].tqdm is _tqdm)

t.finish()
