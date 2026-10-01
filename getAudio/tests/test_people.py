"""项目里多个博主（第二步）：引用博主、共用他的分析、项目里提问 / 写报告时一起检索、出处带人名。

跑生产代码：app 的 /api/projects、/sources*、/sources/channels、/people、/api/chain（project_id）、/api/chains；
ask.load(passages) 的 _add_people、semantic_rank（向量去各自链条取）、embed_chain、answer → _finish；study._prompt。
桩（只在模型 / 网络边界）：ask._embed_texts（按文字哈希出确定的向量）、ask._llm（假回答，引用提示词里真有的 id
和一个编造的）、app.run_chain（不真去下载频道）。数据目录是临时目录。
"""
import json
import os
import re

from _support import Checks, fake_embed, isolate, make_creator

TMP = isolate('people')
import app as A  # noqa: E402
import ask  # noqa: E402
import study  # noqa: E402

calls = {'prompts': []}


def fake_llm(prompt, **k):
    calls['prompts'].append(prompt)
    ids = re.findall(r'^\[#([A-Z]:\d+-\d+)\]', prompt, re.M)
    b = next(i for i in ids if i.startswith('B:'))
    c_ = next(i for i in ids if i.startswith('C:'))
    return f'Alpha thinks small teams win [#{b}]. Beta disagrees [#{c_}]. Wrong letter [#C:0-1]. Made up [#B:9-9].'


ask._embed_texts = fake_embed
ask._llm = fake_llm
A.run_chain = lambda state: None
c = A.app.test_client()
ok = {}


def creator(name, handle, quotes):
    return make_creator(A, name, handle, quotes)


X = creator('Alpha', 'alpha', [['small teams win', 'ship weekly'], ['talk to users']])
Y = creator('Beta', 'beta', [['big teams win'], ['raise a lot of money', 'hire fast']])
for d in (X, Y):
    ask.embed_chain(A._chain_dir(d))

# 1 建项目，从资料库加整个博主 → 引用（不拷贝录音）
P = c.post('/api/projects', json={'name': 'Team size'}).get_json()['id']
r1 = c.post(f'/api/chain/{P}/sources/transcripts', json={'chain_ids': [X]}).get_json()
reg = A._reg(P)
print('1 资料库加博主：', r1, reg['channels'], '| 项目自己的录音', len(A._read_chain(P)['videos']))
ok['add_from_library'] = (r1.get('added') == 1 and [r['tag'] for r in reg['channels']] == ['B']
                          and not A._read_chain(P)['videos'])

# 2 贴一个已经有链条的频道链接 → 直接引用那条，不重跑
r2 = c.post('/api/chain', json={'url': 'https://www.youtube.com/@Beta/', 'project_id': P}).get_json()
print('2 已有的频道：', r2)
ok['existing_url'] = r2.get('existing') is True and r2.get('chain_id') == Y and len(A._reg(P)['channels']) == 2

# 3 贴一个新频道 → 另起一条博主链条（owner_project），按引用加进来；首页不单独列
r3 = c.post('/api/chain', json={'url': 'https://www.youtube.com/@gamma', 'project_id': P, 'author': 'Gamma'}).get_json()
Z = r3['chain_id']
chains = {e['id']: e for e in c.get('/api/chains').get_json()}
print('3 新频道：', r3, '| owner', A._read_chain(Z).get('owner_project') == P, '| 首页 ref_only', chains[Z]['ref_only'],
      chains[X]['ref_only'])
ok['new_channel'] = (Z not in (P, X, Y) and r3.get('project_id') == P and A._read_chain(Z).get('owner_project') == P
                     and chains[Z]['ref_only'] and not chains[X]['ref_only']
                     and [r['tag'] for r in A._reg(P)['channels']] == ['B', 'C', 'D'])

# 4 /people、/sources
ppl = c.get(f'/api/chain/{P}/people').get_json()['people']
srcs = c.get(f'/api/chain/{P}/sources').get_json()
print('4 人：', [(p['name'], p['tag'], p['has_cards'], p['portrait']) for p in ppl],
      '| 来源里的博主', [(x['name'], len(x['videos'])) for x in srcs['channels']])
