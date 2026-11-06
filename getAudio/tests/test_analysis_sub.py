"""添加频道的「分析模型」也能选三家订阅（2026-11-06）：claude:<模型> / codex:<模型> / agy:<模型>。
跑生产代码：config.resolve_analysis、analyze._llm 分派、/api/analysis/models。
桩（只在模型边界）：claude_cli / codex_cli / agy_cli 的 runner、analyze._call_gemini。"""
import os

from _support import Checks, isolate, write_json

isolate('analysissub')
os.environ['CODEX_ENGINE'] = ''
import analyze  # noqa: E402
import app as A  # noqa: E402
import agy_cli  # noqa: E402
import claude_cli  # noqa: E402
import codex_cli  # noqa: E402
import config  # noqa: E402

claude_cli.BIN = 'no-such-claude-binary'
codex_cli.BIN = 'no-such-codex-binary'
t = Checks()
seen = {'claude': [], 'codex': [], 'agy': [], 'gemini': []}


def claude_ok(args, stdin):
    seen['claude'].append(args[args.index('--model') + 1])
    return {'result': 'from claude', 'usage': {'input_tokens': 1, 'output_tokens': 1},
            'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                        'delta': {'type': 'text_delta', 'text': 'from claude'}}}]}


def codex_ok(args, stdin):
    seen['codex'].append(args[args.index('-m') + 1] if '-m' in args else None)
    return [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'from codex'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}], ''


def agy_ok(args):
    seen['agy'].append(args[args.index('--model') + 1])
    return {'status': 'SUCCESS', 'response': 'from agy', 'usage': {}}


def fake_gemini(prompt, grounded=False, model=None, purpose='analysis', **k):
    seen['gemini'].append((model, grounded))
    return 'from gemini'


claude_cli.use_runner(claude_ok)
codex_cli.use_runner(codex_ok)
agy_cli.use_runner(agy_ok)
analyze._call_gemini = fake_gemini

# 1. 预设 → (provider, 抽取, 合成)
t.check('claude 预设', config.resolve_analysis('claude:claude-opus-5-5') == ('claude', 'claude-opus-5-5', 'claude-opus-5-5'))
t.check('codex 预设', config.resolve_analysis('codex:gpt-6.1-sol') == ('codex', 'gpt-6.1-sol', 'gpt-6.1-sol'))
t.check('agy 预设', config.resolve_analysis('agy:gemini-4-argon-high') == ('agy', 'gemini-4-argon-high', 'gemini-4-argon-high'))
t.check('乱写的落回 gemini', config.resolve_analysis('claude:../../x')[0] == 'gemini'
        and config.resolve_analysis('foo:bar')[0] == 'gemini')

# 2. 选了哪家就用哪家；就算开着 CARDS_ENGINE（抽卡默认给 Haiku），用户选的也优先
os.environ['CARDS_ENGINE'] = 'claude'
os.environ['TEXT_ENGINE'] = 'claude'
out = analyze._llm('extract cards', 'claude', 'claude-sonnet-5-5', purpose='cards')
t.check('Claude 预设抽卡用选的 Sonnet，不是 Haiku', out == 'from claude' and seen['claude'] == ['claude-sonnet-5-5'], seen)
out = analyze._llm('portrait', 'codex', 'gpt-6.1-sol', purpose='synth')
t.check('codex 预设走 codex -m', out == 'from codex' and seen['codex'] == ['gpt-6.1-sol'], seen)
out = analyze._llm('cards', 'agy', 'gemini-4-argon-low', purpose='cards')
t.check('agy 预设走 agy --model', out == 'from agy' and seen['agy'] == ['gemini-4-argon-low'], seen)
n = len(seen['claude'])
analyze._llm('lens', 'claude', 'claude-opus-5-5', purpose='lens')
t.check('镜头报告也走它', seen['claude'][n:] == ['claude-opus-5-5'])

# 3. 要上网核实（grounded）的只有 Gemini 能做：交给 Gemini
n = len(seen['gemini'])
analyze._llm('verify', 'claude', 'claude-opus-5-5', grounded=True, purpose='verify')
t.check('grounded 交给 Gemini', len(seen['gemini']) == n + 1 and seen['gemini'][-1][1] is True)

# 4. 订阅那家挂了（撞上限等）：这一次改用 Gemini 同档
os.environ['CARDS_ENGINE'] = ''
os.environ['TEXT_ENGINE'] = ''
agy_cli.use_runner(lambda a: {'status': 'ERROR', 'response': 'quota exhausted'})
n = len(seen['gemini'])
out = analyze._llm('cards', 'agy', 'gemini-4-argon-low', purpose='cards')
_, g_extract, g_synth = config.resolve_analysis('gemini')
t.check('agy 挂了抽卡回 Gemini 抽取档', out == 'from gemini' and seen['gemini'][n:] == [(g_extract, False)], seen['gemini'][n:])
codex_cli.use_runner(lambda a, s: ([{'type': 'error', 'message': 'boom'}], ''))
n = len(seen['gemini'])
analyze._llm('synth', 'codex', 'gpt-6.1-sol', purpose='synth')
t.check('codex 挂了合成回 Gemini 合成档', seen['gemini'][n:] == [(g_synth, False)], seen['gemini'][n:])

# 5. 表单要的列表：三家订阅、id 带前缀；没装就不列
agy_cli.installed = lambda: False
claude_cli.use_runner(None)          # 没有 claude 命令、也没打桩 = 没装
r = A.app.test_client().get('/api/analysis/models').get_json()
t.check('没装就不列', r['models'] == [], r)
agy_cli.installed = lambda: True
os.environ['CODEX_ENGINE'] = 'on'
codex_cli.models_cache_path = lambda: write_json(os.path.join(os.environ['GETAUDIO_DATA_DIR'], 'mc.json'), {'models': [
    {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1-Sol', 'visibility': 'list'}]}) or os.path.join(os.environ['GETAUDIO_DATA_DIR'], 'mc.json')
claude_cli.use_runner(claude_ok)
r = A.app.test_client().get('/api/analysis/models').get_json()
ids = [m['id'] for m in r['models']]
t.check('三家都列、id 是预设名', {'claude:claude-opus-5-5', 'codex:gpt-6.1-sol', 'agy:gemini-4-argon-high'} <= set(ids)
        and {m['group'] for m in r['models']} == {'claude', 'codex', 'agy'}
        and all(config.resolve_analysis(i)[0] in ('claude', 'codex', 'agy') for i in ids), ids)

t.finish()
