"""读书笔记（notes.py，ADR 0010）：每种来源怎么切块、怎么读，出处和引语的把关，接口和自动读的范围，
工作台放不下全部原文时按笔记定计划、按计划取原文。

跑生产代码：notes.units / read / estimate / status / start_read、sources（粘贴文字、PPTX、图片）、repos.create_repo
（真 git 仓库快照）、ask.load / note_cards、study._pick / _notes_plan / _prompt、/api/chain/<id>/notes*、
app._notes_auto、/api/projects、/sources/docs。
桩（只在模型边界）：ask._llm（读笔记：按提示词里真实出现的段落编号造笔记，外加一个编造的编号和一句编造的引语；
定计划：引用第一、二份来源的块）、analyze._call_gemini_mm（读图：记下收到几张图）。
数据：合成的文字文档、PPTX（含一张大图的空白页）、PNG 图片、一期转写、一个本地 git 仓库、一个博主频道（不开原文检索）。
"""
import io
import json
import os
import re
import subprocess
import time
import zipfile

from _support import Checks, isolate, make_creator, make_transcript

TMP = isolate('notes')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import notes  # noqa: E402
import repos  # noqa: E402
import sources  # noqa: E402
import study  # noqa: E402

t = Checks()
calls = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False):
    calls.append((purpose, prompt))
    if purpose == 'notes':
        nums = [int(x) for x in re.findall(r'^\[(\d+)\]', prompt, re.M)]
        code = 'source code with line numbers' in prompt
        body = prompt.split('Text:\n', 1)[-1]
        first_line = re.sub(r'^\[\d+\]( \([^)]*\))? ', '', body.split('\n', 1)[0])
        real = re.split(r'[。.]', first_line)[0][:30]
        if code:
            return json.dumps({'summary': 'Code part', 'points': [{'text': 'Defines main', 'where': 'app.py:1-3'}],
                               'quotes': [], 'topics': ['code']})
        return json.dumps({'summary': f'Part with {len(nums)} passages',
                           'points': [{'text': f'Point from {n}', 'cite': [n, 99999]} for n in nums[:3]],
                           'quotes': [{'text': real, 'cite': nums[:1]}, {'text': 'words that are not in the source at all',
                                                                          'cite': nums[:1]}],
                           'topics': ['t1']}, ensure_ascii=False)
    if 'You are planning a' in prompt:
        return json.dumps({'title': 'Plan', 'segments': [
            {'title': 'Seg A', 'mode': 'detail', 'angle': 'look closely', 'parts': ['S1.1']},
            {'title': 'Seg B', 'mode': 'overview', 'angle': 'broad', 'parts': ['S2.1', 'S99.1']}]})
    return '{"standalone": "q", "keywords": []}'


images_seen = []


def fake_mm(prompt, image_paths, model=None, purpose='xhs'):
    images_seen.append(len(image_paths))
    calls.append((purpose, prompt))
    return json.dumps({'summary': 'A diagram of a CPU', 'points': [{'text': '(image) ALU and registers', 'cite': []}],
                       'quotes': [], 'topics': ['cpu']})


ask._llm = fake_llm
analyze._call_gemini_mm = fake_mm


def wait_ready(doc_id):
    for _ in range(100):
        m = sources.doc_meta(doc_id)
        if m['status'] != 'converting':
            return m
        time.sleep(0.05)
    return sources.doc_meta(doc_id)


# ---- 1 文档：按段落攒块、在小标题处断；出处只留这一块里的编号，编造的引语丢掉 ----
notes.UNIT_CHARS = 700
text = '\n\n'.join(f'# 第{k}章\n\n' + '\n\n'.join(f'第{k}章第{j}段：虚拟内存把硬盘当作内存的延伸，速度比内存慢。' * 3
                                                for j in range(4)) for k in range(3))
