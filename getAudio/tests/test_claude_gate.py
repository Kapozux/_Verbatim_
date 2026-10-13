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

# 给人自己留额度：最近 5 小时「三个应用 + 人自己在 Claude Code 里用的」合计到了 LIMIT - KEEP，批量 / 普通的等着，问照常
import json as _json  # noqa: E402
cc = os.path.join(TMP, 'ccprojects', '-Users-me-proj')
os.makedirs(cc)
os.environ['CLAUDE_GATE_CC_DIR'] = os.path.join(TMP, 'ccprojects')
sqlite3.connect(claude_gate.path(), isolation_level=None).execute("DELETE FROM meta")
now_iso = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
with open(os.path.join(cc, 's.jsonl'), 'w') as f:      # 人自己用 Opus：输入 1000 万 token = $40
    f.write(_json.dumps({'timestamp': now_iso, 'requestId': 'r1', 'message': {'id': 'm1', 'model': 'claude-opus-5-5',
                                                                              'usage': {'input_tokens': 10_000_000, 'output_tokens': 0}}}) + '\n')
appdir = os.path.join(TMP, 'ccprojects', '-Users-me--verbatim-claude')      # 应用自己起的会话：不重复算
os.makedirs(appdir)
with open(os.path.join(appdir, 'a.jsonl'), 'w') as f:
    f.write(_json.dumps({'timestamp': now_iso, 'requestId': 'r2', 'message': {'id': 'm2', 'model': 'claude-opus-5-5',
                                                                              'usage': {'input_tokens': 10_000_000, 'output_tokens': 0}}}) + '\n')
sqlite3.connect(claude_gate.path(), isolation_level=None).execute('DELETE FROM pause')
claude_cli.use_runner(runner)
os.environ['CLAUDE_GATE_LIMIT_5H'] = '140'
os.environ['CLAUDE_GATE_KEEP'] = '100'
st = claude_gate.status()
t.check('读得到人自己的用量、不算应用自己起的会话', round(st['cc_5h']) == 40, str(st['cc_5h']))
try:
    with claude_gate.slot('t', 'cards', 'batch', wait=0):
        got = 'in'
except claude_gate.Paused:
    got = 'waited'
t.check('合计到了 140 - 100：批量等着', got == 'waited')
with claude_gate.slot('t', 'ask', 'live', wait=0):
    pass
t.check('问照常进去', True)
os.environ['CLAUDE_GATE_LIMIT_5H'] = '200'
with claude_gate.slot('t', 'cards', 'batch', wait=0):
    pass
t.check('没到：批量照常', True)

t.finish()
