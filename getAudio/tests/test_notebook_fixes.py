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

t.finish()
