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

from config import GEMINI_API_KEY, GEMINI_MODEL

ANALYZE_PROMPT = """你是一位犀利、诚实的独立研究者。下面是博主「{author}」一期视频《{title}》的完整转写文本。

请只基于这份转写，输出一份扎实的中文 Markdown 分析（不要注水、不要泛泛而谈）：

# {title}

## 一、核心观点
列出他这期表达的主要主张/结论，逐条具体，标注立场强度（强烈主张/顺带一提/反复强调）。

## 二、论点·论据·论证
拆解他如何论证：用了什么论据（数据/案例/类比/个人经历/第一性原理推演），论证链条是什么，
哪些是硬证据、哪些是主观断言或情绪化表达。保留他标志性的措辞/比喻（可少量引用原话）。

## 三、我的独立思考
你自己的判断：哪些站得住、哪些偏颇或以偏概全、哪些可能过时或有事实错误；
补充背景、数据或反例；他的说法对什么人适用、对什么人是坑。

转写文本：
{transcript}"""

SYNTHESIZE_PROMPT = """你会收到博主「{author}」{n} 期视频的独立分析文档。请跨期打通，综合成一份总文档（中文 Markdown）：

# {author}：综合观点研究（基于 {n} 期视频）

## 〇、世界观速写
横跨所有期数，提炼他最底层、反复出现的思维母题与价值判断（判断方法、偏好、执念、盲区），
并列一份「他反复给出的、可被时间证伪的具体预测清单」。

## 按议题综合
自行把内容归纳成 5-10 个议题。每个议题写：
（1）他的综合核心立场（跨期去重合并，标注来源期数关键词）；
（2）论证方式与依据（哪些硬观察、哪些主观断言）；
（3）内在矛盾或前后张力；
（4）你的独立评价。

要求：是综合提炼而非逐期罗列；扎实、有信息量；实事求是。

以下是全部分析文档：

{analyses}"""

# 单次合成的输入安全线（字符）。Gemini 2.5 Pro 上下文约 1M token，
# 中文 1 token≈1.5 字符，留足输出余量后取 60 万字符。
_SYNTH_CHAR_LIMIT = 600_000
_MAX_ATTEMPTS = 3


def _call_gemini(prompt):
    """带重试的 Gemini 调用，返回文本或抛异常。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY 未设置')

    from google import genai
    from google.genai import types

    try:
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=600_000)
        )
    except Exception:
        client = genai.Client(api_key=api_key)

    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt
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


def analyze_transcript(title, transcript_text, author='该博主'):
    """一期转写 → 一份分析 Markdown 文本。"""
    prompt = ANALYZE_PROMPT.format(
        author=author, title=title, transcript=transcript_text
    )
    return _call_gemini(prompt)


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
            author=author, n=len(analyses), analyses=combined
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
            author=author, n=len(batch), analyses=_join(batch)
        ))
        partials.append((f'中间综合{i + 1}', part))

    return _call_gemini(SYNTHESIZE_PROMPT.format(
        author=author,
        n=len(analyses),
        analyses=_join(partials),
    ))
