"""
极简 agent harness —— 确定性编排 + 模型只做叶子。

不做"自主 agent 自己决定下一步"（贵、不可控、爱跑偏）。这里只提供两块积木，
控制流全在调用方的 Python 里写死：

  fanout(items, fn, concurrency)   并发 map，保序；单个失败 → 该位置 None（不拖垮整批）
  agent(call_fn, prompt, schema=…) 一次 LLM 调用：重试 + 可选 JSON 必需键校验

call_fn 由调用方注入（如 analyze._llm），所以本模块**零业务依赖、可独立测试**，
也不会和 analyze / config 形成循环导入。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor


def fanout(items, fn, concurrency=6):
    """并发把 fn 施加到每个 item，**保序**返回。单个抛异常 → 该位置 None。"""
    items = list(items)
    if not items:
        return []

    def _run(i):
        try:
            return i, fn(items[i])
        except Exception:  # noqa: BLE001  单个失败不拖垮整批
            return i, None

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(items)))) as ex:
        for i, r in ex.map(_run, range(len(items))):
            results[i] = r
    return results


def _extract_json(raw):
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹和前言）。"""
    m = re.search(r'```json\s*(.*?)\s*```', raw or '', re.DOTALL)
    if m:
        raw = m.group(1)
    raw = (raw or '').strip()
    s = raw.find('{')
    if s != -1:
        raw = raw[s:]
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def agent(call_fn, prompt, *, schema=None, retries=2):
    """一次 LLM 调用。

    schema=None            → 返回原始文本（失败到底返回 ''）。
    schema=可迭代的必需键名 → 解析 JSON 并校验这些键都在，缺则重试；失败到底返回 None。
    call_fn(prompt) -> str 由调用方注入（保持本模块无业务依赖）。
    """
    for _ in range(retries + 1):
        try:
            out = call_fn(prompt)
        except Exception:  # noqa: BLE001
            out = None
        if not out:
            continue
        if schema is None:
            return out
        obj = _extract_json(out)
        if isinstance(obj, dict) and all(k in obj for k in schema):
            return obj
    return None if schema is not None else ''
