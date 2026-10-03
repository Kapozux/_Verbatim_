"""笔记本 10-03 的几处问题：回答里把提问的人叫「the user」、中文提问 / 「我要中文的」却给英文、
播客开头不说读了什么、来源文件夹、来源概述。

跑生产代码：ask._answer_lang / ANSWER_PROMPT（经 ask._prepare）、study._lang、podcast.PODCAST_SCRIPT、
/api/projects、/sources/docs、/sources/folders、DELETE /sources/<id>、/api/sources/guide（GET / POST）。
桩（只在模型边界）：summarize._call_gemini（写概述）、ask._llm（_prepare 里检索改写用）。
数据：粘贴的文字文档（不碰资料库）。
"""
import json

from _support import Checks, isolate

TMP = isolate('nbfix')
import app as A  # noqa: E402
import ask  # noqa: E402
import podcast  # noqa: E402
import sources  # noqa: E402
import study  # noqa: E402
import summarize  # noqa: E402

t = Checks()
ZH, EN = 'Chinese (简体中文)', 'English'

# ---- 1 回答语言 ----
flash = ('In JPEG transform coding, what does the quantizer do? Reduces the precision of high-frequency coefficients '
         '(fine details) more than low-frequency ones, because the human eye is less sensitive to high-frequency data '
         'loss. This step significantly reduces file size.     这是我的一个Flashcard 我没看懂啊')
t.check('贴了英文闪卡、自己用中文问 → 中文', ask._answer_lang(flash) == ZH, ask._answer_lang(flash))
t.check('英文提问 → 英文', ask._answer_lang('What is the advantage of denormalization') == EN)
t.check('中文提问 → 中文', ask._answer_lang('反规范化有什么好处') == ZH)
t.check('明说「用英文回答」→ 英文', ask._answer_lang('什么是虚拟内存？用英文回答') == EN)
t.check('只有表情按界面语言', ask._answer_lang('👍', 'zh') == ZH)

corpus_en = {'cards': [{'quote': 'Virtual memory lets the OS use disk space as RAM.'}] * 3, 'episodes': []}
t.check('产出：重点写「我要中文的」→ 中文', study._lang(corpus_en, None, '我要中文的') == ZH)
t.check('产出：重点是中文句子 → 中文', study._lang(corpus_en, None, '只讲第一单元的数据库部分') == ZH)
t.check('产出：重点只是英文术语 → 跟来源', study._lang({'cards': [{'quote': '虚拟内存让系统把硬盘当内存用。'}] * 3,
                                                       'episodes': []}, None, 'Unit A') == ZH)
t.check('产出：界面明确选了语言优先', study._lang(corpus_en, 'en', '我要中文的') == EN)

# ---- 2 回答直接对「你」说话，不叫 the user ----
r = A.app.test_client()
pid = r.post('/api/projects', json={'name': 'IB CS'}).get_json()['id']
did = r.post(f'/api/chain/{pid}/sources/docs', json={'title': 'Notes', 'text': (
    '# Memory\n\nVirtual memory is a memory-management technique where the OS uses disk space as an extension '
    'of RAM.\n\n# Compression\n\nLossy compression permanently removes less important data.')}).get_json()['docs'][0]['doc_id']
ask._llm = lambda *a, **k: '{"standalone": "q", "keywords": []}'
cdir = A._chain_dir(pid)
try:
    ctx = ask._prepare(cdir, 'Is my flashcard right?')
    p = ctx['prompt']
    t.check('提示词要求称「你」、不叫 the user', 'Address them as "you"' in p and 'what the user would like' not in p)
except Exception as e:  # noqa: BLE001
    t.check('提示词要求称「你」、不叫 the user', False, repr(e))

# ---- 3 播客开头说读了什么 ----
t.check('播客脚本要求开头说读了哪些材料', 'what material they went through' in podcast.PODCAST_SCRIPT
        and 'what material the hosts went through' in podcast.PODCAST_POLISH)

# ---- 4 来源文件夹 ----
did2 = r.post(f'/api/chain/{pid}/sources/docs', json={'title': 'More', 'text': 'Normalization reduces redundancy.'}
              ).get_json()['docs'][0]['doc_id']
res = r.put(f'/api/chain/{pid}/sources/folders', json={'folders': [
    {'id': 'unita', 'name': 'Unit A', 'items': [did, did2]},
    {'id': 'BAD ID!', 'name': '  ', 'items': [did, 'x' * 100]}]})
