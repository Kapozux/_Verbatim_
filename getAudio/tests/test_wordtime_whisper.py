"""Gemini 转写的期用本机 Whisper 补逐词时间轴（ADR 0012）：Gemini 的时间点是模型写的、一段二三十秒还会漂；
转写存好后把音频留一份排队，本机 Whisper 带逐词时间转一遍存 words.json，转完删音频。
10-04 实测：Whisper large-v3 跟 YouTube 字幕逐词比中位数差 0.24 秒，10 分钟音频 54 秒。
测试里把真正跑 Whisper 的那一步换掉（wordtime.whisper_words）。"""
import json
import os
import queue
import time
import uuid

from _support import Checks, isolate

TMP = isolate('wordtime_whisper')
import app as A  # noqa: E402
import wordtime  # noqa: E402

t = Checks()
runs = []


def fake_whisper(path):
    runs.append(os.path.basename(path))
    return [(3.2, 'hello'), (3.6, 'world')]


_real_ww = wordtime.whisper_words
wordtime.whisper_words = fake_whisper


def wait(cond, sec=3):
    for _ in range(int(sec / 0.05)):
        if cond():
            return True
        time.sleep(0.05)
    return False


audio = os.path.join(TMP, 'a.mp3')
open(audio, 'wb').write(b'\0' * 4096)

# ---- 开关 ----
tid0 = str(uuid.uuid4())
t.check('WORDTIME_WHISPER 没开（测试默认）：不排队', wordtime.queue_whisper(tid0, audio) is False)
# 11-06 用户：逐词时间不要了，改成可选、默认关（本机 Whisper 跟着 600 期频道跑，GPU 显存涨到 35G、电脑卡死）
os.environ.pop('WORDTIME_WHISPER', None)
t.check('没设 WORDTIME_WHISPER：默认不排队', wordtime.queue_whisper(tid0, audio) is False)
os.environ['WORDTIME_WHISPER'] = '1'

# ---- 每跑完一次 mlx Whisper 就把 GPU 缓存还回去（不然 MLX 的缓存只涨不降）----
import sys  # noqa: E402
import types  # noqa: E402
import transcribe_whisper as _tw  # noqa: E402
cleared = []
_fake_mx = types.ModuleType('mlx.core')
_fake_mx.clear_cache = lambda: cleared.append(1)
_fake_mlx = types.ModuleType('mlx')
_fake_mlx.core = _fake_mx
_fake_mw = types.ModuleType('mlx_whisper')
_fake_mw.transcribe = lambda path, **kw: {'segments': [{'words': [{'start': 1.0, 'word': ' hi'}]}]}
_saved = {k: sys.modules.get(k) for k in ('mlx', 'mlx.core', 'mlx_whisper')}
sys.modules.update({'mlx': _fake_mlx, 'mlx.core': _fake_mx, 'mlx_whisper': _fake_mw})
_orig_get_model = _tw._get_model
_tw._get_model = lambda: ('fake-repo', 'mlx')
t.check('mlx Whisper 跑完清 GPU 缓存', _real_ww(audio) == [(1.0, 'hi')] and cleared == [1], str(cleared))
_tw._get_model = _orig_get_model
for k, v in _saved.items():
    if v is None:
        sys.modules.pop(k, None)
    else:
        sys.modules[k] = v

# ---- 排队：留一份音频 → Whisper → 存 words.json（偏移加上）→ 删那份音频 ----
tid = str(uuid.uuid4())
os.makedirs(os.path.join(TMP, 'results', tid))
t.check('排上队', wordtime.queue_whisper(tid, audio, offset_sec=600) is True)
p = os.path.join(TMP, 'results', tid, 'words.json')
ok = wait(lambda: os.path.isfile(p))
w = json.load(open(p)) if ok else {}
t.check('Whisper 跑完存 words.json：来源 whisper、片段偏移加上', w.get('source') == 'whisper'
        and w.get('words') == [[603.2, 'hello'], [603.6, 'world']], json.dumps(w)[:200])
t.check('留的那份音频用完删掉、原音频不动', wait(lambda: not os.listdir(wordtime.align_dir())) and os.path.isfile(audio),
        str(os.listdir(wordtime.align_dir())))
t.check('已经有时间轴的不再排', wordtime.queue_whisper(tid, audio) is False)

# ---- 端到端：gemini 转写、来源不是 YouTube → 排 Whisper；YouTube 来源走字幕那条，不排 Whisper ----
import transcribe_gemini  # noqa: E402
transcribe_gemini.transcribe_audio = lambda path, progress_callback=None: (
    [{'timestamp': '00:00:01', 'text': 'Hello world'}], '[00:00:01] Hello world')
runs.clear()
tid2 = str(uuid.uuid4())
src = os.path.join(TMP, 'b.mp3')
open(src, 'wb').write(b'\0' * 4096)
A.run_transcription(tid2, src, 'gemini', 'b.mp3', queue.Queue(), summarize=False,
                    extra_meta={'source_url': 'https://www.bilibili.com/video/BV1xx411c7mD'})
t.check('B 站来源的 gemini 转写：排了本机 Whisper', wait(lambda: os.path.isfile(os.path.join(TMP, 'results', tid2, 'words.json'))),
        str(runs))
runs.clear()
tid3 = str(uuid.uuid4())
src3 = os.path.join(TMP, 'c.mp3')
open(src3, 'wb').write(b'\0' * 4096)
A.run_transcription(tid3, src3, 'gemini', 'c.mp3', queue.Queue(), summarize=False,
                    extra_meta={'source_url': 'https://www.youtube.com/watch?v=yslXlV2BP_Y'})
time.sleep(0.5)
t.check('YouTube 来源的不排 Whisper（字幕更省）', not runs, str(runs))
os.environ['WORDTIME_WHISPER'] = '0'

t.finish()
