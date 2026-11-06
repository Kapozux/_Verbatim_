"""agy 并发按切片算（2026-11-06，用户：「我目前视频 ag 是切片的，那更应该讨论能并发多少个切片」）。
以前同一个数（4）既管同时几个视频、又管全局同时几个切片，4 个视频一起转时每个视频平均只有 1 片在跑。
现在：AGY_FLASH_CHUNKS / AGY_ARGON_CHUNKS 管全局同时几个切片；一个视频有空位就多拿，不再固定 3 路；
Flash 被拦改走 Argon 时占 Argon 的位子。
桩：transcribe_agy._run_json（假 agy，睡一下、数同时有几个）、split_mp3。"""
import os
import threading
import time

from _support import Checks, isolate

TMP = isolate('agy_chunks')
os.environ['AGY_FLASH_CHUNKS'] = '6'
os.environ['AGY_ARGON_CHUNKS'] = '1'
os.environ['AGY_FLASH_CONCURRENCY'] = ''      # 置空，不然 .env 里的会被 load_dotenv 填回来
os.environ['AGY_QUOTA_WAIT'] = ''
import config  # noqa: E402
import transcribe_agy as TA  # noqa: E402

t = Checks()
lock = threading.Lock()
now = {'flash': 0, 'argon': 0}
peak = {'flash': 0, 'argon': 0}
BLOCK = set()
DENSE = '\n'.join(f'[00:{i // 2:02d}:{i % 2 * 30:02d}] 句子{i}' for i in range(30))


def fake_run(args, timeout, cwd=None):
    if args[:2] == ['-p', '/usage']:
        return {'command': {'data': {'groups': []}}}
    model = args[args.index('--model') + 1]
    k = 'argon' if 'argon' in model else 'flash'
    name = os.path.basename(args[1].split('：', 1)[1].split('\n', 1)[0])
    with lock:
        now[k] += 1
        peak[k] = max(peak[k], now[k])
    time.sleep(0.15)
    with lock:
        now[k] -= 1
    if k == 'flash' and name in BLOCK:
        return {'status': 'SUCCESS', 'response': "This request was blocked by Gemini's filters.", 'usage': {}}
    return {'status': 'SUCCESS', 'response': DENSE, 'usage': {}}


def splitter(n, tag):
    def split(path, secs, out_dir):
        return [(os.path.join(out_dir, f'{tag}_{i:04d}.mp3'), i * 900) for i in range(n)], n * 900
    return split


def reset():
    for d in (now, peak):
        d.update(flash=0, argon=0)


TA._run_json = fake_run
src = os.path.join(TMP, 'a.mp3')
open(src, 'wb').write(b'\0' * 1024)

t.check('切片上限读环境变量', config.AGY_CHUNKS == {'agy_flash': 6, 'agy_argon': 1}, config.AGY_CHUNKS)
t.check('同时几个视频不少于切片上限（短视频一片也能把位子用满）',
        config.ENGINE_CONCURRENCY['agy_flash'] >= 6, config.ENGINE_CONCURRENCY)

# 1. 一个 8 片的长视频：能用满 6 个位子（以前固定 3 路）
reset()
TA.split_mp3 = splitter(8, 'one')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('一个长视频用满 6 个切片位子', peak['flash'] == 6 and segs, peak)

# 2. 三个视频同时转：全局加起来还是最多 6 片
reset()
TA.split_mp3 = splitter(5, 'multi')
th = [threading.Thread(target=TA.transcribe_audio, args=(src, 'agy_flash')) for _ in range(3)]
for x in th:
    x.start()
for x in th:
    x.join()
t.check('三个视频一起转，全局最多 6 片', peak['flash'] == 6, peak)

# 3. Flash 被拦（两次）→ Argon：占 Argon 的位子（上限 1）
reset()
TA.split_mp3 = splitter(4, 'blk')
BLOCK.update(f'blk_{i:04d}.mp3' for i in range(4))
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('四片都改走 Argon，同时最多 1 片', peak['argon'] == 1 and segs, peak)
BLOCK.clear()

# 4. Argon 引擎自己：上限 1
reset()
TA.split_mp3 = splitter(3, 'arg')
TA.transcribe_audio(src, 'agy_argon')
t.check('Argon 引擎同时 1 片', peak['argon'] == 1 and peak['flash'] == 0, peak)

