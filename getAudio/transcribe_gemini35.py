"""
Gemini 3.5 Transcribe —— 专用语音转写模型（2026-08-26 公开预览）。

跟 transcribe_gemini.py 用的通用多模态模型不是一回事：这是专门训练来做 ASR 的新模型，
原生输出词级时间戳 + 最多 8 人说话人分离，一次调用就出结果——不用像"精准模式"
（transcribe_precise.py）那样拿 Gemini 文字稿 + 阿里云说话人稿两份分别转、再合并。

这个新模型走的是全新的 Interactions REST 端点（`/v1beta/interactions`），当前锁定的
google-genai SDK 版本（受这个项目 venv 的 Python 3.9 限制，装不了要求 3.10+ 的新版 SDK）
还没把它封装进去，所以这里手写 REST 调用——文件上传仍然复用 SDK 现成的 Files API
（这部分接口一直稳定，SDK 支持），只有"提交转写"这一步是裸 requests，跟
transcribe_dashscope.py 对阿里云的做法是同一个模式。

限制（Google 官方文档写的）：开说话人分离/词级时间戳时单次最长 30 分钟，不开则 1 小时。
这个模块的整个卖点就是说话人分离，所以统一按"开"的上限来，超过阈值复用
transcribe_gemini.split_audio_file 切块、按块串行调用，再把各块时间戳按偏移拼回去。

已知局限：跨块的说话人编号不保证一致（每块的说话人分离是独立跑的，块 2 的"说话人1"
不一定是块 1 的"说话人1"）——这是本地切块换来能处理长音频的代价，DashScope 那条老
diarization 链路因为是单次调用处理整个文件，没有这个问题。对短于 28 分钟的音频
（绝大多数单集播客/会议录音）完全不受影响。
"""

import mimetypes
import os
import re
import threading
import time

import requests

from config import make_gemini_client
from transcribe_gemini import split_audio_file, _get_audio_duration_seconds
import config
import timecode
import usage

MODEL_NAME = 'gemini-3.5-transcribe'
_INTERACTIONS_URL = 'https://generativelanguage.googleapis.com/v1beta/interactions'

# 官方文档：开说话人分离/词级时间戳单块最长 30 分钟。留 2 分钟余量，
# 别让某个块因为切割误差（±几秒）卡在边界上被服务端拒。
_MAX_CHUNK_SECONDS = 28 * 60

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = [2, 5, 10]

# 429 限流（按项目每分钟输入 token 算，一个 28 分钟块就五万多 token）不算失败：
# 按服务端给的 "retry in Ns" 等，而且所有线程一起等——配额是全项目共享的，
# 一个请求被限流说明大家都该歇一下。最多耐心等这么久，超了才真判失败。
_RATE_LIMIT_PATIENCE_SECONDS = 60 * 60
_rate_lock = threading.Lock()
_rate_until = 0.0


def _wait_rate_gate():
    while True:
        with _rate_lock:
            left = _rate_until - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def _push_rate_gate(text):
    global _rate_until
    m = re.search(r'retry in (\d+(?:\.\d+)?)s', text or '')
    wait = float(m.group(1)) if m else 30.0
    with _rate_lock:
        _rate_until = max(_rate_until, time.time() + wait + 2)

# 同一说话人连续说话时，词与词之间的间隔超过这个值就当一次停顿，另起一段——
# 不然一个人连续讲 5 分钟会被并成一整段，时间戳粒度太粗，不好定位。
_PAUSE_GAP_SECONDS = 1.5
# 就算没有停顿，同一段也不能无限长，到这个时长强制切下一段。
_MAX_SEGMENT_SECONDS = 20

# 文件上传后等它从 PROCESSING 变 ACTIVE 的轮询设置
_UPLOAD_POLL_INTERVAL = 1
_UPLOAD_POLL_TIMEOUT = 120


