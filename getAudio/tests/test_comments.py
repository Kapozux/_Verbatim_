"""B 站评论区（ADR 0019）：每期抓评论（一级翻完 + 每楼回复翻全）、登录态失效自己从浏览器续、工作台「评论区整理」。
10-13 用户：「这个评论能帮我抓吗」「对一些很有意思的保留一些原话，更多用概述，总数有限制」「我啥都不动，它自己就行了」。
跑生产代码：comments.fetch / sessdata / build_prompt / parse_digest，接口 /api/comments/<tid>(/fetch)、
/api/chain/<id>/comments/episodes、/studio/comments(/estimate)、/studio/<oid>。
桩（只在边界）：comments.use_api（B 站接口 + 读浏览器 cookie）、ask._llm（按提示词里真实的 c 编号写一段带出处的整理）。"""
import json
import os
import re
import time

from _support import Checks, isolate, make_transcript

isolate('comments')
import app as A  # noqa: E402
import ask  # noqa: E402
import comments  # noqa: E402

t = Checks()


def reply(rpid, text, like=0, root=0, rcount=0, sub=()):
    return {'rpid': rpid, 'root': root, 'parent': root, 'like': like, 'ctime': 1791600000 + rpid, 'rcount': rcount,
            'member': {'uname': f'u{rpid}'}, 'content': {'message': text}, 'replies': list(sub)}


class FakeBili:
    """一个视频：3 楼一级评论分两页；第 1 楼有 25 条回复（主列表只带 2 条，要翻楼中楼两页）。"""
    def __init__(self):
        self.good = {'GOOD'}
        self.browser_value = ''
        self.calls = []

    def nav(self, sess):
        self.calls.append(('nav', sess))
        return sess in self.good

    def browser(self):
        self.calls.append(('browser',))
        return self.browser_value

    def expand(self, url):
        return 'https://www.bilibili.com/video/BV1vRTT6PEw6?share=1'

    def view(self, bvid):
        return {'aid': 42, 'title': '测试视频', 'reply': 28}

    def main(self, aid, offset, sess):
        self.calls.append(('main', offset, sess))
        subs = [reply(100 + i, f'回复{i}', like=i, root=1) for i in range(2)]
        if not offset:
            return {'replies': [reply(1, '以为自己包了个私家车', like=99, rcount=25, sub=subs),
                                reply(2, '[doge][笑哭]', like=5)],
                    'cursor': {'is_end': False, 'pagination_reply': {'next_offset': 'p2'}}}
        return {'replies': [reply(3, '真理', like=50)], 'cursor': {'is_end': True}}

    def sub(self, aid, root, pn, sess):
        self.calls.append(('sub', root, pn))
        allr = [reply(100 + i, f'回复{i}', like=i, root=1) for i in range(25)]
        return {'replies': allr[(pn - 1) * 20: pn * 20]}


fake = FakeBili()
comments.use_api(fake)

# ---------- 登录态：.env 里的失效了，自己从浏览器取新的、存下来 ----------
os.environ['BILI_SESSDATA'] = 'OLD'
fake.browser_value = 'GOOD'
t.check('失效的登录态会自己从浏览器换新的', comments.sessdata(force=True) == 'GOOD')
saved = json.load(open(comments.LOGIN_FILE, encoding='utf-8'))
t.check('换到的存进 bili_login.local.json', saved.get('sessdata') == 'GOOD')
fake.calls.clear()
t.check('一小时内不重复检查', comments.sessdata() == 'GOOD' and not fake.calls)
fake.browser_value = ''
fake.good = set()
t.check('都不行就返回空，不假装能抓', comments.sessdata(force=True) == '')
try:
    comments.fetch('BV1s1pY6cEZo', sess='')
    t.check('没登录态时 fetch 直接报 bili_login', False)
except RuntimeError as e:
    t.check('没登录态时 fetch 直接报 bili_login', str(e) == 'bili_login')
fake.good = {'GOOD'}
comments.sessdata(force=True)

