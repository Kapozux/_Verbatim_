"""抽证据卡交给本机 Claude Code（CARDS_ENGINE=claude，2026-10-13 用户：「Verbatim 一次量太大可以 haiku（看性能）」）：
默认不开；开了每期抽卡先走 Claude Haiku（订阅额度、记 claude-code / $0），Claude 用不了或报错照旧走原来的模型；
用户自己的模型（custom）选了就不换（ADR 0013）。
桩：claude_cli 的 runner（假的 claude -p 事件流）、analyze._call_openai_compat（假 DeepSeek）。"""
import json
import os

from _support import Checks, isolate

isolate('claudecards', cheap_route=True)
import analyze  # noqa: E402
import claude_cli  # noqa: E402

CARDS = json.dumps({'cards': [{'obs': '他说要去硅谷', 'quote': '出国只来美国', 'timestamp': '00:00:01', 'layer': '他的主张'}]},
                   ensure_ascii=False)
seen = {'claude': [], 'deepseek': 0}


def claude_ok(args, stdin):
    seen['claude'].append((args[args.index('--model') + 1], args[args.index('--effort') + 1]))
    return {'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                                                          'delta': {'type': 'text_delta', 'text': CARDS}}}],
            'result': CARDS, 'usage': {'input_tokens': 5000, 'output_tokens': 300}}


def claude_down(args, stdin):
    seen['claude'].append('down')
    return {'is_error': True, 'result': 'Claude AI usage limit reached|9999999999', 'events': []}


def fake_deepseek(prompt, model, base, key, **kw):
    seen['deepseek'] += 1
    return CARDS


analyze._call_openai_compat = fake_deepseek
t = Checks()
TEXT = '[00:00:01] 出国只来美国，美国只来硅谷'

# 1. 默认不开：照旧 DeepSeek
claude_cli.use_runner(claude_ok)
d = analyze._extract_cards('一期', TEXT, '立党', 'aliyun', 'deepseek-v4-flash')
t.check('默认不开：照旧 DeepSeek', d and d['cards'] and seen['deepseek'] == 1 and not seen['claude'], str(seen))

# 2. 开了：抽卡走 Haiku（medium），不碰 DeepSeek
os.environ['CARDS_ENGINE'] = 'claude'
d = analyze._extract_cards('一期', TEXT, '立党', 'aliyun', 'deepseek-v4-flash')
t.check('开了：抽卡用 Haiku', d and d['cards'][0]['quote'] == '出国只来美国'
        and seen['claude'] == [('claude-haiku-5-5', 'medium')] and seen['deepseek'] == 1, str(seen))
t.check('别的用途（体检 review）不受这个开关影响', analyze._llm('x', 'aliyun', 'deepseek-v4-flash', purpose='review') == CARDS
        and len(seen['claude']) == 1 and seen['deepseek'] == 2)

# 3. 用户自己的模型：不换成 Claude
os.environ['CUSTOM_LLM_BASE'] = 'http://127.0.0.1:9/v1'
try:
    analyze._llm('x', 'custom', 'my-model', purpose='cards')
except Exception:  # noqa: BLE001
    pass
t.check('自己的模型：不偷偷换成 Claude', len(seen['claude']) == 1, str(seen))

# 4. Claude 撞上限：这期退回原来的模型，之后不再去碰 Claude
claude_cli.use_runner(claude_down)
d = analyze._extract_cards('一期', TEXT, '立党', 'aliyun', 'deepseek-v4-flash')
t.check('Claude 撞上限：退回 DeepSeek', d and d['cards'] and seen['deepseek'] == 3, str(seen))
n = len(seen['claude'])
analyze._extract_cards('二期', TEXT, '立党', 'aliyun', 'deepseek-v4-flash')
t.check('停着的时候不再去碰 Claude', len(seen['claude']) == n and seen['deepseek'] == 4, str(seen))

t.finish()
