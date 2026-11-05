"""agy 引擎（Antigravity CLI 跑 Gemini，走用户的 Gemini 订阅；11-04 用户：「Verbatim 先增加 Flash 和 Argon 选项（默认 Flash）」）。
桩：transcribe_agy._run_json（不真的起 agy 子进程）、split_mp3（不真切音频）、transcribe_openrouter / whisper——不连网、不花钱。"""
import os
import queue
import uuid

from _support import Checks, isolate

TMP = isolate('agy_engine')
os.environ['VOICES_AUTO'] = 'off'
os.environ['OPENROUTER_API_KEY'] = ''
os.environ.pop('DEFAULT_ENGINE', None)
import app as A  # noqa: E402
import config  # noqa: E402
import taskdb  # noqa: E402
import transcribe_agy as TA  # noqa: E402
import transcribe_whisper  # noqa: E402
import usage  # noqa: E402

t = Checks()

# ---- 默认引擎 ----
config.agy_installed = lambda: True
t.check('装了 agy：默认 agy_flash', config.default_engine() == 'agy_flash', config.default_engine())
config.agy_installed = lambda: False
t.check('没装 agy：默认还是 gemini35', config.default_engine() == 'gemini35', config.default_engine())
os.environ['DEFAULT_ENGINE'] = 'whisper'
t.check('DEFAULT_ENGINE 优先', config.default_engine() == 'whisper')
os.environ.pop('DEFAULT_ENGINE')
config.agy_installed = lambda: True

# ---- 首页：装了 agy 就有两个选项、Flash 默认勾上 ----
c = A.app.test_client()
html = c.get('/').get_data(as_text=True)
t.check('首页有 agy_flash / agy_argon 选项', 'value="agy_flash"' in html and 'value="agy_argon"' in html)
t.check('首页默认勾 agy_flash', 'value="agy_flash" checked' in html and 'value="gemini35" checked' not in html)
config.agy_installed = lambda: False
html = c.get('/').get_data(as_text=True)
t.check('没装 agy：不显示、默认勾 gemini35', 'value="agy_flash"' not in html and 'value="gemini35" checked' in html)
config.agy_installed = lambda: True

# ---- 时间戳等比拉回 ----
txt = '\n'.join(f'[{m:02d}:{s:02d}] 句子' for m, s in [(0, 0), (5, 0), (10, 0), (16, 30)])
out = TA.rescale_timestamps(txt, 900)
last = TA._parse(TA._TS.findall(out)[-1])
t.check('Flash 时间戳超出块长 → 拉回块内', last <= 900 and last > 800, out)
t.check('没超就不动', TA.rescale_timestamps('[00:00:00] a\n[00:14:00] b', 900) == '[00:00:00] a\n[00:14:00] b')

# ---- 整条转写：两块、偏移、记账、Flash 被拦换 Argon ----
calls = []
replies = {}


def fake_run(args, timeout, cwd=None):
    if args[:2] == ['-p', '/usage']:
        return {'command': {'data': {'groups': [{'buckets': [
            {'id': 'gemini-5h', 'remaining_fraction': quota_now['gemini-5h']},
            {'id': 'gemini-weekly', 'remaining_fraction': 0.9},
            {'id': 'gemini-argon-wtu-weekly', 'remaining_fraction': quota_now['argon']}]}]}}}
    model = args[args.index('--model') + 1]
    path = args[1].split('：', 1)[1].split('\n', 1)[0]
    calls.append((model, os.path.basename(path)))
    r = replies.get((model, os.path.basename(path)), DENSE)
    return {'status': 'SUCCESS', 'response': r, 'usage': {'input_tokens': 100, 'output_tokens': 20}}


quota_now = {'gemini-5h': 0.9, 'argon': 0.9}
DENSE = '\n'.join(f'[00:{i // 2:02d}:{i % 2 * 30:02d}] 句子{i}' for i in range(20))   # 每 30 秒一行，正常密度
TA._run_json = fake_run


def fake_split(path, secs, out_dir):
    return [(os.path.join(out_dir, 'chunk_0000.mp3'), 0), (os.path.join(out_dir, 'chunk_0001.mp3'), 900)], 1500


