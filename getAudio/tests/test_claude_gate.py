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
t.check('调用时占着一个位子：问 = live，抽卡 = batch', [r[:4] for r in held[0]] == [('verbatim', 'ask', 'live', 0)] and held[1][0][2] == 'batch', str(held))
s = claude_gate.status()
t.check('用完就还', s['slots'] == [], str(s['slots']))
t.check('每次记一行账（应用、模型、等值美元）', sorted((r[0], r[1], r[2]) for r in s['5h']) ==
        [('verbatim', 'claude-haiku-5-5', 1), ('verbatim', claude_cli.MODEL, 1)] and round(sum(r[3] for r in s['5h']), 3) == 0.008, str(s['5h']))

# 别的应用（比如墨页）撞了上限：这边也当 Claude 用不了
claude_gate.pause(time.time() + 600, 'usage limit reached', 'moye')
t.check('别的应用撞上限：这边 available() 为假', not claude_cli.available())
import sqlite3  # noqa: E402
sqlite3.connect(claude_gate.path(), isolation_level=None).execute('DELETE FROM pauses')

# 这边撞了上限：写进共用的 pause
claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': 'Claude AI usage limit reached|%d' % (time.time() + 900), 'events': []})
try:
    claude_cli.text('x', purpose='summary')
except Exception:  # noqa: BLE001
    pass
p = claude_gate.status()['pause']['claude']
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
sqlite3.connect(claude_gate.path(), isolation_level=None).execute('DELETE FROM pauses')
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

# 给人自己留周额度（10-31，5 小时上限取消了）：看 Anthropic 那边的「7 天用了几成」，批量先让、问留到最后
os.environ.pop('CLAUDE_GATE_LIMIT_5H', None)
os.environ['CLAUDE_GATE_WEEK_KEEP'] = '0.3'
claude_gate._probe = lambda: (0.75, time.time() + 3600)
sqlite3.connect(claude_gate.path(), isolation_level=None).execute("DELETE FROM meta")
try:
    with claude_gate.slot('t', 'cards', 'batch', wait=0):
        got = 'in'
except claude_gate.Paused:
    got = 'paused'
t.check('周额度用了 75%（给人留 30%）：批量不起 claude', got == 'paused')
with claude_gate.slot('t', 'ask', 'live', wait=0):
    pass
t.check('问到 90% 才让', True)
os.environ.pop('CLAUDE_GATE_WEEK_KEEP', None)

# ---- 第三家：agy（Gemini 订阅；11-05 用户：「不只有 claude gate，应该加入 Codex + AG」）----
# 用户平时自己也用 Gemini：应用默认最多用到 60%（给他留 40%，CLAUDE_GATE_AGY_KEEP），交互的到只剩 10% 才让。
# 额度看 agy -p /usage 的真数（每个桶剩几成），五分钟最多探一次，三边共用 meta 里那份。
probes = []
agy_now = {'gemini-5h': (0.9, time.time() + 3600), 'gemini-weekly': (0.9, time.time() + 86400),
           'gemini-argon-wtu-weekly': (0.9, time.time() + 86400)}


def agy_probe():
    probes.append(1)
    return dict(agy_now)


claude_gate._agy_probe = agy_probe
c0 = sqlite3.connect(claude_gate.path(), isolation_level=None)
c0.execute('DELETE FROM meta')
t.check('agy 是第三家', 'agy' in claude_gate.PROVIDERS and 'agy' in claude_gate.status()['pause'])


def try_slot(prio, model, provider='agy'):
    try:
        with claude_gate.slot('verbatim', 'transcribe', prio, wait=0, provider=provider, model=model):
            return 'in'
    except claude_gate.Paused as e:
        return str(e)


t.check('额度充足：Flash 进得去', try_slot('normal', 'gemini-3.8-flash-low') == 'in')
try_slot('normal', 'gemini-3.8-flash-low')
t.check('五分钟内不重复探', len(probes) == 1, str(len(probes)))

agy_now['gemini-weekly'] = (0.35, time.time() + 86400)
c0.execute('DELETE FROM meta')
r = try_slot('normal', 'gemini-3.8-flash-low')
t.check('Gemini 周额度只剩 35%（给他留 40%）：普通活不起', r != 'in' and 'Gemini' in r and '35%' in r, r)
t.check('Argon 是另一个桶：照常', try_slot('normal', 'gemini-4-argon-low') == 'in')
t.check('交互的到 10% 才让', try_slot('live', 'gemini-3.8-flash-low') == 'in')
os.environ['CLAUDE_GATE_AGY_KEEP'] = '0.2'
t.check('留几成能在 .env 改', try_slot('normal', 'gemini-3.8-flash-low') == 'in')
os.environ.pop('CLAUDE_GATE_AGY_KEEP')

agy_now['gemini-weekly'] = (0.9, time.time() + 86400)
agy_now['gemini-argon-wtu-weekly'] = (0.3, time.time() + 86400)
c0.execute('DELETE FROM meta')
t.check('Argon 只剩 30%：Argon 不起、Flash 照常',
        try_slot('normal', 'gemini-4-argon-low') != 'in' and try_slot('normal', 'gemini-3.8-flash-low') == 'in')

claude_gate._agy_probe = lambda: None
c0.execute('DELETE FROM meta')
t.check('探不到额度（agy 坏了）：不挡', try_slot('normal', 'gemini-4-argon-low') == 'in')
claude_gate._agy_probe = agy_probe

# 并发上限各算各的
os.environ['CLAUDE_GATE_CAP_AGY'] = '2'
os.environ['CLAUDE_GATE_RESERVE_AGY'] = '1'
with claude_gate.slot('verbatim', 'transcribe', 'normal', wait=0, provider='agy', model='gemini-3.8-flash-low'):
    t.check('agy 普通活占满 cap - reserve：再来一个普通的等着', try_slot('normal', 'gemini-3.8-flash-low').startswith('Gemini'))
    t.check('Claude 不受 agy 占位影响', try_slot('normal', None, provider='claude') == 'in')
os.environ.pop('CLAUDE_GATE_CAP_AGY')
os.environ.pop('CLAUDE_GATE_RESERVE_AGY')

# 记账：agy 的 usage（input / output / thinking），订阅不按钱算记 0
claude_gate.record('verbatim', 'transcribe', 'gemini-3.8-flash-low',
                   {'usage': {'input_tokens': 5000, 'output_tokens': 300, 'thinking_tokens': 20}}, 12.0, 'normal', provider='agy')
row = c0.execute("SELECT input_tokens, output_tokens, usd FROM calls WHERE provider='agy'").fetchone()
t.check('agy 记一行账：输出含思考、$0', row == (5000, 320, 0.0), str(row))

# 撞上限只停这一家
claude_gate.pause(time.time() + 600, 'quota exhausted', 'verbatim', provider='agy')
t.check('agy 撞上限：只停 agy', claude_gate.paused_until('agy') > time.time() and not claude_gate.paused_until('claude')
        and not claude_gate.paused_until('codex'))
t.check('停着时 slot 抛 Paused', 'shared pause' in try_slot('live', 'gemini-3.8-flash-low'))
c0.execute('DELETE FROM pauses')

# status：三家的额度一页看
c0.execute('DELETE FROM meta')
s = claude_gate.status()
t.check('status 带 agy 各桶剩几成', abs(s['agy_quota']['gemini-weekly'][0] - 0.9) < 1e-6, str(s.get('agy_quota')))

t.finish()