def transcribe_audio(filepath, progress_callback=None, speaker_count=None):
    """转写整个文件，返回 [{'timestamp': 'HH:MM:SS', 'text': '说话人N：...'}]。

    speaker_count 目前用不上——这个 API 没有暴露"提示说话人数量"的参数
    （不像 DashScope 那样能传 speaker_count 改善聚类），保留这个形参只是为了
    跟其它引擎的 transcribe_audio(...) 签名保持一致，方便 app.py 统一调用。
    """
    api_key = config.gemini_key()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY 未设置（Gemini 3.5 Transcribe 需要）')

    chunks, temp_dir = split_audio_file(filepath, _MAX_CHUNK_SECONDS)
    try:
        client = make_gemini_client(api_key)
        total = len(chunks)

        # 每块自己的长度：下一块的起点减自己的起点；最后一块用音频总长。
        # 跟 transcribe_gemini.py 的 _chunk_len 是同一个算法——那边已经验证过
        # 「块内时间戳不夹到块长会跨块倒退/坍缩」这个 bug，这里切块结构完全一样，
        # 没理由这个更新、更少人用过的预览期模型反而不会复现同样的边界抖动。
        try:
            total_duration = int(_get_audio_duration_seconds(filepath) or 0) or None
        except Exception:  # noqa: BLE001  探不到时长就只夹到最后一块之前的块（最后一块不夹）
            total_duration = None

        def _chunk_len(idx):
            start = chunks[idx][1]
            end = chunks[idx + 1][1] if idx + 1 < total else total_duration
            if not end or end <= start:
                return None
            return end - start

        # 各块并行（之前串行）；结果按块索引回填保序，任一块失败整条任务失败。
        # 宽度沿用 GEMINI_CHUNK_CONCURRENCY，但这个端点还在预览期、配额没摸透，先压到 3。
        from concurrent.futures import ThreadPoolExecutor
        import threading
        results = [None] * total
        done = [0]
        lock = threading.Lock()

        def _one(i):
            chunk_path, offset_sec = chunks[i]
            results[i] = _transcribe_one(client, chunk_path, offset_sec, _chunk_len(i))
            if progress_callback:
                with lock:
                    done[0] += 1
                    n = done[0]
                progress_callback(min(99, int(n / total * 100)))

        width = max(1, min(3, config.GEMINI_CHUNK_CONCURRENCY, total))
        # 不用 with（退出时要等所有在跑的块跑完、照样计费）：一块失败就取消没开跑的、立刻抛出
        pool = ThreadPoolExecutor(max_workers=width)
        try:
            list(pool.map(usage.bound(_one), range(total)))   # list() 让第一个异常在这里抛出
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        all_segments = []
        for segs in results:
            all_segments.extend(segs or [])
        return all_segments
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


def _transcribe_one(client, chunk_path, offset_sec, chunk_seconds=None):
    """上传一个块 + 调 Interactions API + 解析成本模块的段落格式。

    chunk_seconds：这一块自己的长度（秒），传给 _parse_response 夹时间戳用。
    """
    uploaded = _upload_file(client, chunk_path)
    try:
        return _call_with_retry(client, uploaded, chunk_path, offset_sec, chunk_seconds)
    finally:
        # 用完立刻删：Files API 按项目限 20GB，靠 48h 自动过期的话，批量跑一个频道
        # 没几十期就会 429 file_storage_bytes
        try:
            client.files.delete(name=uploaded.name)
        except Exception:  # noqa: BLE001
            pass


def _call_with_retry(client, uploaded, chunk_path, offset_sec, chunk_seconds):
    _wait_active(client, uploaded.name)

    last_err = None
    attempt = 0
    rate_deadline = time.time() + _RATE_LIMIT_PATIENCE_SECONDS
    while attempt < _MAX_ATTEMPTS:
        attempt += 1
        _wait_rate_gate()
        try:
            resp = requests.post(
                _INTERACTIONS_URL,
                headers={'x-goog-api-key': config.gemini_key(), 'Content-Type': 'application/json'},
                json={
                    'model': MODEL_NAME,
                    'input': [{
                        'type': 'audio',
                        'uri': uploaded.uri,
                        'mime_type': uploaded.mime_type,
                    }],
                    'generation_config': {
                        'transcription_config': {
                            'mode': {
                                'type': 'verbatim',
                                'diarization_mode': 'speaker',
                                'timestamp_granularities': ['word'],
                            },
                        },
                    },
                },
                timeout=180,
            )
            if resp.status_code == 429 and time.time() < rate_deadline:
                _push_rate_gate(resp.text)
                last_err = RuntimeError(f'HTTP 429: {resp.text[:300]}')
                attempt -= 1                     # 限流等待不占重试次数
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:300]}')
            resp.raise_for_status()
            data = resp.json()
            _record_usage(data, chunk_path)
            return _parse_response(data, offset_sec, chunk_seconds)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
    raise RuntimeError(f'Gemini 3.5 Transcribe 调用失败: {last_err}')


def _record_usage(data, chunk_path):
    """记账。预览期接口的 usage 字段名还没稳定，几种形态都试；都没有就只记音频秒数
    （价格表里给这个模型配 per_audio_hour 就能算钱）。"""
    try:
        u = (data.get('usage') or data.get('usage_metadata') or {}) if isinstance(data, dict) else {}
        i = u.get('input_tokens') or u.get('prompt_token_count') or u.get('prompt_tokens') or 0
        o = u.get('output_tokens') or u.get('candidates_token_count') or u.get('completion_tokens') or 0
        secs = 0
        try:
            secs = _get_audio_duration_seconds(chunk_path) or 0
        except Exception:  # noqa: BLE001
            pass
        usage.record('gemini', MODEL_NAME, 'transcribe', input_tokens=i, output_tokens=o,
                     audio_tokens=i, audio_seconds=secs)
    except Exception:  # noqa: BLE001
        pass


