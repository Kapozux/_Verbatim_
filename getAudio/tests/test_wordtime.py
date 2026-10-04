"""逐词时间轴（wordtime.py，ADR 0012）：出处跳到原话第一个词真正出现的那一刻，不估算。
用例是 YC「The State of Startups in 2026」1:00–1:35 的 YouTube 自动字幕（10-04 从播放器上读下来的，
听错的词原样保留：heart tech / the bat）。Gemini 转写这一段段首 1:05，原话「the number of hard tech
companies…8% to 20%」实际在 1:19.4。"""
import json
import os
import uuid

from _support import Checks, isolate, make_transcript, write_json

TMP = isolate('wordtime')
import ask  # noqa: E402
import wordtime  # noqa: E402

t = Checks()
R = os.path.join(TMP, 'results')

# YouTube json3 的样子：每个事件一行字幕，segs 里每个词带相对偏移（毫秒）
LINES = [
    (60.0, "Let's get started. Diana, you have a few things to share with us. So we did a bit"),
    (66.95, "of an analysis for all the companies we accepted in the last 18 12 months and we"),
    (73.81, "have some pretty shocking stats to share with everyone. So one of the big ones is"),
    (79.40, "the number of heart tech companies that are in the bat. It has gone from 8% to"),
    (85.24, "20%. There's a lot of uh underlying reasons why that has happened. We we"),
    (90.57, "will go deeper into that. The other one is uh the rate of growth of companies"),
    (96.10, "and what YC does to the companies has accelerated. So the median YC company"),   # 这一行没实测，照转写补的
]


def json3(lines):
    ev = []
    for k, (st, text) in enumerate(lines):
        nxt = lines[k + 1][0] if k + 1 < len(lines) else st + 5
        ws = text.split()
        step = (nxt - st) / len(ws)
        ev.append({'tStartMs': int(st * 1000), 'segs': [{'utf8': (' ' if i else '') + w, 'tOffsetMs': int(i * step * 1000)}
                                                         for i, w in enumerate(ws)]})
        ev.append({'tStartMs': int(nxt * 1000) - 10, 'aAppend': 1, 'segs': [{'utf8': '\n'}]})   # 换行事件，没有词
    return {'events': ev}


words = wordtime.parse_json3(json3(LINES))
t.check('json3 → [(秒, 词)]：每个词都有时间、换行事件不算词', len(words) == sum(len(x[1].split()) for x in LINES)
        and words[0] == (60.0, "Let's") and abs(dict((w, s) for s, w in words)['number'] - 79.8) < 0.5, str(words[:3]))

tid = make_transcript(R, [('00:01:05', 'So, we did a bit of an analysis …')], tid=str(uuid.uuid4()))
wordtime.save(tid, words, source='youtube', lang='en')
QUOTE = 'the number of hard tech companies that are in the batch. It has gone from 8% to 20%.'

s = wordtime.locate(tid, QUOTE, 65)
t.check('原话在段中间：跳到第一个词出现的那一刻（实测 1:19.4，误差 1 秒内）', s is not None and abs(s - 79.4) <= 1, str(s))
s = wordtime.locate(tid, 'So, we did a bit of an analysis for all the companies', 65)
t.check('原话就在段首：也是那个词的时间（So 在 1:05 左右）', s is not None and 63.5 <= s <= 67, str(s))
s = wordtime.locate(tid, 'the rate of growth of companies and what YC does to the companies has accelerated', 65)
t.check('卡片时间点标在上一段：照样找到（1:30 之后）', s is not None and 90 <= s <= 96, str(s))
t.check('原话用「…」拼的：按第一截定位', abs((wordtime.locate(tid, 'hard tech companies … reasons why', 65) or 0) - 80.6) <= 1.2)
t.check('原话字幕里没有：返回 None（不猜）', wordtime.locate(tid, 'we are raising a series B next quarter from sequoia', 65) is None)
t.check('没有逐词时间轴的期：返回 None', wordtime.locate(make_transcript(R, [('00:00', 'x')], tid=str(uuid.uuid4())), QUOTE, 65) is None)

