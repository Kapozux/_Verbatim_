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
from harness import fanout, agent

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

# ---- 转写体检：模型只「标」出识别垃圾，删除交给确定性代码 ----
REVIEW_PROMPT = """下面是一段语音转写（ASR），每行 [编号] 文字。找出【明显是识别垃圾】的段：静音幻听、模型卡死的重复循环、无意义碎片、整段逐字复读。

只输出 JSON：{{"drop": [[起,止], ...]}}（闭区间编号）。
铁律：
- 只删明显垃圾；真实说话内容一律保留，再短也留（"对""OK""好"）。
- 逐字复读只删重复的那几条，保留第一条。
- 拿不准 → 不删。**不要改写任何文字**，只返回要删的编号区间。

段：
{body}"""


def review_transcript(segments, preset=None, window=180):
    """模型体检：返回「该删的段索引」集合（模型只标，代码删）。

    分窗送（每窗 window 段）控 token；用便宜的抽取模型。
    无 key / 出错 / 无结果 → 返回空集合，绝不误伤。
    """
    if not segments:
        return set()
    provider, extract_model, _ = resolve_analysis(preset)
    drop = set()
    for base in range(0, len(segments), window):
        chunk = segments[base:base + window]
        body = '\n'.join(
            "[%d] %s" % (i, re.sub(r'\s+', '', (s.get('text') or ''))[:60])
            for i, s in enumerate(chunk))
        try:
            obj = agent(lambda p: _llm(p, provider, extract_model),
                        REVIEW_PROMPT.format(body=body), schema=['drop'])
        except Exception:  # noqa: BLE001
            obj = None
        for r in (obj or {}).get('drop', []) or []:
            try:
                a, b = int(r[0]), int(r[1])
            except (ValueError, TypeError, IndexError):
                continue
            for k in range(max(0, a), min(len(chunk), b + 1)):
                drop.add(base + k)
    return drop


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

# ---- 证伪层：从画像抽论断 → 逐条 skeptic 反驳 → 撑不住就砍 ----
CLAIMS_PROMPT = """下面是一份对「{author}」的人物画像。抽出其中**评判性 / 概括性的论断**——即对这个人下的判断（他的思维偏好、修辞风格、盲区、总体印象等），不是纯转述、不是小标题、不是元说明（如"转写质量说明"）。

只输出 JSON：{{"claims": ["论断一（尽量照抄画像原句）", "论断二", "..."]}}
只抽**可被证据检验**的判断句；最多 25 条，挑最实质的。

画像：
{portrait}"""

SKEPTIC_PROMPT = """（证伪 · 唱反调）下面有一条对「{author}」的论断，和一批从他视频抽出的证据卡。你的任务是**尽力反驳它**：卡片到底撑不撑得住？

只输出 JSON：{{"verdict": "成立 | 夸大 | 不成立", "why": "一句话依据", "support_quote": "能撑住它的逐字引文，没有留空"}}

判据：
- 成立：至少一张卡的引文能直接支撑。
- 夸大：有影子但说过头了（如证据只到"前后不一致"，论断却说成"虚伪/双标"）。
- 不成立：没有卡片支撑，或与卡片矛盾。
- **拿不准 → 往"夸大/不成立"靠，别轻易放行。**

论断：{claim}

证据卡（JSON）：
{cards}"""

REVISE_PROMPT = """下面是一份人物画像，和对其中若干论断的**证伪结果**。据此修订：
- 判"不成立"的论断：**删掉**。
- 判"夸大"的论断：**改写softer**，只说到证据撑得住的程度。
- 其余不动。保持原结构、原语言、原证据层标签。

输出修订后的画像正文，并在**末尾加一节**：
## 证伪留痕
逐条列出被删/改的论断 + 判定（不成立/夸大）+ 一句依据。

原画像：
{portrait}

证伪结果（JSON）：
{verdicts}"""

_SYNTH_CHAR_LIMIT = 600_000
_MAX_ATTEMPTS = 3

