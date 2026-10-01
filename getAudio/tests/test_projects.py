"""项目（Project）后端：建项目 → 加来源（粘贴文字 / PDF / 资料库转写 / 直接传录音）→ 建索引 → 带范围提问 → 移除来源。

跑生产代码：app 的 /api/projects、/sources*、/ask/stream、/api/docs；ask.load(passages)、scope_pool、
embed_chain、_finish；sources.py 的转换和切段（墨页没开时用 pdftotext 兜底）。
桩（只在模型边界）：ask._embed_texts（按文字哈希出确定的向量）、analyze._call_gemini_stream（假回答，
引用提示词里真实存在的段落 id）、transcribe_gemini35.transcribe_audio（直接传录音那一项）、summarize / enrich。
数据：合成的两期转写、现生成的 PDF 和一段正弦波音频（不碰资料库）。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

from _support import Checks, fake_embed as _embed, isolate, make_transcript

TMP = isolate('projects')
if not shutil.which('cupsfilter'):
    print('没有 cupsfilter（不是 macOS），生成不了测试 PDF，整份跳过')
    print('结论：0 通过，0 失败，1 跳过')
    sys.exit(0)
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import enrich  # noqa: E402
import sources  # noqa: E402
import summarize  # noqa: E402
import transcribe_gemini35  # noqa: E402

# 资料库里两期合成转写（每期 30 段，够切出十几段原文）
_TOPICS = ['solo founders', 'cold email', 'hardware startups', 'AI agents', 'fundraising', 'hiring']
TIDS = [make_transcript(A.config.RESULTS_FOLDER,
                        [(f'00:{m:02d}:{s:02d}', f'Part {k}: we talk about {_TOPICS[k % 6]} and why it matters for '
                          f'early teams. Founders should test ideas with real users before scaling, number {k}.')
                         for k, (m, s) in enumerate((j // 6, (j * 10) % 60) for j in range(30))],
                        title=f'Startup talk {n + 1}', creator='Host') for n in range(2)]
assert A.config.RESULTS_FOLDER.startswith(TMP) and sources.DOCS_DIR.startswith(TMP)
calls = {'embed': 0, 'prompts': []}


def fake_embed(texts, task=None):
    calls['embed'] += len(texts)
    return _embed(texts, task)


def fake_stream(prompt, model=None, purpose='analysis'):
    calls['prompts'].append(prompt)
    d = re.findall(r'^\[#(d\d+-\d+)\] \| DOC', prompt, re.M)
    t = re.findall(r'^\[#(t\d+-\d+)\] \| EP', prompt, re.M)
    parts = ['The outline asks for the host–guest structure ']
    if d:
        parts.append(f'[#{d[0]}]. ')
    if t:
        parts.append(f'In the recording they discuss startups [#{t[0]}]. ')
    for p in parts:
        yield p


ask._embed_texts = fake_embed
analyze._call_gemini_stream = fake_stream
ask._llm = lambda prompt, **k: '{"standalone": "q", "keywords": ["outline", "startups"]}'
summarize.summarize_transcript = lambda *a, **k: None
enrich.enrich_task = lambda *a, **k: False
transcribe_gemini35.transcribe_audio = lambda *a, **k: [
    {'timestamp': '00:00:01', 'text': '说话人1：今天我们讲赤壁赋的主客问答结构，这是考试重点。'},
    {'timestamp': '00:00:09', 'text': '说话人1：大家注意主客两个人对于变与不变的看法。'},
    {'timestamp': '00:00:16', 'text': '说话人1：下节课我们讲劝学。'}]
c = A.app.test_client()
ok = {}


def wait_idle(cid, t=120):
    end = time.time() + t
    while time.time() < end:
        if cid not in A._project_jobs:
            return True
        time.sleep(0.5)
    return False


def ask_stream(cid, q, scope=None):
    resp = c.post(f'/api/chain/{cid}/ask/stream', json={'question': q, 'ui_lang': 'en', 'scope': scope})
    evs = [json.loads(l) for ch in resp.response for l in ch.decode().splitlines() if l.strip()]
    return resp.status_code, evs


# 1 建项目
r = c.post('/api/projects', json={}).get_json()
PID = r['id']
st = A._read_chain(PID)
print('1 建项目（不用选模板、不用起名）：', st['author'], '| kind', st['kind'], 'analyze', st['analyze'], 'index_transcripts', st['index_transcripts'])
ok['create'] = st['kind'] == 'project' and not st['analyze'] and st['index_transcripts'] and st['author'] == 'Untitled project'
rn = c.post(f'/api/chain/{PID}/rename', json={'name': '语文期末'}).get_json()
ok['rename'] = rn.get('ok') and A._read_chain(PID)['author'] == '语文期末' and A._read_chain(PID).get('renamed')
print('   改名：', A._read_chain(PID)['author'])

# 2 加来源：粘贴文字 + PDF（墨页）+ 资料库两期转写
r1 = c.post(f'/api/chain/{PID}/sources/docs', json={'title': '考试提纲', 'text': '# 三、文言文\n\n重点掌握《赤壁赋》的主客问答结构。\n\n# 四、背诵\n\n《劝学》《师说》全文背诵。'}).get_json()
txt = os.path.join(TMP, 'l.txt')
open(txt, 'w').write('\f'.join(f'Lecture {n}\n\nThe host and the guest argue about change. Point {n}.\n' for n in range(1, 4)))
pdf = os.path.join(TMP, 'lecture.pdf')
subprocess.run(['cupsfilter', txt], stdout=open(pdf, 'wb'), stderr=subprocess.DEVNULL)
r2 = c.post(f'/api/chain/{PID}/sources/docs', data={'files': (open(pdf, 'rb'), 'lecture.pdf')},
            content_type='multipart/form-data').get_json()
r3 = c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': TIDS}).get_json()
print('2 加来源：粘贴', r1['docs'][0]['status'], '| PDF', r2['docs'][0]['status'], '| 转写', r3)
idle = wait_idle(PID)
src = c.get(f'/api/chain/{PID}/sources').get_json()
print('   来源列表：', [(d['title'], d['status'], d['converter'], d['pages']) for d in src['docs']],
      '| 录音', len(src['videos']), '| 墨页', src['moye'], '| 索引跑完', idle)
ok['sources'] = (idle and len(src['docs']) == 2 and all(d['status'] == 'ready' for d in src['docs'])
                 and src['docs'][1]['pages'] == 3 and len(src['videos']) == 2)
cards_files = [f for f in os.listdir(A._chain_dir(PID)) if f.startswith('cards_')]
reg = json.load(open(os.path.join(A._chain_dir(PID), 'sources.json')))
print('   默认不抽卡：', not cards_files, '| 文档进了登记表：', len(reg['docs']), '| chain.json 里没有 docs：', 'docs' not in A._read_chain(PID))
ok['study_no_cards'] = not cards_files and len(reg['docs']) == 2 and 'docs' not in A._read_chain(PID)

# 3 索引：文档段落 + 转写段落都有向量
corpus = ask.load(A._chain_dir(PID), passages=True)
kinds = {}
for card in corpus['cards']:
    ep = corpus['episodes'][card['ep']]
    kinds[ep['kind'] + ':' + card['layer']] = kinds.get(ep['kind'] + ':' + card['layer'], 0) + 1
emb = ask.load_embeddings(A._chain_dir(PID))
print('3 索引：', kinds, '| 向量', len(emb[0]) if emb else 0, '/', len(corpus['cards']),
      '| 标签', [e['label'] for e in corpus['episodes']])
ok['index'] = emb and len(emb[0]) == len(corpus['cards']) and kinds.get('doc:source') and kinds.get('episode:source')
plain = ask.load(A._chain_dir(PID))
ok['load_default_unchanged'] = len(plain['cards']) == 0
print('   不传 passages 时（证据卡墙 / 话题 / 预测用的）：', len(plain['cards']), '张')

# 4 提问：引用文档段落 + 转写段落，出处带页码 / 时间点
code, evs = ask_stream(PID, 'What does the outline say about the host and guest?')
done = evs[-1]
cites = done['message']['citations'] if done['type'] == 'done' else {}
print('4 提问：', code, [e['type'] for e in evs][:3], '…', done['type'])
for k, v in cites.items():
    print('   出处', k, '| kind', v['kind'], '| label', v['label'], '| page', v.get('page'), '| ts', v['ts'],
          '| doc_id', bool(v.get('doc_id')), '| task', bool(v.get('task_id')))
ok['ask_cites'] = (done['type'] == 'done' and any(v['kind'] == 'doc' and v['label'].startswith('DOC') for v in cites.values())
                   and any(v['kind'] == 'passage' and v['ts'] for v in cites.values()))
pr = calls['prompts'][-1]
ok['prompt_has_rule'] = 'type "source"' in pr and '(document)' in pr

# 5 范围：只看文档 / 只看录音 / 只看某一份
def ids_in_last_prompt():
    p = calls['prompts'][-1]
    return set(re.findall(r'^\[#([dt]?\d+-\d+)\]', p, re.M))


def titles_in_last_prompt():
    # 提示词里的来源清单：「DOC1 = 考试提纲 (document)」「EP3 = …」，只列这次用到的来源
    return set(re.findall(r'^(?:DOC|EP)\d+ = (.+?)(?: \(document\))?(?: \(\d{4}-\d{2}-\d{2}\))?$', calls['prompts'][-1], re.M))
ask_stream(PID, 'outline?', scope={'type': 'docs'}); a = ids_in_last_prompt()
ask_stream(PID, 'startups?', scope={'type': 'media'}); b = ids_in_last_prompt()
doc1 = src['docs'][0]['doc_id']
ask_stream(PID, 'outline?', scope={'type': 'all', 'sources': [doc1]}); cc = ids_in_last_prompt(); cc_titles = titles_in_last_prompt()
print('5 范围：只看文档 →', sorted({i[0] for i in a}), '| 只看录音 →', sorted({i[0] for i in b}),
      '| 只看提纲 →', sorted(cc))
ok['scope'] = (a and all(i.startswith('d') for i in a) and b and all(i.startswith('t') for i in b)
               and cc and cc_titles == {'考试提纲'})
code, evs = ask_stream(PID, 'x', scope={'type': 'all', 'sources': ['nope']})
print('   范围里什么都没有：', evs[-1])
ok['scope_empty'] = evs[-1]['type'] == 'error'

# 6 文档阅读器接口
d = c.get(f'/api/docs/{src["docs"][1]["doc_id"]}').get_json()
print('6 阅读器：', d['meta']['title'], '| 段落', len(d['passages']), '| 页码', [p['page'] for p in d['passages']],
      '| 原件', c.get(f'/api/docs/{src["docs"][1]["doc_id"]}/original').status_code,
      '| 非法 id', c.get('/api/docs/zzz').status_code)
ok['reader'] = len(d['passages']) == 3 and [p['page'] for p in d['passages']] == [1, 2, 3]

# 7 直接往项目里传录音：转完自己进项目
clip = os.path.join(TMP, 'clip.mp3')
subprocess.run([A.config.FFMPEG_BIN, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=20',
                '-ac', '1', clip], check=True)
r = c.post(f'/api/chain/{PID}/sources/upload', data={'audios': (open(clip, 'rb'), '第3节课.mp3'), 'engine': 'gemini35'},
           content_type='multipart/form-data').get_json()
tid = r['tasks'][0]['task_id']
end = time.time() + 60
while time.time() < end and not any(v['task_id'] == tid for v in A._read_chain(PID)['videos']):
    time.sleep(0.5)
wait_idle(PID)
vids = A._read_chain(PID)['videos']
print('7 直接传录音：转完进了项目', any(v['task_id'] == tid for v in vids), '| 录音数', len(vids))
ok['upload'] = any(v['task_id'] == tid for v in vids)

# 8 移除来源：文档 / 录音；全局文件不删
r = c.delete(f'/api/chain/{PID}/sources/{doc1}')
r2 = c.delete(f'/api/chain/{PID}/sources/{TIDS[0]}')
src = c.get(f'/api/chain/{PID}/sources').get_json()
print('8 移除：', r.status_code, r2.status_code, '| 剩文档', len(src['docs']), '录音', len(src['videos']),
      '| 全局文档还在', os.path.isdir(sources.doc_dir(doc1)), '| 全局转写还在', os.path.isdir(os.path.join(TMP, 'results', TIDS[0])))
ok['remove'] = (r.status_code == 200 and r2.status_code == 200 and len(src['docs']) == 1 and len(src['videos']) == 2
                and os.path.isdir(sources.doc_dir(doc1)))
ask_stream(PID, 'outline?', scope={'type': 'docs'})
left_titles = titles_in_last_prompt()
ok['removed_not_searched'] = left_titles == {'lecture'}
# 存下来的 id 是固定编号：删掉第一份之后，第二份文档的段落真 id 不变（提示词里的短别名会变，那没关系）
pdf_doc = src['docs'][0]['doc_id']
corpus2 = ask.load(A._chain_dir(PID), passages=True)
ok['ids_stable'] = sorted(c['id'] for c in corpus2['cards'] if c['id'].startswith('d')) == [f'd{ask._stable_no(pdf_doc)}-{i}' for i in range(3)]
print('   删掉提纲后提示词里只剩：', left_titles, '| PDF 段落的真 id 没变：', ok['ids_stable'])

# 9 有频道的项目：资料库录音进登记表（不碰频道的视频表），它们的原文段落也能问
creator = 'c' * 32
os.makedirs(A._chain_dir(creator))
A._save_chain({'id': creator, 'url': 'https://www.youtube.com/@x', 'author': 'X', 'stage': 'done', 'videos': []})
a = c.post(f'/api/chain/{creator}/sources/docs', json={'text': 'his book chapter 1'}).status_code
b = c.post(f'/api/chain/{creator}/sources/transcripts', json={'task_ids': TIDS}).get_json()
wait_idle(creator)
creg = json.load(open(os.path.join(A._chain_dir(creator), 'sources.json')))
cc = ask.load(A._chain_dir(creator), passages=True)
print('9 频道项目：加文档', a, '| 加资料库录音', b, '| 登记表录音', len(creg['recordings']), '| 视频表没动', A._read_chain(creator)['videos'] == [],
      '| 可问的段落', len(cc['cards']), '| 标题', [e['title'][:18] for e in cc['episodes']])
ok['creator_sources'] = (a == 200 and b.get('added') == 2 and len(creg['recordings']) == 2
                         and A._read_chain(creator)['videos'] == []
                         and len(cc['cards']) == 1 + sum(len(sources.transcript_passages(t)) for t in TIDS)
                         and not any(re.fullmatch(r'[0-9a-f-]{36}', e['title']) for e in cc['episodes']))
lst = c.get('/api/chains').get_json()
proj = [x for x in lst if x['id'] == PID][0]
print('   列表里：', proj['author'], '| n_sources', proj['n_sources'], '| updated_at', proj['updated_at'])
ok['list_counts'] = proj['n_sources'] == 3 and proj['updated_at']

# 10 往没频道的项目里加频道：同一个项目变成频道项目，原来的录音挪进登记表（run_chain 打桩，不真下）
ran = []
A.run_chain = lambda st: ran.append(st['id'])
p2 = c.post('/api/projects', json={'name': 'X'}).get_json()['id']
c.post(f'/api/chain/{p2}/sources/transcripts', json={'task_ids': TIDS[:1]})
c.post(f'/api/chain/{p2}/sources/docs', json={'text': 'notes'})
r = c.post('/api/chain', json={'url': 'https://www.youtube.com/@someone', 'project_id': p2, 'targets': []}).get_json()
time.sleep(0.5)
st2 = A._read_chain(p2)
reg2 = json.load(open(os.path.join(A._chain_dir(p2), 'sources.json')))
again = c.post('/api/chain', json={'url': 'https://www.youtube.com/@other', 'project_id': p2}).status_code
print('10 加频道：同一个项目', r.get('chain_id') == p2, '| 跑了频道流程', ran == [p2], '| kind', st2.get('kind'), '| url', st2['url'],
      '| 原录音挪进登记表', [x['task_id'] for x in reg2['recordings']] == TIDS[:1], '| 文档还在', len(reg2['docs']),
      '| 第二个频道', again)
# 第二步起：第二个频道不再拒绝，另起一条博主链条按引用加进来（test_people.py 细测）
ok['attach_channel'] = (r.get('chain_id') == p2 and ran[:1] == [p2] and not st2.get('kind') and st2['url'].endswith('@someone')
                        and [x['task_id'] for x in reg2['recordings']] == TIDS[:1] and len(reg2['docs']) == 1 and again == 200
                        and len(A._reg(p2)['channels']) == 1)

# 11 按需抽证据卡（模型打桩：抽卡 / 综述 / 话题）
import analyze as AN
AN.analyze_episode = lambda title, text, author, **k: {'title': title, 'cards': [
    {'obs': 'Argues X', 'quote': text.split('] ', 1)[-1][:60], 'timestamp': '00:00:05', 'layer': '他的主张'}], 'metrics': {}}
AN.synthesize_collection = lambda eps, name, **k: '# Overview\n\nstub'
A._auto_tag = lambda cdir: None
A._annotate_speakers = lambda cdir, eps: None
A._review_episode_transcript = lambda tid, preset=None: json.load(open(os.path.join(TMP, 'results', tid, 'transcript.json')))
before = c.get(f'/api/chain/{PID}/sources').get_json()
r = c.post(f'/api/chain/{PID}/cards/build').get_json()
wait_idle(PID)
after = c.get(f'/api/chain/{PID}/sources').get_json()
st = A._read_chain(PID)
print('11 按需抽卡：之前缺', before['cards_missing'], '| 之后缺', after['cards_missing'], '| 有卡', after['has_cards'],
      '| 综述', st.get('final_doc'), '| 频道项目拒绝', c.post(f'/api/chain/{creator}/cards/build').status_code)
ok['build_cards'] = before['cards_missing'] >= 1 and after['cards_missing'] == 0 and after['has_cards'] and st.get('final_doc')

# 12 删项目：只删它独有的文档
only = [d['doc_id'] for d in c.get(f'/api/chain/{PID}/sources').get_json()['docs']]
c.delete(f'/api/chain/{PID}')
print('12 删项目：项目目录没了', not os.path.isdir(A._chain_dir(PID)), '| 它的文档没了', all(not os.path.isdir(sources.doc_dir(d)) for d in only))
ok['delete'] = not os.path.isdir(A._chain_dir(PID)) and all(not os.path.isdir(sources.doc_dir(d)) for d in only)

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