ok['people'] = ([p['name'] for p in ppl] == ['Alpha', 'Beta', 'Gamma'] and ppl[0]['has_cards'] and ppl[0]['portrait']
                and not ppl[2]['has_cards'] and [len(x['videos']) for x in srcs['channels']] == [2, 2, 0])

# 5 项目里检索：卡片 id 带字母、期标签 B·EP1、记着是谁；向量去各自链条取
corpus = ask.load(A._chain_dir(P), passages=True)
ids = [x['id'] for x in corpus['cards']]
sem = ask.semantic_rank(corpus, 'small teams', corpus['cards'], 4)
print('5 合并：', ids, '| 期', [(e['label'], e.get('person')) for e in corpus['episodes']][:3],
      '| 向量排序', [x['id'] for x in sem] if sem else sem)
ok['merged'] = (ids[:3] == ['B:0-0', 'B:0-1', 'B:1-0'] and any(i.startswith('C:') for i in ids)
                and corpus['episodes'][0]['label'] == 'B·EP1' and corpus['episodes'][0]['person'] == 'Alpha'
                and sem is not None and len(sem) == 4 and set(corpus['parts']) == {'B', 'C', 'D'})
emb = ask.embed_chain(A._chain_dir(P))
ok['no_reembed'] = emb['total'] == 0
print('  项目自己补向量：', emb)

# 6 提问：提示词里写清楚谁是谁；出处带人名；编的 id 删掉
res = ask.answer(A._chain_dir(P), 'Do small teams win?')
pr = calls['prompts'][-1]
print('6 回答：', res['answer'], '| 出处', {k: (v['creator'], v['label']) for k, v in res['citations'].items()},
      '| 删掉', res['dropped_ids'])
ok['answer'] = ('several creators' in pr and '[Alpha]' in pr and '[Beta]' in pr and 'About Alpha:' in pr
                and set(res['citations']) == {'B:0-0', 'C:0-0'} or all(k[0] in 'BC' for k in res['citations']))
ok['answer'] = ok['answer'] and res['citations'][next(k for k in res['citations'] if k.startswith('B:'))]['creator'] == 'Alpha' \
    and res['dropped_ids'] == ['B:9-9'] and all(v['label'].startswith('EP') for v in res['citations'].values()) \
    and 'B:0-1' in res['citations']          # 字母写错（C:0-1 不存在、B:0-1 只有一张）→ 改回来

# 6b 用某个人的口吻回答：只给模型他的卡，提示词里的「他」是他，背景只用他的画像
ask._llm = lambda prompt, **k: (calls['prompts'].append(prompt) or 'I think big teams win [#C:0-0].')
res_as = ask.answer(A._chain_dir(P), 'Do small teams win?', mode='as', persona=Y)
pa = calls['prompts'][-1]
ids_in = set(re.findall(r'^\[#([A-Z]:\d+-\d+)\]', pa, re.M))
print('6b 模拟 Beta：', res_as['answer'], '| persona', res_as.get('persona'), '| 提示词里的卡', sorted(ids_in),
      '| 背景只有 Beta', 'About Alpha' not in pa and 'Beta believes' in pa)
ok['persona'] = (res_as.get('persona') == 'Beta' and ids_in and all(i.startswith('C:') for i in ids_in)
                 and 'the creator "Beta"' in pa and 'About Alpha' not in pa and 'C:0-0' in res_as['citations'])
ask._llm = fake_llm

# 7 报告的提示词也按人归属
sp = study._prompt(corpus, corpus['cards'], 'English', '', 'Task: x')
print('7 报告提示词：', [ln for ln in sp.splitlines() if 'several people' in ln or ln.startswith('B·EP1')])
ok['study'] = 'several people (Alpha, Beta);' in sp and 'B·EP1 = [Alpha] Alpha episode 1' in sp

# 8 拿掉一个博主：只从项目拿掉，他自己的链条还在；字母作废，再加进来换新字母
d8 = c.delete(f'/api/chain/{P}/sources/{X}').get_json()
r8 = c.post(f'/api/chain/{P}/sources/channels', json={'chain_ids': [X]}).get_json()
reg = A._reg(P)
print('8 移除再加：', d8, r8, [(r['tag'], r['chain_id'] == X) for r in reg['channels']], reg.get('retired_tags'),
      '| 链条还在', A._chain_ok(X))