# 期数超过这个就走 map-reduce：分批做中间简报再合成，
# 而不是把几百期卡片硬塞一个 prompt（旧 _digest 会砍卡片、越多期砍越狠）。
_BATCH_SIZE = 24
_BRIEF_CONCURRENCY = 6

# ---- map 阶段：把一批期压成紧凑的中间简报（便宜模型、保留代表性引文供溯源）----
BRIEF_PROMPT = """你会收到「{author}」其中 {n} 期的**证据卡 + 修辞指标**。把这一批压成一份**紧凑的中间简报**（Markdown），供后续跨全部期综合用。**只提炼、不下最终结论、不评判整个人**。

要求：
- 按**反复出现的母题 / 立场 / 修辞手法**归拢，别按期逐条罗列。
- 每个母题下保留 1~2 条**最具代表性的逐字引文 + [时间戳] + 期名**，供后续溯源。
- 保留证据层标签（〔转写自证〕/〔他的主张〕/〔外部核实〕）。
- 末尾一行汇总本批的 hype / hedge / tradeoff 计数。
- 跟随卡片语言。

证据卡与指标（JSON）：
{digest}"""


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


def _agg_metrics(episodes):
    """跨期把 hype/hedge/tradeoff 加成**真实总数**（纯 Python，不靠模型估）。"""
    hype = hedge = tech = trade = 0
    for ep in episodes:
        m = ep.get('metrics') or {}
        hype += (m.get('hype') or {}).get('count', 0) or 0
        hedge += (m.get('hedge') or {}).get('count', 0) or 0
        td = m.get('tradeoff') or {}
        tech += td.get('tech_count', 0) or 0
        trade += td.get('with_tradeoff', 0) or 0
    return {'hype': hype, 'hedge': hedge, 'tech': tech, 'tradeoff': trade}


def _metrics_block(agg, n):
    return (f"【跨 {n} 期真实计数 · Python 统计非模型估算】"
            f"Hype {agg['hype']} 次 · Hedge {agg['hedge']} 次 · "
            f"介绍技术 {agg['tech']} 个其中提代价 {agg['tradeoff']} 个。"
            f"（修辞结论以此为准，别自己重估）")


def _batch_brief(batch, author, provider, model):
    """map 叶子：一批期的卡片 → 一份中间简报（失败返回 None）。"""
    try:
        return _llm(BRIEF_PROMPT.format(
            author=author, n=len(batch),
            digest=_digest(batch, _SYNTH_CHAR_LIMIT),
        ), provider, model)
    except Exception:  # noqa: BLE001
        return None


def _verify_portrait(portrait, digest, author, provider, extract_model, synth_model):
    """证伪层：抽论断 → 逐条 skeptic 反驳（并发、复用画像那步的 digest）→ 撑不住就砍。

    任何一步失败/无可检验论断/全部成立 → 原样返回，绝不把画像搞没。
    """
    obj = agent(lambda p: _llm(p, provider, extract_model),
                CLAIMS_PROMPT.format(author=author, portrait=portrait),
                schema=['claims'])
    claims = [c for c in ((obj or {}).get('claims') or []) if isinstance(c, str) and c.strip()]
    if not claims:
        return portrait

    def _skeptic(claim):
        return agent(lambda p: _llm(p, provider, extract_model),
                     SKEPTIC_PROMPT.format(author=author, claim=claim, cards=digest),
                     schema=['verdict'])

    verdicts = fanout(claims, _skeptic, concurrency=_BRIEF_CONCURRENCY)
    bad = [{'claim': c, 'verdict': v.get('verdict'), 'why': v.get('why', '')}
           for c, v in zip(claims, verdicts)
           if v and v.get('verdict') in ('夸大', '不成立')]
    if not bad:
        return portrait  # 全成立，不动

    revised = _llm(REVISE_PROMPT.format(
        portrait=portrait,
        verdicts=json.dumps(bad, ensure_ascii=False, indent=1),
    ), provider, synth_model)
    return revised or portrait


