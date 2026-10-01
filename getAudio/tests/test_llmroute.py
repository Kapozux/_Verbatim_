"""省钱路由（llmroute.py）以及接进去的几个地方：analyze._call_gemini / _call_gemini_stream、
summarize._call_gemini、enrich._enrich_text、reflect._call_model；usage 的 DeepSeek 记价。

跑生产代码；桩只在网络边界：analyze._call_openai_compat（阿里云）、requests.post（流式）、
config.make_gemini_client（Gemini）。usage.db 指到临时目录。
"""
import json
import os
import sys

from _support import Checks, isolate

TMP = isolate('llmroute', cheap_route=True)
import usage  # noqa: E402
import config  # noqa: E402
import analyze  # noqa: E402
import llmroute  # noqa: E402
import summarize  # noqa: E402
import enrich  # noqa: E402
import reflect  # noqa: E402

calls = {'ds': [], 'gem': []}
NS = type('NS', (), {})


def ns(**k):
    o = NS()
    o.__dict__.update(k)
    return o


def fake_compat(prompt, model, base_url, api_key, extra_payload=None, label='', purpose=''):
    calls['ds'].append({'model': model, 'extra': extra_payload, 'purpose': purpose})
    if fake_compat.fail:
        raise RuntimeError(fake_compat.fail)
    return 'DS:' + purpose
fake_compat.fail = None


class FakeModels:
    def generate_content(self, model=None, contents=None, config=None):
        calls['gem'].append({'model': model, 'thinking': getattr(getattr(config, 'thinking_config', None), 'thinking_budget', 'unset')})
        return ns(text='GEM', usage_metadata=None, candidates=[])

    def generate_content_stream(self, model=None, contents=None):
        calls['gem'].append({'model': model, 'stream': True})
        yield ns(text='GEM-', usage_metadata=None)
        yield ns(text='STREAM', usage_metadata=None)


analyze._call_openai_compat = fake_compat
fake_client = lambda *a, **k: ns(models=FakeModels())   # noqa: E731
config.make_gemini_client = fake_client
analyze.make_gemini_client = fake_client
summarize.make_gemini_client = fake_client
enrich.make_gemini_client = fake_client
reflect.make_gemini_client = fake_client
config.gemini_key = lambda: 'g-key'
ok = {}


def reset():
    calls['ds'].clear()
    calls['gem'].clear()
    fake_compat.fail = None
    llmroute._state.update(paused_until=0.0, reason='')


SAFE = 'Founders should talk to users and ship fast.'
HOT = '今天睡前消息聊一下中共的人事。'

# 1 判断
cases = {SAFE: None, '颠覆式创新和代码审查': None, '美国政府和加州政府的新规': None, HOT: '睡前消息',
         '中国、北京、国内都在讨论': '中国×3', 'The CCP and Xi Jinping': 'CCP', '習近平': '習近平', '言论审查越来越严': '言论审查'}
got = {k: llmroute.china_related(k) for k in cases}
print('1 判断：', got)
ok['classify'] = got == cases

# 2 通用入口：无关内容 + 可换用途 → DeepSeek（关思考）；敏感 / 不在名单 / grounded → Gemini
reset()
a = analyze._call_gemini(SAFE, purpose='ask')
b = analyze._call_gemini(HOT, purpose='ask')
c = analyze._call_gemini(SAFE, purpose='cards')
d = analyze._call_gemini(SAFE, purpose='ask', grounded=True)
print('2 通用：', a, b, c, d, '| DeepSeek 调用', calls['ds'])
ok['generic'] = (a == 'DS:ask' and b == c == d == 'GEM' and len(calls['ds']) == 1
                 and calls['ds'][0]['model'] == 'deepseek-v4-flash' and calls['ds'][0]['extra'] == {'enable_thinking': False})

