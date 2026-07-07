"""
精准模式（说话人分离）合并逻辑。

思路：用每个引擎最擅长的部分，各取所长：
  - 阿里云 Paraformer（diarization）负责"谁在说"——声纹分离，句级 speaker + 时间戳；
  - Gemini 负责"说了什么"——高质量文字；
  - 最后再让 Gemini 把两份稿按时间轴对齐合并，输出带说话人的成稿。

对外只暴露：
  - merge_speaker_transcript(gemini_segments, dashscope_segments) -> (segments, full_text)
  - speaker_only_segments(dashscope_segments) -> segments   # Gemini 失败时的降级
"""

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_MODEL, make_gemini_client
from transcribe_gemini import parse_timestamped_text

# 合并按时间窗口分段做：一次只喂一段给 Gemini，避免长音频（几小时）
# 整份稿塞进单次调用撞输出 token 上限被截断，导致后半段内容丢失。
MERGE_WINDOW_SECONDS = 10 * 60
# 各窗口并发合并（单个精准任务内部），Tier 1 下 4 路稳妥
_MERGE_WORKERS = 4

MERGE_PROMPT = """你会收到同一段音频的两份转写稿：

- 稿A：带时间戳和「说话人」标注，但文字可能不够准确。
- 稿B：文字准确，但没有区分说话人。

请把它们合并成一份最终稿，严格遵守：
1. 文字内容以稿B为准（更准确），不要采用稿A里明显错误的用词。
2. 说话人归属以稿A为准，按时间轴把稿B的文字分配给对应说话人。
3. 同一说话人连续的话合并在一起；说话人切换就另起一行。
4. 保留时间戳，每行格式必须是：[HH:MM:SS] 说话人N：文字
5. 只输出最终转写文本，不要任何解释、前言或 Markdown 包裹。

稿A（说话人 + 时间戳）：
{block_a}

稿B（准确文字 + 时间戳）：
{block_b}
"""


def _speaker_label(seg):
    """把 dashscope 的 speaker_id（一般是 0/1/2…）转成「说话人1/2/3」。"""
    sid = seg.get('speaker')
    if sid is None or sid == '':
        return '说话人?'
    try:
        return f'说话人{int(sid) + 1}'
    except (ValueError, TypeError):
        return f'说话人{sid}'


def _format_a(dashscope_segments):
    return "\n".join(
        f"[{s['timestamp']}] ({_speaker_label(s)}) {s['text']}"
        for s in dashscope_segments
    )


def _format_b(gemini_segments):
    return "\n".join(f"[{s['timestamp']}] {s['text']}" for s in gemini_segments)


def _make_client():
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError("Gemini API Key 未设置，无法执行精准模式的合并步骤。")
    return make_gemini_client(api_key)


def _ts_to_seconds(ts):
    parts = ts.split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return 0


def _speaker_only_text(dashscope_segments):
    """把阿里云说话人稿直接渲染成 [时间] 说话人N：文字（合并失败/无 Gemini 时兜底）。"""
    return "\n".join(
        f"[{s['timestamp']}] {_speaker_label(s)}：{s['text']}"
        for s in dashscope_segments
    )


def _plain_text(gemini_segments):
    """无说话人信息时兜底：只渲染 Gemini 文字。"""
    return "\n".join(f"[{s['timestamp']}] {s['text']}" for s in gemini_segments)


def merge_speaker_transcript(gemini_segments, dashscope_segments):
    """用 Gemini 合并说话人稿(A)与高质量文字稿(B)，返回 (segments, full_text)。

    按时间窗口分段合并：每个窗口单独一次 Gemini 调用，避免长音频整份稿
    超过单次输出上限被截断。各窗口并发跑、结果按时间顺序拼接；
    任一窗口合并失败/返空则退回该窗口的阿里云说话人稿，绝不丢内容。
    """
    client = _make_client()

    a_by_win = defaultdict(list)
    b_by_win = defaultdict(list)
    for s in dashscope_segments:
        a_by_win[_ts_to_seconds(s['timestamp']) // MERGE_WINDOW_SECONDS].append(s)
    for s in gemini_segments:
        b_by_win[_ts_to_seconds(s['timestamp']) // MERGE_WINDOW_SECONDS].append(s)

    windows = sorted(set(a_by_win) | set(b_by_win))
    if not windows:
        raise RuntimeError("说话人合并失败：两份稿都为空")

    def _merge_window(win):
        a = a_by_win.get(win, [])
        b = b_by_win.get(win, [])
        if a and b:
            try:
                prompt = MERGE_PROMPT.format(block_a=_format_a(a), block_b=_format_b(b))
                resp = client.models.generate_content(
                    model=GEMINI_MODEL, contents=[prompt]
                )
                text = (resp.text or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                pass
            # 合并失败或返空：退回阿里云说话人稿，保住这段内容
            return _speaker_only_text(a)
        if b:  # 这段没有说话人信息
            return _plain_text(b)
        return _speaker_only_text(a)  # 这段 Gemini 没文字

    with ThreadPoolExecutor(max_workers=_MERGE_WORKERS) as ex:
        parts = list(ex.map(_merge_window, windows))

    full_text = "\n".join(p for p in parts if p).strip()
    if not full_text:
        raise RuntimeError("说话人合并失败：所有窗口都为空")

    return parse_timestamped_text(full_text), full_text


def speaker_only_segments(dashscope_segments):
    """Gemini 转写失败（如被安全过滤）时的降级：只用阿里云的说话人稿。"""
    return [
        {
            'timestamp': s['timestamp'],
            'text': f"{_speaker_label(s)}：{s['text']}",
        }
        for s in dashscope_segments
    ]
