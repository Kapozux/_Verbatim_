"""日常文字活交给本机 Claude Code（TEXT_ENGINE=claude，2026-10-13 用户：「开销越少越好，订阅额度必须都用上」）：
默认不开；开了以后 llmroute 的用途先走 Claude——摘要 / 标签 / 翻译这类提取活用 Haiku，回顾 / 对比 / 立场这类要判断的用 Opus；
记账 claude-code / $0；Claude 用不了或报错，照旧走 DeepSeek → Gemini。
桩：claude_cli 的 runner（假的 claude -p 事件流）、analyze._call_openai_compat（假 DeepSeek）。"""
import os

from _support import Checks, isolate

isolate('claudetext', cheap_route=True)
import analyze  # noqa: E402
import claude_cli  # noqa: E402
import llmroute  # noqa: E402
import usage  # noqa: E402

seen = {'claude': [], 'deepseek': 0}


def deltas(*parts):
    return [{'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': p}}}
            for p in parts]


def claude_ok(args, stdin):
    seen['claude'].append(args[args.index('--model') + 1])
    return {'events': deltas('Claude ', '写的摘要'), 'result': 'Claude 写的摘要',
            'usage': {'input_tokens': 800, 'output_tokens': 40}}


def claude_down(args, stdin):
    seen['claude'].append('down')
    return {'is_error': True, 'result': 'Not logged in · Please run /login', 'events': []}


def fake_deepseek(prompt, model, base, key, **kw):
    seen['deepseek'] += 1
    return 'DeepSeek 写的'


analyze._call_openai_compat = fake_deepseek
t = Checks()

# 1. 默认不开：不碰 Claude，照旧 DeepSeek
claude_cli.use_runner(claude_ok)
t.check('默认不开：照旧 DeepSeek', llmroute.text('这期讲了什么', 'summary') == 'DeepSeek 写的' and not seen['claude'])

# 2. 开了：摘要走 Haiku、回顾走 Opus，记账 claude-code / $0
os.environ['TEXT_ENGINE'] = 'claude'
out1 = llmroute.text('这期讲了什么', 'summary')
out2 = llmroute.text('这个月在看什么', 'reflect')
t.check('开了：摘要用 Haiku', out1 == 'Claude 写的摘要' and seen['claude'][0] == 'claude-haiku-5-5', str(seen))
t.check('开了：回顾用 Opus', out2 == 'Claude 写的摘要' and seen['claude'][1] == claude_cli.MODEL, str(seen))
rows = usage._conn().execute("SELECT model, purpose, input_tokens, cost_usd FROM calls WHERE provider='claude-code'").fetchall()
t.check('记账：claude-code、$0、token 照记', len(rows) == 2 and all(r[3] == 0 for r in rows) and rows[0][2] == 800, str(rows))
t.check('不在 llmroute 用途里的活（抽卡 / 画像）不碰 Claude', llmroute.text('抽卡', 'analysis') is None and len(seen['claude']) == 2)

# 3. 流式也走 Claude
gen = llmroute.stream('边写边看', 'study')
t.check('流式：Claude 边写边出字', gen is not None and list(gen) == ['Claude ', '写的摘要'] and seen['claude'][-1] == 'claude-haiku-5-5')

# 4. Claude 报错（没登录）：退回 DeepSeek，Claude 停着、下一次不再去碰
claude_cli.use_runner(claude_down)
n = seen['deepseek']
t.check('Claude 失败：退回 DeepSeek', llmroute.text('这期讲了什么', 'summary') == 'DeepSeek 写的' and seen['deepseek'] == n + 1)
before = len(seen['claude'])
llmroute.text('再来一期', 'summary')
t.check('停着的时候不再去碰 Claude', len(seen['claude']) == before)

t.finish()
