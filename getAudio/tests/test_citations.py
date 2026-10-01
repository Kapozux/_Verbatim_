"""出处模块（citations.py）：模型写回来的各种走样出处都认成真 id，编造的删掉并记下来。
纯函数，不用数据目录、不调模型。用例都是这个项目里模型真写过的样子。"""
from _support import Checks, isolate

isolate('citations')
import citations  # noqa: E402

t = Checks()


def ep(label, kind='episode', person_tag='', person='', ui_label=None):
    return {'label': label, 'ui_label': ui_label or label, 'kind': kind, 'person_tag': person_tag, 'person': person,
            'title': label, 'task_id': '', 'video_url': '', 'date': '', 'doc_id': ''}


def card(cid, ep_i, layer='claim'):
    return {'id': cid, 'ep': ep_i, 'layer': layer, 'quote': 'q ' + cid, 'obs': '', 'ts': '', 'sec': None}


# 一个项目：自己的两期 + 一份文档 + 引用博主 B 的一期（段落用固定编号，给模型看短别名）
corpus = {'episodes': [ep('EP1'), ep('EP2'), ep('DOC1', kind='doc'),
                       ep('B·EP1', person_tag='B', person='Alpha', ui_label='EP1')]}
cards = [card('3-12', 0), card('3-13', 0), card('4-1', 1),
         card('t302223-16', 1, 'source'), card('d77-2', 2, 'source'),
         card('B:5-8', 3), card('B:t9001-4', 3, 'source')]


def fresh():
    return citations.Citations(view=lambda c, corpus, prefix='': {'id': prefix + c['id']}).add(corpus, cards)


# 给模型看的 id：卡片原样；段落用跟标签对得上的短别名；引用博主的段落带字母
pid = {c['id']: citations.prompt_id(c, corpus) for c in cards}
t.check('prompt_id：卡片原样、段落短别名、引用博主带字母',
        pid['3-12'] == '3-12' and pid['t302223-16'] == 't2-16' and pid['d77-2'] == 'd1-2'
        and pid['B:5-8'] == 'B:5-8' and pid['B:t9001-4'] == 'B:t1-4')

cases = [
    ('标准写法', 'A [#3-12].', 'A [#3-12].', ['3-12'], []),
    ('一个括号里好几个', 'A [#3-12, #3-13].', 'A [#3-12][#3-13].', ['3-12', '3-13'], []),
    ('@ 写法', 'A [@3-12, @4-1].', 'A [#3-12][#4-1].', ['3-12', '4-1'], []),
    ('裸方括号、全是真 id', 'A [3-12].', 'A [#3-12].', ['3-12'], []),
    ('裸方括号像日期：不动', 'In [2026-09] he said.', 'In [2026-09] he said.', [], []),
    ('前面带下划线 / 反斜杠', 'A [_#3-12] B [\\#4-1].', 'A [#3-12] B [#4-1].', ['3-12', '4-1'], []),
    ('段落短别名 → 真 id', 'Per the outline [#d1-2].', 'Per the outline [#d77-2].', ['d77-2'], []),
    ('别名连字母都省了', 'Said so [#2-16].', 'Said so [#t302223-16].', ['t302223-16'], []),
    ('引用博主的段落别名', 'Alpha [#B:t1-4].', 'Alpha [#B:t9001-4].', ['B:t9001-4'], []),
    ('字母写错、换个字母只有一张：改回', 'Alpha [#C:5-8].', 'Alpha [#B:5-8].', ['B:5-8'], []),
    ('括号没合上', 'X [#3-12, [#3-13, [#4-1].', 'X [#3-12][#3-13][#4-1].', ['3-12', '3-13', '4-1'], []),
    ('前面加「引文：」', '要点（引文：[#3-12]）', '要点（[#3-12]）', ['3-12'], []),
    ('编造的删掉、标点前不留空格', 'Made up [#9-9]. Real [#4-1].', 'Made up. Real [#4-1].', ['4-1'], ['9-9']),
    ('走样到认不出的整段删掉', 'A [##1-0 through #4-236] B', 'A  B', [], []),
]
for name, raw, want, used, dropped in cases:
    cit = fresh()
    got = cit.clean(raw)
    ok = got == want.strip() and list(cit.used) == used and cit.dropped_ids == dropped
    t.check(name, ok, f'得到 {got!r} used={list(cit.used)} dropped={cit.dropped_ids}')

cit = fresh()
cit.clean('One [#3-12]. Two [#9-9]. Three [##x y].')
t.check('删掉的计数含格式坏掉的', cit.dropped == 2 and cit.dropped_ids == ['9-9'])

cit = fresh()
t.check('resolve：短别名 / 带 # / 不存在', cit.resolve('d1-2') == 'd77-2' and cit.resolve('#3-12') == '3-12'
        and cit.resolve('[nope]') is None and list(cit.used) == ['d77-2', '3-12'])

# 对比：几个人各自的卡用 A:/B: 前缀区分；显示时带上人名
other = {'episodes': [ep('EP1')]}
cmp = citations.Citations(view=lambda c, corpus, prefix='': {'id': prefix + c['id']})
cmp.add(other, [card('3-12', 0)], prefix='A:', creator='Ann').add(other, [card('3-12', 0)], prefix='B:', creator='Bob')
out = cmp.clean('Ann [#A:3-12]; Bob [#B:3-12]; nobody [#C:3-12].')
t.check('对比：同一个卡号分属两人、人名挂上', out == 'Ann [#A:3-12]; Bob [#B:3-12]; nobody.'
        and cmp.used['A:3-12']['creator'] == 'Ann' and cmp.used['B:3-12']['creator'] == 'Bob')

t.check('strip：去掉全部出处', citations.strip('A [#3-12] and [#B:t9-1].') == 'A  and .')
t.check('keep：只留满足条件的出处', citations.keep('A [#3-12][#4-1]', lambda i: i == '4-1') == 'A [#4-1]'
        and citations.find('x [#d7-1] y') == ['d7-1'])
t.finish()
