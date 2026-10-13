"""B 站评论区（ADR 0018）：每期抓评论（一级翻完 + 每楼回复翻全）、登录态失效自己从浏览器续、工作台「评论区整理」。
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

# ---------- 抓：一级翻完 + 回复翻全 ----------
seen = []
d = comments.fetch('BV1s1pY6cEZo', progress=lambda h, n: seen.append((h, n)))
ids = [r['id'] for r in d['items']]
t.check('一级评论翻到最后一页', {'1', '2', '3'} <= set(ids), ids)
t.check('第 1 楼 25 条回复全拿到（翻了两页楼中楼）', sum(1 for r in d['items'] if r['root'] == '1') == 25)
t.check('不重复', len(ids) == len(set(ids)) == 28, len(ids))
t.check('没回复的楼不去翻楼中楼', not any(c[0] == 'sub' and c[1] in ('2', '3') for c in fake.calls))
t.check('进度回报带视频报的总数', seen and seen[-1] == (28, 28), seen[-3:])
th = comments.threads(d)
t.check('楼按赞排，回复挂在楼下', th[0]['top']['id'] == '1' and len(th[0]['replies']) == 25 and th[1]['top']['id'] == '3')

# ---------- 整理的提示词和出处 ----------
prompt, refs = comments.build_prompt([('T1', d)], 'short', '只看反驳')
t.check('纯表情的评论不送给模型', '[doge][笑哭]' not in prompt)
t.check('篇幅和侧重写进提示词', '800 字' in prompt and '只看反驳' in prompt)
cid = next(k for k, (_, r) in refs.items() if r['id'] == '1')
md, cites = comments.parse_digest(f'主流观点 > 以为自己包了个私家车 [{cid}] 还有编造的 [c99999]', refs)
t.check('真编号换成 〔1〕，编造的删掉', '〔1〕' in md and 'c99999' not in md and len(cites) == 1, md)
two, _ = comments.parse_digest(f'> 甲 [{cid}]\n> 乙 [{cid}]', refs)
t.check('相邻两条引用分开成两段', two == '> 甲 〔1〕\n\n> 乙 〔1〕', two)
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
t.check('用的是整理那档（Claude 时走 Opus）', prompts and prompts[-1][0] == 'digest')
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

t.finish()