ok['remove'] = d8.get('ok') and A._chain_ok(X) and reg['retired_tags'] == ['B'] and reg['channels'][-1]['tag'] == 'E'

# 9 有自己频道的项目再加一个博主：自己也是一个人，期上记名字
Q = c.post('/api/projects', json={'name': 'Untitled'}).get_json()['id']
r9a = c.post('/api/chain', json={'url': 'https://www.youtube.com/@delta', 'project_id': Q, 'author': 'Delta'}).get_json()
qd = A._chain_dir(Q)
with open(os.path.join(qd, 'cards_000.json'), 'w') as f:
    json.dump({'task_id': 'q' * 32, 'title': 'Delta ep', 'cards': [{'quote': 'remote work', 'obs': 'x', 'layer': '主张'}]}, f)
r9b = c.post('/api/chain', json={'url': 'https://www.youtube.com/@alpha', 'project_id': Q}).get_json()
ppl9 = c.get(f'/api/chain/{Q}/people').get_json()['people']
cq = ask.load(qd, passages=True)
print('9 自己有频道：', r9a.get('chain_id') == Q, r9b, [(p['name'], p['self']) for p in ppl9],
      [(e['label'], e.get('person')) for e in cq['episodes']])
ok['self_person'] = (r9a.get('chain_id') == Q and r9b.get('existing') and [p['name'] for p in ppl9] == ['Delta', 'Alpha']
                     and ppl9[0]['self'] and cq['episodes'][0].get('person') == 'Delta'
                     and ask.card_view(cq['cards'][0], cq)['creator'] == 'Delta')
r9c = c.post('/api/chain', json={'url': 'https://www.youtube.com/@delta', 'project_id': Q}).get_json()
ok['same_channel_twice'] = 'already' in (r9c.get('error') or '')

# 10 格子「立场 / 原话」做一份 = 列表里一条；同一个人同一样只一条；没打标签的顺手开始打（标签那步打桩）
ask.tag_chain = lambda cdir, progress=None, rebuild=False: __import__('time').sleep(0.6) or {'tagged': 0}
v1 = c.post(f'/api/chain/{P}/studio/view', json={'view': 'cards', 'chain': Y}).get_json()
v2 = c.post(f'/api/chain/{P}/studio/view', json={'view': 'cards', 'chain': Y}).get_json()
v3 = c.post(f'/api/chain/{P}/studio/view', json={'view': 'topics', 'chain': Y, 'generate': True}).get_json()
lst = c.get(f'/api/chain/{P}/studio').get_json()['items']
run_now = [o['status'] for o in lst if o.get('view') == 'topics']
bad = c.post(f'/api/chain/{P}/studio/view', json={'view': 'cards', 'chain': Q}).status_code
__import__('time').sleep(1)
run_after = [o['status'] for o in c.get(f'/api/chain/{P}/studio').get_json()['items'] if o.get('view') == 'topics']
print('10 视图条目：', v1['new'], v2['new'], v1['item']['id'] == v2['item']['id'], '| 打标签', v3['started'], run_now, '→', run_after,
      '| 不在项目里的人', bad)
ok['views'] = (v1['new'] and not v2['new'] and v1['item']['id'] == v2['item']['id'] and v3['started']
               and run_now == ['running'] and run_after == ['done'] and bad == 400)

# 11 格子「对比」做一份：后台跑 ask.compare，结果存成一条（模型打桩）
ask._llm = lambda prompt, **k: 'Alpha says small [#A:0-0]. Beta says big [#B:0-0].'
r11 = c.post(f'/api/chain/{P}/studio/compare', json={'chains': [Y, X], 'question': 'Team size?'}).get_json()
oid = r11['item']['id']
for _ in range(50):
    o = c.get(f'/api/chain/{P}/studio/{oid}').get_json()
    if o['status'] != 'running':
        break
    __import__('time').sleep(0.1)
one = c.post(f'/api/chain/{P}/studio/compare', json={'chains': [Y], 'question': 'x'}).status_code
print('11 对比：', o['status'], o['result']['answer'], sorted(o['citations']), '| 只选一个人', one)
ok['compare'] = o['status'] == 'done' and sorted(o['citations']) == ['A:0-0', 'B:0-0'] and one == 400

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
