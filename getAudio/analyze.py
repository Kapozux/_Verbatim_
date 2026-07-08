"""
博主解读：证据卡两段式 —— 逐期只做中立抽取，评判/风格采纳全部上移到合并层。

  1. analyze_episode()  逐期 → 结构化"证据卡"（原子观察 + 逐字引文 + 时间戳 + 证据层）
                        + 三个可数修辞指标（hype/hedge/tradeoff）+ 疑似 ASR 误识实体。
                        零评判、离线。
  2. synthesize()       只吃卡片（不再吃全文）→ 人物画像。母题归纳、风格采纳、批判都在这层，
                        每条结论必须能点回某张卡的引文；批判强度由 critique_level 调（只调语气，
                        不调证据松紧）。
可选 verify=True：逐期附一节联网核实脚注，只作脚注、绝不进卡片/画像结论。
"""

import json
import os
import re
import time
from datetime import datetime

from config import (GEMINI_API_KEY, GEMINI_ANALYSIS_MODEL,
                    GEMINI_EXTRACT_MODEL, GEMINI_FALLBACK_MODELS,
                    DASHSCOPE_API_KEY, ALIYUN_COMPAT_BASE, resolve_analysis,
                    make_gemini_client)

_CALIBRATION_RULES = """【校准兜底 · 必守】
- 今天是 {today}。内容可能涉及你知识截止之后的论文/模型/事件。**不认识 ≠ 不存在 ≠ 编造。**
- 不许判"造假/虚构"。涉及外部事实而无法确认的，一律记"未能核实"，不下真伪判决。
- 保留说话人的存疑标注；区分"他的主张 / 他转述来源 / 他自己标存疑"。"""

# ---- 逐期：抽取证据卡（中立、结构化、离线、零评判）----
CARDS_PROMPT = """你的任务是**中立抽取证据卡**：不评价、不判断、不润色、不下结论。从「{author}」这期《{title}》的转写抽出结构化证据。

严格只输出 JSON：
{{
  "cards": [
    {{"obs": "一条原子观察（他的一个主张／一个修辞手法／一个叙事框架，客观描述，不带评价词）",
      "quote": "支撑它的逐字原话（照抄转写，别改写）",
      "timestamp": "该原话的时间戳（从转写行首 [MM:SS] 取，取不到留空）",
      "layer": "转写自证 | 他的主张 | 外部核实"}}
  ],
  "metrics": {{
    "hype":     {{"count": 0, "examples": ["最高级/军备竞赛类原话，如 碾压/秒杀/新王/无敌/彻底取代/判死刑"]}},
    "hedge":    {{"count": 0, "examples": ["待验证/尚未/存疑/未开源/自己核 类原话"]}},
    "tradeoff": {{"tech_count": 0, "with_tradeoff": 0, "examples": ["介绍某技术时确实提到了代价/权衡的原话"]}}
  }},
  "asr_suspects": ["疑似语音识别错误的实体名，照抄听错的样子（如 千万三 / Dora-CIT）"]
}}

规则（必守）：
- layer：修辞/叙事/措辞观察 = 转写自证；他下的事实/性能/预测判断 = 他的主张；只有你确凿的外部事实核对才 = 外部核实（默认几乎不用）。
- 卡片里**不许出现评价词**（不写"浮夸/严谨/高明/盲区"），只客观描述 + 引文。评价是合并层的事。
- quote 必须是转写里的逐字原话，能让人点回原文。
- metrics：hype/hedge 的 count = 出现次数，examples 放几条代表原话；tradeoff 的 tech_count = 他介绍了几个技术，with_tradeoff = 其中几个提了代价。
- asr_suspects：只放疑似识别错误的**实体名**——这是转写质量问题，不是他的语言风格。
- 今天是 {today}，不认识的新词照抄，别判真假、别猜"正确写法"。

转写：
{transcript}"""

# ---- 可选：联网核实（仅脚注，不进卡片/画像）----
VERIFY_PROMPT = """（联网核实 · 仅作脚注）下面是从一期抽出的、依赖外部事实的 claim。用 Google 搜索逐条核对，只输出一节 Markdown：

## 事实核对（脚注 · 不改变对这个人的解读）
- 每条：`claim → 真实 / 未找到 / 有出入`，加一句依据（给得出源就给源）。
- 疑似语音识别错误的实体，先搜正确写法再核。

claims：
{claims}"""

# ---- 合并层：人物画像（只吃卡片；批判档位只调语气）----
_TONE = {
    'descriptive': '只描述、不评判：呈现他的母题/风格/指标分布，不下价值判断、不展开"盲区/缺陷"。',
    'analytical': '可指出系统性盲区与回避，但归因克制：只依据证据说"他在 X 上回避 Y"，不猜动机、不夸大。',
    'sharp': '可以明确下判断、语气锋利直接。但证据标准丝毫不变：每条评判仍须挂证据层标签、仍须能点回引文——提升的只是语气强度，不是证据松紧。',
}

