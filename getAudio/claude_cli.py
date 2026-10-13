"""本机 Claude Code 当写字的模型（2026-10-13，用户：「问证据卡」交给 Claude）。只在设了 ASK_ENGINE=claude、
电脑上又有 `claude` 命令时才用；别人装的 Verbatim 没有 Claude Code，默认行为不变（CLAUDE.md 多窗口规矩 4）。

    engine()                    → 'claude' / ''：这次「问证据卡」要不要交给 Claude（ASK_ENGINE）
    text_engine()               → 日常文字活（llmroute 的用途）要不要先给 Claude（TEXT_ENGINE）
    available()                 → 有没有 claude 命令、是不是撞了上限停着
    stream(prompt, purpose)     → 生成器，边写边 yield 一小段字；还没出字就失败抛异常（调用方退回原来的模型）
    text(prompt, purpose)       → 一次拿全文；失败抛异常

走的是用户的 Claude 订阅额度（不是 API key），所以记账记 $0、token 照记（provider=claude-code）。
精简模式：自己的系统提示、不给工具、不读设置和 MCP、不留会话；一次性的调用关掉缓存写入
（Claude Code 默认把输入写进 1 小时缓存，写的价钱是普通输入的 2 倍，之后又没人读——Ephemeris 10-13 实测贵一倍）。
同时最多 PARALLEL 个进程；撞上限 / 没登录就停到重置时间（读得到的话），期间 available() 为假。
跟 Ephemeris 的 llm.claude 是同一个路子，各自一份（Verbatim 不 import Ephemeris 的代码）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import usage

MODEL = 'claude-opus-5-5'
BIN = 'claude'
PARALLEL = int(os.environ.get('CLAUDE_PARALLEL') or 6)       # 批量转写时摘要 / 标签一下来很多；6 个进程约 1.4GB
TIMEOUT = 1200                                # 画像合成：几十万字的材料、Opus 写长文，10 分钟不一定够
CWD = os.path.expanduser('~/.verbatim-claude')        # 会话存在这个目录名下，不混进用户自己的 Claude Code 项目
SYSTEM = 'You are the writing engine inside Verbatim, a local research app. Follow the instructions in the user message exactly and output only what it asks for.'
PAUSE_S = 30 * 60
_LIMIT = re.compile(r'usage limit|limit reached|rate limit|resets?|Not logged in|/login|credit balance', re.I)
_sem = threading.BoundedSemaphore(PARALLEL)
_paused = {'until': 0.0}
_runner = None            # 测试用：use_runner(fn)，fn(args, stdin) -> {'events': [...], 'result': ..., 'usage': ...}


def use_runner(fn):
    global _runner
    _runner = fn
    _paused['until'] = 0.0


def available():
    if _paused['until'] > time.time():
        return False
    return bool(_runner) or bool(shutil.which(BIN))


def engine():
    """问证据卡（ask.answer / answer_stream）交不交给 Claude：ASK_ENGINE=claude。"""
    return 'claude' if (os.environ.get('ASK_ENGINE') or '').strip().lower() == 'claude' and available() else ''


def text_engine():
    """日常文字活（llmroute 的那些用途：摘要、标签、回顾……）交不交给 Claude：TEXT_ENGINE=claude（2026-10-13）。"""
    return (os.environ.get('TEXT_ENGINE') or '').strip().lower() == 'claude' and available()


def cards_engine():
    """每期抽证据卡交不交给 Claude Haiku：CARDS_ENGINE=claude（2026-10-13，ADR 0017）。"""
    return (os.environ.get('CARDS_ENGINE') or '').strip().lower() == 'claude' and available()


def _args(effort, model=MODEL, tools=''):
    """tools：默认不给任何工具；联网核实给 'WebSearch'（Claude Code 自带的搜索，10-13 实测 Haiku 一次 11 秒、带来源网址）。"""
    allow = ['--allowedTools', tools] if tools else []
    return ['-p', '--model', model, '--effort', effort, '--system-prompt', SYSTEM, '--tools', tools, *allow,
            '--disable-slash-commands', '--strict-mcp-config', '--setting-sources', '', '--no-session-persistence',
            '--output-format', 'stream-json', '--verbose', '--include-partial-messages']


def _delta(ev):
    if ev.get('type') != 'stream_event':
        return None
    e = ev.get('event') or {}
    if e.get('type') == 'content_block_delta' and (e.get('delta') or {}).get('type') == 'text_delta':
        return e['delta'].get('text', '')
    return None


def _record(d, purpose, model=MODEL):
    u = d.get('usage') or {}
    usage.record('claude-code', model, purpose, input_tokens=u.get('input_tokens', 0) + u.get('cache_creation_input_tokens', 0),
                 output_tokens=u.get('output_tokens', 0), cached_tokens=u.get('cache_read_input_tokens', 0), cost_usd=0.0)


def _fail(d):
    text = (d.get('result') or '').strip()
    if _LIMIT.search(text):
        m = re.search(r'\|(\d{10})', text)
        _paused['until'] = max(int(m.group(1)) if m else time.time() + PAUSE_S, time.time() + 60)
    return RuntimeError(f'Claude: {text[:200] or "empty reply"}')


def stream(prompt, purpose='ask', effort='medium', model=MODEL, tools='', _stdin=None, _extra=()):
    """边写边 yield。调用方中途 close（用户点了停止）：claude 进程当场关掉，已经出的字照样记账。"""
    args = _args(effort, model, tools) + list(_extra)
    prompt = prompt if _stdin is None else _stdin
    with _sem:
        if _runner:
            d = _runner(args, prompt)
            for ev in d.get('events', []):
                t = _delta(ev)
                if t:
                    yield t
            _record(d, purpose, model)
            if d.get('is_error') or not (d.get('result') or '').strip():
                raise _fail(d)
            return
        os.makedirs(CWD, exist_ok=True)
        p = subprocess.Popen([BIN] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=CWD, env={**os.environ, 'DISABLE_PROMPT_CACHING': '1'})
        d, t0 = {'is_error': True, 'result': ''}, time.time()
        try:
            p.stdin.write(prompt)
            p.stdin.close()
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get('type') == 'result':
                    d = ev
                    continue
                t = _delta(ev)
                if t:
                    yield t
                if time.time() - t0 > TIMEOUT:
                    d = {'is_error': True, 'result': f'no answer after {TIMEOUT}s'}
                    break
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
            _record(d, purpose, model)
        if d.get('is_error') or not (d.get('result') or '').strip():
            if not d.get('result'):
                d['result'] = (p.stderr.read() or '')[:300]
            raise _fail(d)


def text(prompt, purpose='ask', effort='medium', model=MODEL, tools=''):
    return ''.join(stream(prompt, purpose, effort, model, tools)).strip()


def vision(prompt, images, purpose='visual', effort='low', model='claude-haiku-5-5'):
    """看图：images 是 [(mime, base64)]，和提示词作为一条用户消息从标准输入进去（--input-format stream-json），
    不给读文件的工具。墨页 10-13 实测 Haiku 读手机拍的作业比 Kimi 准、40 路并发没被限流。"""
    content = [{'type': 'image', 'source': {'type': 'base64', 'media_type': m, 'data': b}} for m, b in images]
    content.append({'type': 'text', 'text': prompt})
    msg = json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}) + '\n'
    return ''.join(stream(prompt, purpose, effort, model, _stdin=msg, _extra=('--input-format', 'stream-json'))).strip()
