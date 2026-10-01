"""流式提问 /api/chain/<id>/ask/stream：正常完成、中途出错、中途停止、挑卡时就停、参数校验、老的非流式接口。
跑生产代码：app 路由、ask._prepare/_finish/answer_stream/finish_partial、出处清理、聊天记录、记账。
桩（只在模型边界）：analyze._call_gemini_stream（假流，可控节奏 / 中途报错）、ask._llm（改写语言那一步）。
数据：合成的博主（3 期 × 3 张卡）。"""
import json
import re
import time

from _support import Checks, isolate, make_creator

isolate('askstream')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import usage  # noqa: E402

CID = make_creator(A, 'Y Combinator', 'ycombinator', [
    ['Solo founders are rising fast.', 'Experienced founders are back.', 'Talk to users every week.'],
    ['The harness matters more than the model.', 'Agents need good tools.', 'Ship small and often.'],
    ['Hardware is getting easier.', 'Robots will use general models.', 'Cold emails need a clear ask.']])
ask.clear_history(A._chain_dir(CID))
CARD_ID = re.compile(r'\[#((?:[A-Z]:)?\d+(?:_[0-9a-f]{8})?-\d+)\]')
plan = {'mode': 'ok', 'delay': 0.0}
seen = {'prompt': None, 'closed': False, 'yielded': 0}


def fake_stream(prompt, model=None, purpose='analysis'):
    seen['prompt'] = prompt
    ids = re.findall(r'^\[#([^\]]+)\] \| EP', prompt, re.M)[:3]   # 只认卡片行，别抓到说明里的示例
    pieces = ['Y Combinator argues ', f'that solo founders are rising [#{ids[0]}]. ',
              f'They also note harness matters [#{ids[1]}][#999-999]. ', 'More detail ', 'follows here. ',
              *[f'Sentence {i}. ' for i in range(30)]]
    try:
        for i, p in enumerate(pieces):
            if plan['mode'] == 'error' and i == 3:
                raise RuntimeError('simulated upstream 503 mid-stream')
            time.sleep(plan['delay'])
            seen['yielded'] += 1
            yield p
        usage.record('gemini', model, purpose, input_tokens=1000, output_tokens=200)
    except GeneratorExit:
        seen['closed'] = True
        usage.record('gemini', model, purpose, input_tokens=1000, output_tokens=20)
        raise


analyze._call_gemini_stream = fake_stream
ask._llm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('no rewrite expected'))
client = A.app.test_client()
URL = f'/api/chain/{CID}/ask/stream'
results = {}


def events(resp):
    out = []
    for chunk in resp.response:
        for ln in chunk.decode().splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    return out


# 1. 正常完成
plan.update(mode='ok', delay=0)
resp = client.post(URL, json={'question': 'What do they think about solo founders?', 'ui_lang': 'en'})
ev = events(resp)
types = [e['type'] for e in ev]
done = ev[-1]
hist = ask.load_history(A._chain_dir(CID))
print('1. 正常：', resp.status_code, resp.mimetype, '事件顺序', types[:3], '…', types[-1],
      f'delta×{types.count("delta")}')
print('   最终回答引用：', list(done['message']['citations']), '删掉编造的出处', done['message']['dropped_citations'])
print('   ctx 没泄露给前端：', 'ctx' not in types, '；记录条数：', len(hist), '；cost_usd：', done['message']['cost_usd'])
results['1'] = (types[0] == 'stage' and ev[0]['stage'] == 'search' and 'delta' in types and types[-1] == 'done'
                and 'ctx' not in types and len(done['message']['citations']) == 2
                and done['message']['dropped_citations'] == 1 and '[#999-999]' not in done['message']['content']
                and len(hist) == 2 and hist[1].get('stopped') is None and done['message']['cost_usd'] > 0)

# 2. 中途出错
ask.clear_history(A._chain_dir(CID))
plan.update(mode='error')
ev = events(client.post(URL, json={'question': 'Anything about hardware?', 'ui_lang': 'en'}))
print('2. 中途出错：最后一个事件', ev[-1], '；记录条数', len(ask.load_history(A._chain_dir(CID))))
results['2'] = ev[-1]['type'] == 'error' and '503' in ev[-1]['error'] and not ask.load_history(A._chain_dir(CID))

# 3. 中途停止（浏览器断开 = 关掉响应迭代器）
ask.clear_history(A._chain_dir(CID))
plan.update(mode='ok', delay=0.05)
seen.update(closed=False, yielded=0)
resp = client.post(URL, json={'question': 'Tell me everything about solo founders', 'ui_lang': 'en'},
                   buffered=False)
it = iter(resp.response)
got = []
while len([g for g in got if '"delta"' in g]) < 4:
    got.append(next(it).decode())
resp.close()                       # 等于浏览器点了停止
time.sleep(0.2)
hist = ask.load_history(A._chain_dir(CID))
bot = hist[1] if len(hist) == 2 else {}
print(f'3. 中途停止：模型那头被关掉={seen["closed"]}，只吐了 {seen["yielded"]} 段（共 35 段）')
print('   存下的半截：stopped=', bot.get('stopped'), '内容=', repr(bot.get('content', '')[:90]))
print('   半截里的出处已校验：', list(bot.get('citations', {})), '；记账 cost_usd=', bot.get('cost_usd'))
results['3'] = (seen['closed'] and seen['yielded'] < 35 and bot.get('stopped') is True
                and bot.get('content') and '[#999-999]' not in bot['content']
                and len(bot.get('citations', {})) >= 1 and (bot.get('cost_usd') or 0) > 0)

# 4. 还在挑卡就停了：一问一答仍成对，回答为空且标了已停止
ask.clear_history(A._chain_dir(CID))
real_prepare = ask._prepare
ask._prepare = lambda *a, **k: (time.sleep(0.3), real_prepare(*a, **k))[1]
resp = client.post(URL, json={'question': 'Stop me early', 'ui_lang': 'en'}, buffered=False)
it = iter(resp.response)
first = next(it).decode()
resp.close()
ask._prepare = real_prepare
hist = ask.load_history(A._chain_dir(CID))
print('4. 挑卡时就停：第一个事件', first.strip(), '；记录', [(m['role'], m.get('stopped'), m['content'][:20]) for m in hist])
results['4'] = len(hist) == 2 and hist[1].get('stopped') is True and hist[1]['content'] == ''

# 5. 参数校验 + 老接口没受影响
r1 = client.post(URL, json={'question': ''})
r2 = client.post(URL, json={'question': 'x' * 2001})
r3 = client.post('/api/chain/' + 'f' * 32 + '/ask/stream', json={'question': 'hi'})
print('5. 空问题', r1.status_code, '；超长', r2.status_code, '；不存在的链', r3.status_code)
ask._llm = lambda prompt, **k: 'Old endpoint still answers [#' + re.findall(r'^\[#([^\]]+)\] \| EP', prompt, re.M)[0] + '].'
old = client.post(f'/api/chain/{CID}/ask', json={'question': 'old path?', 'ephemeral': True}).get_json()
print('   老的非流式接口：ok=', old.get('ok'), '引用数', len(old['message']['citations']))
results['5'] = r1.status_code == 400 and r2.status_code == 400 and r3.status_code == 404 \
    and old.get('ok') and len(old['message']['citations']) == 1

t = Checks()
for k, v in results.items():
    t.check('场景 ' + k, v)
t.finish()
