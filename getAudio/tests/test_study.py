"""工作台复习工具（study.py + app 的 /studio 接口）和问答的「背景」。

跑生产代码：/api/projects、/sources/*、/studio*（后台线程照常起）、/api/export/pdf（墨页没开时只测 503 那一半）、
ask.load(passages)、scope_pool、hybrid、_alias/_unalias、clean_citations、card_view、_background。
桩（只在模型边界）：ask._llm（按提示词里真实出现的段落别名造回答，外加一个编造的 id 看会不会被删）、
ask._embed_texts（确定性向量）、analyze._call_gemini_stream（问答回答）。
数据：一期合成转写（不碰资料库）。
"""
import json
import os
import re
import time

from _support import Checks, fake_embed as _embed, isolate, make_transcript

TMP = isolate('study')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import sources  # noqa: E402
import study  # noqa: E402

TID = make_transcript(A.config.RESULTS_FOLDER,
                      [(f'00:{j // 6:02d}:{(j * 10) % 60:02d}', f'Segment {j}: solo founders, hiring and the harness '
                        f'around AI models; why small teams ship faster and talk to users every week.') for j in range(40)],
                      title='Startup talk', creator='Host', url='https://www.youtube.com/watch?v=yslXlV2BP_Y')
assert A.config.RESULTS_FOLDER.startswith(TMP)
prompts = []


fake_embed = _embed


def ids_in(prompt):
    return re.findall(r'^\[#([dt]?\d+-\d+)\] \|', prompt, re.M)


def fake_llm(prompt, model=None, purpose='ask', grounded=False):
    prompts.append((purpose, prompt))
    if purpose != 'study':
        return '{"standalone": "q", "keywords": ["outline"]}'
    ids = ids_in(prompt) or ['0-0']
    a, b = ids[0], ids[-1]
    if 'Task: write a study guide' in prompt:
        return f'```markdown\n## Unit\n- Point one [#{a}]\n- Point two 引文：[#{b}][#d99-9]\n```'
    if 'briefing document' in prompt or 'Task: write an FAQ' in prompt or 'Task: build a timeline' in prompt \
            or 'Task, written by the user' in prompt:
        return f'## Brief\n- Something [#{a}]'
    if 'Task: make' in prompt:
        n = int(re.search(r'Task: make (\d+) flashcards', prompt).group(1))
        return json.dumps({'cards': [{'front': f'Q{i}', 'back': f'A{i} [#{a}]'} for i in range(n)]})
    if 'multiple-choice' in prompt:
        n = int(re.search(r'Task: write (\d+) multiple', prompt).group(1))
        return json.dumps({'questions': [{'q': f'Q{i}', 'options': ['RIGHT', 'w1', 'w2', 'w3'], 'answer': 0,
                                          'explain': f'because [#{b}]'} for i in range(n)]})
    if 'List its items' in prompt:
        return '{"items": ["文言文", "背诵"]}'
    if 'check a list' in prompt:
        douts = [i for i in ids if i.startswith('d1-')]
        mats = [i for i in ids if not i.startswith('d1-')]
        return json.dumps({'items': [
            {'item': '赤壁赋主客问答', 'outline': douts[0], 'status': 'covered', 'note': f'讲过 [#{mats[0]}]'},
            {'item': '劝学背诵', 'outline': '#' + douts[-1], 'status': 'missing', 'note': f'材料里没有 [#{mats[0]}]'},
            {'item': '只在提纲里', 'outline': 'nope', 'status': 'weird', 'note': f'提纲提到了 [#{douts[0]}]'},
            {'item': '提了一下', 'outline': douts[0], 'status': 'partial', 'note': f'一笔带过 [#{mats[-1]}] [#{douts[0]}]'}]})
    return ''


def fake_stream(prompt, model=None, purpose='analysis'):
    prompts.append(('stream', prompt))
    d = re.findall(r'^\[#(d\d+-\d+)\] \| DOC', prompt, re.M)
    yield f'The outline says so [#{d[0]}].' if d else 'nothing'