fs = res.get_json().get('folders') or []
t.check('存文件夹：一个来源只在一个文件夹、坏 id 换掉、空名字补上',
        res.status_code == 200 and len(fs) == 2 and fs[0]['items'] == [did, did2] and fs[1]['items'] == []
        and fs[1]['id'] != 'BAD ID!' and fs[1]['name'])
got = r.get(f'/api/chain/{pid}/sources').get_json()
t.check('来源列表带文件夹', [f['id'] for f in got.get('folders') or []] == ['unita', fs[1]['id']])
r.delete(f'/api/chain/{pid}/sources/{did2}')
got = r.get(f'/api/chain/{pid}/sources').get_json()
t.check('删掉来源，文件夹里也拿掉', got['folders'][0]['items'] == [did])
t.check('文件夹格式不对 → 400', r.put(f'/api/chain/{pid}/sources/folders', json={'folders': 'x'}).status_code == 400)

# ---- 5 来源概述 ----
calls = []


def fake_summary(prompt):
    calls.append(prompt)
    return '```json\n' + json.dumps({'summary': '这是一份讲内存和压缩的笔记。', 'topics': ['虚拟内存', '有损压缩']},
                                    ensure_ascii=False) + '\n```'


summarize._call_gemini = fake_summary
g0 = r.get(f'/api/sources/guide?doc={did}&lang=zh').get_json()
t.check('没写过概述时 GET 不花钱、返回空', g0 == {'guide': None} and not calls)
g1 = r.post('/api/sources/guide', json={'doc': did, 'lang': 'zh'}).get_json()
g2 = r.post('/api/sources/guide', json={'doc': did, 'lang': 'zh'}).get_json()
g3 = r.get(f'/api/sources/guide?doc={did}&lang=zh').get_json()
t.check('写概述：要中文就在提示词里要中文，写一次存下来',
        g1['guide']['topics'] == ['虚拟内存', '有损压缩'] and g2 == g1 == g3 and len(calls) == 1
        and 'Chinese' in calls[0] and 'Virtual memory' in calls[0])
big = 'Intro line.\n' + ''.join(f'# Chapter {i}\n\n' + ('word ' * 4000) + '\n' for i in range(80))
s = sources._guide_sample(big)
t.check('长文档只送目录 + 抽样', len(s) <= sources.GUIDE_SAMPLE_CHARS + 2000 and '# Chapter 79' in s)
t.check('概述：坏 id → 404', r.get('/api/sources/guide?doc=nope').status_code == 404
        and r.get('/api/sources/guide?task=../../etc').status_code == 404)

tid = 'a' * 32
import os  # noqa: E402
os.makedirs(os.path.join(A.config.RESULTS_FOLDER, tid))
with open(os.path.join(A.config.RESULTS_FOLDER, tid, 'summary.json'), 'w', encoding='utf-8') as f:
    json.dump({'overview': 'An episode about hiring.', 'sections': [{'title': 'Hiring'}, {'title': 'Pay'}]}, f)
gt = r.get(f'/api/sources/guide?task={tid}').get_json()
t.check('转写用现成的摘要', gt['guide'] == {'summary': 'An episode about hiring.', 'topics': ['Hiring', 'Pay']}
        and len(calls) == 1)

# ---- 6 材料太多：不只拿片头；没重点先挑主题再检索；播客太短接着写 ----
pid2 = r.post('/api/projects', json={'name': 'Big show'}).get_json()['id']
for k in range(30):
    paras = [f'大家好，欢迎收看第{k}期节目。'] + [f'第{k}期第{j}段：讨论{"房价" if k % 3 == 0 else "教育" if k % 3 == 1 else "医疗"}问题的细节，'
                                              f'包括数据、案例和政策建议。' * 3 for j in range(1, 10)]
    r.post(f'/api/chain/{pid2}/sources/docs', json={'title': f'第{k}期', 'text': '\n\n'.join(f'# 段{j}\n\n{x}' for j, x in enumerate(paras))})
