"""复习笔记模板：每份来源按用户自己的模板出一份笔记，加来源时自动出（2026-10-06 用户要的，照 Open Notebook 的 transformation）。
用户唯一被验证过的用途是「课堂录音 + 卷子 → 复习笔记」：每节课的笔记格式要一样，不想每次去工作台再写一遍要求。
做法：笔记本存一份模板 + 「加来源时自动出」开关；一份笔记 = 一份自定义报告（study，format=custom、prompt=模板、
范围只勾这一份来源），带 revnote 标记记下是哪份来源的，所以不重复出、出处照样能点回原文。
跑生产代码：/api/projects、/sources/docs、/sources/transcripts、/api/chain/<id>/revnotes、/studio、_project_refresh（后台线程照常起）。
桩（只在模型边界）：ask._llm（记下提示词，按里面真实的段落编号造一句带出处的笔记）、ask._embed_texts。"""
import re
import time

from _support import Checks, fake_embed, isolate, make_transcript

isolate('revnotes')
import app as A  # noqa: E402
import ask  # noqa: E402
import revnotes  # noqa: E402

t = Checks()
prompts = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False, thinking=None):
    prompts.append((purpose, prompt))
    ids = re.findall(r'^\[#([dt]?\d+-\d+)\] \|', prompt, re.M) or ['0-0']
    return f'## 考点\n- 一条考点 [#{ids[0]}]'


ask._llm = fake_llm
ask._embed_texts = fake_embed
A.config.gemini_key = lambda: 'test-key'
c = A.app.test_client()
TEMPLATE = '## 本节考点\n## 老师强调的原话\n## 易错点'


def idle(pid, limit=60):
    end = time.time() + limit
    while time.time() < end:
        items = c.get(f'/api/chain/{pid}/studio').get_json().get('items') or []
        if pid not in A._project_jobs and not any(o['status'] == 'running' for o in items):
            return items
        time.sleep(0.2)
    return c.get(f'/api/chain/{pid}/studio').get_json().get('items') or []


def notes_for(items):
    return {o['revnote']['sid']: o for o in items if o.get('revnote')}


def add_doc(pid, title, text):
    c.post(f'/api/chain/{pid}/sources/docs', json={'title': title, 'text': text})
    idle(pid)
    return [d['doc_id'] for d in c.get(f'/api/chain/{pid}/sources').get_json()['docs'] if d['title'] == title][0]


PID = c.post('/api/projects', json={'name': '语文'}).get_json()['id']
D1 = add_doc(PID, '第一课 赤壁赋', '赤壁赋里苏子与客的问答，体现了变与不变的哲理。\n\n' + '\n\n'.join(f'第{i}段：虚词用法。' for i in range(8)))

g = c.get(f'/api/chain/{PID}/revnotes?lang=zh').get_json()
t.check('没设过：有默认模板、自动没开、列出还没出笔记的来源和估价', bool(g.get('default_template')) and g.get('auto') is False
        and [m['id'] for m in g.get('missing', [])] == [D1] and g.get('est_each_usd', 0) > 0, str(g)[:300])

r = c.post(f'/api/chain/{PID}/revnotes', json={'template': TEMPLATE, 'auto': True, 'run_missing': True}).get_json()
items = idle(PID)
n1 = notes_for(items)
o = n1.get(D1) or {}
t.check('保存模板并勾「给已有的也出」：给这份来源出了一份笔记', r.get('started') == 1 and o.get('status') == 'done'
        and o.get('format') == 'custom' and o.get('prompt') == TEMPLATE, f'{r} {o}')
p = [x for x in prompts if x[0] == 'study'][-1][1]
t.check('提示词里是用户的模板，范围只有这一份来源', TEMPLATE.split('\n')[0] in p
        and o.get('scope') == {'type': 'all', 'sources': [D1]}, p[-400:])
full = c.get(f'/api/chain/{PID}/studio/{o.get("id")}').get_json()
t.check('笔记带出处，能点回原文', bool(full.get('citations')) and '[#' in (full.get('result') or {}).get('md', ''))

D2 = add_doc(PID, '第二课 劝学', '劝学讲学习要积累。\n\n' + '\n\n'.join(f'第{i}段：比喻论证。' for i in range(8)))
n2 = notes_for(idle(PID))
t.check('开着自动：新加的来源自动出一份，已有的不重复出', set(n2) == {D1, D2} and len([x for x in idle(PID) if x.get('revnote')]) == 2)

TID = make_transcript(A.config.RESULTS_FOLDER, [(f'00:0{j}:00', f'老师说：第{j}个考点要背下来。') for j in range(6)], title='第三课录音')
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID]})
n3 = notes_for(idle(PID))
t.check('课堂录音也自动出', TID in n3 and n3[TID]['status'] == 'done', str(list(n3)))

c.post(f'/api/chain/{PID}/revnotes', json={'template': TEMPLATE, 'auto': False})
D3 = add_doc(PID, '第四课 师说', '师说讲从师。\n\n' + '\n\n'.join(f'第{i}段：对比。' for i in range(8)))
n4 = notes_for(idle(PID))
g = c.get(f'/api/chain/{PID}/revnotes').get_json()
t.check('关掉自动：新来源不出；设置里看得到还差这一份', D3 not in n4 and [m['id'] for m in g['missing']] == [D3]
        and g['made'] == 3 and g['template'] == TEMPLATE, str(g)[:300])

r = c.post(f'/api/chain/{PID}/revnotes', json={'template': TEMPLATE, 'auto': False, 'run_missing': True}).get_json()
n5 = notes_for(idle(PID))
t.check('手动补出：只补没出的那一份', r.get('started') == 1 and set(n5) == {D1, D2, TID, D3})

# 上限按「一次刷新」算：先关着自动加两份，再开自动、只刷新一次
c.post(f'/api/chain/{PID}/revnotes', json={'template': TEMPLATE, 'auto': False})
add_doc(PID, '第五课', '\n\n'.join(f'第五课第{i}段。' for i in range(8)))
add_doc(PID, '第六课', '\n\n'.join(f'第六课第{i}段。' for i in range(8)))
revnotes.AUTO_MAX = 1
c.post(f'/api/chain/{PID}/revnotes', json={'template': TEMPLATE, 'auto': True})
A._revnotes_auto(PID)
n6 = notes_for(idle(PID))
t.check('一次自动最多出这么多份（花钱有上限），剩下的在设置里看得到', len(n6) == 5
        and len(c.get(f'/api/chain/{PID}/revnotes').get_json()['missing']) == 1, str(len(n6)))

t.check('模板太长、空模板：拒绝', c.post(f'/api/chain/{PID}/revnotes', json={'template': '', 'auto': True}).status_code == 400
        and c.post(f'/api/chain/{PID}/revnotes', json={'template': 'x' * 5000, 'auto': True}).status_code == 400)
t.finish()
