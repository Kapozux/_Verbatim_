"""转写页「最近转写」要几秒才出来。
2026-10-03 用户报的：5001 上 3410 条转写、70 个笔记本，/api/history 一次 1 秒，打开转写页又同时拉两次（资料库列表 + 最近转写），
最近转写要等 2.2 秒。1 秒里 870 毫秒花在：给每条转写判断「属于哪个博主」时，都重新检查一遍所有笔记本的 chain.json 有没有改过
（3410 × 70 ≈ 24 万次文件系统调用）。
跑生产代码：GET /api/history、/api/history?limit=N、/api/search。没有桩：只读本地文件。
计数而不计时：数一次请求里碰了多少次 _chains 目录（os.stat / os.listdir），转写条数翻倍时这个数不该变——计时在忙的机器上会抖。"""
import json
import os

from _support import Checks, isolate, make_transcript, write_json

isolate('historyfast')
import app as A  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
os.makedirs(A.CHAINS_DIR, exist_ok=True)


def transcript(i, title, filename=None):
    tid = make_transcript(R, [('00:00:01', f'第 {i} 条。')], title=title)
    p = os.path.join(R, tid, 'meta.json')
    m = json.load(open(p, encoding='utf-8'))
    m['date'] = f'2026-09-{1 + i // 100:02d} {i // 60 % 24:02d}:{i % 60:02d}:00'
    if filename:
        m['filename'] = filename
    write_json(p, m)
    return tid


# 10 个博主笔记本，各 3 期；另有一条重转后留下的旧结果（task_id 不在任何笔记本里，但文件名里的 [video_id] 在）
n = 0
in_chain = {}
for ci in range(10):
    vids = []
    for k in range(3):
        tid = transcript(n, f'博主{ci} 第{k}期')
        n += 1
        in_chain[tid] = f'博主{ci}'
        vids.append({'index': k, 'task_id': tid, 'status': 'done', 'video_id': f'vid{ci:02d}{k:02d}abcd'})
    write_json(os.path.join(A.CHAINS_DIR, f'chain{ci:02d}', 'chain.json'),
               {'id': f'chain{ci:02d}', 'author': f'博主{ci}', 'stage': 'done', 'videos': vids})
orphan = transcript(n, '旧结果', filename='旧结果 [vid0101abcd].m4a')
n += 1
own = [transcript(n + i, f'我自己的 {i}') for i in range(29)]
n += 29                                              # 一共 60 条


def chain_touches(path):
    """一次请求里碰 _chains 目录的次数。"""
    count = [0]
    real_stat, real_listdir = os.stat, os.listdir

    def stat(p, *a, **k):
        if str(p).startswith(A.CHAINS_DIR):
            count[0] += 1
        return real_stat(p, *a, **k)

    def listdir(p='.'):
        if str(p).startswith(A.CHAINS_DIR):
            count[0] += 1
        return real_listdir(p)

    os.stat, os.listdir = stat, listdir
    try:
        r = c.get(path)
    finally:
        os.stat, os.listdir = real_stat, real_listdir
    return count[0], r.get_json()


c = A.app.test_client()
c.get('/api/history')                                # 先把笔记本索引读进缓存
few, full = chain_touches('/api/history')
few_s, _ = chain_touches('/api/search?q=' + '条')
for i in range(60):
    transcript(n + i, f'又一条 {i}')
n += 60                                              # 翻倍到 120 条
many, full2 = chain_touches('/api/history')
many_s, hits = chain_touches('/api/search?q=' + '条')
t.check('列转写时检查笔记本的次数不随转写条数增长', few == many,
        f'60 条时 {few} 次，120 条时 {many} 次')
t.check('搜索也一样', few_s == many_s and len(hits) == 120, f'60 条时 {few_s} 次，120 条时 {many_s} 次，命中 {len(hits)}')

by_id = {e['id']: e for e in full2}
t.check('笔记本里的转写仍标成那个博主的', all(by_id[tid]['source'] == 'pipeline' and by_id[tid]['creator'] == a
                                       for tid, a in in_chain.items()))
t.check('重转留下的旧结果按文件名里的视频 id 认回来', by_id[orphan]['source'] == 'pipeline'
        and by_id[orphan]['creator'] == '博主1', json.dumps(by_id[orphan], ensure_ascii=False)[:200])
t.check('自己的转写标成 mine', all(by_id[tid]['source'] == 'mine' and not by_id[tid].get('creator') for tid in own))

r = c.get('/api/history?limit=12').get_json()
t.check('limit=12 只给最新的 12 条，顺序跟完整列表一样', [e['id'] for e in r] == [e['id'] for e in full2[:12]],
        f'拿到 {len(r)} 条')
t.check('完整列表按时间从新到旧', [e['date'] for e in full2] == sorted((e['date'] for e in full2), reverse=True))
t.finish()
