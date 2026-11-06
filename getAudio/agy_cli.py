"""Antigravity CLI（agy，用户的 Gemini 订阅）当写字的模型（2026-11-06，用户：「分析模型里面也能选所有的，
codex、ag、Claude 三个公司的都能选」）。转写那边是 transcribe_agy.py；这里只管「给一段提示词、要一段字」。

    installed()            → 本机有没有 agy
    models()               → [(id, 名字)]：`agy models` 列的 Gemini 那几个（Argon / Flash × low/medium/high），缓存 6 小时；
                             读不到就用下面的 FALLBACK（不挡页面）。agy 里也有 Claude / GPT-OSS，Claude 走自己的订阅更直接，不重复列
    text(prompt, model)    → 一次拿全文；失败抛异常（调用方退回原来的模型）

走订阅额度：记账 $0、token 照记（provider=agy）；开了共用的闸就先问闸（给用户留几成）、占位子、记同一本账。
提示词里写明不要用工具；工作目录是一个空目录，它想读文件也没东西可读。
"""
import json
import os
import subprocess
import threading
import time

import claude_gate
import config
import usage

BIN = 'agy'
TIMEOUT = 600
CWD = os.path.expanduser('~/.verbatim-agy')
FALLBACK = (('gemini-4-argon-high', 'Gemini 4 Argon (High)'), ('gemini-4-argon-medium', 'Gemini 4 Argon (Medium)'),
            ('gemini-4-argon-low', 'Gemini 4 Argon (Low)'), ('gemini-3.8-flash-high', 'Gemini 3.8 Flash (High)'),
            ('gemini-3.8-flash-medium', 'Gemini 3.8 Flash (Medium)'), ('gemini-3.8-flash-low', 'Gemini 3.8 Flash (Low)'))
_CACHE_S = 6 * 3600
_cache = {'t': 0.0, 'models': None, 'busy': False}
_lock = threading.Lock()
_runner = None            # 测试用：use_runner(fn)，fn(args) -> agy --output-format json 的那个对象
NO_TOOLS = ('Answer directly from the text below. Do not use any tools, do not open or read any files, '
            'do not run any commands.\n\n')


def use_runner(fn):
    global _runner
    _runner = fn


def installed():
    return config.agy_installed()


def _bin():
    from transcribe_agy import agy_bin
    return agy_bin() or BIN


def _refresh():
    try:
        out = subprocess.run([_bin(), 'models'], capture_output=True, text=True, timeout=60).stdout or ''
        got = []
        for ln in out.splitlines():
            mid, _, name = ln.partition('\t')
            mid = mid.strip()
            if mid.startswith('gemini-') and name.strip():
                got.append((mid, name.strip()))
        if got:
            with _lock:
                _cache.update(t=time.time(), models=tuple(got))
    except Exception:  # noqa: BLE001  读不到就接着用 FALLBACK
        pass
    finally:
        _cache['busy'] = False


def models():
    if not installed():
        return ()
    with _lock:
        fresh = _cache['models'] and time.time() - _cache['t'] < _CACHE_S
        if not fresh and not _cache['busy'] and not _runner:
            _cache['busy'] = True
            threading.Thread(target=_refresh, daemon=True).start()   # `agy models` 要联网几秒：后台拉，这次先给手里的
        return _cache['models'] or FALLBACK


def text(prompt, model, purpose='ask'):
    prio = claude_gate_prio(purpose)
    short = claude_gate.agy_short(model, prio)
    if short:
        raise RuntimeError(f'agy quota: {short}')
    args = ['-p', NO_TOOLS + prompt, '--model', model, '--print-timeout', f'{TIMEOUT}s', '--output-format', 'json']
    t0 = time.time()
    with claude_gate.slot('verbatim', purpose, prio, provider='agy', model=model):
        d = _runner(args) if _runner else _exec(args)
    claude_gate.record('verbatim', purpose, model, d, time.time() - t0, prio, provider='agy')
    u = d.get('usage') or {}
    usage.record('agy', model, purpose, input_tokens=u.get('input_tokens') or 0, output_tokens=u.get('output_tokens') or 0,
                 thinking_tokens=u.get('thinking_tokens') or 0, cost_usd=0.0)
    out = (d.get('response') or '').strip()
    if str(d.get('status') or '').upper() != 'SUCCESS' or not out:
        low = out.lower()
        if 'quota' in low and any(w in low for w in ('exceed', 'exhaust', 'reached', 'limit')):
            claude_gate.pause(time.time() + 1800, out[:200], 'verbatim', provider='agy')
        raise RuntimeError(f"agy {d.get('status')}: {out[:200] or 'empty reply'}")
    return out


def claude_gate_prio(purpose):
    import claude_cli
    return claude_cli._prio(purpose)


def _exec(args):
    os.makedirs(CWD, exist_ok=True)
    p = subprocess.run([_bin(), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=TIMEOUT + 60, cwd=CWD)
    line = next((ln for ln in reversed((p.stdout or '').strip().splitlines()) if ln.lstrip().startswith('{')), None)
    if line is None:
        raise RuntimeError(f'agy returned nothing (exit {p.returncode}): {(p.stderr or p.stdout or "")[:200]}')
    return json.loads(line)