ask._embed_texts = fake_embed
ask._llm = fake_llm
analyze._call_gemini_stream = fake_stream
A.config.gemini_key = lambda: 'test-key'
c = A.app.test_client()
ok = {}


def wait_idle(cid, t=120):
    end = time.time() + t
    while time.time() < end and cid in A._project_jobs:
        time.sleep(0.3)


def run(kind, **body):
    r = c.post(f'/api/chain/{PID}/studio', json={'kind': kind, **body})
    if r.status_code != 200:
        return r.status_code, r.get_json()
    oid = r.get_json()['item']['id']
    for _ in range(200):
        o = c.get(f'/api/chain/{PID}/studio/{oid}').get_json()
        if o['status'] != 'running':
            return 200, o
        time.sleep(0.1)
    return 0, o


PID = c.post('/api/projects', json={'name': '语文'}).get_json()['id']
d1 = c.post(f'/api/chain/{PID}/sources/docs', json={'title': '考试提纲', 'text':
            '# 三、文言文\n\n重点掌握《赤壁赋》的主客问答结构。\n\n# 四、背诵\n\n《劝学》《师说》全文背诵。'}).get_json()
d2 = c.post(f'/api/chain/{PID}/sources/docs', json={'title': '课堂笔记', 'text':
            '赤壁赋里苏子与客的问答，体现了变与不变的哲理。\n\n' + '\n\n'.join(f'第{i}段笔记：关于文言虚词的用法说明。' for i in range(30))}).get_json()
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID]})
wait_idle(PID)
src = c.get(f'/api/chain/{PID}/sources').get_json()
DOC1, DOC2 = src['docs'][0]['doc_id'], src['docs'][1]['doc_id']
print('来源：', [(d['title'], d['status']) for d in src['docs']], '| 录音', len(src['recordings']))

# 1 复习提纲：真 id、编造的删掉、代码块外壳去掉、记了费用字段
st, o = run('report', format='guide')
md = o.get('result', {}).get('md', '')
print('1 提纲：', st, o['status'], o.get('error'), '|', md.replace('\n', ' ')[:120], '| 删掉', o.get('dropped_citations'),
      '| 出处', list(o.get('citations', {}))[:3])
real = all(re.fullmatch(r'[dt]\d{4,}-\d+', k) for k in o.get('citations', {}))
ok['guide'] = o['status'] == 'done' and '```' not in md and '引文' not in md and o['dropped_citations'] == 1 and real \
    and len(o['citations']) >= 1 and 'cost_usd' in o and o['coverage']['sources'] == 3

# 2 闪卡：数量钳在 5–40
st, o = run('flashcards', n=999)
st2, o2 = run('flashcards', n=7)
print('2 闪卡：n=999 →', len(o['result']['cards']), '| n=7 →', len(o2['result']['cards']), '| 背面', o2['result']['cards'][0]['back'])
ok['flashcards'] = len(o['result']['cards']) == 40 and len(o2['result']['cards']) == 7 and '[#' in o2['result']['cards'][0]['back']

# 3 自测题：选项打乱后正确答案还指着 RIGHT，且不全在第一个
st, o = run('quiz', n=12)
qs = o['result']['questions']
right = all(q['options'][q['answer']] == 'RIGHT' for q in qs)
spread = len({q['answer'] for q in qs}) > 1
print('3 自测：', len(qs), '题 | 答案对得上', right, '| 答案位置分散', spread)
ok['quiz'] = len(qs) == 12 and right and spread

# 4 提纲对照：提纲出处换成真 id；状态不认识的归 partial；计数
st, o = run('coverage', outline=DOC1)
items = o['result']['items']
op = [p for k, p in prompts if 'check a list' in p][-1]
print('4 对照：', [(i['item'], i['status'], i['outline']) for i in items], '|', o['result']['counts'])
print('   说明：', [i['note'] for i in items])
ok['outline'] = (o['status'] == 'done' and 'The LIST is DOC1' in op and items[0]['outline']
                 and items[0]['outline'].startswith('d') and items[1]['outline'] and '[#' not in items[1]['note']
                 and items[2]['status'] == 'missing' and '[#' not in items[2]['note'] and items[2]['outline'] is None
                 and items[3]['status'] == 'partial' and items[3]['note'].count('[#') == 1 and '[#d' not in items[3]['note'].split('[#')[0]
                 and o['result']['counts'] == {'covered': 1, 'partial': 1, 'missing': 2})