# ---------- 抓：一级翻完 + 回复翻全（每楼上限调到 50，走翻楼中楼那条路）----------
os.environ['COMMENTS_MAX_REPLIES'] = '50'
seen = []
d = comments.fetch('BV1s1pY6cEZo', progress=lambda have, rep, **k: seen.append((have, rep, k.get('stage'), k.get('i'), k.get('n'))))
ids = [r['id'] for r in d['items']]
t.check('一级评论翻到最后一页', {'1', '2', '3'} <= set(ids), ids)
t.check('第 1 楼 25 条回复全拿到（翻了两页楼中楼）', sum(1 for r in d['items'] if r['root'] == '1') == 25)
t.check('不重复', len(ids) == len(set(ids)) == 28, len(ids))
t.check('没回复的楼不去翻楼中楼', not any(c[0] == 'sub' and c[1] in ('2', '3') for c in fake.calls))
t.check('进度回报带视频报的总数', seen and seen[-1][:2] == (28, 28), seen[-3:])
t.check('进度：先报楼（第几楼 / 上限），再报楼中楼（第几楼 / 要翻的楼数）', seen[0][2:] == ('top', 2, 500) and seen[-1][2:] == ('replies', 1, 1), seen)
th = comments.threads(d)
t.check('楼按赞排，回复挂在楼下', th[0]['top']['id'] == '1' and len(th[0]['replies']) == 25 and th[1]['top']['id'] == '3')

# ---------- 整理的提示词和出处 ----------
prompt, refs = comments.build_prompt([('T1', d)], 'short', '只看反驳')
t.check('纯表情的评论不送给模型', '[doge][笑哭]' not in prompt)
t.check('篇幅和侧重写进提示词', '800 字' in prompt and '只看反驳' in prompt)
cid = next(k for k, (_, r) in refs.items() if r['id'] == '1')
md, cites = comments.parse_digest(f'主流观点 > 以为自己包了个私家车 [{cid}] 还有编造的 [c99999]', refs)
t.check('真编号换成 〔1〕，编造的删掉', '〔1〕' in md and 'c99999' not in md and len(cites) == 1, md)
two, _ = comments.parse_digest(f'> 以为自己 [{cid}]\n> 包了个私家车 [{cid}]', refs)
t.check('相邻两条引用分开成两段', two == '> 以为自己 〔1〕\n\n> 包了个私家车 〔1〕', two)
t.check('出处带用户、赞数、原话', cites[0]['user'] == 'u1' and cites[0]['like'] == 99 and cites[0]['tid'] == 'T1')

# ---------- 接口：笔记本里的一期 ----------
c = A.app.test_client()
RES = A.config.RESULTS_FOLDER
TID = make_transcript(RES, [('00:00:10', '开场')], title='夏鸣前为什么', url='https://www.bilibili.com/video/BV1s1pY6cEZo/?x=1')
YT = make_transcript(RES, [('00:00:10', 'hi')], title='YouTube 那期', url='https://www.youtube.com/watch?v=abc')
PID = c.post('/api/projects', json={'name': '评论'}).get_json()['id']
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID, YT]})

eps = c.get(f'/api/chain/{PID}/comments/episodes').get_json()['episodes']
t.check('弹框只列 B 站的期，标没抓过', [(e['task_id'], e['count']) for e in eps] == [(TID, None)], eps)
j = c.get(f'/api/comments/{TID}').get_json()
t.check('没抓过：has=False、是 B 站', j['bili'] and not j['has'])
t.check('不是 B 站的期不能抓', c.post(f'/api/comments/{YT}/fetch').status_code == 400)

r = c.post(f'/api/comments/{TID}/fetch')
t.check('⋯「抓评论区」开始后台抓', r.status_code == 200)
for _ in range(100):
    j = c.get(f'/api/comments/{TID}').get_json()
    if j['has'] and not j['job']:
        break
    time.sleep(0.05)
