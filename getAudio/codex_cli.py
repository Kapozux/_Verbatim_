"""本机 Codex CLI 当后备的写字模型（2026-11-03，用户：GPT 也有订阅，像 claude -p 那样接进来，跟 Ephemeris / 墨页一起归 claude_gate 管）。

    on()                        → 开了（CODEX_ENGINE=on）、有 codex 命令、没撞上限停着
    text(prompt, purpose)       → 一次拿全文；失败抛异常

只在 Claude 用不了时顶上（llmroute.text / stream 里，排在 DeepSeek 前面）；批量的用途（claude_cli.BATCH：摘要、标签、抽卡……）
不走它——Plus 的额度小，一批转写就能用完。走的是用户的 ChatGPT 订阅，记账记 $0、token 照记（provider=codex）。
codex exec --ephemeral（不留会话）、--ignore-user-config（不带用户自己的插件 / MCP / 记忆）、只读沙盒；
它自带的 shell 关不掉，所以只给纯文字的活，不给「问」这种要守着资料范围的。
跟 Ephemeris 的 llm.codex 是同一个路子，各自一份（Verbatim 不 import Ephemeris 的代码）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import claude_cli
import claude_gate
import usage

BIN = 'codex'
TIMEOUT = 900
CWD = os.path.expanduser('~/.verbatim-codex')
EFFORT = {'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh', 'max': 'xhigh'}
_sem = threading.BoundedSemaphore(int(os.environ.get('CODEX_PARALLEL') or 2))
_paused = {'until': 0.0}
_runner = None            # 测试用：use_runner(fn)，fn(args, stdin) -> (codex exec --json 的事件列表, stderr)


def use_runner(fn):
    global _runner
    _runner = fn
    _paused['until'] = 0.0


def model():
    m = (os.environ.get('CODEX_MODEL') or '').strip()
    if m:
        return m
    try:
        with open(os.path.expanduser('~/.codex/config.toml'), encoding='utf-8') as f:
            for line in f:
                hit = re.match(r'\s*model\s*=\s*"([^"]+)"', line)
                if hit:
                    return hit.group(1)
                if line.lstrip().startswith('['):
                    break
    except OSError:
        pass
    return ''


def models_cache_path():
    return os.path.expanduser('~/.codex/models_cache.json')


def list_models():
    """[(slug, 名字)]：codex 自己缓存的模型表里能列的那些（visibility=list），读不到就只有 config 里那个。"""
    try:
        with open(models_cache_path(), encoding='utf-8') as f:
            d = json.load(f)
        ms = d.get('models', d) if isinstance(d, dict) else d
        got = [(m['slug'], m.get('display_name') or m['slug']) for m in ms
               if isinstance(m, dict) and m.get('slug') and m.get('visibility', 'list') == 'list']
    except (OSError, ValueError, KeyError, TypeError):
        got = []
    cur = model()
    if cur and cur not in dict(got):
        got.insert(0, (cur, cur))
    return got


def on():
    if (os.environ.get('CODEX_ENGINE') or '').strip().lower() not in ('on', '1', 'true', 'yes'):
        return False
    if _paused['until'] > time.time() or claude_gate.paused_until('codex'):
        return False
    return bool(_runner) or bool(shutil.which(BIN))


def fits(purpose):
    """这个用途能不能交给 GPT：批量的不行（额度小）。"""
    return claude_cli._prio(purpose) != 'batch'


def _exec(args, stdin):
    if _runner:
        return _runner(args, stdin)
    os.makedirs(CWD, exist_ok=True)
    r = subprocess.run([shutil.which(BIN) or BIN] + args, input=stdin, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=CWD, timeout=TIMEOUT)
    events = []
    for line in r.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, r.stderr


def text(prompt, purpose='summary', effort='medium', model_id=None):
    m = model_id or model()
    args = ['exec', '--json', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--ignore-rules',
            '-s', 'read-only', '-C', CWD, '-c', f'model_reasoning_effort="{EFFORT.get(effort, "medium")}"',
            '--disable', 'apps', '--disable', 'browser_use', '--disable', 'computer_use']
    if m:
        args += ['-m', m]
    args.append('-')
    stdin = claude_cli.SYSTEM + ' Do not run any commands or read any files; everything you need is below.\n\n' + prompt
    t0 = time.time()
    with _sem, claude_gate.slot(claude_cli.APP, purpose, claude_cli._prio(purpose), provider='codex'):
        events, err = _exec(args, stdin)
    texts, u, errors = [], {}, []
    for e in events:
        item = e.get('item') or {}
        if e.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            texts.append(item.get('text') or '')
        elif e.get('type') == 'turn.completed':
            u = e.get('usage') or {}
        elif e.get('type') in ('error', 'turn.failed'):
            errors.append(str(e.get('message') or (e.get('error') or {}).get('message') or e))
    out = (texts[-1] if texts else '').strip()
    bad = bool(errors) or not out
    claude_gate.record(claude_cli.APP, purpose, 'codex:' + (m or 'default'), {'usage': u, 'is_error': bad},
                       time.time() - t0, claude_cli._prio(purpose), provider='codex')
    usage.record('codex', m or 'codex', purpose, input_tokens=(u.get('input_tokens') or 0) - (u.get('cached_input_tokens') or 0),
                 output_tokens=(u.get('output_tokens') or 0) + (u.get('reasoning_output_tokens') or 0),
                 cached_tokens=u.get('cached_input_tokens') or 0, cost_usd=0.0)
    if bad:
        why = ' '.join(errors) or (err or '').strip()[-300:] or 'empty reply'
        if claude_cli._LIMIT.search(why):
            hit = re.search(r'resets?[^0-9]*(\d{10})', why)
            _paused['until'] = max(int(hit.group(1)) if hit else time.time() + claude_cli.PAUSE_S, time.time() + 60)
            claude_gate.pause(_paused['until'], why, claude_cli.APP, provider='codex')
        raise RuntimeError(f'Codex: {why[:200]}')
    return out