doc = sources.create_doc(text=text, title='讲义')
us, skipped = notes.units('doc', doc['id'])
n1 = notes.read('doc', doc['id'])
allowed_ok = all(c in u['ids'] for u in n1['units'] for p in u['points'] for c in p['cite'])
t.check('文档：切成几块、每块带页 / 标题位置', len(us) >= 3 and all(u['where'] for u in us) and not skipped, [u['where'] for u in us])
t.check('出处只留这一块里有的编号；编造的引语丢掉、真的留下',
        allowed_ok and all(99999 not in p['cite'] for u in n1['units'] for p in u['points'])
        and all(len(u['quotes']) == 1 for u in n1['units']), n1['units'][0])
t.check('读过就存下，再读不花钱；内容变了指纹变', notes.load('doc', doc['id'])['fp'] == notes.fingerprint('doc', doc['id'])
        and notes.read('doc', doc['id']) == n1 and sum(1 for p, _ in calls if p == 'notes') == len(us))

# ---- 2 PPTX：图多字少的幻灯片把图片带上 ----
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P_NS = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def slide(*paras):
    body = ''.join(f'<a:p><a:r><a:t>{x}</a:t></a:r></a:p>' for x in paras)
    return f'<p:sld {P_NS} {A_NS}><p:cSld><p:spTree><p:sp><p:txBody>{body}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'


png_path = os.path.join(TMP, 'big.png')
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'mandelbrot=size=640x480', '-frames:v', '1', png_path], check=True)
png = open(png_path, 'rb').read()
buf = io.BytesIO()
with zipfile.ZipFile(buf, 'w') as z:
    z.writestr('ppt/slides/slide1.xml', slide('The CPU fetches, decodes and executes instructions in a cycle, '
                                              'using the program counter and the instruction register.'))
    z.writestr('ppt/slides/slide2.xml', slide('Diagram'))
    z.writestr('ppt/slides/_rels/slide2.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId2" Target="../media/image1.png"/></Relationships>')
    z.writestr('ppt/media/image1.png', png)
pp = wait_ready(sources.create_doc(filename='CPU.pptx', data=buf.getvalue())['id'])
images_seen.clear()
n2 = notes.read('doc', pp['id'])
t.check('PPTX：图为主的那张幻灯片的图给模型看', pp['status'] == 'ready' and images_seen and sum(images_seen) >= 1
        and any(u.get('images') for u in n2['units']) and not os.path.isdir(os.path.join(sources.doc_dir(pp['id']), 'notes_img')),
        (pp.get('status'), images_seen))

# ---- 3 图片：整张图给模型看（不只靠 OCR 的字）----
img = sources.create_doc(filename='diagram.png', data=png)
m_img = sources.doc_meta(img['id'])
m_img.update(status='ready', chars=0)
sources._save_meta(m_img)
images_seen.clear()
n3 = notes.read('doc', img['id'])
t.check('图片：整张图读成笔记', images_seen == [1] and n3['units'][0]['points'][0]['text'].startswith('(image)'))

# ---- 4 转写：按时间攒块，位置是时间段 ----
tid = make_transcript(A.config.RESULTS_FOLDER, [(f'00:{j:02d}:00', f'第{j}分钟：讲到职业教育和普通高中的分流。' * 4)
                                                for j in range(30)], title='教育')
ut, _ = notes.units('task', tid)
n4 = notes.read('task', tid)
t.check('转写：按时间切块，位置写时间段', len(ut) >= 2 and re.match(r'\d\d:\d\d', ut[0]['where']) and n4['units'],
        [u['where'] for u in ut])

# ---- 5 代码库：没读过的读快照文件，出处写「文件:行」----
gdir = os.path.join(TMP, 'proj')
os.makedirs(os.path.join(gdir, 'node_modules'))
with open(os.path.join(gdir, 'app.py'), 'w') as f:
    f.write('def main():\n    print("hi")\n\nmain()\n')
with open(os.path.join(gdir, 'README.md'), 'w') as f:
    f.write('# Proj\nA tiny app.\n')
with open(os.path.join(gdir, 'node_modules', 'x.js'), 'w') as f:
    f.write('junk')
for cmd in (['init', '-q'], ['add', '.'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'x']):
    subprocess.run(['git', *cmd], cwd=gdir, check=True, capture_output=True)
