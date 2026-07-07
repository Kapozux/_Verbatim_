"""
博主解读：分层 pipeline —— 从他的原话里读出"这个人怎么思考他的领域"。

默认离线、不判对错。分三层，避免"一个大 prompt 焊死"导致的诬告/复读来回横跳：
  1. extract()          中立抽取：他说了什么(带来源+认识论状态) + 修辞观察。不评价、不核实。
  2. analyze_transcript() 逐期解读：只吃抽取料 + 转写，文本内推断他怎么讲/怎么想。analyst 视角，不联网。
  3. synthesize()       人物画像：跨期综合，每条结论强制标来源层（转写自证/他的主张/外部核实）。
可选：verify=True 时，额外一趟联网核实，结果只作"脚注"，绝不进人物结论当论据。
"""

import json
import os
import re
import time
from datetime import datetime

from config import GEMINI_API_KEY, GEMINI_ANALYSIS_MODEL, make_gemini_client

# 校准兜底（任何一层都适用）：unknown≠false、保留存疑、ASR 提醒。
_CALIBRATION_RULES = """【校准兜底 · 必守】
- 今天是 {today}。内容可能涉及你知识截止之后的论文/模型/事件。**不认识 ≠ 不存在 ≠ 编造。**
- 不许判"造假/虚构"。涉及外部事实而你无法确认的，一律记为"未能核实"，不下真伪判决。
- 保留说话人的存疑标注；区分"他的主张 / 他转述来源 / 他自己标存疑"，别塌缩。
- 转写可能有语音识别错误（专名/英文/模型名易听错）：按疑似误识处理，别把糊音当实质主张。"""

# ---- 层 1：抽取（中立、结构化、离线）----
EXTRACT_PROMPT = """你的任务是**中立抽取**，不评价、不核实、不润色。从「{author}」这期《{title}》的转写里抽出结构化原料。

严格输出 JSON（只输出 JSON，不要别的文字）：
{{
  "claims": [{{"text": "他表达的一个具体主张/结论(简述)", "quote": "支撑的原话片段", "status": "主张|转述|存疑"}}],
  "rhetoric": [{{"obs": "一个修辞/叙事观察(比喻/英雄反派/标题党/demo怎么摆/夸张措辞)", "quote": "原话片段"}}],
  "topics": ["这期涉及的技术/主题/作品"],
  "framing": {{"heroes": ["被捧的对象"], "villains": ["被贬/被宣判淘汰的对象"]}}
}}

规则：
- status：他自己下的判断=主张；转述某论文/某来源=转述；他自己说"待核实/没验证/存疑"=存疑。
- 只抽转写里真有的，别脑补。quote 用原话别改写。
- 语音识别可能有错：照抄转写原样，别猜"正确写法"（那是后面的事）。今天是 {today}，不认识的新东西照抄，别判真假。

转写：
{transcript}"""

# ---- 层 2：逐期解读（analyst 视角，文本内，离线）----
READ_PROMPT = """你在做的是"解读一个人怎么思考他的领域"，不是给他打分。基于下面的抽取原料 + 转写，写这一期的解读（Markdown）。你是分析者、有自己的视角，但**只在文本内推断，不联网、不判外部事实真假**。

**语言：整份文档（含小标题）用与转写相同的语言。下面的小标题是示例，请翻成对应语言。**

{rules}

# {title}

## 他讲了什么
这期主题 + 主要 claim。涉及外部事实的标"（他称，未核实）"；他自己标了存疑的如实写"（他自己标了存疑）"。

## 他怎么讲的（风格与修辞）
从修辞原料出发：比喻、叙事框架、英雄/反派、标题与 demo 手法、夸张 vs 克制。**这层是转写自证的，放心写实。**

## 他怎么思考（方法与母题）
论证套路、反复出现的思维母题、他把技术转译给谁/靠什么手法、系统性回避什么（如 trade-off）。

## 存疑与边界
需要外部核实才能定的（列为"他的 claim，未核实"，不判真伪）；以及这一期看不出的。

抽取原料(JSON)：
{extraction}

转写：
{transcript}"""

# ---- 层 3：人物画像（跨期综合，强制标层）----
PORTRAIT_PROMPT = """你会收到「{author}」{n} 期的逐期解读。综合成一份**人物画像**（Markdown）：这个人怎么看、怎么思考他的领域。

**语言：整份文档（含小标题）用与下方解读相同的语言。下面的小标题是示例，请翻成对应语言。**

{rules}
- **每条结论必须自带来源层标签**：〔转写自证〕稳的风格/叙事观察；〔他的主张〕他的判断/预测（转述，未核实）；〔外部核实〕若有。
- **严禁**把他的判断/预测洗成你自己的客观洞察——凡他的世界观一律挂〔他的主张〕。
- 别因为某个无法核实的说法在多期反复出现就当成坐实的事实（共同盲区 ≠ 证据）。

# {author}：人物解读（基于 {n} 期）

## 他怎么看他的领域
他对这个领域的总体世界观/判断（记住这些是他的，挂〔他的主张〕）。

## 思维方法与母题
跨期反复出现的思考方式、第一性原理/类比/判断偏好、执念。以〔转写自证〕为主。

## 叙事与修辞风格
他怎么讲：英雄叙事、浮夸 vs 克制的分布（在哪些事上 hedge、在哪些事上"碾压/秒杀"）、demo 手法、标题风格。〔转写自证〕。

## 系统性盲区
他反复回避或轻视什么（trade-off、反例、失败案例）。

## 综合印象
读完这些，他是个怎样的创作者/思考者。分析者视角，但把"他的主张"和"你的判断"分清。

逐期解读：
{analyses}"""

