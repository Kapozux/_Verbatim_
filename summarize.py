"""
AI-powered transcript summarization.
Supports Gemini and Qwen (DashScope) as backends.
"""

import json
import os
import re

from config import (
    GEMINI_API_KEY, GEMINI_MODEL,
    DASHSCOPE_API_KEY, DASHSCOPE_LLM_MODEL,
)

SUMMARY_PROMPT = """你是一个专业的内容分析助手。请对以下音频/视频转录文本进行总结分析。

请严格按照以下 JSON 格式输出，不要输出任何其他内容：

```json
{
  "overview": "用 3-5 句话概括整段内容的主题、核心观点和结论。",
  "sections": [
    {
      "title": "该部分的主题标题（简短）",
      "time_range": "HH:MM:SS - HH:MM:SS",
      "summary": "该部分讨论了什么内容，1-3 句话"
    }
  ]
}
```

要求：
1. overview 是对全部内容的整体概括
2. sections 按照内容的自然段落/话题切换来划分，通常 3-8 个段落
3. 每个 section 要标注对应的时间范围（从转录文本中的时间戳推断）
4. title 要简短有力，能概括该段主题
5. 用中文输出
6. 只输出 JSON，不要有其他文字

以下是转录文本：

"""


def summarize_transcript(full_text, use_qwen=False):
    """
    Summarize a transcript using Gemini or Qwen.

    Args:
        full_text: The full timestamped transcript text.
        use_qwen: If True, use Qwen via DashScope instead of Gemini.

    Returns:
        dict with 'overview' and 'sections', or None on failure.
    """
    if not full_text or len(full_text.strip()) < 50:
        return None

    prompt = SUMMARY_PROMPT + full_text

    if use_qwen:
        raw = _call_qwen(prompt)
    else:
        raw = _call_gemini(prompt)

    if not raw:
        return None

    return _parse_summary_json(raw)


def _call_gemini(prompt):
    """Call Gemini API and return raw response text."""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        return None

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        return response.text.strip()
    except Exception:
        return None


def _call_qwen(prompt):
    """Call Qwen via DashScope OpenAI-compatible API."""
    api_key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
    if not api_key:
        return None

    try:
        import requests
        resp = requests.post(
            'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': DASHSCOPE_LLM_MODEL,
                'messages': [
                    {'role': 'user', 'content': prompt},
                ],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content'].strip()
    except Exception:
        return None


def _parse_summary_json(raw_text):
    """Extract and parse JSON from LLM response."""
    json_match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
    if json_match:
        raw_text = json_match.group(1)

    raw_text = raw_text.strip()
    if not raw_text.startswith('{'):
        start = raw_text.find('{')
        if start != -1:
            raw_text = raw_text[start:]

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None

    overview = data.get('overview', '')
    sections = data.get('sections', [])

    if not overview:
        return None

    valid_sections = []
    for sec in sections:
        if isinstance(sec, dict) and sec.get('title') and sec.get('summary'):
            valid_sections.append({
                'title': sec['title'],
                'time_range': sec.get('time_range', ''),
                'summary': sec['summary'],
            })

    return {
        'overview': overview,
        'sections': valid_sections,
    }