PORTRAIT_PROMPT = """你会收到「{author}」{n} 期的**证据卡 + 修辞指标**（不是全文）。基于这些卡片综合成一份人物画像（Markdown）：这个人怎么看、怎么思考他的领域。

**语言：跟随卡片语言；下面的小标题是示例，请翻成对应语言。**

{rules}

【评判档位：{level}】{tone}

【硬约束 · 任何档位都必守】
- 每条评判性结论必须挂证据层标签〔转写自证〕/〔他的主张〕/〔外部核实〕，且能点回某张卡的引文；点不回引文的**不许写**。
- 他的判断/预测一律挂〔他的主张〕，**不许**洗成你自己的客观结论。
- 涉及外部事实的只标"未核实/流行说法"，**不要背书**（别把营销回声当已核实）。
- 只做行为对比、**不猜动机**（可写"他只在非开源项目上 hedge、别处浮夸"，不许写"为维持人设"）。
- 区分"他个人的选择性回避"与"这个体裁天生不做的事"，后者别算进他的盲区。
- 跨语境的态度不一致，如实描述为"不一致"（如"对开源项目 hedge、对闭源项目浮夸"）；**不许**升级成"双重标准/知行不一/虚伪"这类道德指控——证据只支撑到不一致，支撑不到诛心。
- 修辞结论用给到的 hype/hedge/tradeoff 跨期分布支撑；**不要**输出"客观性/可信度"这类合成总分，只用可数分项。
- 疑似 ASR 误识的实体名属于转写质量，放最后的「转写质量说明」里，**不许**当成他的语言风格。

# {author}：人物解读（基于 {n} 期）

## 他怎么看他的领域
他对这个领域的总体世界观/判断（记住这些是他的，挂〔他的主张〕）。

## 思维方法与母题
跨期反复出现的思考方式、判断偏好、执念。以〔转写自证〕为主。

## 叙事与修辞风格
用 hype / hedge / tradeoff 的跨期分布 + 卡片引文支撑：他在哪些事上浮夸、在哪些事上 hedge、介绍技术时提不提代价。

## 系统性盲区
（descriptive 档可略过或只陈述事实；analytical/sharp 才展开。区分个人回避 vs 体裁固有。）

## 综合印象（本节为 {level} 档下的判断，换档/换语气可能变化）
读完这些他是个怎样的创作者/思考者。分析者视角，但把"他的主张"和"你的判断"分清。尽量把总体判断锚到可数指标（hype/hedge/tradeoff 的跨期分布）上，而不是笼统的情绪定性。{impression}

## 转写质量说明
本报告基于的转写可能含语音识别错误，下列实体名可能失真（仅元数据，非其风格）：按卡片里的 asr_suspects 汇总。

证据卡与指标（JSON）：
{digest}"""

_SYNTH_CHAR_LIMIT = 600_000
_MAX_ATTEMPTS = 3


def _rules():
    return _CALIBRATION_RULES.format(today=datetime.now().strftime('%Y-%m-%d'))