def _build_digest(episodes, author, provider, extract_model):
    """把 N 期证据卡压成合成层的输入：少量期直接进卡片；多期走 map-reduce
    分批简报，避免几百期硬塞一个 prompt 被砍。合成和各镜头共用。"""
    if len(episodes) <= _BATCH_SIZE:
        return _digest(episodes, _SYNTH_CHAR_LIMIT)
    batches = [episodes[i:i + _BATCH_SIZE]
               for i in range(0, len(episodes), _BATCH_SIZE)]
    briefs = fanout(
        batches,
        lambda b: _batch_brief(b, author, provider, extract_model),
        concurrency=_BRIEF_CONCURRENCY,
    )
    briefs = [b for b in briefs if b]
    if briefs:
        return '\n\n---\n\n'.join(
            f'## 简报 {i + 1}/{len(briefs)}\n{b}' for i, b in enumerate(briefs))
    return _digest(episodes, _SYNTH_CHAR_LIMIT)  # 全批失败兜底


def synthesize(episodes, author='该博主', critique_level='analytical',
               impression_bias='', preset=None, self_verify=False):
    """N 期证据卡 → 一份人物画像。critique_level: descriptive/analytical/sharp。

    self_verify=True：合成后再跑一轮证伪——抽出每条论断、逐条 skeptic 拿证据反驳，
    证据撑不住的删、夸大的改软，末尾留痕。

    impression_bias：仅供校准回归测试用——注入一条语气基线看结论会不会跟着漂。
    """
    episodes = [e for e in episodes if e and e.get('cards') is not None]
    if not episodes:
        raise RuntimeError('没有可综合的证据卡')
    level = critique_level if critique_level in _TONE else 'analytical'
    impression = f'（综合印象的语气基线：{impression_bias}）' if impression_bias else ''
    provider, extract_model, synth_model = resolve_analysis(preset)
    agg = _agg_metrics(episodes)
    digest = _build_digest(episodes, author, provider, extract_model)

    portrait = _llm(PORTRAIT_PROMPT.format(
        author=author, n=len(episodes), rules=_rules(),
        level=level, tone=_TONE[level], impression=impression,
        digest=_metrics_block(agg, len(episodes)) + '\n\n' + digest,
    ), provider, synth_model)

    if self_verify:
        portrait = _verify_portrait(
            portrait, digest, author, provider, extract_model, synth_model)
    return portrait


# ==== 镜头（lenses）：同一批证据卡 + 不同的合成 prompt，边际成本≈0 ====
_LENS_GROUND = """铁律（违反就是编造真人 = 造谣，不是解读）：
- 每条判断/吐槽/结论**必须挂得回某张卡的原话**（给〔证据层〕和「原话」），点不回引文的**一律不许写**。
- 他的主张挂〔他的主张〕，别洗成客观事实；跨语境不一致如实说"不一致"，别升级成诛心。
- 只评他的【内容/主张/修辞/自相矛盾】，不捏造私德、隐私、人身。"""

LENSES = {
    'roast': """你是吐槽大会的毒舌选手。下面是「{author}」{n} 期视频的证据卡。写一段辛辣的 roast，像脱口秀 roast 那样损、刻薄、可以粗俗——**但最狠的弹药永远是"他自己打自己脸"**（跨期自相矛盾、又当又立）。

""" + _LENS_GROUND + """

结构：
## 罪状清单
（每条：一句损的话 —— 挂〔证据层〕「他的原话」，矛盾就并列两句原话）
## 总结陈词
（一段，收个狠的）

证据卡（JSON）：
{digest}""",

    'craft': """你是内容创作教练。下面是「{author}」{n} 期视频的证据卡。拆解**他是怎么做内容/写稿子的**，给想偷师、想模仿他的人一份可操作的说明书。

""" + _LENS_GROUND + """

结构（每条都挂原话举例）：
## 开头怎么钩人
## 常用结构 / 套路
## 修辞与话术手法（用 hype/hedge/tradeoff 分布佐证）
## 节奏与信息密度
## 可复制的招 vs 学不来的
证据卡（JSON）：
{digest}""",

    'fun': """你是选题/追更判断官。下面是「{author}」{n} 期视频的证据卡。回答一个问题：**这个人有意思吗？看点在哪？**

""" + _LENS_GROUND + """

结构：
## 看点在哪（幽默/反转/信息量/人设魅力，挂原话）
## 什么样的人会爱看 / 会划走
## 最出彩 vs 最无聊的部分
## 一句话结论：值不值得追
证据卡（JSON）：
{digest}""",

    'quotes': """下面是「{author}」{n} 期视频的证据卡。挑出他**最有代表性 / 最出圈 / 最能体现其风格**的原话，做一份金句集。

铁律：**只用卡片里的逐字原话**，不许改写、不许编。每条标出处集名（若有）。

结构：
## 金句集
- 按主题归类，每条：「逐字原话」—— 一句话点评它为什么有代表性
证据卡（JSON）：
{digest}""",

    'worldview': """下面是「{author}」{n} 期视频的证据卡。把他对各类事物的立场整理成一张**世界观地图**。

""" + _LENS_GROUND + """

结构：
## 世界观地图
| 议题 | 他的立场 | 证据〔层〕「原话」 |
（每行一个议题；跨期不一致的，在立场里如实写"不一致：A / B"并各挂原话）
## 底层母题
（这些立场背后反复出现的 1~3 个底层假设）
证据卡（JSON）：
{digest}""",
}

