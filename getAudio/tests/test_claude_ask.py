"""「问证据卡」可以交给本机 Claude Code 写（ASK_ENGINE=claude，2026-10-13）：默认不开，行为跟以前一样。
开了：边写边出字、出处照样校验、记账（订阅额度，记 $0、token 照记）、Claude 报错且还没出字就退回原来的模型、
中途停止把 Claude 进程关掉、电脑上没有 claude 命令就当没开。
桩（只在模型边界）：claude_cli 的 runner（假的 claude -p 事件流）、analyze._call_gemini_stream、ask._llm。"""
import json
import os
import re

from _support import Checks, isolate, make_creator

isolate('claudeask')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import claude_cli  # noqa: E402
import usage  # noqa: E402

CID = make_creator(A, 'Y Combinator', 'ycombinator', [
    ['Solo founders are rising fast.', 'Experienced founders are back.', 'Talk to users every week.'],
    ['The harness matters more than the model.', 'Agents need good tools.', 'Ship small and often.']])
URL = f'/api/chain/{CID}/ask/stream'
client = A.app.test_client()
seen = {'gemini': 0, 'claude': [], 'closed': 0}


def card_ids(prompt):
    return re.findall(r'^\[#([^\]]+)\] \| EP', prompt, re.M)[:2]


def fake_gemini(prompt, model=None, purpose='analysis'):
    seen['gemini'] += 1
    yield f'Gemini says solo founders rise [#{card_ids(prompt)[0]}].'


def deltas(*parts):
    return [{'type': 'stream_event', 'event': {'type': 'message_start'}}] + [
        {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': p}}}
        for p in parts]


def claude_ok(args, stdin):
    seen['claude'].append(args)
    a, b = card_ids(stdin)
    parts = ['Claude: solo founders ', f'are rising [#{a}]. ', f'Harness matters [#{b}][#999-999].']
    return {'events': deltas(*parts), 'result': ''.join(parts), 'total_cost_usd': 0.12,
            'usage': {'input_tokens': 900, 'output_tokens': 60, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}


def claude_down(args, stdin):
    seen['claude'].append(args)
    return {'is_error': True, 'result': 'Claude AI usage limit reached|9999999999', 'events': []}


analyze._call_gemini_stream = fake_gemini
ask._llm = lambda prompt, **k: f'Plain answer [#{card_ids(prompt)[0]}].' if '[#' in prompt else prompt


def ask_stream(q):
    ask.clear_history(A._chain_dir(CID))
    out = []
    for chunk in client.post(URL, json={'question': q, 'ui_lang': 'en'}).response:
        out += [json.loads(ln) for ln in chunk.decode().splitlines() if ln.strip()]
    return out


t = Checks()

# 1. 默认（没设 ASK_ENGINE）：跟以前一样走原来的模型，Claude 碰都不碰
os.environ.pop('ASK_ENGINE', None)
claude_cli.use_runner(claude_ok)
ev = ask_stream('Solo founders?')
t.check('默认不开：走原来的模型', seen['gemini'] == 1 and not seen['claude'] and ev[-1]['type'] == 'done')

# 2. 开了：Claude 边写边出字，出处照样校验（编造的 999-999 删掉），模型名、记账对
os.environ['ASK_ENGINE'] = 'claude'
ev = ask_stream('Solo founders?')
msg = ev[-1]['message']
types = [e['type'] for e in ev]
t.check('开了：Claude 流式写', types.count('delta') == 3 and len(seen['claude']) == 1 and seen['gemini'] == 1,
        str(types))
t.check('开了：出处照样校验', len(msg['citations']) == 2 and msg['dropped_citations'] == 1 and '[#999-999]' not in msg['content'])
res = [e for e in ask.answer_stream(A._chain_dir(CID), 'Solo founders?', ui_lang='en') if e['type'] == 'result'][0]
t.check('开了：结果里的模型是 Claude', res.get('model') == claude_cli.MODEL, res.get('model'))
args = seen['claude'][0]
t.check('只许写字：不给工具、不读设置、一次性不写缓存',
        args[args.index('--tools') + 1] == '' and '--setting-sources' in args and '--no-session-persistence' in args)
calls = usage._conn().execute("SELECT model, input_tokens, cost_usd FROM calls WHERE provider='claude-code'").fetchall()
t.check('记账：token 照记、钱记 0（订阅额度）', calls and calls[-1][1] == 900 and calls[-1][2] == 0, str(calls))

# 3. Claude 报错、还没出字：退回原来的模型，Claude 停到重置时间
claude_cli.use_runner(claude_down)
ev = ask_stream('Solo founders?')
t.check('Claude 撞上限：退回原来的模型', ev[-1]['type'] == 'done' and seen['gemini'] == 2
        and 'Gemini says' in ev[-1]['message']['content'])
t.check('撞上限后停着，不再去碰 Claude', not claude_cli.available())
claude_cli.use_runner(claude_ok)                     # use_runner 顺手清掉「停着」

# 4. 非流式老接口也走 Claude
ask.clear_history(A._chain_dir(CID))
old = client.post(f'/api/chain/{CID}/ask', json={'question': 'old path?', 'ephemeral': True}).get_json()
t.check('非流式接口：Claude 写', old.get('ok') and old['message']['content'].startswith('Claude:'), str(old)[:200])

# 5. 中途停止：Claude 的生成器被关掉
def claude_slow(args, stdin):
    a, _ = card_ids(stdin)
    return {'events': deltas(*[f'part {i} [#{a}]. ' for i in range(50)]), 'result': 'x'}


claude_cli.use_runner(claude_slow)
gen = ask.answer_stream(A._chain_dir(CID), 'Stop me', ui_lang='en')
got = [next(gen) for _ in range(5)]
gen.close()
t.check('中途停止：生成器关得掉', any(e.get('type') == 'delta' for e in got))

# 6. 电脑上没有 claude 命令：当没开
claude_cli.use_runner(None)
claude_cli.BIN = '/nonexistent/claude'
ev = ask_stream('Solo founders?')
t.check('没装 Claude Code：自动走原来的模型', ev[-1]['type'] == 'done' and seen['gemini'] == 3)

t.finish()