TA.split_mp3 = fake_split
src = os.path.join(TMP, 'a.mp3')
open(src, 'wb').write(b'\0' * 1024)

segs, full = TA.transcribe_audio(src, 'agy_flash')
t.check('两块都走 Flash', sorted(calls) == [('gemini-3.8-flash-low', 'chunk_0000.mp3'), ('gemini-3.8-flash-low', 'chunk_0001.mp3')], str(calls))
t.check('第二块加了 15 分钟偏移', any(s['timestamp'] == '00:15:00' for s in segs), str(segs))

calls.clear()
replies[('gemini-3.8-flash-low', 'chunk_0001.mp3')] = "This request was blocked by Gemini's filters. They can occasionally trigger by mistake"
TA._quota_cache.update(t=0, data=None)
segs, full = TA.transcribe_audio(src, 'agy_flash')
t.check('Flash 某块被拦 → 那块换 Argon', ('gemini-4-argon-low', 'chunk_0001.mp3') in calls
        and ('gemini-4-argon-low', 'chunk_0000.mp3') not in calls, str(calls))

replies[('gemini-4-argon-low', 'chunk_0001.mp3')] = replies[('gemini-3.8-flash-low', 'chunk_0001.mp3')]
try:
    TA.transcribe_audio(src, 'agy_flash')
    err = None
except Exception as e:  # noqa: BLE001
    err = e
t.check('Flash、Argon 都拦 → 报内容拦截（app 认得）', err is not None and A._is_content_block(err), str(err))
replies.clear()

# 时间戳太稀：带提醒再要一次，取时间戳多的那份
calls.clear()
replies[('gemini-3.8-flash-low', 'chunk_0000.mp3')] = '[00:00:00] 一整块只有一个时间戳'
strict_seen = []
_orig = fake_run


def fake_run_strict(args, timeout, cwd=None):
    if '--model' in args and '再强调一次' in args[1]:
        strict_seen.append(1)
        return {'status': 'SUCCESS', 'response': '\n'.join(f'[00:{m:02d}:00] 句' for m in range(15)), 'usage': {}}
    return _orig(args, timeout, cwd)


TA._run_json = fake_run_strict
segs, full = TA.transcribe_audio(src, 'agy_flash')
t.check('时间戳太稀 → 带提醒再要一次、用密的那份', strict_seen and sum(1 for s in segs if s['timestamp'] < '00:15:00') >= 15,
        f'{len(strict_seen)} {len(segs)}')
TA._run_json = fake_run
replies.clear()

# Flash 第一次被拦、再试一次就好：不碰 Argon
calls.clear()
_flaky = {'n': 0}


def fake_run_flaky(args, timeout, cwd=None):
    if '--model' in args and 'chunk_0001' in args[1] and 'flash' in args[args.index('--model') + 1]:
        _flaky['n'] += 1
        if _flaky['n'] == 1:
            return {'status': 'SUCCESS', 'response': "This request was blocked by Gemini's filters.", 'usage': {}}
    return fake_run(args, timeout, cwd)


TA._run_json = fake_run_flaky
TA.transcribe_audio(src, 'agy_flash')
t.check('Flash 偶尔误拦：先再试 Flash、不动 Argon', _flaky['n'] == 2 and not any('argon' in m for m, _ in calls), str(calls))

# 权限被拒（音频不在工作目录）：明确报错，不当成空稿
TA._run_json = lambda args, timeout, cwd=None: ({'status': 'SUCCESS', 'response': '', 'usage': {},
                                                 'denied_actions': [{'action': 'read_file'}]}
                                                if '--model' in args else fake_run(args, timeout, cwd))
try:
    TA.transcribe_audio(src, 'agy_flash')
    err = None
except Exception as e:  # noqa: BLE001
    err = e
t.check('read_file 被拒 → 报错写明', err is not None and 'read_file' in str(err), str(err))
TA._run_json = fake_run

# 额度
quota_now['argon'] = 0.02
TA._quota_cache.update(t=0, data=None)
try:
    TA.transcribe_audio(src, 'agy_argon')
    err = None
except Exception as e:  # noqa: BLE001
    err = e
