"""建合集的请求重发不能建出两个合集。
2026-10-02 外联会话：服务卡住时建合集的请求超时，客户端（MCP / 脚本）重发，结果有了两个一模一样的合集，各抽一遍卡、各花一份钱。
跑生产代码：POST /api/collections（同名、同一批转写，十分钟内再来一次 → 还是原来那个；两个同时到 → 也只建一个）。
桩（只在模型边界）：analyze.analyze_episode / synthesize_collection、ask._embed_texts；_auto_tag / _annotate_speakers /
_review_episode_transcript 置空；downloader.fetch_upload_date（补日期要连网）。"""
import json
import os
import threading
import time

from _support import Checks, fake_embed, isolate, make_transcript

isolate('colidem')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import downloader  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
analyze.analyze_episode = lambda title, text, author, **k: {
    'title': title, 'cards': [{'obs': 'x', 'quote': text[:20], 'timestamp': '00:00:01', 'layer': '他的主张'}], 'metrics': {}}
analyze.synthesize_collection = lambda eps, name, **k: '# 综述\n'
ask._embed_texts = fake_embed
downloader.fetch_upload_date = lambda url: ''
A._auto_tag = lambda cdir: None
A._annotate_speakers = lambda cdir, eps: None
A._review_episode_transcript = lambda tid, preset=None: json.load(
    open(os.path.join(R, tid, 'transcript.json'), encoding='utf-8'))


def collections():
    out = []
    for d in os.listdir(A.CHAINS_DIR):
        p = os.path.join(A.CHAINS_DIR, d, 'chain.json')
        if os.path.isfile(p):
            st = json.load(open(p, encoding='utf-8'))
            if st.get('kind') == 'collection':
                out.append(st)
    return out


def post(body):
    r = A.app.test_client().post('/api/collections', json=body)
    return r.status_code, r.get_json() or {}


a, b, c3 = (make_transcript(R, [('00:00:01', f'第{n}期：讲利率。')], title=f'E{n}') for n in (1, 2, 3))

# 1 同名、同一批转写，紧接着再发一次：还是原来那个
s1, r1 = post({'name': 'All-In 2025', 'task_ids': [a, b]})
s2, r2 = post({'name': 'All-In 2025', 'task_ids': [b, a]})          # 顺序不同也算同一批
t.check('重发同一个请求：拿回原来那个合集，不新建', s1 == 200 and s2 == 200 and r1.get('id') == r2.get('id')
        and len(collections()) == 1, f'{r1} {r2} n={len(collections())}')

# 2 两个同时到（客户端超时后马上重发，第一个还没回）：也只建一个
real_members = A._collection_members
together = threading.Barrier(2, timeout=10)


def members_together(body):
    out = real_members(body)
    try:
        together.wait()
    except threading.BrokenBarrierError:
        pass
    return out


A._collection_members = members_together
got = []
ths = [threading.Thread(target=lambda: got.append(post({'name': 'Lex 2024', 'task_ids': [a, c3]}))) for _ in range(2)]
for th in ths:
    th.start()
for th in ths:
    th.join(20)
A._collection_members = real_members
ids = {r.get('id') for _, r in got}
t.check('两个同时到：只建一个', len(got) == 2 and all(s == 200 for s, _ in got) and len(ids) == 1
        and sum(1 for st in collections() if st['author'] == 'Lex 2024') == 1, f'{got}')

# 3 不是同一个请求的不受影响：成员不同、名字不同都照建
s3, r3 = post({'name': 'All-In 2025', 'task_ids': [a, b, c3]})
s4, r4 = post({'name': 'All-In 2026', 'task_ids': [a, b]})
t.check('成员或名字不同：照常新建', r3.get('id') not in (r1.get('id'),) and r4.get('id') not in (r1.get('id'), r3.get('id'))
        and len(collections()) == 4, f'n={len(collections())}')

# 4 隔了很久（超过十分钟）再建同名同成员的：当成用户真的想再建一个
p = os.path.join(A._chain_dir(r1['id']), 'chain.json')
st = json.load(open(p, encoding='utf-8'))
st['created_at'] = '2026-01-01 00:00:00'
json.dump(st, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
s5, r5 = post({'name': 'All-In 2025', 'task_ids': [a, b]})
t.check('隔了十分钟以上再建：新建一个', s5 == 200 and r5.get('id') != r1.get('id'), f'{r5}')

end = time.time() + 20
while time.time() < end and any(st.get('stage') not in ('done', 'failed') for st in collections()):
    time.sleep(0.1)
t.finish()