big = ask.load(A._chain_dir(pid2), passages=True)
size = study._size(big, big['cards'])
few, thinned = study._thin(big, big['cards'], size // 40)
t.check('来源比预算多得多：挑一部分期、不超预算、不拿片头', thinned and len({c['ep'] for c in few}) < 30
        and study._size(big, few) <= size // 40 * 1.5 and not any('欢迎收看' in c['quote'] for c in few))
one, _ = study._thin(big, big['cards'], size // 9)
t.check('每期一段时拿中间、不拿片头', len({c['ep'] for c in one}) == 30 and not any('欢迎收看' in c['quote'] for c in one))
llm_calls = []


def fake_llm2(prompt, model=None, purpose='ask', grounded=False):
    llm_calls.append(prompt)
    if 'Pick the' in prompt and 'themes' in prompt:
        return json.dumps({'themes': [{'title': '房价', 'query': '房价 数据 政策'}, {'title': '医疗', 'query': '医疗 案例'}]},
                          ensure_ascii=False)
    return '{"standalone": "q", "keywords": []}'


ask._llm = fake_llm2
info = {}
picked, th = study._pick(big, big['cards'], '', budget=size // 10, what='podcast episode', info=info)
topics = {'房价' if '房价' in c['quote'] else '医疗' if '医疗' in c['quote'] else '其它' for c in picked}
t.check('没重点：先挑主题，按主题检索', info.get('themes') == ['房价', '医疗'] and th and topics <= {'房价', '医疗'}
        and len({c['ep'] for c in picked}) >= 4 and 'podcast episode' in llm_calls[0] and '第29期' in llm_calls[0])
short = {'title': 'T', 'plan': ['a'], 'lines': [{'speaker': '小林', 'text': '房价很高。', 'style': '', 'cite': []}]}
long_ = {'title': 'T', 'lines': short['lines'] + [{'speaker': '阿周', 'text': '对，' + '房价的细节很多。' * 40, 'style': '', 'cite': []}]}
seen = []


def fake_json(prompt):
    seen.append(prompt)
    if 'Make it that long.' in prompt:
        return dict(long_)
    if 'you are the editor' in prompt:
        return json.loads(prompt.split('Draft:\n', 1)[1].split('\n\n1. Check it', 1)[0])
    return dict(short)


podcast._llm_json = fake_json
script, _ = podcast.write_script(big, picked[:20], ZH, '', 'deep_dive', 4, ('小林', '阿周'), n_clips=0)
t.check('播客写短了：接着写一次，用长的那版', any('Make it that long.' in p for p in seen)
        and len(script['lines']) == 2 and 'never reuse the sources\' own greetings' in seen[0])

# ---- 7 材料放不下：弹框问一下、预填默认要求；要求带进挑主题那一步；默认要求不算选了语言 ----
f_small = r.get(f'/api/chain/{pid}/studio/fit').get_json()
study.BUDGET_TOKENS, keep_budget = 50, study.BUDGET_TOKENS
f_big = r.get(f'/api/chain/{pid2}/studio/fit').get_json()
f_scoped = r.get(f'/api/chain/{pid2}/studio/fit?scope=' + json.dumps({'type': 'all', 'sources': [big['episodes'][0]['doc_id']]})).get_json()
study.BUDGET_TOKENS = keep_budget
t.check('放得下 / 放不下、按勾选的范围算', f_small['fits'] and not f_big['fits'] and f_big['sources'] == 30
        and f_scoped['sources'] == 1 and f_big['default']['zh'] == study.DEFAULT_LARGE['zh'])
llm_calls.clear()
ask._llm = fake_llm2
study._pick(big, big['cards'], '只讲医疗', budget=size // 10, what='quiz', info={})
t.check('重点里的要求带进挑主题那一步', '只讲医疗' in llm_calls[0] and 'Pick the' in llm_calls[0])
llm_calls.clear()
study._pick(big, big['cards'], '', budget=size // 10, what='quiz', info={})
t.check('没写重点：用默认要求', study.DEFAULT_LARGE['en'] in llm_calls[0])
t.check('预填的默认要求不算选了中文', study._lang(corpus_en, None, study.DEFAULT_LARGE['zh']) == EN)

# ---- 8 两个线程同时写同一份缓存不会撞 ----
import threading  # noqa: E402
errs = []


def writer(k):
    try:
        for i in range(200):
            sources._write_json(os.path.join(TMP, 'race.json'), {'k': k, 'i': i})
    except Exception as e:  # noqa: BLE001
        errs.append(e)


ths = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
[x.start() for x in ths]
[x.join() for x in ths]
t.check('并发写同一个文件不报错', not errs and json.load(open(os.path.join(TMP, 'race.json')))['i'] == 199, errs[:1])

t.finish()