LENS_META = {
    'roast': ('🔥 锐评 / 吐槽', '拿证据损他，最狠的是他自己打脸的地方'),
    'craft': ('✍️ 写作 / 内容拆解', '他怎么做内容，给想偷师的人'),
    'fun': ('😂 看点 / 有意思吗', '值不值得追'),
    'quotes': ('💬 金句集', '他最有代表性的原话'),
    'worldview': ('🗺 世界观地图', '他对各类事的立场一张表'),
}


def render_lens(episodes, lens, author='该博主', preset=None):
    """同一批证据卡 → 指定镜头的报告（Markdown）。复用合成层的证据卡逻辑。"""
    episodes = [e for e in episodes if e and e.get('cards') is not None]
    if not episodes:
        raise RuntimeError('没有可用的证据卡')
    tpl = LENSES.get(lens)
    if not tpl:
        raise ValueError(f'未知镜头：{lens}')
    provider, extract_model, synth_model = resolve_analysis(preset)
    agg = _agg_metrics(episodes)
    digest = _build_digest(episodes, author, provider, extract_model)
    return _llm(tpl.format(
        author=author, n=len(episodes),
        digest=_metrics_block(agg, len(episodes)) + '\n\n' + digest,
    ), provider, synth_model)


# ==== 小红书笔记分析（多模态：读图 + 评论 → 逐篇结构化 → 聚合报告）====
import glob as _glob
import csv as _csv

_XHS_IMG_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.webp': 'image/webp', '.heic': 'image/heic'}


def _call_gemini_mm(prompt, image_paths, model=None):
    """多模态 Gemini：文字 + 图片一起送。带模型降级链、重试。返回文本。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY 未设置')
    from google.genai import types
    client = make_gemini_client(api_key)
    parts = [prompt]
    for p in (image_paths or [])[:12]:          # 每篇最多喂 12 张，控 token
        try:
            ext = os.path.splitext(p)[1].lower()
            with open(p, 'rb') as f:
                parts.append(types.Part.from_bytes(
                    data=f.read(), mime_type=_XHS_IMG_MIME.get(ext, 'image/png')))
        except Exception:  # noqa: BLE001
            pass
    ladder = []
    for m in [model or GEMINI_EXTRACT_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in ladder:
            ladder.append(m)
    last_err = None
    for m in ladder:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.models.generate_content(model=m, contents=parts)
                text = (resp.text or '').strip()
                if text:
                    return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
    raise RuntimeError(f'Gemini 多模态调用失败：{last_err}')


XHS_EXTRACT_PROMPT = """这是一篇小红书笔记：图片 + 一级评论 + 元信息。**图片里往往是正文/截图/信息主体，务必仔细读图**。