# 5 参数校验
a = c.post(f'/api/chain/{PID}/studio', json={'kind': 'coverage'}).status_code
b = c.post(f'/api/chain/{PID}/studio', json={'kind': 'nope'}).status_code
d = c.get(f'/api/chain/{PID}/studio/../../x').status_code
print('5 校验：缺提纲', a, '| 未知工具', b, '| 坏 id', d)
ok['validate'] = a == 400 and b == 400 and d == 404

# 6 范围：只勾课堂笔记 → 提示词里只有它；提纲对照时提纲没勾也照样带上
st, o = run('report', format='guide', scope={'type': 'all', 'sources': [DOC2]})
gp = [p for k, p in prompts if 'Task: write a study guide' in p][-1]
srcs = set(re.findall(r'^\[#[dt]?\d+-\d+\] \| (\w+)', gp, re.M))
st, o2 = run('coverage', outline=DOC1, scope={'type': 'all', 'sources': [DOC2]})
op = [p for k, p in prompts if 'check a list' in p][-1]
osrcs = set(re.findall(r'^\[#[dt]?\d+-\d+\] \| (\w+)', op, re.M))
print('6 范围：提纲用到', srcs, '| 对照用到', osrcs, o2['status'])
ok['scope'] = srcs == {'DOC2'} and osrcs == {'DOC1', 'DOC2'} and o2['status'] == 'done'

# 7 放不下：没重点时每个来源都抽到；有重点时走检索
study.BUDGET_TOKENS = 600
st, o = run('report', format='guide')
gp = [p for k, p in prompts if 'Task: write a study guide' in p][-1]
srcs = set(re.findall(r'^\[#[dt]?\d+-\d+\] \| (\w+)', gp, re.M))
st, o2 = run('report', format='guide', focus='虚词')
gp2 = [p for k, p in prompts if 'Task: write a study guide' in p][-1]
print('7 抽样：', o['coverage'], srcs, '| 重点', o2['coverage'], 'Focus:' in gp2)
ok['thin'] = (o['coverage']['thinned'] and o['coverage']['passages_used'] < o['coverage']['passages_total']
              and srcs == {'DOC1', 'DOC2', 'EP1'} and o2['coverage']['thinned'] and 'Focus: the user wants' in gp2)
# 材料放不下时的提纲对照：先拆条目再检索
st, o3 = run('coverage', outline=DOC1)
print('   对照（放不下）：', o3['status'], o3['coverage'], any('List its items' in p for k, p in prompts))
ok['outline_big'] = o3['status'] == 'done' and o3['coverage']['thinned'] and any('List its items' in p for k, p in prompts)
study.BUDGET_TOKENS = 120_000

# 8 存笔记：从聊天记录取，不信前端
resp = c.post(f'/api/chain/{PID}/ask/stream', json={'question': '提纲说什么', 'ui_lang': 'zh'})
list(resp.response)
hist = ask.load_history(A._chain_dir(PID))
bot = hist[-1]
r = c.post(f'/api/chain/{PID}/studio/note', json={'at': bot['at'], 'head': bot['content'][:80]}).get_json()
n = c.get(f"/api/chain/{PID}/studio/{r['item']['id']}").get_json()
bad = c.post(f'/api/chain/{PID}/studio/note', json={'at': bot['at'], 'head': '别人的回答'}).status_code
bad2 = c.post(f'/api/chain/{PID}/studio/note', json={'at': '1999-01-01 00:00:00', 'head': ''}).status_code
print('8 笔记：', n['kind'], n['title'], n['result']['md'][:50], list(n['citations']), '| 内容对不上', bad, '| 时间对不上', bad2)
ok['note'] = n['kind'] == 'note' and n['title'] == '提纲说什么' and n['citations'] and bad == 404 and bad2 == 404