t.check('Argon 周额度只剩 2% → 撞上限（app 认得）', isinstance(err, TA.AgyQuotaOut) and A._is_quota_out(err), str(err))
quota_now['argon'] = 0.9
TA._quota_cache.update(t=0, data=None)

# ---- app 里跑一条：agy_flash 转出来、记成 agy_flash；撞额度落 Whisper ----
A._on_battery = lambda: False
A._check_transcript = lambda *a, **k: None
whisper_calls = [0]


def whisper_tx(path, progress_callback=None):
    whisper_calls[0] += 1
    return [{'start': 1, 'end': 2, 'text': '你好'}]


transcribe_whisper.transcribe_audio = whisper_tx


def run(engine, name):
    tid = str(uuid.uuid4())
    p = os.path.join(TMP, name)
    open(p, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, name, engine, None, p)
    A.run_transcription(tid, p, engine, name, queue.Queue(), fallback_whisper=False, summarize=False)
    row = taskdb.get(tid) or {}
    import json
    mp = os.path.join(config.RESULTS_FOLDER, tid, 'meta.json')
    return row, (json.load(open(mp, encoding='utf-8')).get('engine') if os.path.isfile(mp) else None)


row, eng = run('agy_flash', 'b.mp3')
t.check('app：agy_flash 转写完成', row.get('status') == 'done', str(row.get('error'))[:300])
t.check('app：结果记成 agy_flash', eng == 'agy_flash', str(eng))

quota_now['gemini-5h'] = 0.01
TA._quota_cache.update(t=0, data=None)
A._quota_out.clear()
row, eng = run('agy_flash', 'c.mp3')
t.check('app：Flash 额度用完 → 落 Whisper', row.get('status') == 'done' and eng == 'whisper' and whisper_calls[0] == 1,
        f"{row.get('status')} {eng} {row.get('error')}")

# ---- 接上共用的闸（claude_gate 第三家 agy，11-05）：额度看闸、记进共用账本、撞上限三边一起停 ----
import sqlite3  # noqa: E402
import time  # noqa: E402
import claude_gate  # noqa: E402

os.environ['CLAUDE_GATE'] = os.path.join(TMP, 'gate', 'gate.db')
gate_q = {'gemini-5h': (0.9, 0), 'gemini-weekly': (0.9, 0), 'gemini-argon-wtu-weekly': (0.9, 0)}
claude_gate._agy_probe = lambda: dict(gate_q)
TA._run_json = fake_run
quota_now.update({'gemini-5h': 0.9, 'argon': 0.9})
TA._quota_cache.update(t=0, data=None)
calls.clear()
TA.transcribe_audio(src, 'agy_flash')
g = sqlite3.connect(claude_gate.path(), isolation_level=None)
rows = g.execute("SELECT app, purpose, model FROM calls WHERE provider='agy'").fetchall()
t.check('开了闸：每块记进共用账本（provider=agy）', len(rows) == 2 and rows[0][:2] == ('verbatim', 'transcribe'), str(rows))

gate_q['gemini-weekly'] = (0.3, 0)            # 给用户留四成：Flash 周额度只剩 30%
g.execute('DELETE FROM meta')
try:
    TA.transcribe_audio(src, 'agy_flash')
    err = None
except Exception as e:  # noqa: BLE001
    err = e
t.check('闸说 Gemini 额度要留给用户 → 当撞上限（app 认得、落后备）', isinstance(err, TA.AgyQuotaOut) and A._is_quota_out(err), str(err))
gate_q['gemini-weekly'] = (0.9, 0)
g.execute('DELETE FROM meta')

TA._run_json = lambda args, timeout, cwd=None: ({'status': 'SUCCESS', 'response': 'You have exhausted your quota limit.', 'usage': {}}
                                                if '--model' in args else fake_run(args, timeout, cwd))
try:
    TA.transcribe_audio(src, 'agy_flash')
except Exception:  # noqa: BLE001
    pass
t.check('agy 回撞上限 → 写进共用的 pause（只停 agy）', claude_gate.paused_until('agy') > time.time()
        and not claude_gate.paused_until('claude'))
TA._run_json = fake_run
g.execute('DELETE FROM pauses')

t.finish()