t.check('抓完在阅读器里能看：28 条、3 楼', j['has'] and j['info']['count'] == 28 and len(j['threads']) == 3, j.get('info'))

# ---------- 工作台「评论区整理」 ----------
prompts = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False, thinking=None):
    prompts.append((purpose, prompt))
    first = re.search(r'^\[(c\d+)\]', prompt, re.M).group(1)
    return f'## 主要讨论方向\n**私家车**：大家觉得是既要又要。\n> 以为自己包了个私家车 [{first}]'


ask._llm = fake_llm
est = c.get(f'/api/chain/{PID}/studio/comments/estimate?tids={TID}').get_json()
t.check('估价接口回数字', isinstance(est.get('usd'), (int, float)), est)
r = c.post(f'/api/chain/{PID}/studio/comments', json={'task_ids': [TID, YT], 'length': 'short'})
t.check('开始整理', r.status_code == 200, r.get_json())
oid = r.get_json()['item']['id']
for _ in range(100):
    o = c.get(f'/api/chain/{PID}/studio/{oid}').get_json()
    if o['status'] != 'running':
        break
    time.sleep(0.05)
t.check('整理完成', o['status'] == 'done', o.get('error'))
t.check('只整理 B 站那期', o['tids'] == [TID])
t.check('正文带出处标记，出处指回那条评论', '〔1〕' in o['result']['md'] and o['citations'][0]['id'] == '1')
t.check('工作台整理用手动那档（comments-digest）', prompts and prompts[-1][0] == 'comments-digest', prompts[-1][0] if prompts else '')
items = c.get(f'/api/chain/{PID}/studio').get_json()['items']
t.check('进工作台列表', any(i['id'] == oid and i['kind'] == 'comments' for i in items))

# 没抓过的期：整理时先抓
TID2 = make_transcript(RES, [('00:00:10', 'x')], title='另一期', url='https://www.bilibili.com/video/BV1Djbj6qEn6')
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID2]})
fake.good = set()
fake.browser_value = ''
comments.sessdata(force=True)
r = c.post(f'/api/chain/{PID}/studio/comments', json={'task_ids': [TID2]})
t.check('要先抓但登录不了：直说，不开工', r.status_code == 400 and r.get_json()['error'] == 'bili_login')
fake.good = {'GOOD'}
fake.browser_value = 'GOOD'
comments.sessdata(force=True)
oid2 = c.post(f'/api/chain/{PID}/studio/comments', json={'task_ids': [TID2]}).get_json()['item']['id']
for _ in range(100):
    o2 = c.get(f'/api/chain/{PID}/studio/{oid2}').get_json()
    if o2['status'] != 'running':
        break
    time.sleep(0.05)
t.check('没抓过的期整理时先抓再写', o2['status'] == 'done' and comments.load(TID2) is not None, o2.get('error'))

# ---------- 转写页：这一期自己的整理 ----------
TID3 = make_transcript(RES, [('00:00:10', 'y')], title='第三期', url='https://www.bilibili.com/video/BV1X2ZFBPEVq')
t.check('转写页：不是 B 站不能整理', c.post(f'/api/comments/{YT}/digest', json={}).status_code == 400)
est = c.get(f'/api/comments/{TID3}/estimate').get_json()
t.check('转写页：估价接口', isinstance(est.get('usd'), (int, float)), est)
r = c.post(f'/api/comments/{TID3}/digest', json={'length': 'long', 'focus': '只看反驳'})
t.check('转写页：没抓过也能直接整理（先抓）', r.status_code == 200 and r.get_json()['digest']['status'] == 'running', r.get_json())
for _ in range(100):
    j = c.get(f'/api/comments/{TID3}').get_json()
    if (j.get('digest') or {}).get('status') not in (None, 'running'):
        break
    time.sleep(0.05)
