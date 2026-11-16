"""「进行中」列表（/api/activity）+ 画面描述 / 证据卡几期同时做。

用户 11-16：「能不能例如我某个地方开启转写，我即使页面关了也能显示呢？」——任务本来在后台跑，
页面上的队列却只在浏览器内存里。这里测：服务器记下每个任务最后一条进度、列表能一次拿全（转写 / 画面描述 / 工作台）。
同一天又问了画面证据卡的并发：从一期一期改成 VISION_EPISODES 期同时做，一期里几批同时读，记账归属要跟进线程池。

跑生产代码：app._TaskQueue、taskdb.create/activity、/api/activity、scenes.queue（真后台线程）、study._save / live、
frames.run_batch / triage / read_frames（线程池）、usage.bound。
桩：scenes.build、frames.build（只睡一下、记下同时在跑几个）、frames._call（记下当时的记账归属）。
"""
import json
import os
import threading
import time

from _support import Checks, isolate, make_transcript

TMP = isolate('activity')
import app as A  # noqa: E402
import frames  # noqa: E402
import scenes  # noqa: E402
import study  # noqa: E402
import taskdb  # noqa: E402
import usage  # noqa: E402

t = Checks()
c = A.app.test_client()

# 1 转写：进度记在服务器上（没人读 SSE 也在）；博主链的不进列表；太早结束的不进列表
T1, T2, T3, T4 = (f'{n}1111111-2222-3333-4444-555555555555' for n in '1234')
taskdb.create(T1, 'lecture.mp4', 'openrouter', None, '/x')
taskdb.create(T2, 'chain ep [BV1].m4a', 'openrouter', None, '/y', origin='chain')
taskdb.create(T3, 'done.mp3', 'openrouter', None, '/z')
taskdb.set_status(T3, 'done')
taskdb.create(T4, 'old.mp3', 'openrouter', None, '/w')
taskdb.set_status(T4, 'done')
with taskdb._conn() as cn:
    cn.execute("UPDATE tasks SET updated_at = '2026-01-01 00:00:00' WHERE id = ?", (T4,))
q = A._TaskQueue(T1)
A.tasks[T1] = q
q.put(json.dumps({'type': 'queued', 'message': 'Queued...'}))
q.put(json.dumps({'type': 'progress', 'percent': 42, 'message': 'Transcribing... 42%'}))
q.put(json.dumps({'type': 'segment', 'text': 'hi'}))
taskdb.set_status(T1, 'running')
items = c.get('/api/activity').get_json()['items']
by = {i['id']: i for i in items if i['kind'] == 'transcribe'}
print('1 转写：', [(i['title'], i['status'], i.get('percent'), i.get('message')) for i in by.values()])
t.check('在跑的带最后一条进度', by.get(T1, {}).get('status') == 'running' and by[T1]['percent'] == 42
        and by[T1]['message'] == 'Transcribing... 42%')
t.check('SSE 照旧能读到全部消息', q.qsize() == 3)
q.put(json.dumps({'type': 'progress', 'percent': 10, 'phase': 'scenes', 'message': 'reading'}))
ph = {i['id']: i for i in c.get('/api/activity').get_json()['items']}[T1]
t.check('改读画面的阶段带标记', ph['phase'] == 'scenes' and ph['percent'] == 10)
t.check('博主链的不进列表、太早结束的不进', T2 not in by and T4 not in by)
t.check('刚做完的还在列表里', by.get(T3, {}).get('status') == 'done')

# 2 画面描述：排队 / 在做 / 做完都看得到，带标题；VISION_EPISODES 期同时做
os.environ['VISION_EPISODES'] = '2'
live, peak, gate = [0], [0], threading.Event()
lk = threading.Lock()


def slow_build(results_dir, tid, **k):
    with lk:
        live[0] += 1
        peak[0] = max(peak[0], live[0])
    gate.wait(5)
    time.sleep(0.05)
    with lk:
        live[0] -= 1
    return {'scenes': []}


scenes.build = slow_build
tids = [make_transcript(A.config.RESULTS_FOLDER, [('00:00', 'x')], title=f'Ep {k}') for k in range(4)]
for tid in tids:
    scenes.queue(A.config.RESULTS_FOLDER, tid, url='https://example.com/v')
