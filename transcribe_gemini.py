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
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_INLINE_LIMIT

TRANSCRIPTION_PROMPT = """请将这段音频精确转录为中文文本。

要求：
1. 每隔约30秒插入一个时间戳，格式为 [HH:MM:SS]
2. 时间戳放在新一行的开头
3. 保持原始语言（中文），不要翻译
4. 包含标点符号
5. 如果有英文专有名词，保留英文

输出格式示例：
[00:00:00] 大家好，欢迎来到今天的节目。
[00:00:32] 今天我们要讨论的话题是人工智能。
"""

CHUNK_DURATION_SECONDS = 15 * 60


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

    client = genai.Client(api_key=api_key)
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
    """Transcribe one audio file with Gemini and return raw timestamped text."""
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

        while uploaded.state.name == "PROCESSING":
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)

        if uploaded.state.name != "ACTIVE":
            raise RuntimeError(
                f"File processing failed with state: {uploaded.state.name}"
            )

        if progress_callback:
            progress_callback(20)
        content_parts = [TRANSCRIPTION_PROMPT, uploaded]
    else:
        with open(filepath, 'rb') as f:
            audio_bytes = f.read()
        content_parts = [
            TRANSCRIPTION_PROMPT,
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ]
        if progress_callback:
            progress_callback(20)

    full_text = ""
    chunk_count = 0
    for chunk in client.models.generate_content_stream(
        model=GEMINI_MODEL,
        contents=content_parts,
    ):
        if chunk.text:
            full_text += chunk.text
            chunk_count += 1
            if progress_callback:
                pct = min(90, 20 + chunk_count * 3)
                progress_callback(pct)
    if progress_callback:
        progress_callback(100)

    return full_text


def _can_split_audio():
    """Check if ffmpeg and ffprobe are available for chunking."""
    ffmpeg_bin = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    ffprobe_bin = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
    return os.path.exists(ffmpeg_bin) and os.path.exists(ffprobe_bin)


def _get_audio_duration_seconds(filepath):
    """Return audio duration in seconds using ffprobe."""
    ffprobe_bin = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
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
        ffmpeg_bin = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        chunk_path = os.path.join(temp_dir, f"chunk_{index:04d}.wav")
        ffmpeg_cmd = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-ss",
            str(start),
            "-t",
            str(chunk_duration_seconds),
            "-i",
            filepath,
            "-ac",
            "1",
            "-ar",
            "16000",
            chunk_path,
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
        chunks.append((chunk_path, int(start)))
        start += chunk_duration_seconds
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
