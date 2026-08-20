"""
Gemini transcription engine.
Uses Google Gemini API (AI Studio) for cloud-based speech-to-text.
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_INLINE_LIMIT, make_gemini_client
import config

TRANSCRIPTION_PROMPT = """请对这段音频进行精确的逐字转录。

要求：
1. 严格保留音频原始语言，绝对不要翻译：中文就输出中文，英文就输出英文，日文就输出日文，多语种混杂则按说话人实际使用的语言原样转录。
2. 每隔约 30 秒在新一行的开头插入一个时间戳，格式为 [HH:MM:SS]（例如 [00:00:00]、[00:01:45]）。
3. 完整保留标点符号和说话人的语气。
4. 专有名词、人名、地名、品牌名保留原文拼写，不要音译。
5. 不要输出任何解释、说明、或 Markdown 包裹，只输出纯转录文本。
6. 只转录你真正听到的语音。静音、环境噪音、无人说话的时段——直接跳过，不要用「嗯、啊、然後」之类的语气词去填补空白。
7. 不要因为前文说了什么就脑补后面的内容；每一段都只依据该时刻音频里实际的声音。听不清的地方宁可少写，也不要猜。
8. 严禁复读：绝不要把同一句话反复输出多次。若某段确实在重复，也只如实记录一次。