# 5. 额度用到线（AGY_QUOTA_WAIT=on，用户 11-06：「等额度恢复再转」，不改走付费的 OpenRouter）：
#    先暂停、隔一阵再看，恢复了接着转，不抛 AgyQuotaOut
TA.WAIT_POLL_S = 0.05
os.environ['AGY_QUOTA_WAIT'] = 'on'
short = {'n': 3}
waits = []


def fake_check(engine):
    if short['n'] > 0:
        short['n'] -= 1
        raise TA.AgyQuotaOut('agy quota: gemini-5h 只剩 39%')


real_check = TA.check_quota
TA.check_quota = fake_check
TA.on_wait = lambda why: waits.append(why)
TA.split_mp3 = splitter(2, 'wait')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('额度不够先等、恢复了接着转', segs and short['n'] == 0 and len(waits) >= 1, (short, waits))

# 6. 等的时候链条被停了：不再干等，报「已停止」，不是额度用完（不会触发 OpenRouter 回退）
short['n'] = 10 ** 6
stopped = {'v': False}
th = threading.Thread(target=lambda: res.append(_try()), daemon=True)
res = []


def _try():
    try:
        TA.transcribe_audio(src, 'agy_flash', should_stop=lambda: stopped['v'])
        return 'ok'
    except TA.AgyQuotaOut as e:
        return 'quota:' + str(e)
    except Exception as e:  # noqa: BLE001
        return 'other:' + str(e)


th.start()
time.sleep(0.3)
stopped['v'] = True
th.join(3)
t.check('等额度时停了链条：报停止，不当成额度用完', res and res[0].startswith('other:') and '停止' in res[0], res)

# 7. 开关没开：照旧马上抛 AgyQuotaOut（交给 app 的后备，跟以前一样）
os.environ['AGY_QUOTA_WAIT'] = ''
short['n'] = 1
try:
    TA.transcribe_audio(src, 'agy_flash')
    got = 'ok'
except TA.AgyQuotaOut:
    got = 'quota'
t.check('开关没开：照旧马上交给后备', got == 'quota', got)

# 8. 闸说要留给用户（slot 抛 Paused）也按同样的规矩等
os.environ['AGY_QUOTA_WAIT'] = 'on'
TA.check_quota = lambda engine: None
gate_short = {'n': 2}
import claude_gate  # noqa: E402
from contextlib import contextmanager  # noqa: E402
real_slot = claude_gate.slot


@contextmanager
def fake_slot(*a, **k):
    if gate_short['n'] > 0:
        gate_short['n'] -= 1
        raise claude_gate.Paused('Gemini（agy）额度 gemini-5h 只剩 39%，剩下的留给你自己')
    yield


claude_gate.slot = fake_slot
TA.split_mp3 = splitter(1, 'gate')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('闸让等就等，过后接着转', segs and gate_short['n'] == 0)
claude_gate.slot = real_slot
TA.check_quota = real_check

# 9. 整条转写流程（app.run_transcription）：等额度时链条被停了 → 记失败，不去 OpenRouter（花钱）、也不落 Whisper
import json as _json  # noqa: E402
import queue  # noqa: E402
import uuid  # noqa: E402
import app as A  # noqa: E402
import taskdb  # noqa: E402
import transcribe_openrouter  # noqa: E402
import transcribe_whisper  # noqa: E402

paid = {'or': 0, 'whisper': 0}
os.environ['OPENROUTER_API_KEY'] = 'test-or-key'
transcribe_openrouter.transcribe_audio = lambda *a, **k: (paid.__setitem__('or', paid['or'] + 1), ([{'start': 0, 'end': 1, 'text': 'x'}], 'x'))[1]
transcribe_whisper.transcribe_audio = lambda *a, **k: (paid.__setitem__('whisper', paid['whisper'] + 1), [{'start': 0, 'end': 1, 'text': 'x'}])[1]


def run_app(cid):
    tid = str(uuid.uuid4())
    p = os.path.join(TMP, tid + '.mp3')
    open(p, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, 'v.mp3', 'agy_flash', None, p)
    A.run_transcription(tid, p, 'agy_flash', 'v.mp3', queue.Queue(), fallback_whisper=False, summarize=False,
                        extra_meta={'chain_id': cid})
    row = taskdb.get(tid) or {}
    mp = os.path.join(config.RESULTS_FOLDER, tid, 'meta.json')
    return row, (_json.load(open(mp, encoding='utf-8')).get('engine') if os.path.isfile(mp) else None)


