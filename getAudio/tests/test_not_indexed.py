"""只转写、没分析也没把原文加进检索的博主放进笔记本（GitHub #9）：以前对话 / 工作台只报
「Nothing to work from」，不说为什么。现在：
  · 左栏那个博主一组上标「还没加进检索」+ 估价（/sources 的 not_indexed）；
  · 对话、报告、播客、幻灯片报错时点名是谁、告诉去哪加；
  · 点「加进检索」（/sources/index_creator）→ 打开他的 index_transcripts、后台补向量，之后就能问。

跑生产代码：app 的 /api/projects、/sources、/sources/transcripts、/sources/index_creator、/ask/stream；
ask.load / _prepare / embed_chain、study.start、podcast.start、slides.start。
桩：ask._embed_texts（确定的假向量）、ask._llm / ask._write_with（假回答）、app.run_chain。
"""
import glob
import json
import os
import re
import time

from _support import Checks, fake_embed, isolate, make_creator

TMP = isolate('not_indexed')
import app as A  # noqa: E402
import ask  # noqa: E402
import podcast  # noqa: E402
import slides  # noqa: E402
import study  # noqa: E402

t = Checks()
ask._embed_texts = fake_embed


def fake_llm(prompt, **k):
    ids = re.findall(r'^\[#([A-Za-z0-9:-]+)\]', prompt, re.M)
    return f'Alpha says small teams win [#{ids[0]}].' if ids else 'nothing'


ask._llm = fake_llm
ask._write_with = lambda prompt, model: iter([('fake', fake_llm(prompt))])     # 对话流式写那一步
A.run_chain = lambda state: None
c = A.app.test_client()

# 只转写、没分析的博主：有转写，删掉证据卡，analyze 关、index_transcripts 没开
U = make_creator(A, 'Alpha', 'alpha', [['small teams win', 'ship weekly'], ['talk to users']], transcripts=True,
                 portrait=False)
for f in glob.glob(os.path.join(A._chain_dir(U), 'cards_*.json')):
    os.remove(f)
st = A._read_chain(U)
st['analyze'] = False
A._save_chain(st)
# 分析过的博主做对照
V = make_creator(A, 'Beta', 'beta', [['big teams win']])

P = c.post('/api/projects', json={'name': 'Teams'}).get_json()['id']
c.post(f'/api/chain/{P}/sources/transcripts', json={'chain_ids': [U, V]})

src = c.get(f'/api/chain/{P}/sources').get_json()
by = {ch['chain_id']: ch for ch in src['channels']}
ni = by[U].get('not_indexed') or {}
t.check('没加进检索的博主：左栏那组标出来', ni.get('episodes') == 2 and ni.get('passages', 0) > 0, ni)
t.check('带估价（美元数字）', isinstance(ni.get('usd'), (int, float)), ni)
t.check('分析过的博主不标', not by[V].get('not_indexed'), by[V].get('not_indexed'))

# 只看他一个人：对话范围勾他的期 → 报错点名、说去哪加
scope = {'tasks': [v['task_id'] for v in A._read_chain(U)['videos']]}


def ask_err(scope=None):
    r = c.post(f'/api/chain/{P}/ask/stream', json={'question': 'what about teams?', 'scope': scope})
    evs = [json.loads(x) for x in r.get_data(as_text=True).splitlines() if x.strip()]
    return next((e for e in evs if e.get('type') == 'error'), {}), evs


# 项目里只有他（拿掉 Beta）→ 整个笔记本没材料
c.delete(f'/api/chain/{P}/sources/{V}')
err, evs = ask_err()
t.check('对话报错点名是谁', 'Alpha' in err.get('error', ''), err or evs[-1:])
t.check('对话报错说是没加进检索', 'searchable' in err.get('error', '').lower(), err)
t.check('报错带上没加进检索的博主（前端好给按钮）', (err.get('not_indexed') or [{}])[0].get('chain_id') == U, err)

cdir = A._chain_dir(P)
o = study.start(cdir, P, 'report', run=lambda f: f())
t.check('报告也点名、说没加进检索', o['status'] == 'failed' and 'Alpha' in (o.get('error') or '')
        and 'searchable' in (o.get('error') or '').lower(), o.get('error'))
o = podcast.start(cdir, P, run=lambda f: f())
t.check('播客也点名', 'Alpha' in (o.get('error') or ''), o.get('error'))
o = slides.start(cdir, P, run=lambda f: f())
t.check('幻灯片也点名', 'Alpha' in (o.get('error') or ''), o.get('error'))

# 点「加进检索」
r = c.post(f'/api/chain/{P}/sources/index_creator', json={'chain_id': 'f' * 32})
t.check('不在这个笔记本里的博主：拒绝', r.status_code == 404, r.status_code)
r = c.post(f'/api/chain/{P}/sources/index_creator', json={'chain_id': U})
t.check('加进检索：成功', r.status_code == 200 and r.get_json().get('ok'), r.get_json())
t.check('他的 index_transcripts 打开了', A._read_chain(U).get('index_transcripts') is True)
for _ in range(100):
    if A._chain_dir(U) not in ask._emb_jobs:
        break
    time.sleep(0.05)
t.check('后台给他的原文段落建了向量', os.path.exists(os.path.join(A._chain_dir(U), 'embeddings.npz')))
ppl = {x['chain_id']: x for x in c.get(f'/api/chain/{P}/people').get_json()['people']}
t.check('人名表里标他能检索了（前端靠它打开对话）', ppl[U].get('searchable') is True, ppl.get(U))
src = c.get(f'/api/chain/{P}/sources').get_json()
t.check('左栏不再标', not src['channels'][0].get('not_indexed'), src['channels'][0].get('not_indexed'))
err, evs = ask_err()
done = next((e for e in evs if e.get('type') == 'done'), None)
t.check('之后能问、引到他的原文', done and done['message']['citations'], err or evs[-1:])

# 正在跑的博主（同步频道时 chain.json 会被整份重写）：先别改
st = A._read_chain(V)
st['stage'] = 'transcribing'
A._save_chain(st)
c.post(f'/api/chain/{P}/sources/transcripts', json={'chain_ids': [V]})
r = c.post(f'/api/chain/{P}/sources/index_creator', json={'chain_id': V})
t.check('还在跑的博主：先不改（409）', r.status_code == 409, r.status_code)

t.finish()