rm = repos.create_repo(gdir)
ur, _ = notes.units('repo', rm['id'])
n5 = notes.read('repo', rm['id'])
t.check('代码库：读源码、跳过 node_modules、README 在前、出处是文件:行',
        ur and ur[0]['text'].startswith('=== README.md') and 'node_modules' not in ur[0]['text'] and '1: def main():' in ur[0]['text']
        and n5['units'][0]['points'][0].get('where') == 'app.py:1-3', ur[0]['text'][:120] if ur else None)

# ---- 6 估价、状态 ----
e = notes.estimate('doc', doc['id'])
t.check('估价：按字数和块数，正数且很小', 0 < e['usd'] < 0.05 and e['units'] == len(us), e)
t.check('状态：读过 / 没读', notes.status('doc', doc['id'])['status'] == 'done'
        and notes.status('task', make_transcript(A.config.RESULTS_FOLDER, [('00:00:01', 'x')]))['status'] == 'none')

# ---- 7 接口和自动读：单独加的文档自动读；频道里的期不自动读，要点了才读 ----
c = A.app.test_client()
pid = c.post('/api/projects', json={'name': 'Mixed'}).get_json()['id']
d_new = c.post(f'/api/chain/{pid}/sources/docs', json={'title': 'New notes', 'text': '新加的讲义：二进制和十六进制的转换方法。' * 20}
               ).get_json()['docs'][0]['doc_id']
ch = make_creator(A, 'Host', 'host', [['Small teams ship faster.', 'Talk to users.'], ['Hire slowly.']], transcripts=True)
A._reg_save(pid, dict(A._reg(pid), channels=[{'chain_id': ch, 'tag': 'B', 'added_at': '2026-10-03'}]))
A._notes_auto(pid)
for _ in range(100):
    if not any(notes.reading(k, s) for k, s, _ in A._note_sources(pid)):
        break
    time.sleep(0.05)
st = c.get(f'/api/chain/{pid}/notes?estimate=1&items=1').get_json()
kinds = {x['kind']: x['status'] for x in st['items'] if x['id'] == d_new}
eps = [x for x in st['items'] if x['kind'] == 'task']
t.check('加的文档自动读；引用博主的期不自动读', kinds.get('doc') == 'done' and eps and all(x['status'] == 'none' for x in eps)
        and st['unread'] == len(eps) and st['estimate_usd'] > 0, st)
r = c.post(f'/api/chain/{pid}/notes/read', json={}).get_json()
for _ in range(100):
    st2 = c.get(f'/api/chain/{pid}/notes').get_json()
    if not st2['reading']:
        break
    time.sleep(0.05)
t.check('点了「读」：剩下的都读完', r['started'] == len(eps) and st2['read'] == st2['total'], st2)

# ---- 8 工作台放不下：按笔记定计划，细讲取那几块原文，概括只取引到的段；转写段落现场补进来 ----
calls.clear()
corpus = ask.load(A._chain_dir(pid), passages=True)
info = {}
cards, thinned = study._pick(corpus, corpus['cards'], '', budget=60, what='podcast episode', info=info)
plan_prompt = next(p for k, p in calls if 'You are planning a' in p)
ids = [c['id'] for c in cards]
t.check('按笔记定计划：看到每份来源的块摘要、计划写进提示词', info.get('plan') == 'notes' and info['themes'] == ['Seg A', 'Seg B']
        and 'S1.1 [' in plan_prompt and 'S2.1 [' in plan_prompt and 'Seg A (detail)' in corpus.get('_plan', '')
        and 'Seg A (detail)' in study._prompt(corpus, cards, 'English', '', ''), (info, ids))
t.check('按计划取原文：只取计划里那几块的段落', thinned and ids and len({c['ep'] for c in cards}) <= 2, ids)
tcards = [c for c in cards if c['id'].startswith('t')]
t.check('博主的期没开原文检索：段落现场补上、出处能对上', all(c in corpus['cards'] for c in cards)
        and (not tcards or all(c.get('ts') is not None for c in tcards)), ids)

t.finish()
