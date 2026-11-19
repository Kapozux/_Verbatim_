"""isolate() 把会起本机子进程的开关都关掉（GitHub #8）：作者 .env 里 CODEX_ENGINE=on / ASK_ENGINE=claude 开着时，
没清掉的开关会让测试真去起 codex / claude 进程——禁外网只拦得住本进程的 socket，拦不住子进程，
结果是整套测试卡住、还花订阅额度。

跑生产代码：codex_cli.on、claude_cli.engine / text_engine / cards_engine、llmroute.text。
桩：codex_cli / claude_cli 的 use_runner（装了 CLI 的机器上「命令在」就是这个状态），被调到就记一笔。
"""
import os

# 模拟作者的环境：开关在进测试之前就开着（.env 或 shell 里）
SWITCHES = {'CODEX_ENGINE': 'on', 'ASK_ENGINE': 'claude', 'TEXT_ENGINE': 'claude', 'CARDS_ENGINE': 'claude'}
os.environ.update(SWITCHES)

from _support import Checks, isolate  # noqa: E402

TMP = isolate('isolation')
import claude_cli  # noqa: E402
import codex_cli  # noqa: E402
import config  # noqa: E402,F401  （读 .env：不能把已经置空的开关又填回去）
import llmroute  # noqa: E402

t = Checks()
for k in SWITCHES:
    t.check(f'isolate() 之后 {k} 是空的', os.environ.get(k) == '', os.environ.get(k))

calls = []


def codex_runner(args, stdin):
    calls.append('codex')
    return [], ''


def claude_runner(*a, **k):
    calls.append('claude')
    raise RuntimeError('claude must not run in an isolated test')


codex_cli.use_runner(codex_runner)
claude_cli.use_runner(claude_runner)
t.check('装了 codex 也不用：codex_cli.on() 关着', not codex_cli.on(), codex_cli.on())
t.check('装了 claude 也不用：engine() 关着', not claude_cli.engine(), claude_cli.engine())
t.check('text_engine() 关着', not claude_cli.text_engine())
t.check('cards_engine() 关着', not claude_cli.cards_engine())

# 走一次日常文字活：两个 CLI 都不许被调到（没有别的路可走时返回 None，调用方接着用 Gemini）
out = llmroute.text('Summarize: hello world', 'summary')
t.check('日常文字活没去调 codex / claude', not calls, calls)
t.check('没有可走的路 → None', out is None, out)

# 开关本身还管用：测试自己在 isolate() 之后打开，就走得到
os.environ['CODEX_ENGINE'] = 'on'
t.check('isolate() 之后自己打开：codex_cli.on() 为真', codex_cli.on())

t.finish()
