"""
经 OpenRouter 调 Gemini 转写——官方 Gemini（AI Studio）转不了时的后备。

2026-10-28：官方项目撞上月度花费上限（spending cap），整条转写落到本地 Whisper，
笔记本又在用电池、Whisper 原地等插电，界面一直「转写中」。用户：「如果 google 官方的不行就用 OR」。
OpenRouter 是另一本账（预充值额度），官方那边的上限管不到它。

做法跟 transcribe_gemini.py 一样（通用多模态 Gemini + 逐字转录提示词 + 每 30 秒一个时间戳），
只是换成 OpenRouter 的 OpenAI 兼容接口、音频 base64 内联（input_audio）。
没有 Gemini 3.5 Transcribe 那种原生说话人分离 / 词级时间戳——那个专用模型 OpenRouter 上没有，
说话人靠提示词让模型自己标「说话人N：」。
长音频复用 transcribe_gemini.split_audio_file 切 15 分钟一块（16k 单声道 Opus，约 5 MB），各块并行。
"""

import base64
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION

import requests

import config
import usage
from transcribe_gemini import (
    CHUNK_DURATION_SECONDS, TRANSCRIPTION_PROMPT, _can_split_audio, _get_audio_duration_seconds,
    parse_timestamped_text, shift_timestamps, split_audio_file,
)

# OpenRouter 上带音频输入的 Gemini。3.8 flash：音频 $0.825 / 百万 token（约 $0.08 / 小时音频），
# 输出 $4.125 / 百万。想换就在 .env 设 OPENROUTER_TRANSCRIBE_MODEL。
DEFAULT_MODEL = 'google/gemini-3.8-flash'

PROMPT = TRANSCRIPTION_PROMPT + """
补充：如果有不止一个人说话，在每段开头标出说话人，格式为「说话人1：」「说话人2：」（按第一次出现的先后编号）；只有一个人说话就不用标。
"""

_TIMEOUT_S = 300          # 一块 15 分钟音频通常一分钟内回；挂住就判失败，别永远占着线程
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = [3, 8]

# OpenRouter input_audio 认的格式名（按后缀）
_FORMATS = {'.mp3': 'mp3', '.wav': 'wav', '.flac': 'flac', '.m4a': 'm4a', '.aac': 'aac',
            '.ogg': 'ogg', '.opus': 'ogg', '.aiff': 'aiff', '.aif': 'aiff'}


def model_name():
    return (os.environ.get('OPENROUTER_TRANSCRIBE_MODEL') or '').strip() or DEFAULT_MODEL


def api_key():
    return (os.environ.get('OPENROUTER_API_KEY') or '').strip()


def transcribe_audio(filepath, progress_callback=None):
    """转写整个文件，返回 (segments, full_text)，跟 transcribe_gemini.transcribe_audio 一样。"""
    key = api_key()
    if not key:
        raise RuntimeError('OPENROUTER_API_KEY 未设置（Settings 里填 OpenRouter key）')

    chunks, temp_dir = [(filepath, 0)], None
    if _can_split_audio():
        try:
            chunks, temp_dir = split_audio_file(filepath, CHUNK_DURATION_SECONDS)
        except Exception:  # noqa: BLE001  切不了就整段送
            chunks, temp_dir = [(filepath, 0)], None
    try:
        total = len(chunks)
        try:
            total_duration = int(_get_audio_duration_seconds(filepath) or 0) or None
        except Exception:  # noqa: BLE001
            total_duration = None

        def _chunk_len(idx):
            start = chunks[idx][1]
            end = chunks[idx + 1][1] if idx + 1 < total else total_duration
            return end - start if end and end > start else None

        parts = [None] * total
        done = [0]
        lock = threading.Lock()

        def _one(idx):
            path, offset = chunks[idx]
            text = _transcribe_chunk(key, path)
            parts[idx] = shift_timestamps(text, offset, chunk_seconds=_chunk_len(idx)).strip()
            if progress_callback:
                with lock:
                    done[0] += 1
                    n = done[0]
                progress_callback(min(99, 5 + int(90 * n / total)))

        if progress_callback:
            progress_callback(5)
        if total == 1:
            _one(0)
        else:
            pool = ThreadPoolExecutor(max_workers=max(1, min(config.GEMINI_CHUNK_CONCURRENCY, total)))
            try:
                futures = [pool.submit(usage.bound(_one), i) for i in range(total)]
                wait(futures, return_when=FIRST_EXCEPTION)
                failed = next((f for f in futures if f.done() and f.exception()), None)
                if failed is not None:
                    raise failed.exception()
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        full_text = '\n'.join(p for p in parts if p)
        if progress_callback:
            progress_callback(100)
        return parse_timestamped_text(full_text), full_text
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


def _transcribe_chunk(key, path):
    fmt = _FORMATS.get(os.path.splitext(path)[1].lower(), 'mp3')
    with open(path, 'rb') as f:
        data = base64.b64encode(f.read()).decode('ascii')
    model = model_name()
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': PROMPT},
            {'type': 'input_audio', 'input_audio': {'data': data, 'format': fmt}},
        ]}],
        'temperature': 0,
    }
    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                config.OPENROUTER_COMPAT_BASE.rstrip('/') + '/chat/completions',
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json=body, timeout=_TIMEOUT_S,
            )
            if resp.status_code in (401, 402, 403):
                # key 不对 / 余额不足：重试没用
                raise _Fatal(f'HTTP {resp.status_code}: {resp.text[:300]}')
            if resp.status_code != 200:
                raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:300]}')
            data_out = resp.json()
            if data_out.get('error'):
                raise RuntimeError(str(data_out['error'])[:300])
            u = data_out.get('usage') or {}
            usage.record('openrouter', data_out.get('model') or model, 'transcribe',
                         input_tokens=u.get('prompt_tokens') or 0,
                         output_tokens=u.get('completion_tokens') or 0,
                         cost_usd=u.get('cost'))
            choice = (data_out.get('choices') or [{}])[0]
            text = (choice.get('message') or {}).get('content') or ''
            if not text.strip():
                # 正常收尾却没字 = 这块没人说话（提示词让它跳过静音）。10-28「CLL 伤逝」最后 40 秒是静音，
                # 当成失败重试 → 撞上断网 → 整条落 Whisper。整条稿是不是白卷由 app 的 _verify 把关。
                if str(choice.get('finish_reason') or '').lower() == 'stop':
                    return ''
                raise RuntimeError(f"返回空文本（finish_reason={choice.get('finish_reason')}）")
            return text
        except _Fatal as e:
            raise RuntimeError(f'OpenRouter 转写失败: {e}') from e
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
    raise RuntimeError(f'OpenRouter 转写失败（已重试 {_MAX_ATTEMPTS} 次）: {last_err}') from last_err


class _Fatal(Exception):
    pass
