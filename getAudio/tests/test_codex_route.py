"""GPT（Codex CLI，2026-11-03）：Claude 用不了时先让 GPT 顶上，排在 DeepSeek 前面；批量的用途不走它；
走共用的 claude_gate（provider='codex'），撞上限只停 GPT；要 CODEX_ENGINE=on 才用。"""
import os
import time

from _support import Checks, isolate

TMP = isolate('codexroute', cheap_route=True)
os.environ['CLAUDE_GATE'] = os.path.join(TMP, 'gate', 'gate.db')
os.environ['TEXT_ENGINE'] = 'claude'
os.environ['CODEX_ENGINE'] = 'on'
import claude_cli  # noqa: E402
import claude_gate  # noqa: E402
import codex_cli  # noqa: E402
import llmroute  # noqa: E402

t = Checks()
calls = []
OK = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'GPT 回的'}},
      {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 5}}]


def gpt(events):
    def run(args, stdin):
        calls.append((args, claude_gate.status()['slots']))
        return events, ''
    codex_cli.use_runner(run)


claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': '断了', 'events': []})
gpt(OK)
t.check('Claude 失败：GPT 顶上', llmroute.text('写个回顾', 'reflect') == 'GPT 回的')
t.check('不留会话、只读沙盒', '--ephemeral' in calls[0][0] and 'read-only' in calls[0][0], str(calls[0][0]))
t.check('占的是 GPT 的位子', calls[0][1] and calls[0][1][0][4] == 'codex', str(calls[0][1]))
t.check('账记在共用的闸里', any(r[5] == 'codex' for r in claude_gate.status()['5h']))
n = len(calls)
llmroute.text('摘要', 'summary')
t.check('批量的用途（摘要）不走 GPT', len(calls) == n)
s = llmroute.stream('写个回顾', 'reflect')
t.check('流式：GPT 一次给全文', s is not None and ''.join(s) == 'GPT 回的')

gpt([{'type': 'error', 'message': "You've hit your usage limit."}])
llmroute.text('写个回顾', 'reflect')
t.check('撞上限：只停 GPT', claude_gate.paused_until('codex') and not claude_gate.paused_until('claude'))
t.check('停着就不再调', not codex_cli.on())

os.environ['CODEX_ENGINE'] = ''
codex_cli.use_runner(None)
t.check('没开就不用', not codex_cli.on())
t.finish()