# 9 列表 / 中断 / 删除
cdir = A._chain_dir(PID)
json.dump({'id': 'abcdefabcdef', 'kind': 'quiz', 'status': 'running', 'created_at': '2030-01-01 00:00:00'},
          open(os.path.join(cdir, 'studio', 'abcdefabcdef.json'), 'w'))
lst = c.get(f'/api/chain/{PID}/studio').get_json()['items']
first = lst[0]
dl = c.delete(f"/api/chain/{PID}/studio/{lst[1]['id']}").get_json()
lst2 = c.get(f'/api/chain/{PID}/studio').get_json()['items']
print('9 列表：', len(lst), '条 | 最新在前', first['id'], first['status'], first.get('error'), '| 不带正文', 'result' not in first,
      '| 删一条', dl, len(lst2))
ok['list'] = first['status'] == 'failed' and first['error'] == 'interrupted' and 'result' not in lst[2] \
    and dl['ok'] and len(lst2) == len(lst) - 1

# 10 导出 PDF：墨页在跑 → PDF；墨页不在 → 503
alive = sources.moye_alive()
r = c.post('/api/export/pdf', json={'title': '复习提纲', 'markdown': '# 复习提纲\n\n- 赤壁赋：主客问答 [#d1-0]\n'})
real_moye = sources.MOYE_URL
sources.MOYE_URL = 'http://127.0.0.1:9'
r2 = c.post('/api/export/pdf', json={'title': 'x', 'markdown': 'y'})
sources.MOYE_URL = real_moye
empty = c.post('/api/export/pdf', json={'markdown': ' '}).status_code
print('10 PDF：墨页在跑', alive, '|', r.status_code, r.data[:5], len(r.data), '| 墨页不在', r2.status_code, r2.get_json(), '| 空', empty)
ok['pdf'] = (not alive or (r.status_code == 200 and r.data.startswith(b'%PDF'))) and r2.status_code == 503 and empty == 400

# 11 问答的背景：没画像不加；有画像只带讲观点的几节
corpus = ask.load(cdir, passages=True)
none_bg = ask._background(corpus)
open(os.path.join(cdir, '总分析.md'), 'w').write(
    '# X：人物解读\n\n## 他怎么看他的领域\n\n他认为体制是根源〔他的主张〕。\n\n## 叙事与修辞风格\n\n爱用反问。\n\n'
    '## 综合印象\n\n把移民视为最理性的出路。\n\n## 证伪留痕\n\n被删的论断。\n')
bg = ask._background(corpus)
ctx = ask._prepare(cdir, '提纲说什么')
print('11 背景：无画像', repr(none_bg), '| 有画像', bg.replace('\n', ' '), '| 进了提示词', 'Background:' in ctx['prompt'])
ok['background'] = (none_bg == '' and '体制是根源' in bg and '最理性的出路' in bg and '反问' not in bg
                    and '被删' not in bg and '〔' not in bg and 'Never cite it' in ctx['prompt'])

# 12 报告的几种格式
fm = {}
for f in ('briefing', 'faq', 'timeline'):
    st, o = run('report', format=f)
    fm[f] = (o['status'], o.get('format'))
rp = {k: [p for kk, p in prompts if k in p] for k in ('briefing document', 'Task: write an FAQ', 'Task: build a timeline')}
st, d0 = run('report')
nocustom = c.post(f'/api/chain/{PID}/studio', json={'kind': 'report', 'format': 'custom'})
st, cu = run('report', format='custom', prompt='把每篇课文的作者和朝代列成表 {x}')
cp = [p for k, p in prompts if 'Task, written by the user' in p][-1]
print('12 格式：', fm, '| 默认', d0.get('format'), '| 自定义没写要求', nocustom.status_code, nocustom.get_json(),
      '| 自定义', cu['status'], cu.get('prompt'))