dg = j.get('digest') or {}
t.check('转写页：整理好跟这一期存在一起', dg.get('status') == 'done' and '〔1〕' in dg['result']['md'] and dg['citations'][0]['tid'] == TID3, dg.get('error'))
t.check('转写页：评论也一起抓好了', j['has'] and j['info']['count'] == 28)
t.check('转写页：篇幅和侧重进了提示词', '4000 字' in prompts[-1][1] and '只看反驳' in prompts[-1][1])
comments._digesting.discard(TID3)
o = comments.load_digest(TID3)
o['status'] = 'running'
comments._save_digest(TID3, o)
t.check('转写页：重启后残留的「在整理」当成中断', comments.load_digest(TID3)['status'] == 'failed')

# ---------- 转写页贴链接：不转写，只下载评论区 + 总结（按视频存，跟期共用一份）----------
r = c.post('/api/comments/resolve', json={'url': 'https://www.bilibili.com/video/BV1s1pY6cEZo/?trackid=x'})
t.check('贴链接：认出 BV 号，已经抓过的直接说有', r.get_json() == {'bvid': 'BV1s1pY6cEZo', 'has': True, 'title': '测试视频'}, r.get_json())
t.check('贴链接：b23.tv 短链跟跳转', c.post('/api/comments/resolve', json={'url': 'https://b23.tv/abc'}).get_json().get('bvid') == 'BV1vRTT6PEw6')
t.check('贴链接：不是 B 站的说不是', c.post('/api/comments/resolve', json={'url': 'https://youtu.be/x'}).status_code == 400)
j = c.get('/api/comments/BV1s1pY6cEZo').get_json()
t.check('同一个视频：按 BV 号看到的就是期里抓的那份', j['bili'] and j['has'] and j['info']['count'] == 28)
BV = 'BV1vRTT6PEw6'
t.check('没转写的视频也能抓', c.post(f'/api/comments/{BV}/fetch').status_code == 200)
for _ in range(100):
    j = c.get(f'/api/comments/{BV}').get_json()
    if j['has'] and not j['job']:
        break
    time.sleep(0.05)
t.check('按 BV 号抓好', j['has'] and j['info']['count'] == 28)
r = c.post(f'/api/comments/{BV}/digest', json={'length': 'short'})
for _ in range(100):
    dg = c.get(f'/api/comments/{BV}').get_json().get('digest') or {}
    if dg.get('status') not in (None, 'running'):
        break
    time.sleep(0.05)
t.check('按 BV 号总结', dg.get('status') == 'done' and dg['citations'][0]['tid'] == BV, dg.get('error'))
r = c.get(f'/api/comments/{BV}/csv')
body = r.get_data(as_text=True)
t.check('导出 CSV：带 BOM、表头、楼在回复前面', r.status_code == 200 and body.startswith('\ufeffrpid,root,user')
        and body.index('以为自己包了个私家车') < body.index('回复0'), body[:120])
t.check('乱写的 key 不碰文件系统', c.get('/api/comments/..%2F..%2Fetc').status_code == 404)

# ---------- 只下载评论区：抓完自动总结（10-13 用户：「不下载视频的话，默认做一个总结」）----------
AUTO = 'BV1UTEv6FEqY'
n0 = len(prompts)
r = c.post(f'/api/comments/{AUTO}/fetch', json={'auto_digest': True, 'length': 'short'})
t.check('自动总结：抓的接口收 auto_digest', r.status_code == 200, r.get_json())
for _ in range(200):
    dg = c.get(f'/api/comments/{AUTO}').get_json().get('digest') or {}
    if dg.get('status') == 'done':
        break
    time.sleep(0.05)
t.check('自动总结：抓完接着总结，标了 auto', dg.get('status') == 'done' and dg.get('auto') is True and dg.get('length') == 'short', dg)
t.check('自动总结：只调了一次模型', len(prompts) == n0 + 1, len(prompts) - n0)
r = c.post(f'/api/comments/{AUTO}/digest', json={'auto': True})
t.check('自动总结：已经有一份就不重做', r.get_json()['digest']['created_at'] == dg['created_at'] and len(prompts) == n0 + 1)
r = c.post(f'/api/comments/{AUTO}/fetch', json={})
for _ in range(100):
    if not (c.get(f'/api/comments/{AUTO}').get_json().get('job')):
        break
    time.sleep(0.05)
