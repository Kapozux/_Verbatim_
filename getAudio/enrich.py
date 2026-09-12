"""
AI 卡片元数据生成：给每条转写生成「标题 + 一句话简介 + 标签」，
让历史列表不点开就能看懂每条是什么。

输入优先用已有的 summary overview（短、便宜），没有才退回转写正文开头。
模型用 Gemini Flash（快、便宜），失败静默返回 None，不影响主流程。
"""

import json
import os
import re

from config import GEMINI_API_KEY, GEMINI_ENRICH_MODEL, make_gemini_client

ENRICH_PROMPT = """根据下面这条音频转写的信息，生成用于列表卡片展示的元数据。

文件名：{filename}
内容摘要或正文开头：
{content}

严格按以下 JSON 格式输出，不要输出其他任何内容：
{{
  "title": "不超过18个字的标题，说清这条内容是什么，别照抄文件名",
  "one_line": "一句话简介，不超过40字，让人不点开就知道大致内容",
  "tags": ["2到4个简短标签，如：访谈、课堂、播客、情感短剧、时政评论、英语、会议"],
  "filename_meaningful": true或false
}}

filename_meaningful 的判断标准：上面的文件名（去掉后缀和 [视频id]）本身是不是一个能说明内容的人写的标题。
像"恋爱经验少于3次、出现情感问题是必然的"这种是 true；像"录音 3""New Recording 12""2026-07-08 14.22.15"
"IMG_0001""audio_1234""output""下载""a1b2c3d4"这种日期、序号、设备默认名、随机串是 false。

要求：title / one_line / tags 用与内容相同的语言输出（内容是英文就用英文，是中文就用中文）；
标题要具体（宁可写"杜甫生平纪录片解说"也不要写"历史内容"）；
若内容明显是废稿/空白/无意义，title 写"（内容为空或无效）"。"""


def generate_card_meta(filename, content):
    """生成 {title, one_line, tags}；失败返回 None（调用方自行兜底）。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key or not content or len(content.strip()) < 10:
        return None

    # 控制输入长度：overview 本来就短；退回正文时只取开头
    content = content.strip()[:3000]

    try:
        client = make_gemini_client(api_key)
        resp = client.models.generate_content(
            model=GEMINI_ENRICH_MODEL,
            contents=ENRICH_PROMPT.format(filename=filename, content=content),
        )
        raw = (resp.text or '').strip()
    except Exception:
        return None

    return _parse_json(raw)


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


def translate_tags(tags, cache_path):
    """把中文标签批量译成简短英文，结果持久化缓存到 cache_path(JSON)。

    返回 {原标签: 英文}。已缓存的标签不再调模型；无 key / 调用失败时，
    未命中的标签回落为原文（前端切到 EN 也不会空）。
    """
    tags = [t for t in dict.fromkeys(tags) if t]   # 去重保序
    if not tags:
        return {}

    cache = {}
    try:
        if os.path.isfile(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
    except Exception:
        cache = {}

    missing = [t for t in tags if t not in cache]
    if missing:
        added = _gemini_translate(missing)
        if added:
            cache.update(added)
            try:
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(cache, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    return {t: cache.get(t, t) for t in tags}


def _gemini_translate(tags):
    """一次性把一批标签译成英文；返回 {中文: English} 或 None。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        return None
    prompt = (
        "把下面这些中文内容标签逐个翻译成简短的英文标签"
        "（每个 1-3 个单词，Title Case，如 时政评论→Politics、职业规划→Careers）。\n"
        "严格输出一个 JSON 对象，key 是原中文、value 是英文，不要输出别的：\n"
        + json.dumps(tags, ensure_ascii=False)
    )
    try:
        client = make_gemini_client(api_key)
        resp = client.models.generate_content(
            model=GEMINI_ENRICH_MODEL,
            contents=prompt,
        )
        raw = (resp.text or '').strip()
    except Exception:
        return None
    data = _parse_json_obj(raw)
    if not isinstance(data, dict):
        return None
    return {str(k): str(v).strip()[:30] for k, v in data.items() if str(v).strip()}


def _parse_json(raw):
    data = _parse_json_obj(raw)
    if data is None:
        return None

    title = str(data.get('title', '')).strip()
    if not title:
        return None
    tags = data.get('tags', [])
    if not isinstance(tags, list):
        tags = []
    return {
        'title': title[:30],
        'one_line': str(data.get('one_line', '')).strip()[:60],
        'tags': [str(t).strip()[:10] for t in tags if str(t).strip()][:4],
        'filename_meaningful': bool(data.get('filename_meaningful', False)),
    }


def enrich_content_for(task_dir):
    """给某条结果挑选 enrich 输入：优先 summary overview，退回 transcript 开头。"""
    summary_path = os.path.join(task_dir, 'summary.json')
    if os.path.isfile(summary_path):
        try:
            with open(summary_path, 'r', encoding='utf-8') as f:
                s = json.load(f)
            parts = [s.get('overview', '')]
            for sec in s.get('sections', [])[:6]:
                parts.append(f"- {sec.get('title', '')}：{sec.get('summary', '')}")
            text = '\n'.join(p for p in parts if p).strip()
            if len(text) >= 20:
                return text
        except Exception:
            pass

    transcript_path = os.path.join(task_dir, 'transcript.json')
    if os.path.isfile(transcript_path):
        try:
            with open(transcript_path, 'r', encoding='utf-8') as f:
                segs = json.load(f)
            return '\n'.join(s.get('text', '') for s in segs[:40])
        except Exception:
            pass
    return ''


def enrich_task(task_dir):
    """对单条结果生成并写入 ai_title/ai_one_line/ai_tags。返回 True=成功。"""
    meta_path = os.path.join(task_dir, 'meta.json')
    if not os.path.isfile(meta_path):
        return False
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:
        return False

    content = enrich_content_for(task_dir)
    card = generate_card_meta(meta.get('filename', ''), content)
    if not card:
        return False

    meta['ai_title'] = card['title']
    meta['ai_one_line'] = card['one_line']
    meta['ai_tags'] = card['tags']
    meta['filename_meaningful'] = card['filename_meaningful']
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return True


FILENAME_JUDGE_PROMPT = """下面是一批音视频文件名（已去掉后缀和 [视频id]）。逐个判断它本身是不是一个能说明内容的、人写的标题。
像"恋爱经验少于3次、出现情感问题是必然的""杜甫生平纪录片解说"这种是 true；
像"录音 3""New Recording 12""2026-07-08 14.22.15""IMG_0001""audio_1234""output""下载""a1b2c3d4"
这种日期、序号、设备默认名、随机串、纯数字是 false。

严格输出一个 JSON 对象，key 是原文件名、value 是 true/false，不要输出别的：
{names}"""


def judge_filenames(names):
    """一次判断一批文件名是否"有意义"；返回 {name: bool}，失败返回 None。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key or not names:
        return None
    try:
        client = make_gemini_client(api_key)
        resp = client.models.generate_content(
            model=GEMINI_ENRICH_MODEL,
            contents=FILENAME_JUDGE_PROMPT.format(names=json.dumps(names, ensure_ascii=False)),
        )
        data = _parse_json_obj((resp.text or '').strip())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): bool(v) for k, v in data.items()}


_VID_SUFFIX_RE = re.compile(r'\s*\[[A-Za-z0-9_-]{6,}\]\s*$')


def display_stem(filename):
    """原始文件名 → 用于命名的主体：去后缀、去结尾的 [视频id]。"""
    stem = os.path.splitext(filename or '')[0]
    return _VID_SUFFIX_RE.sub('', stem).strip()