def _call_gemini(prompt, grounded=False, model=None):
    """带重试的 Gemini 调用。grounded=True 开 Google 搜索。model 缺省用合成模型。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY 未设置')

    client = make_gemini_client(api_key)
    cfg = None
    if grounded:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )

    # 模型降级链：主模型 → 兜底模型（去重保序）。某个模型限流/挂了就换下一个。
    ladder = []
    for m in [model or GEMINI_ANALYSIS_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in ladder:
            ladder.append(m)

    last_err = None
    for m in ladder:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.models.generate_content(
                    model=m, contents=prompt, config=cfg
                )
                text = (resp.text or '').strip()
                if text:
                    return text
                last_err = RuntimeError('Gemini 返回空文本')
            except Exception as e:  # noqa: BLE001
                last_err = e
                s = str(e)
                transient = any(k in s for k in (
                    '429', 'RESOURCE_EXHAUSTED', '503', 'UNAVAILABLE',
                    'overloaded', 'deadline', 'timeout'))
                if not transient:
                    break            # 模型名错/安全拦截等：别耗重试，直接换下一个模型
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(5 * attempt)
        # 该模型跑不通 → 降级到链条里的下一个
    raise RuntimeError(f'Gemini 调用失败（已试模型 {ladder}）: {last_err}')


def _call_openai_compat(prompt, model, base_url, api_key):
    """OpenAI 兼容端点（阿里云百炼：DeepSeek/Qwen/Kimi/GLM）。带重试，返回文本或抛异常。"""
    import requests
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=600)
            if r.status_code == 200:
                txt = ((r.json().get('choices') or [{}])[0]
                       .get('message', {}).get('content') or '').strip()
                if txt:
                    return txt
                last_err = RuntimeError('空文本')
            else:
                last_err = RuntimeError(f'{r.status_code}: {r.text[:200]}')
                if r.status_code not in (429, 500, 502, 503, 504):
                    break               # 4xx（key 错/模型名错）别耗重试
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(5 * attempt)
    raise RuntimeError(f'阿里云({model}) 调用失败: {last_err}')


def _llm(prompt, provider, model, grounded=False):
    """按 provider 分发：gemini 走 google-genai，aliyun 走百炼 OpenAI 兼容。"""
    if provider == 'aliyun':
        key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
        if not key:
            raise RuntimeError('DASHSCOPE_API_KEY 未设置（阿里云分析需要）')
        return _call_openai_compat(prompt, model, ALIYUN_COMPAT_BASE, key)
    return _call_gemini(prompt, grounded=grounded, model=model)


def _parse_json_obj(raw):
    m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    raw = raw.strip()
    start = raw.find('{')
    if start != -1:
        raw = raw[start:]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_cards(title, transcript_text, author, provider, model):
    """逐期抽取证据卡。返回 {cards, metrics, asr_suspects} 或 None。"""
    prompt = CARDS_PROMPT.format(
        author=author, title=title, transcript=transcript_text,
        today=datetime.now().strftime('%Y-%m-%d'),
    )
    try:
        data = _parse_json_obj(_llm(prompt, provider, model))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault('cards', [])
    data.setdefault('metrics', {})
    data.setdefault('asr_suspects', [])
    return data


def _cards_markdown(title, data):
    """把证据卡渲染成可读的逐期文档（可回溯，无评判）。"""
    lines = [f'# {title}', '', '## 证据卡']
    for c in data.get('cards', []):
        ts = f" [{c['timestamp']}]" if c.get('timestamp') else ''
        q = c.get('quote', '')
        lines.append(f"- 〔{c.get('layer', '?')}〕{c.get('obs', '')}"
                     + (f"  ——「{q}」{ts}" if q else ''))
    m = data.get('metrics', {}) or {}
    lines += ['', '## 修辞指标']
    h, hd, td = m.get('hype', {}), m.get('hedge', {}), m.get('tradeoff', {})
    lines.append(f"- Hype（最高级/军备竞赛词）：{h.get('count', 0)} 次")
    lines.append(f"- Hedge（待验证/存疑类）：{hd.get('count', 0)} 次")
    lines.append(f"- 提代价/权衡：介绍 {td.get('tech_count', 0)} 个技术中 {td.get('with_tradeoff', 0)} 个提了")
    asr = data.get('asr_suspects', []) or []
    if asr:
        lines += ['', '## 转写质量（疑似识别错误，非风格）', '- ' + '、'.join(asr)]
    return '\n'.join(lines)


def _external_claims(data):
    return [c.get('obs') or c.get('quote') for c in data.get('cards', [])
            if c.get('layer') == '他的主张' and (c.get('obs') or c.get('quote'))]


def analyze_episode(title, transcript_text, author='该博主', verify=False, preset=None):
    """逐期 → {title, cards, metrics, asr_suspects, markdown}。verify 时附核实脚注。"""
    provider, extract_model, _ = resolve_analysis(preset)
    data = _extract_cards(title, transcript_text, author, provider, extract_model) or {
        'cards': [], 'metrics': {}, 'asr_suspects': [],
    }
    md = _cards_markdown(title, data)

    if verify:
        claims = _external_claims(data)
        if claims:
            try:
                footnote = _call_gemini(
                    VERIFY_PROMPT.format(
                        claims='\n'.join(f'- {c}' for c in claims[:40])),
                    grounded=True,
                )
                md = md + '\n\n' + footnote
            except Exception:  # noqa: BLE001
                pass

    return {
        'title': title,
        'cards': data.get('cards', []),
        'metrics': data.get('metrics', {}),
        'asr_suspects': data.get('asr_suspects', []),
        'markdown': md,
    }


def _digest(episodes, budget):
    """把各期卡片压成合并层的输入（不含全文）。超预算就每期少留几张卡。"""
    keep = 100
    while keep > 3:
        items = []
        for ep in episodes:
            items.append({
                'title': ep.get('title', ''),
                'cards': (ep.get('cards') or [])[:keep],
                'metrics': ep.get('metrics', {}),
                'asr_suspects': ep.get('asr_suspects', []),
            })
        s = json.dumps(items, ensure_ascii=False, indent=1)
        if len(s) <= budget:
            return s
        keep = keep // 2
    return s  # 尽力而为


def synthesize(episodes, author='该博主', critique_level='analytical',
               impression_bias='', preset=None):
    """N 期证据卡 → 一份人物画像。critique_level: descriptive/analytical/sharp。

    impression_bias：仅供校准回归测试用——在"综合印象"注入一条语气基线
    （如"中性 / 略带怀疑 / 略带欣赏"），看结论会不会跟着漂。默认空。
    """
    episodes = [e for e in episodes if e and e.get('cards') is not None]
    if not episodes:
        raise RuntimeError('没有可综合的证据卡')
    level = critique_level if critique_level in _TONE else 'analytical'
    impression = f'（综合印象的语气基线：{impression_bias}）' if impression_bias else ''
    provider, _, synth_model = resolve_analysis(preset)
    return _llm(PORTRAIT_PROMPT.format(
        author=author, n=len(episodes), rules=_rules(),
        level=level, tone=_TONE[level], impression=impression,
        digest=_digest(episodes, _SYNTH_CHAR_LIMIT),
    ), provider, synth_model)
