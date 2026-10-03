"""来源标签（照 NotebookLM 的 auto-label）：/sources/autolabel 按主题给来源分组，每个来源一个标签；
重新生成只替换自动分的组，手动放的保留、而且优先；/sources/folders 整份保存时一个来源只留在一个标签里。

跑生产代码：/api/projects、/sources/docs、/sources/folders、/sources/autolabel、/sources（列表带 folders）、
sources.doc_guide（文档概述当作来源的一句话）。桩（只在模型边界）：ask._llm（按提示词里的来源编号造标签，
故意把一个来源放进两组、再塞一个不存在的编号）。数据：四份粘贴的讲义。
"""
import json
import os
import re

from _support import Checks, isolate, write_json

TMP = isolate('labels')
import app as A  # noqa: E402
import ask  # noqa: E402
import sources  # noqa: E402

t = Checks()
c = A.app.test_client()
pid = c.post('/api/projects', json={'name': 'CS'}).get_json()['id']
ids = []
for title, body in [('Normalization', '1NF 2NF 3NF remove redundancy.'), ('SQL joins', 'Inner and outer joins.'),
                    ('CPU cycle', 'Fetch decode execute.'), ('Cache', 'Caches cut memory latency.')]:
    ids.append(c.post(f'/api/chain/{pid}/sources/docs', json={'title': title, 'text': body * 10}).get_json()['docs'][0]['doc_id'])
# 第一份有概述：自动分组时它那一行要带上概述和主题
write_json(os.path.join(sources.doc_dir(ids[0]), 'guide.json'), {'en': {'summary': 'Database normal forms.', 'topics': ['normal forms']}})
prompts = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False):
    prompts.append((purpose, prompt))
    n = len(re.findall(r'^\d+\. ', prompt.split('Sources:', 1)[1], re.M))
    return json.dumps({'labels': [{'name': '数据库', 'items': [1, 2, 99]}, {'name': '硬件', 'items': [3, 4, 1]}]}, ensure_ascii=False) \
        if n == 4 else '{}'


ask._llm = fake_llm
r = c.post(f'/api/chain/{pid}/sources/autolabel', json={'lang': 'zh'}).get_json()
fs = r.get('folders') or []
by = {f['name']: f['items'] for f in fs}
t.check('自动分组：每个来源一个标签、编造的编号丢掉', by == {'数据库': [ids[0], ids[1]], '硬件': [ids[2], ids[3]]}
        and all(f['auto'] for f in fs), fs)
p = prompts[-1][1]
t.check('提示词：要中文标签、按来源数定组数、每个来源一行带概述和主题', 'written in Chinese' in p and 'exactly one label' in p
        and 'Make about 2 labels' in p
        and 'Database normal forms. | topics: normal forms' in p and prompts[-1][0] == 'tag')
# 手动把 Cache 放进自己的标签，再重新自动分：手动的留着、Cache 不会被抢走；旧的自动组被换掉
manual = {'id': 'mine', 'name': 'Exam', 'items': [ids[3]]}
c.put(f'/api/chain/{pid}/sources/folders', json={'folders': [manual] + fs})
r2 = c.post(f'/api/chain/{pid}/sources/autolabel', json={'lang': 'zh'}).get_json()
fs2 = r2['folders']
by2 = {f['name']: f['items'] for f in fs2}
t.check('重新自动分：手动标签保留且优先，自动组整批换新', by2.get('Exam') == [ids[3]] and by2.get('硬件') == [ids[2]]
        and [f['name'] for f in fs2].count('数据库') == 1 and not next(f for f in fs2 if f['name'] == 'Exam')['auto'], fs2)
lst = c.get(f'/api/chain/{pid}/sources').get_json()
t.check('来源列表带标签', [f['name'] for f in lst['folders']] == [f['name'] for f in fs2])
res = c.put(f'/api/chain/{pid}/sources/folders', json={'folders': [
    {'id': 'a', 'name': 'A', 'items': [ids[0], ids[1]]}, {'id': 'b', 'name': 'B', 'items': [ids[1], ids[2]]}]}).get_json()
t.check('手动保存：一个来源只留在第一个标签里', [f['items'] for f in res['folders']] == [[ids[0], ids[1]], [ids[2]]])
ask._llm = lambda *a, **k: 'not json'
t.check('模型没给出标签 → 报错、原来的不动', c.post(f'/api/chain/{pid}/sources/autolabel', json={}).status_code == 502
        and [f['name'] for f in c.get(f'/api/chain/{pid}/sources').get_json()['folders']] == ['A', 'B'])
empty = c.post('/api/projects', json={'name': 'Empty'}).get_json()['id']
t.check('没有来源 → 400', c.post(f'/api/chain/{empty}/sources/autolabel', json={}).status_code == 400)

t.finish()
