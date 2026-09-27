"""
LLM / ASR 用量与费用记账。

每次模型调用结束后记一行：谁（provider / model）、干什么（purpose）、为谁
（ref = 转写 task_id 或博主分析 chain_id）、用了多少 token、花了多少钱。
存 usage.db（DATA_DIR 下，跟 tasks.db 并列），Settings → Costs 里汇总。

费用来源两种：
  - reported：OpenRouter 每次响应直接带 usage.cost（美元），照抄。
  - table：Gemini 只返回 token 数，按下面 PRICES（美元 / 百万 token）算。
    价格表可被 DATA_DIR/prices.json 覆盖（同结构），模型涨价 / 新模型自己补。
  查不到价的模型（阿里云各模型、Gemini 3.5 Transcribe、阿里云 ASR）只记 token
  / 音频秒数，cost 留空，界面上单列"未计价"，不瞎猜。

调用点不知道自己在给哪条任务干活（转写引擎、摘要、enrich 都是通用模块），
所以 ref 走线程局部变量：app.py 在 worker 线程入口 `with usage.scope(ref=task_id)`，
中间任何层调 record() 都自动带上。线程池会丢线程局部，fan-out 的地方用
usage.bound(fn) 把当前 scope 带进子线程。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import config

DB_PATH = os.path.join(config.DATA_DIR, 'usage.db')
PRICES_PATH = os.path.join(config.DATA_DIR, 'prices.json')

# 美元 / 百万 token。'tiers' 按 prompt token 数分档（Gemini Pro 超 20 万 token 更贵）：
# [(上限, 输入, 输出), ...]，最后一档上限 None。audio_in：音频模态输入单价（没有则同 input）。
# cached：命中上下文缓存的输入单价（没有则同 input）。
PRICES = {
    'gemini-2.5-flash':      {'input': 0.30, 'audio_in': 1.00, 'output': 2.50, 'cached': 0.075},
    'gemini-flash-latest':   {'input': 0.30, 'audio_in': 1.00, 'output': 2.50, 'cached': 0.075},
    'gemini-2.5-flash-lite': {'input': 0.10, 'audio_in': 0.30, 'output': 0.40, 'cached': 0.025},
    'gemini-2.5-pro':        {'tiers': [(200_000, 1.25, 10.0), (None, 2.50, 15.0)], 'cached': 0.31},
    'gemini-3-pro-preview':  {'tiers': [(200_000, 2.00, 12.0), (None, 4.00, 18.0)], 'cached': 0.20},
    'gemini-3-flash-preview': {'input': 0.50, 'audio_in': 1.00, 'output': 3.00, 'cached': 0.05},
    'gemini-3.5-flash-lite': {'input': 0.30, 'audio_in': 0.30, 'output': 2.50, 'cached': 0.03},
    'gemini-3.5-flash':      {'input': 1.50, 'audio_in': 1.50, 'output': 9.00, 'cached': 0.15},
    # 按音频时长计价的 ASR：美元 / 小时。默认不填（阿里云按 CNY 计、Gemini 3.5 Transcribe 预览期价格未定），
    # 需要时在 prices.json 里加 {"qwen-audio-3.0-asr-flash-filetrans": {"per_audio_hour": 0.xx}}。
}

_lock = threading.Lock()
_local = threading.local()


# ---------- 线程局部的归属信息 ----------

class scope:
    """`with usage.scope(ref=task_id, chain=chain_id):` 内的所有 record() 自动带上归属。"""

    def __init__(self, ref=None, chain=None):
        self.ref, self.chain = ref, chain

    def __enter__(self):
        self.prev = getattr(_local, 'ctx', None)
        prev = self.prev or {}
        _local.ctx = {'ref': self.ref or prev.get('ref'),
                      'chain': self.chain or prev.get('chain')}
        return self

    def __exit__(self, *exc):
        _local.ctx = self.prev
        return False


def current():
    return dict(getattr(_local, 'ctx', None) or {})


def bound(fn):
    """把当前线程的 scope 绑进 fn，供线程池调用（线程局部不会自动跨线程）。"""
    ctx = getattr(_local, 'ctx', None)

    @wraps(fn)
    def _wrapped(*a, **kw):
        prev = getattr(_local, 'ctx', None)
        _local.ctx = ctx
        try:
            return fn(*a, **kw)
        finally:
            _local.ctx = prev
    return _wrapped


# ---------- 存储 ----------

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''
            CREATE TABLE IF NOT EXISTS calls (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                provider      TEXT NOT NULL,
                model         TEXT NOT NULL,
                purpose       TEXT NOT NULL,
                ref           TEXT,
                chain         TEXT,
                input_tokens  INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                audio_tokens  INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                thinking_tokens INTEGER DEFAULT 0,
                audio_seconds REAL DEFAULT 0,
                cost_usd      REAL,
                cost_source   TEXT
            )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls(ts)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_calls_ref ON calls(ref)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_calls_chain ON calls(chain)')


# ---------- 价格 ----------

_prices_cache = {'stamp': float('-inf'), 'data': None}   # -inf：首次一定读文件（monotonic 可能从 0 起算）


def prices():
    """内置价格表 + prices.json 覆盖；文件 30 秒重读一次，改完不用重启。"""
    now = time.monotonic()
    if _prices_cache['data'] is not None and now - _prices_cache['stamp'] < 30:
        return _prices_cache['data']
    merged = {k: dict(v) for k, v in PRICES.items()}
    try:
        with open(PRICES_PATH, 'r', encoding='utf-8') as f:
            for k, v in (json.load(f) or {}).items():
                if isinstance(v, dict):
                    merged[k] = v
    except FileNotFoundError:
        pass
    except Exception:
        pass
    _prices_cache.update(stamp=now, data=merged)
    return merged


def _price_for(model):
    table = prices()
    if model in table:
        return table[model]
    # 带版本后缀 / 日期后缀的名字（gemini-2.5-flash-preview-05-20）按前缀最长匹配
    best = None
    for k in table:
        if model.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    return table.get(best) if best else None


def estimate_cost(model, input_tokens=0, output_tokens=0, audio_tokens=0,
                  cached_tokens=0, thinking_tokens=0, audio_seconds=0):
    """按价格表算美元；模型没价 → None。thinking 按输出计价（Gemini 就是这么收的）。"""
    p = _price_for(model or '')
    if not p:
        return None
    if 'per_audio_hour' in p:
        return round(audio_seconds / 3600.0 * p['per_audio_hour'], 6) if audio_seconds else None
    prompt_total = input_tokens
    if 'tiers' in p:
        in_rate = out_rate = None
        for limit, i_r, o_r in p['tiers']:
            if limit is None or prompt_total <= limit:
                in_rate, out_rate = i_r, o_r
                break
    else:
        in_rate, out_rate = p.get('input'), p.get('output')
    if in_rate is None or out_rate is None:
        return None
    audio_rate = p.get('audio_in', in_rate)
    cached_rate = p.get('cached', in_rate)
    text_in = max(0, input_tokens - audio_tokens - cached_tokens)
    usd = (text_in * in_rate + audio_tokens * audio_rate + cached_tokens * cached_rate
           + (output_tokens + thinking_tokens) * out_rate) / 1e6
    return round(usd, 6)


# ---------- 记录 ----------

def record(provider, model, purpose, ref=None, chain=None, input_tokens=0, output_tokens=0,
           audio_tokens=0, cached_tokens=0, thinking_tokens=0, audio_seconds=0,
           cost_usd=None):
    """记一次调用。cost_usd 传入 = 服务商报的实价；不传则按价格表估。永不抛异常。"""
    try:
        ctx = current()
        ref = ref or ctx.get('ref')
        chain = chain or ctx.get('chain')
        source = None
        if cost_usd is not None:
            source = 'reported'
        else:
            cost_usd = estimate_cost(model, input_tokens, output_tokens, audio_tokens,
                                     cached_tokens, thinking_tokens, audio_seconds)
            source = 'table' if cost_usd is not None else None
        with _lock, _conn() as c:
            c.execute('''INSERT INTO calls (ts, provider, model, purpose, ref, chain,
                             input_tokens, output_tokens, audio_tokens, cached_tokens,
                             thinking_tokens, audio_seconds, cost_usd, cost_source)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                      (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), provider, model or '',
                       purpose, ref, chain, int(input_tokens or 0), int(output_tokens or 0),
                       int(audio_tokens or 0), int(cached_tokens or 0),
                       int(thinking_tokens or 0), float(audio_seconds or 0), cost_usd, source))
    except Exception:
        pass


def record_gemini(response, model, purpose, **kw):
    """从 google-genai 的响应对象里取 usage_metadata 记一笔。"""
    try:
        um = getattr(response, 'usage_metadata', None)
        if um is None:
            return
        audio = 0
        for d in (getattr(um, 'prompt_tokens_details', None) or []):
            mod = getattr(d, 'modality', None)
            if str(getattr(mod, 'name', mod) or '').upper().endswith('AUDIO'):
                audio += int(getattr(d, 'token_count', 0) or 0)
        record('gemini', model, purpose,
               input_tokens=getattr(um, 'prompt_token_count', 0) or 0,
               output_tokens=getattr(um, 'candidates_token_count', 0) or 0,
               audio_tokens=audio,
               cached_tokens=getattr(um, 'cached_content_token_count', 0) or 0,
               thinking_tokens=getattr(um, 'thoughts_token_count', 0) or 0, **kw)
    except Exception:
        pass


def record_openai(data, provider, model, purpose, **kw):
    """从 OpenAI 兼容响应 JSON 的 usage 里记一笔。OpenRouter 带 usage.cost（请求要带
    "usage": {"include": true}），照抄为实价；阿里云没有 cost，按价格表（默认无价）。"""
    try:
        u = (data or {}).get('usage') or {}
        cost = u.get('cost')
        details = u.get('completion_tokens_details') or {}
        pdetails = u.get('prompt_tokens_details') or {}
        record(provider, model, purpose,
               input_tokens=u.get('prompt_tokens', 0) or 0,
               output_tokens=u.get('completion_tokens', 0) or 0,
               cached_tokens=pdetails.get('cached_tokens', 0) or 0,
               thinking_tokens=details.get('reasoning_tokens', 0) or 0,
               cost_usd=float(cost) if cost is not None else None, **kw)
    except Exception:
        pass


# ---------- 汇总 ----------

def _sum_rows(rows):
    out = []
    for r in rows:
        out.append({
            'key': r['k'], 'calls': r['n'],
            'input_tokens': r['i'] or 0, 'output_tokens': r['o'] or 0,
            'audio_seconds': round(r['a'] or 0, 1),
            'cost_usd': round(r['c'], 6) if r['c'] is not None else 0.0,
            'unpriced': r['u'] or 0,
        })
    return out


_GROUP_SQL = '''SELECT {col} AS k, COUNT(*) AS n, SUM(input_tokens) AS i, SUM(output_tokens) AS o,
                       SUM(audio_seconds) AS a, SUM(cost_usd) AS c,
                       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS u
                FROM calls WHERE ts >= ? GROUP BY {col} ORDER BY c DESC'''


def summary():
    """Settings → Costs 用：本月 / 全部，按服务商 / 用途 / 模型分组，近 30 天按日。"""
    now = datetime.now()
    month_start = now.strftime('%Y-%m-01 00:00:00')
    d30 = (now - timedelta(days=30)).strftime('%Y-%m-%d 00:00:00')
    with _conn() as c:
        def total(since):
            r = c.execute('''SELECT COUNT(*) n, SUM(cost_usd) c,
                                    SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) u,
                                    SUM(input_tokens) i, SUM(output_tokens) o
                             FROM calls WHERE ts >= ?''', (since,)).fetchone()
            return {'calls': r['n'], 'cost_usd': round(r['c'] or 0, 6), 'unpriced': r['u'] or 0,
                    'input_tokens': r['i'] or 0, 'output_tokens': r['o'] or 0}
        first = c.execute('SELECT MIN(ts) FROM calls').fetchone()[0]
        groups = {}
        for col in ('provider', 'purpose', 'model'):
            groups[col] = {
                'month': _sum_rows(c.execute(_GROUP_SQL.format(col=col), (month_start,))),
                'all': _sum_rows(c.execute(_GROUP_SQL.format(col=col), ('0000',))),
            }
        daily = [{'date': r['d'], 'cost_usd': round(r['c'] or 0, 6), 'calls': r['n']}
                 for r in c.execute('''SELECT substr(ts,1,10) d, SUM(cost_usd) c, COUNT(*) n
                                       FROM calls WHERE ts >= ? GROUP BY d ORDER BY d''', (d30,))]
        unpriced_models = [r[0] for r in c.execute(
            'SELECT DISTINCT model FROM calls WHERE cost_usd IS NULL ORDER BY model')]
    return {'since': first, 'month': total(month_start), 'all': total('0000'),
            'by': groups, 'daily': daily, 'unpriced_models': unpriced_models,
            'prices_path': PRICES_PATH}


def cost_for(ref=None, chain=None):
    """单条转写 / 单条博主分析的费用：{cost_usd, calls, unpriced}。chain 含其下所有期的转写。"""
    if not ref and not chain:
        return None
    with _conn() as c:
        if chain:
            r = c.execute('''SELECT COUNT(*) n, SUM(cost_usd) c,
                                    SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) u
                             FROM calls WHERE chain = ? OR ref = ?''', (chain, chain)).fetchone()
        else:
            r = c.execute('''SELECT COUNT(*) n, SUM(cost_usd) c,
                                    SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) u
                             FROM calls WHERE ref = ?''', (ref,)).fetchone()
    if not r or not r['n']:
        return None
    return {'cost_usd': round(r['c'] or 0, 6), 'calls': r['n'], 'unpriced': r['u'] or 0}


init()
