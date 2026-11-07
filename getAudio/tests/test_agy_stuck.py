"""agy 单片卡住就掐掉重来（2026-11-07，用户：「15 分钟的一个切片是不是有一个固定的转写时间…如果超过 1.5 倍时间，自动删了重试」）。
gate.db 里 1319 次 Flash 实测：15 分钟一片中位 40–50 秒、九成在 80 秒内；卡住的（思考绕圈、输出上万 token）要跑 300–500 秒才报 ERROR。
所以 Flash 每分钟音频给 8 秒（15 分钟 = 120 秒、最少 30 秒），到点掐掉 agy 马上重来，不等 20 秒；最多掐 2 次，第 3 次按原来的 900 秒等。
桩：transcribe_agy._run_json（假 agy：按计划「卡住」= 抛 TimeoutExpired，记下每次给的超时）、split_mp3。"""
import os
import subprocess
import threading
import time

from _support import Checks, isolate

TMP = isolate('agy_stuck')
os.environ['AGY_FLASH_CHUNKS'] = '6'
os.environ['AGY_ARGON_CHUNKS'] = '2'
os.environ['AGY_FLASH_CONCURRENCY'] = ''
os.environ['AGY_QUOTA_WAIT'] = ''
os.environ['AGY_STUCK_PER_MIN'] = ''
os.environ['AGY_TIMEOUT_S'] = ''
import transcribe_agy as TA  # noqa: E402

t = Checks()
lock = threading.Lock()
calls = []          # (片名, 模型, 给的超时)
STUCK = {}          # 片名 → 还要卡几次
BLOCK = set()
DENSE = '\n'.join(f'[00:{i // 2:02d}:{i % 2 * 30:02d}] 句子{i}' for i in range(30))


def fake_run(args, timeout, cwd=None):
    if args[:2] == ['-p', '/usage']:
        return {'command': {'data': {'groups': []}}}
    model = args[args.index('--model') + 1]
    name = os.path.basename(args[1].split('：', 1)[1].split('\n', 1)[0])
    with lock:
        calls.append((name, model, timeout))
        stuck = STUCK.get(name, 0) > 0
        if stuck:
            STUCK[name] -= 1
    if stuck:
        raise subprocess.TimeoutExpired(['agy'], timeout)
    if 'flash' in model and name in BLOCK:
        return {'status': 'SUCCESS', 'response': "This request was blocked by Gemini's filters.", 'usage': {}}
    return {'status': 'SUCCESS', 'response': DENSE, 'usage': {}}


def splitter(lens, tag):
    def split(path, secs, out_dir):
        out, off = [], 0
        for i, n in enumerate(lens):
            out.append((os.path.join(out_dir, f'{tag}_{i:04d}.mp3'), off))
            off += n
        return out, off
    return split


def timeouts(name):
    return [c[2] for c in calls if c[0] == name]


TA._run_json = fake_run
TA.FLAKY_WAITS = (5, 5, 5)          # 卡住要马上重来：要是走了「出错等一会」这条路，测试会慢出 5 秒
src = os.path.join(TMP, 'a.mp3')
open(src, 'wb').write(b'\0' * 1024)

# 1. 掐的时间按片长算：15 分钟 120 秒，5 分钟 40 秒，2 分钟最少 30 秒
t.check('15 分钟一片 120 秒', TA.stuck_limit(900) == 120, TA.stuck_limit(900))
t.check('5 分钟一片 40 秒', TA.stuck_limit(300) == 40, TA.stuck_limit(300))
t.check('很短的片最少 30 秒', TA.stuck_limit(60) == 30, TA.stuck_limit(60))
t.check('不知道片长按 15 分钟算', TA.stuck_limit(None) == 120, TA.stuck_limit(None))

# 2. 正常转：Flash 每次调用的超时就是这个数，不是 900 秒
calls.clear()
TA.split_mp3 = splitter([900, 900, 300], 'ok')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('正常转：15 分钟片给 120 秒', timeouts('ok_0000.mp3') == [120], timeouts('ok_0000.mp3'))
t.check('正常转：最后 5 分钟片给 40 秒', timeouts('ok_0002.mp3') == [40], timeouts('ok_0002.mp3'))

# 3. 一片卡住一次：马上重来（不等 FLAKY_WAITS），整期照样转完
calls.clear()
STUCK.clear()
STUCK['s1_0001.mp3'] = 1
TA.split_mp3 = splitter([900, 900, 900], 's1')
t0 = time.time()
segs, text = TA.transcribe_audio(src, 'agy_flash')
took = time.time() - t0
t.check('卡住一次：掐掉重来、整期转完', segs and timeouts('s1_0001.mp3') == [120, 120], timeouts('s1_0001.mp3'))
t.check('卡住重来不等 20 秒', took < 3, round(took, 2))

# 4. 连卡两次：第 3 次按原来的长超时（900 + 60）等，不再掐
calls.clear()
STUCK.clear()
STUCK['s2_0000.mp3'] = 2
TA.split_mp3 = splitter([900], 's2')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('最多掐 2 次，第 3 次给满时间', segs and timeouts('s2_0000.mp3') == [120, 120, TA._TIMEOUT_S + 60],
        timeouts('s2_0000.mp3'))

# 5. 第 3 次（长超时）也超时：照旧当「出错」走原来的等一会再试，不会无限掐
calls.clear()
STUCK.clear()
STUCK['s3_0000.mp3'] = 3
TA.FLAKY_WAITS = (0.01, 0.01, 0.01)
TA.split_mp3 = splitter([900], 's3')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('长超时也超了：走原来的出错重试', segs and timeouts('s3_0000.mp3')[:3] == [120, 120, TA._TIMEOUT_S + 60]
        and len(timeouts('s3_0000.mp3')) == 4, timeouts('s3_0000.mp3'))

# 6. Argon 不掐（没实测过它多久，额度也金贵）：照旧 900 + 60
calls.clear()
STUCK.clear()
TA.split_mp3 = splitter([900], 'arg')
TA.transcribe_audio(src, 'agy_argon')
t.check('Argon 引擎不掐', timeouts('arg_0000.mp3') == [TA._TIMEOUT_S + 60], timeouts('arg_0000.mp3'))

# 7. Flash 被拦改走 Argon：Argon 那次也不掐
calls.clear()
BLOCK.add('blk_0000.mp3')
TA.split_mp3 = splitter([900], 'blk')
TA.transcribe_audio(src, 'agy_flash')
argon_to = [c[2] for c in calls if c[0] == 'blk_0000.mp3' and 'argon' in c[1]]
t.check('Flash 被拦改走 Argon：Argon 不掐', argon_to == [TA._TIMEOUT_S + 60], calls)
BLOCK.clear()

# 8. AGY_STUCK_PER_MIN=0 关掉：跟以前一样一律 900 + 60
os.environ['AGY_STUCK_PER_MIN'] = '0'
calls.clear()
TA.split_mp3 = splitter([900], 'off')
TA.transcribe_audio(src, 'agy_flash')
t.check('设成 0 就不掐', timeouts('off_0000.mp3') == [TA._TIMEOUT_S + 60], timeouts('off_0000.mp3'))
os.environ['AGY_STUCK_PER_MIN'] = ''

t.finish()
