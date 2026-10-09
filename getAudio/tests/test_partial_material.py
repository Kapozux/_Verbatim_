"""材料放不下、只送了一部分时，提示词里要写明（借 Open Notebook 的 source_text_status）：
模型不知道自己只看到一部分，会说「他从没提过」「提纲第 3 条没讲」——其实只是那段没送进去。

跑生产代码：study.start（报告，同步执行）/ _pick / _thin / _prompt；ask._prepare / select_cards。
桩（只在模型边界）：ask._llm（记下提示词）、ask._embed_texts（确定性向量）、study._notes_plan 和 study._themes
（返回空，走均匀抽样那条路）。预算调小到几张卡，用不着造几万字的材料。
"""
import json
import os

from _support import Checks, fake_embed, isolate, make_creator

TMP = isolate('partial')
import app as A  # noqa: E402
import ask  # noqa: E402
import study  # noqa: E402

t = Checks()
prompts = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False):
    prompts.append(prompt)
    if 'standalone' in prompt:
        return '{"standalone": "teams", "keywords": []}'
    return 'Small teams win [#1-1].'


ask._llm = fake_llm
ask._embed_texts = fake_embed
study._notes_plan = lambda *a, **k: None
study._themes = lambda *a, **k: []

eps = [[f'point {e}-{j} about small teams and shipping' for j in range(6)] for e in range(6)]
cid = make_creator(A, 'Ann', 'ann', eps)
cdir = A._chain_dir(cid)
for i in range(len(eps)):                       # 都打上同一个话题：按话题问时走「沿时间线均匀取」
    path = os.path.join(cdir, f'cards_{i:03d}.json')
    d = json.load(open(path, encoding='utf-8'))
    for c in d['cards']:
        c['topic'] = 'teams'
    json.dump(d, open(path, 'w', encoding='utf-8'))


def report():
    prompts.clear()
    out = study.start(cdir, cid, 'report', run=lambda f: f())
    return out, next(p for p in prompts if 'study material' in p)


# ---- 1 放得下：不加说明 ----
out, p = report()
t.check('全部放得下：报告成功', out['status'] == 'done', out.get('error'))
t.check('全部放得下：提示词里没有「只看到一部分」', study.PARTIAL_MARK not in p)

# ---- 2 放不下：均匀抽样，提示词写明看到了多少 ----
real_budget = study.BUDGET_TOKENS
study.BUDGET_TOKENS = study._size(ask.load(cdir, passages=True), ask.load(cdir, passages=True)['cards']) // 3
out, p = report()
cov = out.get('coverage') or {}
t.check('放不下：报告成功、标了抽样', out['status'] == 'done' and cov.get('thinned'), str(cov))
t.check('放不下：提示词写明只看到一部分', study.PARTIAL_MARK in p)
t.check('放不下：写出用了几段、共几段',
        f"{cov.get('passages_used')} of the {cov.get('passages_total')} passages" in p, p[:600])
study.BUDGET_TOKENS = real_budget

# ---- 3 问答：卡少整批送，不加说明；卡多检索，写明只送了检索到的 ----
ctx = ask._prepare(cdir, 'What does Ann think about teams?')
t.check('问答整批：没有「只看到一部分」', ctx['cov']['mode'] == 'all' and ask.PARTIAL_MARK not in ctx['prompt'])
real_full = ask.FULL_BUDGET_TOKENS
ask.FULL_BUDGET_TOKENS = 50
ask.SEARCH_TOP_K = 8
ctx = ask._prepare(cdir, 'What does Ann think about teams?')
cov = ctx['cov']
t.check('问答检索：写明只看到一部分', cov['mode'] == 'search' and ask.PARTIAL_MARK in ctx['prompt'], str(cov))
t.check('问答检索：写出送了几张、共几张',
        f"{cov['cards_used']} of the {cov['pool_cards']}" in ctx['prompt'])
ctx = ask._prepare(cdir, 'How did her view change?', topic='teams')
t.check('问答按话题均匀取：也写明', ctx['cov']['mode'] == 'spread' and ask.PARTIAL_MARK in ctx['prompt'],
        str(ctx['cov']))
ask.FULL_BUDGET_TOKENS = real_full

t.finish()
