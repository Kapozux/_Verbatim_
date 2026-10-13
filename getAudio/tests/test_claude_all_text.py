"""所有文字活都先给本机 Claude（TEXT_ENGINE=claude，2026-10-13 用户：「画像也可以 Claude 吧（类似所有文字处理的）」）：
不只 llmroute 的那几个用途——画像合成 / 镜头 / 简报 / 证伪（analyze._llm，不管项目选的是 DeepSeek 还是 Gemini）、
播客 / 幻灯片 / 声纹命名 / 小红书报告（ask._llm、_call_gemini）都先走 Claude。
要判断、要写长文的用 Opus（输入很大时用 1M 上下文版），提取类用 Haiku。联网核实（grounded）、抽证据卡（归 CARDS_ENGINE）不走这条。
桩：claude_cli 的 runner、analyze._call_openai_compat（假 DeepSeek）、Gemini 客户端（假的，碰了就记一笔）。"""
import os

from _support import Checks, isolate

isolate('claudeall', cheap_route=True)
import analyze  # noqa: E402
import ask  # noqa: E402
import claude_cli  # noqa: E402

seen = []


def claude_ok(args, stdin):
    seen.append((args[args.index('--model') + 1], len(stdin)))
    return {'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                                                          'delta': {'type': 'text_delta', 'text': 'Claude 写的'}}}],
            'result': 'Claude 写的', 'usage': {'input_tokens': 100, 'output_tokens': 10}}


other = {'deepseek': 0, 'gemini': 0}
analyze._call_openai_compat = lambda *a, **k: other.__setitem__('deepseek', other['deepseek'] + 1) or 'DeepSeek 写的'


class FakeGemini:
    class models:
        @staticmethod
        def generate_content(**kw):
            other['gemini'] += 1
            raise RuntimeError('gemini stub')


analyze.make_gemini_client = lambda key: FakeGemini()
claude_cli.use_runner(claude_ok)
os.environ['TEXT_ENGINE'] = 'claude'
t = Checks()

t.check('画像合成（项目选 DeepSeek）：Opus', analyze._llm('卡片……', 'aliyun', 'deepseek-v4-flash', purpose='synth') == 'Claude 写的'
        and seen[-1][0] == claude_cli.MODEL and other['deepseek'] == 0, str(seen))
t.check('画像镜头（项目选 Gemini）：Opus，只问一次', analyze._llm('卡片……', 'gemini', 'gemini-2.5-flash', purpose='lens') == 'Claude 写的'
        and seen[-1][0] == claude_cli.MODEL and len(seen) == 2 and other['gemini'] == 0, str(seen))
t.check('批次简报：Haiku', analyze._llm('一批卡', 'openrouter', 'deepseek/deepseek-v4-flash', purpose='brief') == 'Claude 写的'
        and seen[-1][0] == 'claude-haiku-5-5', str(seen))
t.check('播客稿（ask._llm）：Opus', ask._llm('写一期播客', purpose='podcast') == 'Claude 写的' and seen[-1][0] == claude_cli.MODEL)
t.check('重试那次也一样（podcast-retry）', ask._llm('再写', purpose='podcast-retry') == 'Claude 写的' and seen[-1][0] == claude_cli.MODEL)
t.check('声纹命名：Haiku', ask._llm('谁在说话', purpose='voices') == 'Claude 写的' and seen[-1][0] == 'claude-haiku-5-5')
big = '卡' * 400_000
analyze._llm(big, 'aliyun', 'deepseek-v4-flash', purpose='synth')
t.check('输入很大的画像：用 1M 上下文的 Opus', seen[-1] == (claude_cli.MODEL + '[1m]', len(big)), str(seen[-1][0]))

args_seen = []
_orig = claude_ok


def claude_web(args, stdin):
    args_seen.append(args)
    return _orig(args, stdin)


claude_cli.use_runner(claude_web)
out = ask._llm('核实这句', purpose='predict', grounded=True)
a = args_seen[-1]
t.check('联网核实（grounded）：Claude Opus 带 WebSearch 工具', out == 'Claude 写的' and seen[-1][0] == claude_cli.MODEL
        and a[a.index('--tools') + 1] == 'WebSearch' and 'WebSearch' in a[a.index('--allowedTools') + 1], str(a))
ask._llm('普通的活', purpose='voices')
a = args_seen[-1]
t.check('不联网的活：照旧不给工具', a[a.index('--tools') + 1] == '' and '--allowedTools' not in a, str(a))
n = len(seen)
analyze._llm('抽卡', 'aliyun', 'deepseek-v4-flash', purpose='cards')
t.check('抽卡没开 CARDS_ENGINE：不因为 TEXT_ENGINE 走 Claude', len(seen) == n and other['deepseek'] == 1, str(other))

os.environ['TEXT_ENGINE'] = ''
analyze._llm('卡片', 'aliyun', 'deepseek-v4-flash', purpose='synth')
t.check('没开：照旧 DeepSeek', len(seen) == n and other['deepseek'] == 2)

t.finish()
