"""追问「你确定吗 / are you sure?」接到更早那张闪卡上（GitHub #10）：聊天里先后贴过好几张闪卡时，
这种不带内容的追问检索全靠模型改写，改写只看得到每条消息前 300 字，常常改到更早那张上。
现在：认出这类追问，就把最近一条有内容的提问（贴的那张闪卡）接进检索；改写和回答的提示词都标出「最近一轮」。

跑生产代码：ask._prepare → select_cards / _expand / hybrid、ANSWER_PROMPT、_history_text。
桩：ask._llm（改写那步：故意只回原问题、不给关键词，模拟改写没帮上忙）、ask._embed_texts（确定的假向量）。
"""
import os

from _support import Checks, fake_embed, isolate, make_creator

TMP = isolate('followup_anchor')
import app as A  # noqa: E402
import ask  # noqa: E402

t = Checks()
ask._embed_texts = fake_embed
expand_prompts = []


def fake_llm(prompt, **k):
    if 'search keywords' in prompt:
        expand_prompts.append(prompt)
        q = prompt.rsplit('New question:', 1)[1].split('\n', 1)[0].strip()
        return '{"standalone": "%s", "keywords": []}' % q
    return 'ok'


ask._llm = fake_llm
photo = ['photosynthesis turns sunlight into sugar in the chloroplast', 'chlorophyll absorbs red and blue light',
         'the Calvin cycle fixes carbon dioxide']
mito = ['mitochondria make ATP through respiration', 'the electron transport chain sits on the inner membrane',
        'mitochondria have their own DNA']
filler = [[f'filler topic {i} about something unrelated number {i}'] for i in range(8)]
C = make_creator(A, 'Biology', 'bio', [photo, mito] + filler)
cdir = A._chain_dir(C)
ask.FULL_BUDGET_TOKENS = 10        # 卡不多也走检索（不然整批送，测不出挑卡）
ask.SEARCH_TOP_K = 3

pad = ' Extra context on this card. ' * 15      # 让闪卡超过 300 字：以前改写只看得到前 300 字
history = [
    {'role': 'user', 'content': 'My flashcard: Q: What does photosynthesis produce? A: sugar from sunlight in the chloroplast.' + pad + 'Is this right?'},
    {'role': 'assistant', 'content': 'Yes, photosynthesis makes sugar [#0-0].'},
    {'role': 'user', 'content': 'My flashcard: Q: Where is the electron transport chain? A: inner membrane of the mitochondria.' + pad + 'KEYWORD_AT_END respiration ATP. Is this right?'},
    {'role': 'assistant', 'content': 'Yes, on the inner membrane [#1-1].'},
]
mito_ids = {c['id'] for c in ask.load(cdir, passages=True)['cards'] if c['ep'] == 1}


def picked_for(q, hist):
    ctx = ask._prepare(cdir, q, history=hist)
    return {c['id'] for c in ctx['picked']}, ctx


for q in ('are you sure?', '你确定吗？', 'Really?'):
    got, ctx = picked_for(q, history)
    t.check(f'「{q}」接到最近那张闪卡（线粒体）', len(got & mito_ids) >= 2, sorted(got))

# 连着追问两次：第二次「why?」也还是那张（跳过上一句追问，找最近一条有内容的提问）
hist2 = history + [{'role': 'user', 'content': 'are you sure?'}, {'role': 'assistant', 'content': 'Yes [#1-1].'}]
got, _ = picked_for('why?', hist2)
t.check('连着追问：还是最近那张闪卡', len(got & mito_ids) >= 2, sorted(got))

# 改写那步看得到最近那条的全文（以前每条只给 300 字，闪卡后半截看不到）
expand_prompts.clear()
picked_for('are you sure?', history)
t.check('改写提示词里有最近那张闪卡的后半截', expand_prompts and 'KEYWORD_AT_END' in expand_prompts[-1])
t.check('改写提示词说明短追问指最近一轮', expand_prompts and 'most recent' in expand_prompts[-1].lower())

# 回答的提示词：标出最近一轮
_, ctx = picked_for('are you sure?', history)
p = ctx['prompt']
t.check('回答提示词标出最近一轮', 'most recent exchange' in p.lower(), p[-800:])
t.check('最近一轮是线粒体那张，不是光合作用那张',
        p.lower().index('most recent exchange') < p.index('Where is the electron transport chain') and
        p.index('photosynthesis produce') < p.lower().index('most recent exchange'))

# 有内容的新问题不受影响：不把上一张闪卡硬塞进检索
queries = []
real_hybrid = ask.hybrid


def spy_hybrid(corpus, terms, query_text, *a, **k):
    queries.append(query_text)
    return real_hybrid(corpus, terms, query_text, *a, **k)


ask.hybrid = spy_hybrid
got, ctx = picked_for('What does he say about chlorophyll and light?', history)
t.check('新问题照常检索（叶绿素那张）', any('chlorophyll' in c['quote'] for c in ctx['picked']), sorted(got))
t.check('新问题的检索里没接上最近那张闪卡', queries and 'electron transport chain' not in queries[-1], queries[-1:])
picked_for('are you sure?', history)
t.check('追问的检索里接上了最近那张闪卡', 'electron transport chain' in queries[-1], queries[-1:])
ask.hybrid = real_hybrid

# 认不认得出：带语气词的也算，有自己话题的不算
for q in ('为什么呢？', '为啥呢', '真的吗？', '你确定吗', 'Are you sure?', 'why?'):
    t.check(f'「{q}」算不带内容的追问', ask.bare_followup(q))
for q in ('为什么线粒体有自己的 DNA 呢？', 'Why does he say that about ATP?', '呢'):
    t.check(f'「{q}」不算', not ask.bare_followup(q))
# 「为什么呢」之后再追问，还是钉在闪卡上，不钉在「为什么呢」上
hist3 = history + [{'role': 'user', 'content': '为什么呢？'}, {'role': 'assistant', 'content': 'Because [#1-1].'}]
got, _ = picked_for('are you sure?', hist3)
t.check('「为什么呢」之后再追问：还是最近那张闪卡', len(got & mito_ids) >= 2, sorted(got))

t.finish()