# 中文：按字比
zh = make_transcript(R, [('00:00:10', '……')], tid=str(uuid.uuid4()))
wordtime.save(zh, [(10 + i * 0.25, ch) for i, ch in enumerate('大家好今天我们聊一聊房价我认为未来十年房价会跌一半这是我的判断')], source='qwen', lang='zh')
s = wordtime.locate(zh, '未来十年房价会跌一半', 10)
t.check('中文按字对齐', s is not None and abs(s - (10 + 15 * 0.25)) <= 0.3, str(s))

# card_view：有时间轴就用准的；没有就原样（不估算）
corpus = {'episodes': [{'title': 'State of Startups', 'date': '', 'task_id': tid, 'label': 'EP1',
                        'video_url': 'https://www.youtube.com/watch?v=yslXlV2BP_Y'}]}
card = {'id': '1-3', 'ep': 0, 'quote': QUOTE, 'obs': '', 'layer': 'claim', 'ts': '01:05', 'sec': 65}
v = ask.card_view(card, corpus)
t.check('card_view：sec / ts / 链接是原话那一秒，标 exact', v['sec'] == 79 and v['ts'] == '01:19' and v['video_url'].endswith('t=79')
        and v.get('exact') is True, json.dumps({k: v.get(k) for k in ('sec', 'ts', 'video_url', 'exact')}))
t.check('card_view 不改卡片本身', card['sec'] == 65 and card['ts'] == '01:05')
plain = {'episodes': [{**corpus['episodes'][0], 'task_id': zh}]}
v2 = ask.card_view(card, plain)
t.check('找不到原话：时间不动、不标 exact', v2['sec'] == 65 and v2['ts'] == '01:05' and not v2.get('exact'))

# 旧回答：聊天记录里存的是段首，读的时候再对一遍
old = [{'role': 'assistant', 'citations': {'1-3': {'task_id': tid, 'quote': QUOTE, 'sec': 65, 'ts': '01:05', 'kind': 'card',
                                                   'video_url': 'https://www.youtube.com/watch?v=yslXlV2BP_Y&t=65'}}}]
c = ask.retime_citations(old)[0]['citations']['1-3']
t.check('旧回答的出处读的时候换成原话那一秒（链接里的 t= 也换）', c['sec'] == 79 and c['ts'] == '01:19' and c['exact']
        and c['video_url'] == 'https://www.youtube.com/watch?v=yslXlV2BP_Y&t=79', str(c))

# 新转写顺手补：YouTube 的期排进后台队列（测试里换掉真正去拉字幕的那一步）
import time  # noqa: E402
calls = []
wordtime.fetch_youtube = lambda tid_, url: (calls.append((tid_, url)) or (100, None))
fresh = make_transcript(R, [('00:00', 'x')], tid=str(uuid.uuid4()))
t.check('WORDTIME_AUTO=0（测试默认）：不排队', wordtime.queue(fresh, 'https://youtu.be/yslXlV2BP_Y') is False)
os.environ['WORDTIME_AUTO'] = '1'
t.check('别的网站不排队', wordtime.queue(fresh, 'https://www.bilibili.com/video/BV1xx411c7mD') is False)
t.check('已经有时间轴的不排队', wordtime.queue(tid, 'https://youtu.be/yslXlV2BP_Y') is False)
queued = wordtime.queue(fresh, 'https://www.youtube.com/watch?v=yslXlV2BP_Y')
for _ in range(40):
    if calls:
        break
    time.sleep(0.05)
t.check('YouTube 的期：后台线程去拉', queued and calls == [(fresh, 'https://www.youtube.com/watch?v=yslXlV2BP_Y')], str(calls))
os.environ['WORDTIME_AUTO'] = '0'

t.finish()
