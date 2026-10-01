"""项目首页偏好：置顶 / 表情 / 合集（/api/chains/prefs、/api/groups*、/api/chains、/api/chain/<id>）。
跑生产代码；不调任何模型，没有桩。"""
import json
import os
import uuid

from _support import Checks, isolate

isolate('prefs')
import app as A  # noqa: E402

c = A.app.test_client()
t = Checks()
ids = []
for name in ('甲', '乙'):
    cid = uuid.uuid4().hex
    os.makedirs(A._chain_dir(cid))
    A._save_chain({'id': cid, 'author': name, 'stage': 'done', 'kind': 'project', 'videos': []})
    ids.append(cid)
a, b = ids


def P(body):
    return c.post('/api/chains/prefs', json=body)


r1 = P({'id': a, 'pinned': True}).status_code
P({'id': b, 'pinned': True})
P({'id': a, 'emoji': '📚'})
P({'id': b, 'emoji': '<img src=x>'})
bad_id = P({'id': 'nope', 'pinned': True}).status_code
lst = {x['id']: x for x in c.get('/api/chains').get_json()}
t.check('置顶按先后排、表情只收表情', r1 == 200 and lst[b]['pin'] == 0 and lst[a]['pin'] == 1
        and lst[a]['emoji'] == '📚' and lst[b]['emoji'] == '' and bad_id == 400)

P({'id': a, 'pinned': False})
P({'id': a, 'emoji': ''})
lst = {x['id']: x for x in c.get('/api/chains').get_json()}
t.check('取消置顶 / 清掉表情', lst[a]['pin'] is None and lst[a]['emoji'] == '')

col = c.post('/api/groups', json={'name': '语文', 'add': a}).get_json()['id']
c.post(f'/api/groups/{col}', json={'id': b, 'add': True})
c.post(f'/api/groups/{col}', json={'id': a, 'add': False})
noname = c.post('/api/groups', json={'name': ' '}).status_code
cols = c.get('/api/groups').get_json()['items']
lst = {x['id']: x for x in c.get('/api/chains').get_json()}
t.check('合集：加入 / 移出 / 空名拒绝', cols == [{'id': col, 'name': '语文', 'count': 1}]
        and lst[b]['collections'] == [col] and not lst[a]['collections'] and noname == 400)

c.delete(f'/api/groups/{col}')
t.check('删合集不删项目', c.get('/api/groups').get_json()['items'] == [] and os.path.isdir(A._chain_dir(b)))

P({'id': a, 'emoji': '🧠'})
d = c.get(f'/api/chain/{a}').get_json()
raw = json.load(open(os.path.join(A._chain_dir(a), 'chain.json'), encoding='utf-8'))
t.check('详情带表情、chain.json 不存表情', d.get('emoji') == '🧠' and 'emoji' not in raw)
t.finish()
