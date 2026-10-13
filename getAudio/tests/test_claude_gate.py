"""三个应用共用一份 Claude 额度的分配器（claude_gate，CLAUDE_GATE 指向共用的 SQLite；2026-10-13 用户：「一个监控所有 usage 的分配器」）：
claude_cli 每次起 claude 进程前占一个位子（交互的问 = live，抽卡 / 摘要这类 = batch，其余 normal），用完记一行账；
撞上限写进共用的 pause，别的应用也跟着停；没设 CLAUDE_GATE 就跟以前一样。"""
import os
import time

from _support import Checks, isolate

TMP = isolate('claudegate', cheap_route=True)
os.environ['CLAUDE_GATE'] = os.path.join(TMP, 'gate', 'gate.db')
import claude_cli  # noqa: E402
import claude_gate  # noqa: E402

held = []


def runner(args, stdin):
    held.append(claude_gate.status()['slots'])
    return {'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                                                          'delta': {'type': 'text_delta', 'text': '好'}}}],
            'result': '好', 'usage': {'input_tokens': 300, 'output_tokens': 5}, 'total_cost_usd': 0.004}


claude_cli.use_runner(runner)
t = Checks()

claude_cli.text('问一句', purpose='ask')
claude_cli.text('抽卡', purpose='cards', model='claude-haiku-5-5')
t.check('调用时占着一个位子：问 = live，抽卡 = batch', held[0] == [('verbatim', 'ask', 'live', 0)] and held[1][0][2] == 'batch', str(held))
s = claude_gate.status()
t.check('用完就还', s['slots'] == [], str(s['slots']))
t.check('每次记一行账（应用、模型、等值美元）', sorted((r[0], r[1], r[2]) for r in s['5h']) ==
        [('verbatim', 'claude-haiku-5-5', 1), ('verbatim', claude_cli.MODEL, 1)] and round(sum(r[3] for r in s['5h']), 3) == 0.008, str(s['5h']))

# 别的应用（比如墨页）撞了上限：这边也当 Claude 用不了
claude_gate.pause(time.time() + 600, 'usage limit reached', 'moye')
t.check('别的应用撞上限：这边 available() 为假', not claude_cli.available())
import sqlite3  # noqa: E402
sqlite3.connect(claude_gate.path(), isolation_level=None).execute('DELETE FROM pause')

# 这边撞了上限：写进共用的 pause
claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': 'Claude AI usage limit reached|%d' % (time.time() + 900), 'events': []})
try:
    claude_cli.text('x', purpose='summary')
except Exception:  # noqa: BLE001
    pass
p = claude_gate.status()['pause']
t.check('这边撞上限：写进共用的 pause，别的应用也停', p and p[2] == 'verbatim' and p[0] > time.time() + 800, str(p))
t.finish()