t.check('不带 auto_digest 的抓（笔记本 ⋯、重新抓）不总结', len(prompts) == n0 + 1)

t.check('自动总结用 comments-auto 那档', [p for p, _ in prompts][n0] == 'comments-auto', [p for p, _ in prompts][n0:])

# ---------- 原话核对：归一化后是子串才留，改过的换成原文 ----------
cites = [{'text': '以为自己包了个私家车[doge]，结果私家车晚上还偷偷背着自己出去跑滴滴赚外快'}]
ok_line = '> 以为自己包了个私家车，结果私家车晚上还偷偷背着自己出去跑滴滴 〔1〕'
t.check('原话：去掉表情和标点差异后是子串就留着', comments._check_quote(ok_line, cites) == ok_line)
bad = comments._check_quote('> 他就是想包个私家车而已 〔1〕', cites)
t.check('原话：改过的换成那条评论的原文', '以为自己包了个私家车[doge]' in bad and bad.endswith('〔1〕'), bad)
t.check('不是引用行不动', comments._check_quote('普通一句话 〔1〕', cites) == '普通一句话 〔1〕')

# ---------- 上限：按楼算，每楼回复按赞留 ----------
os.environ['COMMENTS_MAX_TOP'] = '2'
os.environ['COMMENTS_MAX_REPLIES'] = '5'
fake.calls.clear()
capd = comments.fetch('BV1s1pY6cEZo')
tops = [r for r in capd['items'] if r['root'] == '0']
reps = sorted(r['like'] for r in capd['items'] if r['root'] == '1')
t.check('上限：只要前 2 楼（翻到够了就不往下翻）', len(tops) == 2 and not any(c[0] == 'main' and c[1] == 'p2' for c in fake.calls), [r['id'] for r in tops])
t.check('上限：每楼按赞留 5 条', reps == [15, 16, 17, 18, 19], reps)
t.check('上限：楼中楼最多翻两倍上限（1 页）', sum(1 for c in fake.calls if c[0] == 'sub') == 1)
t.check('上限：标了截断和上限', capd['capped'] and capd['limits'] == {'top': 2, 'replies': 5})
del os.environ['COMMENTS_MAX_TOP'], os.environ['COMMENTS_MAX_REPLIES']
t.check('上限：默认 500 楼、2 条（10-13 用户：「就 500 楼，顶多 2 条回复」）', comments.limits() == (500, 2))
fake.calls.clear()
dflt = comments.fetch('BV1s1pY6cEZo')
t.check('默认：每楼只留主列表里带的最热 2 条，不翻楼中楼', not any(c[0] == 'sub' for c in fake.calls)
        and sorted(r['like'] for r in dflt['items'] if r['root'] == '1') == [0, 1], [c[0] for c in fake.calls])
t.check('贴链接先拿到标题', c.post('/api/comments/resolve', json={'url': 'https://www.bilibili.com/video/BV1vYDDB8E8B'}).get_json()['title'] == '测试视频')
os.environ['COMMENTS_MAX_REPLIES'] = '50'

# ---------- 记下是哪个模型写的；模型返回空要说清楚 ----------
t.check('记下写的模型（这里是假模型，没记账就空着）', 'model' in dg)
ask._llm = lambda prompt, model=None, purpose='ask', grounded=False, thinking=None: ''
r = c.post(f'/api/comments/{AUTO}/digest', json={'length': 'short'})
for _ in range(100):
    dg2 = c.get(f'/api/comments/{AUTO}').get_json().get('digest') or {}
    if dg2.get('status') not in (None, 'running'):
        break
    time.sleep(0.05)
t.check('模型返回空：标 failed / empty，界面能说「可能被拦」', dg2.get('status') == 'failed' and dg2.get('error') == 'empty', dg2)

t.finish()
