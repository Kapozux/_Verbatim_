"""
博主观点分析：每期独立分析 + 跨期总合成。

复刻多 agent workflow 的思路，但用纯 Gemini API 调用实现（可嵌入产品）：
  - analyze_transcript(): 一期转写 → 一份「观点/论点论据论证/AI 独立思考」小文档。
    每期一次独立调用，绝不把多期塞进同一次调用（防丢信息）。
  - synthesize(): N 份小文档 → 一份跨期综合总文档。
    超过单次上下文安全线时自动分批做中间综合再终合。
"""

import os
import time
from datetime import datetime

from config import GEMINI_API_KEY, GEMINI_ANALYSIS_MODEL, make_gemini_client

# 事实校准规则：直接对症那几类失败——截止日期盲区、对冲信息被压平、
# ASR 音译泥、以及"为了犀利牺牲校准"。两份 prompt 都注入。
_CALIBRATION_RULES = """【事实校准规则 · 必须严格遵守】
- 今天是 {today}。转写可能涉及你知识截止之后才出现的论文/模型/产品/事件。**不认识 ≠ 不存在，更 ≠ 编造。**
- 下"虚构 / 造假 / 编造数据 / 查无此物"这类结论前必须有依据。核实不了就明确写"未能核实"，**严禁**把"我不认识"升级成"他在造假"。
- 保留说话人的存疑标注：他若说过"数据来自原文、待核实 / 我没验证"，就如实记为"说话人已自行标注存疑"，不得吞掉这句再反过来指责他不标注。
- 区分认识论状态：某条信息是①说话人自己的主张、②他转述某来源、还是③他明确标了存疑？不要把"他对 X 存疑"塌缩成"X 成立"。
- 转写可能有语音识别错误（专有名词/英文/模型名尤其易被听错、音译错）。遇到明显像 ASR 误识的实体，按"疑似识别错误"处理，别把糊掉的词当成实质主张来批判。
- 校准优先于犀利：真实、可核查 > 修辞锋利。宁可写"这点存疑/未能核实"，也不要为了叙事漂亮下自信断言。"""

# 核实模式（联网搜索）开启时才注入
_VERIFY_ADDENDUM = """【联网核实 · 已开启】
- 你可以使用 Google 搜索。请主动核实转写里的专有名词、论文、模型、产品、数据是否真实、是否属实。
- 转写里疑似语音识别错误的实体（音译泥、错拼、英文听岔），先搜出正确写法再使用，别照着错的分析。
- 在文档开头加一节「## 实体核实」：列关键实体 + 结果（真实 / 未找到 / 疑似误识→正确名）。"""

ANALYZE_PROMPT = """你是一位严谨、校准良好的知识整理者。下面是「{author}」一期内容《{title}》的完整转写。

**语言：整份文档（包括所有小标题）必须用与下方转写相同的语言撰写——转写是中文就用中文，是英文就用英文，其他语言同理。下面给出的小标题只是结构示例，请翻译成对应语言。**

{rules}
{verify}

请基于这份转写，输出一份清晰、有信息量的**知识分析**（Markdown，不注水、不泛泛）：

# {title}

## 概览
这一期讲了什么：主题和覆盖范围，2-4 句说清。

## 关键内容
逐条梳理实质内容：讲解的概念、方法、事实、结论、案例。具体、准确，保留关键术语和数字。

## 涉及的概念与工作
提到的论文 / 模型 / 技术 / 工具 / 人物 / 产品等，列出来便于查证。

## 要点与结论
这一期最值得记住的核心结论、洞见或实用信息。

## 补充与存疑
需要补充的背景；以及任何你无法确认真伪、值得进一步核实的具体事实（标"未能核实"，不臆断）。

转写文本：
{transcript}"""

SYNTHESIZE_PROMPT = """你会收到「{author}」{n} 期内容的独立知识分析。请跨期综合成一份总文档（Markdown）。

**语言：整份总文档（包括所有小标题）必须用与下方分析文档相同的语言撰写——它们是中文就用中文，是英文就用英文。下面给出的小标题只是结构示例，请翻译成对应语言。**

{rules}
- 合并注意：若同一个无法核实的说法在多期反复出现，更可能是**共同的识别/知识盲区**而非事实，别因反复出现就当成坐实。

# {author}：内容综合（基于 {n} 期）

## 覆盖范围
这个创作者整体在讲什么领域 / 主题。

## 主要议题
归纳成 5-10 个议题。每个：讲了哪些内容、涉及哪些关键概念/工作、跨期的脉络。

## 反复出现的重点
高频出现的概念、方法、主题或关注点。

## 综合要点
读完这些能带走的核心知识与结论。

以下是全部分析文档：

{analyses}"""

# 单次合成的输入安全线（字符）。Gemini 2.5 Pro 上下文约 1M token，
# 中文 1 token≈1.5 字符，留足输出余量后取 60 万字符。
_SYNTH_CHAR_LIMIT = 600_000
_MAX_ATTEMPTS = 3


def _call_gemini(prompt, grounded=False):
    """带重试的 Gemini 调用，返回文本或抛异常。grounded=True 开 Google 搜索核实。"""
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


def _rules():
    return _CALIBRATION_RULES.format(today=datetime.now().strftime('%Y-%m-%d'))


def analyze_transcript(title, transcript_text, author='该博主', verify=False):
    """一期转写 → 一份知识分析 Markdown。verify=True 开联网核实。"""
    prompt = ANALYZE_PROMPT.format(
        author=author, title=title, transcript=transcript_text,
        rules=_rules(), verify=(_VERIFY_ADDENDUM if verify else ''),
    )
    return _call_gemini(prompt, grounded=verify)


def synthesize(analyses, author='该博主'):
    """
    N 份分析 → 一份总文档 Markdown 文本。

    Args:
        analyses: list of (title, analysis_markdown)
    """
    if not analyses:
        raise RuntimeError('没有可综合的分析文档')

    def _join(items):
        return '\n\n---\n\n'.join(md for _t, md in items)

    combined = _join(analyses)
    if len(combined) <= _SYNTH_CHAR_LIMIT:
        return _call_gemini(SYNTHESIZE_PROMPT.format(
            author=author, n=len(analyses), analyses=combined, rules=_rules()
        ))

    # 超长：切批 → 每批中间综合 → 终合
    batches = []
    cur, cur_len = [], 0
    for item in analyses:
        item_len = len(item[1])
        if cur and cur_len + item_len > _SYNTH_CHAR_LIMIT:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(item)
        cur_len += item_len
    if cur:
        batches.append(cur)

    partials = []
    for i, batch in enumerate(batches):
        part = _call_gemini(SYNTHESIZE_PROMPT.format(
            author=author, n=len(batch), analyses=_join(batch), rules=_rules()
        ))
        partials.append((f'中间综合{i + 1}', part))

    return _call_gemini(SYNTHESIZE_PROMPT.format(
        author=author,
        n=len(analyses),
        analyses=_join(partials),
        rules=_rules(),
    ))