输出格式示例（示例仅用于展示格式，请按音频实际语言输出）：
[00:00:00] 大家好，欢迎来到今天的节目。
[00:00:32] Today we are going to talk about artificial intelligence.
"""

CHUNK_DURATION_SECONDS = 15 * 60

# 单次 generate_content 调用的重试策略：总共最多尝试 MAX_ATTEMPTS 次，
# 第 n 次失败后等待 RETRY_BACKOFF_SECONDS[n-1] 秒再重试。
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [2, 5, 10]

# 这些 finish_reason 表示内容被模型侧确定性拦截（安全策略/背诵/敏感信息等），
# 重试不会有任何改变，直接失败并把原因报清楚。
_NON_RETRYABLE_FINISH_REASONS = {
    'SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII',
}


class _ContentBlocked(Exception):
    """Gemini 确定性拒绝（安全过滤等），重试无意义。"""


def _diagnose_empty_response(response):
    """空文本时诊断真实原因，返回 (人类可读原因, 是否值得重试)。"""
    try:
        pf = getattr(response, 'prompt_feedback', None)
        block_reason = getattr(pf, 'block_reason', None) if pf else None
        if block_reason:
            return f'提示词被拦截 (block_reason={block_reason})', False

        candidates = getattr(response, 'candidates', None) or []
        if not candidates:
            return '无候选内容返回', True

        finish_reason = getattr(candidates[0], 'finish_reason', None)
        fr_name = getattr(finish_reason, 'name', None) or str(finish_reason or '')
        if fr_name in _NON_RETRYABLE_FINISH_REASONS:
            return f'内容被安全过滤拦截 (finish_reason={fr_name})', False
        if fr_name == 'MAX_TOKENS':
            return '输出超长被截断 (MAX_TOKENS)', False
        if fr_name:
            return f'finish_reason={fr_name}', True
        return '未知原因（无 finish_reason）', True
    except Exception:
        return '未知原因', True


def _is_rate_limit_error(err):
    msg = str(err).lower()
    return '429' in msg or 'resource_exhausted' in msg or 'quota' in msg or 'rate' in msg


def transcribe_audio(filepath, progress_callback=None):
    """
    Transcribe audio using Gemini API.

    Args:
        filepath: Path to the audio file.
        progress_callback: Optional callable(percent: int) for progress updates.

    Returns:
        List of segment dicts with keys: timestamp (str), text (str).
    """
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            "Gemini API Key not set. Please set GEMINI_API_KEY environment variable."
        )

    # 给底层 HTTP 调用设 10 分钟超时，防止代理 / Gemini 侧 socket 挂起后永远不 return。
    # 老版本 SDK 不支持 HttpOptions 就回退到默认。
    client = make_gemini_client(api_key)
    temp_dir = None

    try:
        if progress_callback:
            progress_callback(5)

        chunk_files = [(filepath, 0)]
        if _can_split_audio():
            try:
                chunk_files, temp_dir = split_audio_file(
                    filepath, CHUNK_DURATION_SECONDS
                )
            except Exception:
                # If splitting fails for any reason, keep single-pass transcription.
                chunk_files, temp_dir = [(filepath, 0)], None

        merged_text_parts = []
        total_chunks = len(chunk_files)

        for idx, (chunk_path, start_offset_seconds) in enumerate(chunk_files):
            chunk_start_pct = 5 + int((idx / total_chunks) * 90)
            chunk_end_pct = 5 + int(((idx + 1) / total_chunks) * 90)

            def chunk_progress(local_pct):
                if not progress_callback:
                    return
                mapped = chunk_start_pct + int(
                    (chunk_end_pct - chunk_start_pct) * (local_pct / 100.0)
                )
                progress_callback(min(99, mapped))

            chunk_text = _transcribe_single_file(
                client=client,
                filepath=chunk_path,
                progress_callback=chunk_progress if progress_callback else None,
            )
            shifted_text = shift_timestamps(chunk_text, start_offset_seconds)
            merged_text_parts.append(shifted_text.strip())

        full_text = "\n".join(part for part in merged_text_parts if part)
        if progress_callback:
            progress_callback(100)

        return parse_timestamped_text(full_text), full_text
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def _transcribe_single_file(client, filepath, progress_callback=None):
    """Transcribe one audio file with Gemini and return raw timestamped text.

    使用非流式 generate_content：比流式更抗代理/网络抖动。
    对瞬时失败做自动重试（指数退避），重试时文件不需要重新上传。
    """
    file_size = os.path.getsize(filepath)

    # Determine MIME type from extension
    ext = os.path.splitext(filepath)[1].lower()
    mime_map = {
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.flac': 'audio/flac',
        '.m4a': 'audio/mp4',
        '.ogg': 'audio/ogg',
        '.webm': 'audio/webm',
    }
    mime_type = mime_map.get(ext, 'audio/mpeg')

    if file_size > GEMINI_INLINE_LIMIT:
        uploaded = client.files.upload(file=filepath)
        if progress_callback:
            progress_callback(10)

        _poll_deadline = time.monotonic() + 600      # File API 处理封顶 10 分钟，别永久挂着占信号量
        while uploaded.state.name == "PROCESSING":
            if time.monotonic() > _poll_deadline:
                raise RuntimeError("Gemini File API 处理超时（>10 分钟仍在 PROCESSING）")
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)

        if uploaded.state.name != "ACTIVE":
            raise RuntimeError(
                f"File processing failed with state: {uploaded.state.name}"
            )

        if progress_callback:
            progress_callback(30)
        content_parts = [TRANSCRIPTION_PROMPT, uploaded]
    else:
        with open(filepath, 'rb') as f:
            audio_bytes = f.read()
        content_parts = [
            TRANSCRIPTION_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ]
        if progress_callback:
            progress_callback(30)

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if progress_callback:
                # 本次尝试的进度区间：30 -> 95。重试会从 30 重新开始，体现"再试一次"
                progress_callback(min(95, 30 + attempt * 10))

            response = client.models.generate_content(
                model=os.environ.get('GEMINI_TRANSCRIBE_MODEL') or GEMINI_MODEL,
                contents=content_parts,
            )
            text = response.text or ""
            if not text.strip():
                reason, retryable = _diagnose_empty_response(response)
                if not retryable:
                    # 安全过滤等确定性拒绝，重试无意义，直接报明原因
                    raise _ContentBlocked(reason)
                raise RuntimeError(f"Gemini 返回空文本（{reason}）")

            if progress_callback:
                progress_callback(100)
            return text
        except _ContentBlocked as e:
            raise RuntimeError(f"Gemini 未返回文本：{e}（重试无效，可换阿里云或 Whisper 引擎）") from e
        except Exception as e:
            last_err = e
            if attempt >= MAX_ATTEMPTS:
                break
            backoff = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            # 命中速率限制时退避更久，给并发中的其它请求让路
            if _is_rate_limit_error(e):
                backoff *= 3
            time.sleep(backoff)

    hint = ''
    if _is_rate_limit_error(last_err):
        hint = '（疑似并发过高被限流，可调低 config.ENGINE_CONCURRENCY["gemini"]）'
    raise RuntimeError(
        f"Gemini 转写失败（已重试 {MAX_ATTEMPTS} 次）: {last_err}{hint}"
    ) from last_err


def _can_split_audio():
    """Check if ffmpeg and ffprobe are available for chunking."""
    ffmpeg_bin = config.FFMPEG_BIN
    ffprobe_bin = config.FFPROBE_BIN
    return os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin)


def _get_audio_duration_seconds(filepath):
    """Return audio duration in seconds using ffprobe."""
    ffprobe_bin = config.FFPROBE_BIN
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        filepath,
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,          # 损坏音频不该让 ffprobe 挂死整条转写线程
    )
    return float(result.stdout.strip())


def split_audio_file(filepath, chunk_duration_seconds):
    """
    Split long audio into fixed-size chunks using ffmpeg.

    Returns:
        (chunks, temp_dir)
        chunks = list of (chunk_path, start_offset_seconds)
    """
    duration = _get_audio_duration_seconds(filepath)
    if duration <= chunk_duration_seconds:
        return [(filepath, 0)], None

    temp_dir = tempfile.mkdtemp(prefix="gemini_chunks_")
    chunks = []
    start = 0
    index = 0
    while start < duration:
        remaining = duration - start
        # 尾巴太短(<30s)就并进这一块：几秒的碎块 Gemini 常返空 → 会拖垮整条任务
        this_len = chunk_duration_seconds
        if remaining <= chunk_duration_seconds + 30:
            this_len = remaining + 1        # 最后一块，吃掉全部剩余
        ffmpeg_bin = config.FFMPEG_BIN
        chunk_path = os.path.join(temp_dir, f"chunk_{index:04d}.wav")
        ffmpeg_cmd = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-ss",
            str(start),
            "-t",
            str(this_len),
            "-i",
            filepath,
            "-ac",
            "1",
            "-ar",
            "16000",
            chunk_path,
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True, timeout=600)
        chunks.append((chunk_path, int(start)))
        start += this_len
        index += 1
    return chunks, temp_dir


def _timestamp_to_seconds(timestamp):
    """Convert MM:SS or HH:MM:SS string to total seconds."""
    parts = timestamp.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + int(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    raise ValueError(f"Unsupported timestamp format: {timestamp}")


def _seconds_to_hhmmss(total_seconds):
    """Convert total seconds to HH:MM:SS."""
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def shift_timestamps(text, offset_seconds):
    """Shift [MM:SS] / [HH:MM:SS] timestamps by offset seconds."""
    if offset_seconds <= 0:
        return _normalize_timestamps(text)

    pattern = r'\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]'

    def repl(match):
        original_ts = match.group(1)
        total_seconds = _timestamp_to_seconds(original_ts) + offset_seconds
        return f"[{_seconds_to_hhmmss(total_seconds)}]"

    return re.sub(pattern, repl, text)


def _normalize_timestamps(text):
    """Normalize all timestamps in text to HH:MM:SS."""
    pattern = r'\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]'

    def repl(match):
        total_seconds = _timestamp_to_seconds(match.group(1))
        return f"[{_seconds_to_hhmmss(total_seconds)}]"

    return re.sub(pattern, repl, text)


def parse_timestamped_text(text):
    """
    Parse Gemini output into segments with timestamps.

    Expected format: [HH:MM:SS] Some text here...

    Returns:
        List of dicts: [{"timestamp": "00:00:00", "text": "..."}, ...]
    """
    segments = []
    pattern = r'\[((?:\d{1,2}:)?\d{1,2}:\d{2})\]\s*(.*?)(?=\n?\[(?:(?:\d{1,2}:)?\d{1,2}:\d{2})\]|$)'
    matches = re.findall(pattern, text, re.DOTALL)

    for timestamp, content in matches:
        content = content.strip()
        if content:
            normalized_ts = _seconds_to_hhmmss(_timestamp_to_seconds(timestamp))
            segments.append({
                'timestamp': normalized_ts,
                'text': content,
            })

    # Fallback: if no timestamps found, return the whole text as one segment
    if not segments and text.strip():
        segments.append({
            'timestamp': '00:00:00',
            'text': text.strip(),
        })

    return segments