只输出 JSON：
{{
  "summary": "这篇讲了什么，一两句",
  "topic": "它属于什么话题/子类",
  "key_points": ["正文（含图片内容）里的关键信息点，逐条"],
  "author_stance": "发帖人的立场/态度（读不出留空）",
  "notable_comments": ["评论区最有信息量/代表性的几条原话"],
  "entities": ["提到的具体实体：学校/公司/人名/产品/地名等"],
  "sentiment": "评论区整体情绪：正面/负面/混合/中性",
  "relevant": true
}}
铁律：只写图片/评论里**真有**的，读不出来就留空/空数组，别编。跟随内容语言。

元信息：{meta}

评论（前 {ncmt} 条）：
{comments}"""


def analyze_xhs_note(note_dir):
    """一篇笔记 → 多模态结构化抽取（读图+评论）。失败返回 None。"""
    meta = {}
    try:
        with open(os.path.join(note_dir, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    comments = []
    cpath = os.path.join(note_dir, 'comments.csv')
    if os.path.isfile(cpath):
        try:
            with open(cpath, encoding='utf-8-sig') as f:   # -sig 去掉表头 BOM
                for row in _csv.DictReader(f):
                    # 列名是 comment_text（老数据可能是 text）
                    t = (row.get('comment_text') or row.get('text') or '').strip()
                    if not t:
                        continue
                    lk = (row.get('like_count') or '').strip()
                    comments.append((int(lk) if lk.isdigit() else 0, t))
            comments.sort(key=lambda x: -x[0])             # 高赞评论排前，更有代表性
            comments = [f'(赞{lk}) {t}' if lk else t for lk, t in comments]
        except Exception:  # noqa: BLE001
            pass
    imgs = sorted(sum((_glob.glob(os.path.join(note_dir, f'*{e}'))
                       for e in _XHS_IMG_MIME), []))
    prompt = XHS_EXTRACT_PROMPT.format(
        meta=json.dumps(meta, ensure_ascii=False),
        ncmt=min(60, len(comments)),
        comments='\n'.join('- ' + c for c in comments[:60])[:8000])
    try:
        data = _parse_json_obj(_call_gemini_mm(prompt, imgs))
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        return None
    data['_note_id'] = os.path.basename(note_dir.rstrip('/'))
    data['_title'] = meta.get('title', '')
    data['_n_images'] = len(imgs)
    data['_n_comments'] = len(comments)
    return data


XHS_REPORT_PROMPT = """你收到从小红书采集的 {n} 篇笔记的结构化抽取（每篇：话题/要点/立场/代表评论/实体/情绪）。这些笔记来自关键词搜索、围绕某个话题。写一份中文调研报告（Markdown）。

要求：
- 先判断这批在聊什么主话题（可能不止一个，按簇分）。
- 按**反复出现的主题/模式**组织，别逐篇复述。
- 每个结论尽量挂**具体例子**（哪篇的要点 / 哪条评论原话），别空泛。
- 有可数的就给数字（多少篇提到 X、情绪分布）。
- 只写抽取里**真有**的，不编、不脑补。

结构（按内容灵活取舍）：
# {title}
## 概览（这批在聊什么、样本规模）
## 主要主题 / 模式
## 值得注意的案例 / 金句
## 情绪与分歧
## 小结

抽取结果（JSON）：
{digest}"""


def xhs_report(note_dirs, title='小红书调研报告', on_progress=None):
    """一批笔记目录 → (报告 Markdown, 逐篇抽取列表)。逐篇多模态抽取（并发）→ 聚合。"""
    done = [0]

    def _one(d):
        r = analyze_xhs_note(d)
        done[0] += 1
        if on_progress:
            on_progress(done[0], len(note_dirs))
        return r

    extractions = [e for e in fanout(note_dirs, _one, concurrency=_BRIEF_CONCURRENCY) if e]
    if not extractions:
        raise RuntimeError('没有可分析的笔记（抽取全失败）')
    digest = json.dumps(extractions, ensure_ascii=False, indent=1)[:_SYNTH_CHAR_LIMIT]
    report = _call_gemini(XHS_REPORT_PROMPT.format(
        n=len(extractions), title=title, digest=digest))
    return report, extractions