def _upload_file(client, path):
    """按路径传给 SDK 的 files.upload() 时踩过一个坑：SDK 内部会把文件名原样塞进
    一个 HTTP 请求头（X-Goog-Upload-File-Name），不做任何转义/URL编码——文件名里
    有中文（比如 yt-dlp 用番剧标题命名的下载文件）就直接 UnicodeEncodeError，
    是 google-genai SDK 自己的 bug，不是这边调用姿势的问题。
    绕过方法：不传路径，传一个已打开的二进制文件对象——SDK 只在"传的是路径"这个
    分支才会设那个坏请求头，传文件对象就完全跳过，mime_type 自己猜好显式传进去即可。
    """
    mime_type, _ = mimetypes.guess_type(path)
    with open(path, 'rb') as f:
        return client.files.upload(
            file=f, config={'mime_type': mime_type or 'application/octet-stream'},
        )


def _wait_active(client, file_name):
    """等文件从 PROCESSING 变 ACTIVE（小文件通常瞬间完成，长音频需要几秒）。"""
    deadline = time.time() + _UPLOAD_POLL_TIMEOUT
    while time.time() < deadline:
        f = client.files.get(name=file_name)
        state = str(f.state)
        if 'PROCESSING' not in state:
            if 'FAILED' in state:
                raise RuntimeError(f'文件处理失败: {state}')
            return
        time.sleep(_UPLOAD_POLL_INTERVAL)
    raise RuntimeError('等待文件处理超时')


def _speaker_label(spk):
    """'spk:0' → '说话人1'（跟 transcribe_precise._speaker_label 的编号习惯对齐，从1开始）。"""
    if not spk:
        return '说话人?'
    digits = ''.join(c for c in str(spk) if c.isdigit())
    return f'说话人{int(digits) + 1}' if digits else '说话人?'


def _parse_response(data, offset_sec, chunk_seconds=None):
    """把 interactions 响应的 word_info 标注按「说话人切换/停顿/时长上限」分组成段落。

    chunk_seconds：这一块自己的长度（秒），有值就把块内偏移夹到 [0, chunk_seconds]
    再加 offset_sec——模型偶尔会在某一块里吐出超过块长的偏移，直接加会越界压到
    后面几块的区间上，造成时间戳倒退（transcribe_gemini.py 的 shift_timestamps
    已经验证过同一类问题：3 小时的稿里实测出现过 00:30:00 → 02:30:00 的跳跃）。
    """
    steps = data.get('steps') or []
    if not steps:
        return []
    content = (steps[0].get('content') or [{}])[0]
    full_text = content.get('text') or ''
    words = [a for a in (content.get('annotations') or []) if a.get('type') == 'word_info']
    if not words:
        # 没有标注（比如模型这次没按预期带 word_info）→ 至少别把这段文字丢了
        if full_text.strip():
            return [{'timestamp': timecode.hms(offset_sec), 'text': full_text.strip()}]
        return []

    groups = []
    cur = [words[0]]
    for w in words[1:]:
        gap = _parse_offset(w.get('start_offset')) - _parse_offset(cur[-1].get('end_offset'))
        span = _parse_offset(w.get('end_offset')) - _parse_offset(cur[0].get('start_offset'))
        same_speaker = w.get('speaker') == cur[-1].get('speaker')
        if same_speaker and gap <= _PAUSE_GAP_SECONDS and span <= _MAX_SEGMENT_SECONDS:
            cur.append(w)
        else:
            groups.append(cur)
            cur = [w]
    groups.append(cur)

    segments = []
    for g in groups:
        start_idx, end_idx = g[0].get('start_index'), g[-1].get('end_index')
        text = full_text[start_idx:end_idx].strip() if start_idx is not None else \
            ' '.join(w.get('text', '') for w in g)
        if not text:
            continue
        intra_offset = _parse_offset(g[0].get('start_offset'))
        if chunk_seconds is not None:
            intra_offset = max(0, min(intra_offset, chunk_seconds))
        start_sec = offset_sec + intra_offset
        segments.append({
            'timestamp': timecode.hms(start_sec),
            'text': f'{_speaker_label(g[0].get("speaker"))}：{text}',
        })
    return segments


def _parse_offset(s):
    """'10.400s' → 10.4；空/异常兜底 0.0。"""
    try:
        return float(str(s).rstrip('s'))
    except (TypeError, ValueError):
        return 0.0