time.sleep(0.3)
mid = [i for i in c.get('/api/activity').get_json()['items'] if i['kind'] == 'scenes']
gate.set()
for _ in range(100):
    if all(scenes.status(A.config.RESULTS_FOLDER, x).get('status') == 'done' for x in tids):
        break
    time.sleep(0.05)
after = [i for i in c.get('/api/activity').get_json()['items'] if i['kind'] == 'scenes']
print('2 画面描述：', [(i['title'], i['status']) for i in mid], '| 同时最多', peak[0], '|', [i['status'] for i in after])
t.check('画面描述：在做的 + 排队的都列出来、带标题', len(mid) == 4 and {i['status'] for i in mid} == {'running', 'queued'}
        and {i['title'] for i in mid} == {f'Ep {k}' for k in range(4)})
t.check('画面描述：同时做 VISION_EPISODES 期', peak[0] == 2)
t.check('画面描述：做完的留在列表里', len(after) == 4 and all(i['status'] == 'done' for i in after))

# 3 工作台产出：生成中出现在列表（带笔记本名、进度），做完就消失
cdir = os.path.join(A.CHAINS_DIR, 'nb1')
os.makedirs(cdir, exist_ok=True)
with open(os.path.join(cdir, 'chain.json'), 'w') as f:
    json.dump({'name': 'IB 物理'}, f)
out = {'id': 'abcdef012345', 'kind': 'visual', 'status': 'running', 'chain': 'nb1', 'created_at': 'x',
       'progress': {'done': 1, 'total': 3, 'current': 'Ep 2'}}
study._save(cdir, out)
st1 = [i for i in c.get('/api/activity').get_json()['items'] if i['kind'] == 'studio']
out['status'] = 'done'
study._save(cdir, out)
st2 = [i for i in c.get('/api/activity').get_json()['items'] if i['kind'] == 'studio']
print('3 工作台：', st1, st2)
t.check('工作台：生成中的带笔记本名和进度', len(st1) == 1 and st1[0]['title'] == 'IB 物理' and st1[0]['chain'] == 'nb1'
        and st1[0]['done'] == 1 and st1[0]['total'] == 3 and st1[0]['studio_kind'] == 'visual')
t.check('工作台：做完就不在了', st2 == [])

# 4 证据卡几期同时做：进度数只增不减；记账归属跟进线程池
live[0], peak[0] = 0, 0
gate.clear()
real_build = frames.build


def fbuild(results_dir, tid, url=None):
    with lk:
        live[0] += 1
        peak[0] = max(peak[0], live[0])
    time.sleep(0.15)
    with lk:
        live[0] -= 1
    return {'cards': [1, 2]}


frames.build = fbuild
prog = []
rows = [{'task_id': f't{k}', 'title': f'T{k}'} for k in range(5)]
res = frames.run_batch(A.config.RESULTS_FOLDER, rows,
                       on_episode=lambda i, r, s: prog.append(i + (s == 'done')))
print('4 证据卡：', [r['task_id'] for r in res], '| 同时最多', peak[0], '| 进度', prog)
t.check('证据卡：同时做 VISION_EPISODES 期、结果顺序不乱', peak[0] == 2 and [r['task_id'] for r in res] == [f't{k}' for k in range(5)])
t.check('证据卡：进度只增不减、最后是全部', prog == sorted(prog) and prog[-1] == 5)
frames.build = real_build

refs = []
frames._call = lambda prompt, images: refs.append(usage.current().get('ref')) or '{"keep": [0], "cards": []}'
os.environ['VISION_CALLS'] = '3'
with usage.scope(ref='visual:abc'):
    frames.triage([(f'/s{k}.jpg', [k]) for k in range(4)])
    frames.read_frames([{'i': k, 'shown': [[0, 1]], 'said': '', 'img': f'/i{k}.jpg'} for k in range(13)], 'English')
print('   归属：', refs)
t.check('线程池里的模型调用照样记到这一期', len(refs) == 4 + 3 and all(r == 'visual:abc' for r in refs))

t.finish()