ok['formats'] = (all(v == ('done', k) for k, v in fm.items()) and all(rp.values()) and d0.get('format') == 'briefing'
                 and nocustom.status_code == 400 and cu['status'] == 'done' and '作者和朝代列成表 {x}' in cp
                 and cu['prompt'].startswith('把每篇'))

# 13 网页来源：本机 / 内网 / 非 http 拦掉；网页转正文；指向 PDF 的存成原件；读不出字的报失败；重复的不加
blocked = [sources._public_url(u) for u in ('http://127.0.0.1:5001/', 'http://10.0.0.8/x', 'http://[::1]/',
                                             'file:///etc/passwd', 'ftp://a.b/c')]
import requests as _rq  # noqa: E402


class FakeResp:
    def __init__(self, url, ctype, body, status=200):
        self.url, self.status_code, self.headers = url, status, {'content-type': ctype}
        self.encoding = 'utf-8' if 'html' in ctype or 'text' in ctype else None
        self.raw = type('R', (), {'read': lambda _s, n, decode_content=True: body})()


PAGES = {
    'https://example.org/heteng': FakeResp('https://example.org/heteng', 'text/html; charset=utf-8', (
        '<html><head><title>荷塘月色赏析</title></head><body><nav>首页 | 课文</nav>'
        '<div class="share-bar">分享到微博</div><article><h1>荷塘月色赏析</h1>'
        + ''.join(f'<p>第{i}段：朱自清借荷塘月色抒发淡淡的哀愁与喜悦，情景交融，叠词增强了画面感。</p>' for i in range(8))
        + '<ul><li>要点一：情景交融</li><li>要点二：叠词</li></ul></article>'
        '<div class="comments">网友评论：写得好</div><footer>版权所有</footer></body></html>').encode()),
    'https://example.org/paper.pdf': FakeResp('https://example.org/paper.pdf', 'application/pdf', b'%PDF-1.4 fake'),
    'https://example.org/empty': FakeResp('https://example.org/empty', 'text/html', b'<html><body><div id="app"></div></body></html>'),
    'https://example.org/404': FakeResp('https://example.org/404', 'text/html', b'nope', 404),
}
real_get, real_pub = _rq.get, sources._public_url
_rq.get = lambda url, **k: PAGES[url]
sources._public_url = lambda u: u.startswith('https://example.org')
r = c.post(f'/api/chain/{PID}/sources/web', json={'urls': list(PAGES)}).get_json()
dup = c.post(f'/api/chain/{PID}/sources/web', json={'urls': ['https://example.org/heteng']})
wait_idle(PID)
time.sleep(1)
rows = {d['url']: d for d in c.get(f'/api/chain/{PID}/sources').get_json()['docs'] if d.get('url')}
page = rows['https://example.org/heteng']
md = sources.doc_markdown(page['doc_id'])
_rq.get, sources._public_url = real_get, real_pub
print('13 网页：拦截', blocked, '| 加了', r.get('added'), '| 重复', dup.status_code)
print('   正文页', page['status'], page['title'], page['converter'], '| 有导航/评论/页脚', any(x in md for x in ('首页', '分享到', '网友评论', '版权')),
      '| 列表', '- 要点一' in md, '| 段数', len(sources.doc_passages(page['doc_id'])))
pdfm = sources.doc_meta(rows['https://example.org/paper.pdf']['doc_id'])
print('   PDF', pdfm['ext'], pdfm['filename'], os.path.isfile(os.path.join(sources.doc_dir(pdfm['id']), 'original.pdf')),
      '| 空页', rows['https://example.org/empty']['status'], rows['https://example.org/empty']['error'][:40],
      '| 404', rows['https://example.org/404']['error'])
ok['web'] = (not any(blocked) and r.get('added') == 4 and dup.status_code == 400 and page['status'] == 'ready'
             and page['title'] == '荷塘月色赏析' and page['converter'] == 'web' and '朱自清借荷塘' in md
             and not any(x in md for x in ('首页', '分享到', '网友评论', '版权')) and '- 要点一' in md
             and pdfm['ext'] == 'pdf' and pdfm['filename'] == 'paper.pdf'
             and rows['https://example.org/empty']['status'] == 'failed' and '404' in rows['https://example.org/404']['error'])