# ---- 可选层：联网核实（仅脚注，不进结论）----
VERIFY_PROMPT = """（联网核实 · 仅作脚注）下面是从一期内容抽出的、依赖外部事实的 claim。请用 Google 搜索逐条核对，只输出一节 Markdown：

## 事实核对（脚注 · 不改变对这个人的解读）
- 每条：`claim → 真实 / 未找到 / 有出入`，加一句依据。
- 疑似语音识别错误的实体，先搜正确写法再核。

claims：
{claims}"""

_SYNTH_CHAR_LIMIT = 600_000
_MAX_ATTEMPTS = 3


def _rules():
    return _CALIBRATION_RULES.format(today=datetime.now().strftime('%Y-%m-%d'))


def _call_gemini(prompt, grounded=False):
    """带重试的 Gemini 调用。grounded=True 开 Google 搜索。返回文本或抛异常。"""
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

    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_ANALYSIS_MODEL, contents=prompt, config=cfg
            )
            text = (resp.text or '').strip()
            if text:
                return text
            last_err = RuntimeError('Gemini 返回空文本')
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(5 * attempt)
    raise RuntimeError(f'Gemini 调用失败（已重试 {_MAX_ATTEMPTS} 次）: {last_err}')


def _parse_json_obj(raw):
    """从模型输出里抠出第一个 JSON 对象，失败返回 None。"""
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


def extract(title, transcript_text, author='该博主'):
    """层 1：中立抽取，返回 dict 或 None（失败时调用方回落到直接看转写）。"""
    prompt = EXTRACT_PROMPT.format(
        author=author, title=title, transcript=transcript_text,
        today=datetime.now().strftime('%Y-%m-%d'),
    )
    try:
        return _parse_json_obj(_call_gemini(prompt))
    except Exception:  # noqa: BLE001  抽取失败不致命，回落
        return None


def _external_claims(ext):
    """从抽取里挑出依赖外部事实的 claim（主张/转述），供可选核实用。"""
    out = []
    for c in (ext or {}).get('claims', []) or []:
        if c.get('status') in ('主张', '转述') and c.get('text'):
            out.append(c['text'])
    return out


def analyze_transcript(title, transcript_text, author='该博主', verify=False):
    """层 1→2：抽取 → 逐期解读。verify=True 时附加一节联网核实脚注（不进结论）。"""
    ext = extract(title, transcript_text, author)
    extraction = (json.dumps(ext, ensure_ascii=False, indent=2)
                  if ext else '（抽取未成功，请直接基于下面的转写解读）')

    read = _call_gemini(READ_PROMPT.format(
        title=title, rules=_rules(),
        extraction=extraction, transcript=transcript_text,
    ))

    if verify:
        claims = _external_claims(ext)
        if claims:
            try:
                footnote = _call_gemini(
                    VERIFY_PROMPT.format(
                        claims='\n'.join(f'- {c}' for c in claims[:40])),
                    grounded=True,
                )
                read = read + '\n\n' + footnote
            except Exception:  # noqa: BLE001  核实失败不影响解读
                pass
    return read


def synthesize(analyses, author='该博主'):
    """层 3：N 份逐期解读 → 一份人物画像（强制标来源层）。

    Args:
        analyses: list of (title, read_markdown)
    """
    if not analyses:
        raise RuntimeError('没有可综合的解读文档')

    def _join(items):
        return '\n\n---\n\n'.join(md for _t, md in items)

    def _portrait(items, n):
        return _call_gemini(PORTRAIT_PROMPT.format(
            author=author, n=n, analyses=_join(items), rules=_rules()
        ))

    combined = _join(analyses)
    if len(combined) <= _SYNTH_CHAR_LIMIT:
        return _portrait(analyses, len(analyses))

    # 超长：切批 → 每批中间综合 → 终合
    batches, cur, cur_len = [], [], 0
    for item in analyses:
        if cur and cur_len + len(item[1]) > _SYNTH_CHAR_LIMIT:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(item)
        cur_len += len(item[1])
    if cur:
        batches.append(cur)

    partials = [(f'中间综合{i + 1}', _portrait(b, len(b)))
                for i, b in enumerate(batches)]
    return _portrait(partials, len(analyses))