os.environ['AGY_QUOTA_WAIT'] = 'on'
TA.split_mp3 = splitter(1, 'app')
short['n'] = 2
TA.check_quota = fake_check
row, eng = run_app(None)
t.check('app：额度恢复后照常用 agy 转完，没花钱', row.get('status') == 'done' and eng == 'agy_flash' and paid == {'or': 0, 'whisper': 0},
        (row.get('status'), eng, paid, row.get('error')))

short['n'] = 10 ** 6
cid = uuid.uuid4().hex
out = []
th = threading.Thread(target=lambda: out.append(run_app(cid)), daemon=True)
th.start()
time.sleep(0.4)
A._cancel_chains.add(cid)
th.join(5)
row = out[0][0] if out else {}
t.check('app：等额度时停了链条 → 失败、没去 OpenRouter / Whisper', row.get('status') == 'failed' and paid == {'or': 0, 'whisper': 0},
        (row.get('status'), paid, str(row.get('error'))[:120]))
A._cancel_chains.discard(cid)
TA.check_quota = real_check

# 10. agy 偶尔断线（status ERROR、空回复 / 没回结果）：这一块隔一会儿再试 agy，不马上交给付费后备
TA.FLAKY_WAITS = (0.01, 0.01, 0.01)
TA.check_quota = lambda engine: None
flaky = {'n': 2}
real_run = TA._run_json


def flaky_run(args, timeout, cwd=None):
    if args[:2] != ['-p', '/usage'] and flaky['n'] > 0:
        flaky['n'] -= 1
        return {'status': 'ERROR', 'response': '', 'usage': {}}
    return fake_run(args, timeout, cwd)


TA._run_json = flaky_run
TA.split_mp3 = splitter(1, 'flaky')
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('断线两次后第三次成功：照常用 agy 转完', segs and flaky['n'] == 0)

flaky['n'] = 10 ** 6
try:
    TA.transcribe_audio(src, 'agy_flash')
    got = 'ok'
except Exception as e:  # noqa: BLE001
    got = str(e)
t.check('一直断：试满 4 次才往上抛（交给 app 的后备）', got.startswith('agy 状态 ERROR') and flaky['n'] == 10 ** 6 - 4, (got, flaky))


def notlogged(args, timeout, cwd=None):
    if args[:2] != ['-p', '/usage'] and flaky['n'] > 0:
        flaky['n'] -= 1
        raise RuntimeError('agy 没有返回结果（exit 1）：You are not logged into Antigravity.')
    return fake_run(args, timeout, cwd)


flaky['n'] = 1
TA._run_json = notlogged
segs, _ = TA.transcribe_audio(src, 'agy_flash')
t.check('偶尔读不到登录也重试', segs and flaky['n'] == 0)
TA._run_json = fake_run
TA.check_quota = real_check

# 11. 长视频一片彻底失败：别的片马上停，不再对着删掉的临时目录重试
calls = {'n': 0}


def one_dies(args, timeout, cwd=None):
    if args[:2] == ['-p', '/usage']:
        return {'command': {'data': {'groups': []}}}
    name = os.path.basename(args[1].split('：', 1)[1].split('\n', 1)[0])
    with lock:
        calls['n'] += 1
    time.sleep(0.05)
    if name == 'die_0000.mp3':                   # 这一片是「不重试」的错（权限被拒），马上失败
        return {'status': 'SUCCESS', 'response': '', 'usage': {}, 'denied_actions': [{'action': 'read_file'}]}
    return {'status': 'ERROR', 'response': '', 'usage': {}} if name == 'die_0001.mp3' else fake_run(args, timeout, cwd)


TA.FLAKY_WAITS = (0.3, 0.3, 0.3)
TA.check_quota = lambda engine: None
TA._run_json = one_dies
TA.split_mp3 = splitter(2, 'die')
t0 = time.time()
try:
    TA.transcribe_audio(src, 'agy_flash')
except Exception:  # noqa: BLE001
    pass
t.check('一片失败后同期别的片不再重试', calls['n'] <= 3 and time.time() - t0 < 1, (calls, round(time.time() - t0, 2)))
TA._run_json = fake_run
TA.check_quota = real_check

t.finish()