# 3 失败回退：审核拦截只回退这一次；欠费停 30 分钟、这期间不再去试
reset()
fake_compat.fail = '400: data_inspection_failed'
r1 = analyze._call_gemini(SAFE, purpose='ask')
paused1 = llmroute.status()['paused_until']
fake_compat.fail = '400: Arrearage - account overdue'
r2 = analyze._call_gemini(SAFE, purpose='ask')
n_before = len(calls['ds'])
fake_compat.fail = None
r3 = analyze._call_gemini(SAFE, purpose='ask')
st = llmroute.status()
print('3 回退：', r1, r2, r3, '| 审核后暂停', bool(paused1), '| 欠费后暂停', bool(st['paused_until']), st['pause_reason'][:30],
      '| 暂停期间又试了', len(calls['ds']) - n_before)
ok['fallback'] = r1 == r2 == r3 == 'GEM' and not paused1 and st['paused_until'] and len(calls['ds']) == n_before

# 4 开关和没 key
reset()
os.environ['CHEAP_TEXT_ROUTE'] = 'off'
x1 = analyze._call_gemini(SAFE, purpose='ask')
os.environ.pop('CHEAP_TEXT_ROUTE')
os.environ['DASHSCOPE_API_KEY'] = ''
x2 = analyze._call_gemini(SAFE, purpose='ask')
os.environ['DASHSCOPE_API_KEY'] = 'test-key'
print('4 关掉 / 没 key：', x1, x2, len(calls['ds']))
ok['switch'] = x1 == x2 == 'GEM' and not calls['ds']

# 5 流式：DeepSeek 的 SSE 逐段吐字、记一笔 deepseek 的账；连不上回到 Gemini 流式
import requests  # noqa: E402


class FakeStream:
    def __init__(self, status, lines):
        self.status_code, self._lines, self.text, self.closed = status, lines, 'err', False

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        self.closed = True


sse = ['data: ' + json.dumps({'choices': [{'delta': {'content': '你好'}}]}), '',
       'data: ' + json.dumps({'choices': [{'delta': {'content': '，世界'}}]}),
       'data: ' + json.dumps({'choices': [], 'usage': {'prompt_tokens': 1000, 'completion_tokens': 500}}), 'data: [DONE]']
reset()
recorded = []
real_record = usage.record
usage.record = lambda *a, **k: recorded.append((a, k))
requests.post = lambda *a, **k: FakeStream(200, sse)
s1 = ''.join(analyze._call_gemini_stream(SAFE, purpose='ask'))
requests.post = lambda *a, **k: FakeStream(400, [])
s2 = ''.join(analyze._call_gemini_stream(SAFE, purpose='ask'))
s3 = ''.join(analyze._call_gemini_stream(HOT, purpose='ask'))
usage.record = real_record
print('5 流式：', s1, '|', s2, '|', s3, '| 记账', [(a[:3], k.get('input_tokens'), k.get('output_tokens')) for a, k in recorded][:2])
ok['stream'] = (s1 == '你好，世界' and s2 == s3 == 'GEM-STREAM'
                and recorded[0][0][:3] == ('aliyun', 'deepseek-v4-flash', 'ask') and recorded[0][1]['input_tokens'] == 1000)

# 6 摘要 / 补全标签 / 回顾面板都接上了；走 Gemini 时不开思考
reset()
o1 = summarize._call_gemini('总结：' + SAFE)
o2 = summarize._call_gemini(HOT)
o3 = enrich._enrich_text(SAFE)
o4 = enrich._enrich_text(HOT)
o5 = reflect._call_model(SAFE)
o6 = reflect._call_model(HOT)
think = [g.get('thinking') for g in calls['gem']]
print('6 接入：', o1, o2, o3, o4, o5, o6, '| Gemini 思考预算', think, '| DeepSeek 用途', [c['purpose'] for c in calls['ds']])
ok['wired'] = (o1 == 'DS:summary' and o2 == 'GEM' and o3 == 'DS:enrich' and o4 == 'GEM' and o5 == 'DS:reflect'
               and o6 == 'GEM' and think[:2] == [0, 0])

# 7 记价：DeepSeek 有价，按表算（输入 1000、输出 500 token）
cost = usage.estimate_cost('deepseek-v4-flash', 1000, 500)
print('7 记价：', cost)
ok['price'] = cost is not None and abs(cost - (1000 * 0.14 + 500 * 0.28) / 1e6) < 1e-9

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