# 14 找来源：网页结果只用搜索给的真实网址（跳转解析、去重、对上模型那一句），已在项目里的标出来
import discover  # noqa: E402
import config as _cfg  # noqa: E402
NS = type('NS', (), {})


def ns(**k):
    o = NS()
    o.__dict__.update(k)
    return o


fake_resp = ns(candidates=[ns(grounding_metadata=ns(
    web_search_queries=['q1', 'q2'],
    grounding_chunks=[ns(web=ns(uri='https://vertexaisearch.cloud.google.com/grounding-api-redirect/A', title='example.org')),
                      ns(web=ns(uri='https://vertexaisearch.cloud.google.com/grounding-api-redirect/B', title='wiki.org')),
                      ns(web=ns(uri='https://vertexaisearch.cloud.google.com/grounding-api-redirect/C', title='dup.org'))],
    grounding_supports=[ns(segment=ns(text='**荷塘月色赏析** — 讲情景交融'), grounding_chunk_indices=[0]),
                        ns(segment=ns(text='2. 朱自清 - 维基百科 — 生平'), grounding_chunk_indices=[1, 2])]))],
    usage_metadata=None)
_cfg.make_gemini_client = lambda *a, **k: ns(models=ns(generate_content=lambda **kw: fake_resp))
discover._resolve = lambda u: {'A': 'https://example.org/heteng', 'B': 'https://zh.wikipedia.org/wiki/朱自清',
                               'C': 'https://zh.wikipedia.org/wiki/朱自清'}[u[-1]]
web = c.post(f'/api/chain/{PID}/discover', json={'query': '荷塘月色', 'kind': 'web'}).get_json()
fake_yt = lambda q, n=12: [{'type': 'video', 'url': 'https://www.youtube.com/watch?v=yslXlV2BP_Y', 'title': 'YC',
                             'site': 'YouTube'}, {'type': 'video', 'url': 'https://www.youtube.com/watch?v=AAAAAAAAAAA',
                                                   'title': 'new', 'site': 'YouTube'}]
discover.search_youtube = fake_yt
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID]})
yt = c.post(f'/api/chain/{PID}/discover', json={'query': 'x', 'kind': 'youtube'}).get_json()
empty = c.post(f'/api/chain/{PID}/discover', json={'query': '  ', 'kind': 'web'}).status_code
from bilibili_api import search as _bs  # noqa: E402


async def fake_bili(q, search_type=None, page=1):
    return {'result': [{'bvid': 'BV1s44y1k74F', 'title': '像这样读《<em class="keyword">荷塘月色</em>》', 'author': '倪文尖',
                        'duration': '37:1', 'play': 5}, {'title': 'no bvid'}]}
_bs.search_by_type = fake_bili
bili = c.post(f'/api/chain/{PID}/discover', json={'query': '荷塘月色', 'kind': 'bilibili'}).get_json()
print('14 找来源：网页', [(i['title'], i['url'][-12:], i['snippet'], i['have']) for i in web['items']], '| 费用', web['cost_usd'])
print('   YouTube', [(i['title'], i['have']) for i in yt['items']], '| 空查询', empty, '| B站', bili['items'])
ok['discover'] = (len(web['items']) == 2 and web['items'][0]['title'] == '荷塘月色赏析' and web['items'][0]['have']
                  and web['items'][1]['title'] == '朱自清 - 维基百科' and web['items'][1]['snippet'] == '生平'
                  and not web['items'][1]['have'] and abs(web['cost_usd'] - 0.028) < 1e-6
                  and [i['have'] for i in yt['items']] == [True, False] and empty == 400
                  and bili['items'] == [{'type': 'video', 'url': 'https://www.bilibili.com/video/BV1s44y1k74F',
                                         'title': '像这样读《荷塘月色》', 'site': 'Bilibili', 'channel': '倪文尖',
                                         'duration': '37:01', 'views': 5, 'have': False}])

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
