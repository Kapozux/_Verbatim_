# Verbatim — 完整源代码合并文档

> 生成时间：2026-08-10　·　音视频转写 + 博主观点研究台（本地自托管 Flask）

## 目录

- [`app.py`](#apppy) — Flask 主应用：路由 · 转写任务 · 链条编排 · 小红书子进程 · 鉴权 · 统计（2991 行）
- [`config.py`](#configpy) — 配置：引擎/并发/限流参数、分析大脑预设（149 行）
- [`taskdb.py`](#taskdbpy) — 任务持久化（SQLite）+ 重启恢复（84 行）
- [`downloader.py`](#downloaderpy) — yt-dlp 下载（probe/download_one，cookies + zh-CN 标题 + 合集展开 + B站活动页自愈）（477 行）
- [`harness.py`](#harnesspy) — 极简 agent 原语：fanout/agent，确定性编排、零业务依赖（72 行）
- [`analyze.py`](#analyzepy) — 证据卡抽取 + 跨期合成 + 证伪层 + 镜头 + 小红书多模态分析（827 行）
- [`sanitize.py`](#sanitizepy) — 转写证伪层：反幻听、复读死循环折叠、时间轴修复（297 行）
- [`summarize.py`](#summarizepy) — AI 内容摘要（154 行）
- [`enrich.py`](#enrichpy) — AI 卡片元数据（标题/一句话/标签）（194 行）
- [`audioutil.py`](#audioutilpy) — 转写后音频 Opus 压缩归档（104 行）
- [`transcribe_whisper.py`](#transcribewhisperpy) — 本地引擎：mlx（Apple Silicon GPU）→ faster-whisper（CPU）→ openai-whisper（227 行）
- [`transcribe_gemini.py`](#transcribegeminipy) — 云端引擎：Gemini（分块 + 重试 + 安全过滤诊断）（395 行）
- [`transcribe_dashscope.py`](#transcribedashscopepy) — 云端引擎：阿里云 Qwen-ASR（273 行）
- [`transcribe_precise.py`](#transcribeprecisepy) — 精准模式：Gemini 文字 + 阿里云说话人分离，逐窗口合并校验（185 行）
- [`templates/index.html`](#templatesindexhtml) — 单页结构（4 个 tab：Transcribe/Xiaohongshu/Creators/Library）（609 行）
- [`static/app.js`](#staticappjs) — 前端逻辑（上传/SSE/链条/镜头/小红书/统计/Markdown 渲染）（2424 行）
- [`static/style.css`](#staticstylecss) — Claude 风格设计系统（803 行）

---

## `app.py`

> Flask 主应用：路由 · 转写任务 · 链条编排 · 小红书子进程 · 鉴权 · 统计

```python
"""
Flask application for audio/video transcription.
Supports Whisper (local) and Gemini (cloud) engines with SSE progress streaming.
Persists results (audio + transcript + summary) to disk for history playback.
"""

import hmac
import json
import os
import queue
import signal
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request, send_file

import config
import taskdb


# task_id 从 URL 直接拼到 os.path.join，必须严格校验防止路径穿越
_TASK_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _is_valid_task_id(task_id):
    return bool(task_id) and bool(_TASK_ID_RE.match(task_id))

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH


@app.errorhandler(413)
def _too_large(_e):
    # 超过上限时给清楚的 JSON 原因，别让前端只看到通用 "Upload failed (413)"
    return jsonify({
        'error': f'File exceeds the {config.MAX_UPLOAD_MB} MB upload limit. '
                 f'For a long video, extract the audio first (much smaller) and upload that, '
                 f'or raise MAX_UPLOAD_MB.'
    }), 413


os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(config.RESULTS_FOLDER, exist_ok=True)

taskdb.init()


# ========== 用户设置（API keys / base URL）==========
# 让用户在网页 Settings 里填 key，免去手动改 .env。存到 gitignore 的
# settings.local.json；启动时和每次保存后写进 os.environ，各引擎调用时即时读到
# （模块里都是 `KEY or os.environ.get(...)` 在调用时求值，所以不用重启）。
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), 'settings.local.json')
# 前端字段名 -> 环境变量名
_SETTING_ENV = {
    'gemini_key': 'GEMINI_API_KEY',
    'gemini_base_url': 'GEMINI_BASE_URL',
    'dashscope_key': 'DASHSCOPE_API_KEY',
    'openrouter_key': 'OPENROUTER_API_KEY',
    # Models（非秘密，运行时读 env，保存即生效）
    'whisper_model': 'WHISPER_MODEL_SIZE',
    'gemini_transcribe_model': 'GEMINI_TRANSCRIBE_MODEL',
    'gemini_analysis_model': 'GEMINI_ANALYSIS_MODEL',
    'gemini_extract_model': 'GEMINI_EXTRACT_MODEL',
}

# 非秘密、可清空（空 = 回默认）的设置字段
_PLAIN_FIELDS = ('gemini_base_url', 'whisper_model', 'gemini_transcribe_model',
                 'gemini_analysis_model', 'gemini_extract_model')


def _load_settings():
    try:
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _apply_settings_to_env(data):
    for field, env in _SETTING_ENV.items():
        val = (data.get(field) or '').strip()
        if val:
            os.environ[env] = val


_apply_settings_to_env(_load_settings())

tasks = {}
# task_id -> 最近的转写进度百分比（供链条详情页在封面上显示 %）
_task_progress = {}


# ========== 可选鉴权 ==========
# 不设置 GETAUDIO_TOKEN（默认）时下面两个钩子等于不存在，本地使用完全无感。
# 设置后：首次访问带 ?token=xxx 或 Authorization: Bearer xxx，之后走 cookie。

_AUTH_COOKIE = 'getaudio_token'


@app.before_request
def _check_auth():
    if not config.AUTH_TOKEN:
        return None
    if request.path.startswith('/static/'):
        return None

    supplied = (
        request.cookies.get(_AUTH_COOKIE)
        or request.args.get('token')
        or request.headers.get('Authorization', '').replace('Bearer ', '', 1).strip()
    )
    # 常量时间比较，避免定时侧信道
    if supplied and hmac.compare_digest(str(supplied), str(config.AUTH_TOKEN)):
        return None
    return jsonify({'error': 'Unauthorized — append ?token=YOUR_TOKEN to the URL, or send an Authorization: Bearer header.'}), 401


@app.after_request
def _persist_auth_cookie(resp):
    # 通过 ?token= 验证成功的请求，把令牌种进 cookie，后续请求免带参数
    if config.AUTH_TOKEN and request.args.get('token') == config.AUTH_TOKEN:
        resp.set_cookie(
            _AUTH_COOKIE, config.AUTH_TOKEN,
            max_age=30 * 24 * 3600, httponly=True, samesite='Lax',
        )
    return resp

# 批量转录：所有任务都提交到同一个线程池，池子开得足够大（不成为瓶颈），
# 真正的并发上限由每个引擎各自的信号量控制（见 config.ENGINE_CONCURRENCY）。
# 这样用户可以一次丢进很多文件——云引擎几乎同时开跑，本地 Whisper 自动排队。
_pool_size = max(sum(config.ENGINE_CONCURRENCY.values()), 4)
executor = ThreadPoolExecutor(max_workers=_pool_size)
_engine_semaphores = {
    engine: threading.Semaphore(n)
    for engine, n in config.ENGINE_CONCURRENCY.items()
}

# 链条的全局限流闸（所有链条共享，不按每条链条算）——见 config 里的说明。
# 无论开多少条链，下载/分析对外部的瞬时压力都封顶，不会"并行叠并行"打爆。
_chain_download_sem = threading.Semaphore(config.CHAIN_DOWNLOAD_CONCURRENCY)
_chain_analysis_sem = threading.Semaphore(config.CHAIN_ANALYSIS_CONCURRENCY)
# 下载本身也丢进一个线程池并发跑（受上面的信号量真正限流）
_download_executor = ThreadPoolExecutor(max_workers=config.CHAIN_DOWNLOAD_CONCURRENCY)


def allowed_file(filename):
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS


def format_seconds(s):
    total = int(s)
    minutes = total // 60
    seconds = total % 60
    return f"{minutes:02d}:{seconds:02d}"


def _shift_ts(ts, offset_sec):
    """把 'MM:SS'/'HH:MM:SS' 时间戳字符串整体加偏移秒，再格式化回去。取不到就原样返回。"""
    from sanitize import _ts_to_seconds
    sec = _ts_to_seconds(ts)
    if sec is None:
        return ts
    return format_seconds(sec + offset_sec)


def _offset_segments(segments, offset_sec):
    """片段转写（只截了 10:00–25:00）出来的时间戳从 0 起 → 加偏移显示成原视频真实位置。"""
    if not offset_sec:
        return segments
    for seg in segments:
        if seg.get('timestamp'):
            seg['timestamp'] = _shift_ts(seg['timestamp'], offset_sec)
        if seg.get('end'):
            seg['end'] = _shift_ts(seg['end'], offset_sec)
    return segments


def is_video_file(filepath):
    ext = os.path.splitext(filepath)[1].lower().lstrip('.')
    return ext in config.VIDEO_EXTENSIONS


def resolve_ffmpeg_binary():
    return shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'


def resolve_ffprobe_binary():
    return shutil.which('ffprobe') or '/opt/homebrew/bin/ffprobe'


def probe_audio_duration_seconds(filepath):
    """Return audio duration in seconds, or None if ffprobe unavailable / failed."""
    ffprobe_bin = resolve_ffprobe_binary()
    if not os.path.exists(ffprobe_bin):
        return None
    try:
        result = subprocess.run(
            [
                ffprobe_bin, '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                filepath,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None


def extract_audio_from_video(video_path, output_path):
    ffmpeg_bin = resolve_ffmpeg_binary()
    if not os.path.exists(ffmpeg_bin):
        raise RuntimeError('未检测到 ffmpeg，无法从视频中提取音频')

    # 先看有没有音轨——屏幕录制经常没录声音，直接给清楚的原因，别让 ffmpeg 报一句晦涩的
    ffprobe_bin = os.path.join(os.path.dirname(ffmpeg_bin), 'ffprobe')
    if os.path.exists(ffprobe_bin):
        try:
            pr = subprocess.run(
                [ffprobe_bin, '-v', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', video_path],
                capture_output=True, text=True, timeout=60)
            if pr.returncode == 0 and 'audio' not in (pr.stdout or ''):
                raise RuntimeError('这个视频没有音轨（没有声音），没有可转写的内容——'
                                   '屏幕录制很常见这种情况。')
        except subprocess.TimeoutExpired:
            pass

    cmd = [
        ffmpeg_bin, '-y', '-v', 'error',
        '-i', video_path,
        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
        output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        # 把 ffmpeg 真正的报错（stderr 末几行）带出来，别只显示一句命令
        lines = [ln for ln in (r.stderr or '').strip().splitlines() if ln.strip()]
        reason = ' / '.join(lines[-3:])[:300] if lines else f'exit code {r.returncode}'
        raise RuntimeError(f'ffmpeg 提取音频失败：{reason}')
    return output_path


def _run_summary(full_text, q, use_qwen=False):
    try:
        from summarize import summarize_transcript

        q.put(json.dumps({
            'type': 'progress',
            'percent': 95,
            'message': '正在生成内容总结...',
        }))
        summary_data = summarize_transcript(full_text, use_qwen=use_qwen)
        if summary_data:
            q.put(json.dumps({'type': 'summary', **summary_data}))
        return summary_data
    except Exception:
        return None


def _maybe_sanitize(segments, audio_path, duration=None):
    """转写「证伪层」：清掉静音幻听、复读死循环、坏时间戳 + 重建单调时间轴。

    只处理 {timestamp,text} 形态（Gemini/Precise/阿里云）；Whisper 的
    {start,end} 形态已在解码层用 condition_on_previous_text=False 等治理，跳过。
    返回 (clean_segments, report_or_None)；report=None 表示没动过。
    任何异常都吞掉、原样返回——证伪层绝不许拖垮保存。
    """
    if not segments or 'timestamp' not in segments[0]:
        return segments, None
    try:
        from sanitize import (clean_transcript, detect_silence,
                              _is_micro, _looks_filler, _core)
    except Exception:
        return segments, None
    try:
        # 先跑纯文字清洗 + 时间轴重建（零成本，抓成片 filler / 复读 / 坏时间戳 / Precise 乱时间戳）
        clean, report = clean_transcript(segments, duration=duration)
        # 残留可疑：还剩不少「孤立语气词微段」→ 才值得回音频取静音轴深清一遍
        residual = sum(1 for s in clean
                       if _is_micro(s) and _looks_filler(_core(s['text'])))
        if residual >= 8 and audio_path and os.path.isfile(audio_path):
            silence = detect_silence(audio_path)
            if silence:
                clean, report = clean_transcript(
                    segments, silence_intervals=silence, duration=duration)
        changed = any(report.get(k) for k in
                      ('removed', 'loops', 'bad_ts', 'silence_dropped',
                       'ts_repaired', 'intra_loops'))
        return (clean, report) if changed else (clean, None)
    except Exception:
        return segments, None


def _save_results(task_id, original_filename, engine, audio_source_path,
                  segments, summary, model_review=False):
    """Persist transcription results to results/<task_id>/。

    model_review=True：规则清洗之后，再送模型体检一遍（模型只标垃圾、代码删），
    捞规则漏掉的循环/复读/碎片。单文件转写路径开、链条走白嫖不在这开。
    """
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    os.makedirs(task_dir, exist_ok=True)

    ext = os.path.splitext(audio_source_path)[1].lower()
    audio_dest = os.path.join(task_dir, f"audio{ext}")
    shutil.copy2(audio_source_path, audio_dest)

    duration = probe_audio_duration_seconds(audio_dest)

    # 证伪层：清洗前先留住原始稿，只有真删了东西才落 transcript_raw.json
    raw_segments = segments
    segments, san_report = _maybe_sanitize(segments, audio_source_path, duration)

    # 模型体检（可选）：在规则清洗后的稿上，让模型标出残余垃圾，代码删
    if model_review and segments and 'timestamp' in (segments[0] or {}):
        try:
            from analyze import review_transcript
            drop = review_transcript(segments)
            if drop:
                segments = [s for i, s in enumerate(segments) if i not in drop]
                san_report = dict(san_report or {})
                san_report['model_dropped'] = len(drop)
        except Exception:  # noqa: BLE001  体检失败绝不拖垮保存
            pass

    meta = {
        'id': task_id,
        'filename': os.path.basename(original_filename or ''),
        'engine': engine,
        'date': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_ext': ext,
        'segment_count': len(segments),
        'duration_seconds': round(duration, 2) if duration else None,
        'has_summary': bool(summary),
    }
    if san_report:
        meta['sanitized'] = san_report
    with open(os.path.join(task_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(task_dir, 'transcript.json'), 'w', encoding='utf-8') as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    if san_report:  # 保留清洗前的原始稿，随时可回溯 / 对比
        with open(os.path.join(task_dir, 'transcript_raw.json'), 'w', encoding='utf-8') as f:
            json.dump(raw_segments, f, ensure_ascii=False, indent=2)

    if summary:
        with open(os.path.join(task_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


# 云引擎「内容拦截」标记：确定性拒绝（版权/安全/敏感），重试同一引擎毫无意义
_CONTENT_BLOCK_MARKERS = (
    'RECITATION', 'PROHIBITED_CONTENT', 'SAFETY', 'BLOCKLIST', 'SPII',
    '内容被安全过滤', '提示词被拦截', '重试无效', 'block_reason',
)


def _is_content_block(err):
    """是不是云引擎的确定性内容拦截（区别于网络/限流这类间歇失败）。"""
    s = str(err)
    return any(m in s for m in _CONTENT_BLOCK_MARKERS)


def run_transcription(task_id, filepath, engine, original_filename, q,
                      speaker_count=None, fallback_whisper=False,
                      model_review=False, offset_sec=0):
    """Background worker: runs transcription, saves results, pushes events.

    每个引擎有独立信号量限流。任务提交后可能先排队（quota 已满），
    抢到信号量后才真正开跑，所以先推一条 queued，再推 progress。
    """

    def progress_cb(percent):
        _task_progress[task_id] = percent
        q.put(json.dumps({
            'type': 'progress',
            'percent': percent,
            'message': f'Transcribing... {percent}%',
        }))

    cleanup_paths = [filepath]
    input_path = filepath

    # 排队等待本引擎的并发额度
    sem = _engine_semaphores.get(engine)
    q.put(json.dumps({'type': 'queued', 'message': 'Queued...'}))
    if sem is not None:
        sem.acquire()

    try:
        taskdb.set_status(task_id, 'running')
        q.put(json.dumps({
            'type': 'progress',
            'percent': 1,
            'message': 'Starting transcription...',
        }))
        if is_video_file(filepath):
            q.put(json.dumps({
                'type': 'progress',
                'percent': 2,
                'message': 'Extracting audio from video...',
            }))
            audio_path = os.path.join(
                app.config['UPLOAD_FOLDER'],
                f"{task_id}_audio.wav",
            )
            input_path = extract_audio_from_video(filepath, audio_path)
            cleanup_paths.append(input_path)

        segments = []
        summary_data = None

        def _finish_ok(segs, summary, engine_used):
            """成功收尾：落盘 + enrich + 标记 done + 压缩音频（主路径/兜底路径共用）。"""
            # 片段截取（如只转 10:00–25:00）：把 0 起的时间戳整体加偏移，
            # 落盘 + done 事件都带真实位置。放这里是所有引擎/兜底路径的唯一收口。
            segs = _offset_segments(segs, offset_sec)
            _save_results(task_id, original_filename, engine_used, input_path,
                          segs, summary, model_review=model_review)
            try:
                from enrich import enrich_task
                enrich_task(os.path.join(config.RESULTS_FOLDER, task_id))
            except Exception:
                pass
            taskdb.set_status(task_id, 'done')
            q.put(json.dumps({
                'type': 'done',
                'task_id': task_id,
                'segments': segs,
                'summary': summary,
            }))
            try:
                from audioutil import compress_task
                compress_task(os.path.join(config.RESULTS_FOLDER, task_id))
            except Exception:
                pass

        def _whisper_transcribe():
            """本地 Whisper 转写（主路径 + 云引擎失败时的兜底路径共用）。"""
            from transcribe_whisper import transcribe_audio

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': 'Loading Whisper model (first run may download)...',
            }))
            raw_segments = transcribe_audio(input_path, progress_callback=progress_cb)
            segs = []
            for seg in raw_segments:
                item = {
                    'timestamp': format_seconds(seg['start']),
                    'end': format_seconds(seg['end']),
                    'text': seg['text'].strip(),
                }
                segs.append(item)
                q.put(json.dumps({'type': 'segment', **item}))
            full = "\n".join(f"[{s['timestamp']}] {s['text']}" for s in segs)
            return segs, full

        if engine == 'whisper':
            segments, full_text = _whisper_transcribe()
            summary_data = _run_summary(full_text, q)

        elif engine == 'gemini':
            from transcribe_gemini import transcribe_audio

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': '正在上传文件到 Gemini...',
            }))

            segments, full_text = transcribe_audio(
                input_path, progress_callback=progress_cb
            )

            for seg in segments:
                q.put(json.dumps({'type': 'segment', **seg}))

            summary_data = _run_summary(full_text, q)

        elif engine in ('qwenasr', 'dashscope'):
            # 阿里云 ASR。两个 key 都走这条链路：qwenasr 是现用引擎，dashscope 是
            # 老转写留下的引擎名（保留可用，重转时同样落到当前模型）。
            from transcribe_dashscope import transcribe_audio

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': '正在上传文件到阿里云...',
            }))

            segments = transcribe_audio(
                input_path, progress_callback=progress_cb
            )

            for seg in segments:
                q.put(json.dumps({'type': 'segment', **seg}))

            full_text = "\n".join(
                f"[{s['timestamp']}] {s['text']}" for s in segments
            )
            summary_data = _run_summary(full_text, q, use_qwen=True)

        elif engine == 'precise':
            # 精准模式：Gemini 转写(文字) + 阿里云分离(说话人) 并行跑，最后 Gemini 合并
            from transcribe_gemini import transcribe_audio as _gemini_tx
            from transcribe_dashscope import transcribe_audio as _dashscope_tx
            from transcribe_precise import (
                merge_speaker_transcript,
                speaker_only_segments,
            )

            q.put(json.dumps({
                'type': 'progress',
                'percent': 10,
                'message': '精准模式：Gemini 转写 + 阿里云说话人分离（并行中）...',
            }))

            holder = {}

            def _do_gemini():
                try:
                    segs, _full = _gemini_tx(input_path)
                    holder['gemini'] = segs
                except Exception as e:  # noqa: BLE001
                    holder['gemini_err'] = e

            def _do_dashscope():
                try:
                    holder['dashscope'] = _dashscope_tx(
                        input_path, diarization=True,
                        speaker_count=speaker_count,
                    )
                except Exception as e:  # noqa: BLE001
                    holder['dashscope_err'] = e

            tg = threading.Thread(target=_do_gemini)
            td = threading.Thread(target=_do_dashscope)
            tg.start()
            td.start()
            tg.join()
            td.join()

            g = holder.get('gemini')
            d = holder.get('dashscope')

            if g and d:
                q.put(json.dumps({
                    'type': 'progress',
                    'percent': 70,
                    'message': '正在合并说话人与文字...',
                }))
                segments, full_text = merge_speaker_transcript(g, d)
            elif d and not g:
                # Gemini 失败（常见：安全过滤）→ 降级用阿里云的说话人稿
                q.put(json.dumps({
                    'type': 'progress',
                    'percent': 70,
                    'message': 'Gemini 未成功，降级为阿里云说话人稿',
                }))
                segments = speaker_only_segments(d)
                full_text = "\n".join(
                    f"[{s['timestamp']}] {s['text']}" for s in segments
                )
            elif g and not d:
                # 阿里云失败 → 只有 Gemini 文字，无说话人
                q.put(json.dumps({
                    'type': 'progress',
                    'percent': 70,
                    'message': 'Diarization failed — text only (no speakers)',
                }))
                segments = g
                full_text = "\n".join(
                    f"[{s['timestamp']}] {s['text']}" for s in segments
                )
            else:
                raise RuntimeError(
                    f"精准模式失败：Gemini={holder.get('gemini_err')}; "
                    f"阿里云={holder.get('dashscope_err')}"
                )

            for seg in segments:
                q.put(json.dumps({'type': 'segment', **seg}))

            summary_data = _run_summary(full_text, q)

        else:
            taskdb.set_status(task_id, 'failed', error=f'Unknown engine: {engine}')
            q.put(json.dumps({
                'type': 'error',
                'message': f'Unknown engine: {engine}',
            }))
            return

        _finish_ok(segments, summary_data, engine)

    except Exception as e:
        # 默认：失败就失败（原引擎内部已重试；Continue 会用原引擎再试一轮）。
        # 只有明确勾了"失败兜底 Whisper"才自动改用本地 Whisper——不偷偷换引擎/降质量。
        # 例外：**内容拦截**（RECITATION/PROHIBITED 等确定性拒绝）无视开关直接落 Whisper——
        # 因为重试同一云引擎永远是白搭，只有 Whisper 或放弃两条路。
        content_block = _is_content_block(e)
        fell_back = False
        if engine != 'whisper' and (fallback_whisper or content_block):
            try:
                why = ('Content-blocked by Gemini (deterministic)' if content_block
                       else f'{engine} failed ({str(e)[:50]})')
                q.put(json.dumps({
                    'type': 'progress', 'percent': 0,
                    'message': f'{why} — falling back to local Whisper...',
                }))
                # 换并发闸：放掉云引擎额度，改排 Whisper 的队（本地 CPU 只允许 2 路，
                # 不然 12 路 whisper 同时烧 CPU）。finally 里统一释放当前 sem。
                if sem is not None:
                    sem.release()
                sem = _engine_semaphores.get('whisper')
                if sem is not None:
                    sem.acquire()
                segments, full_text = _whisper_transcribe()
                summary_data = _run_summary(full_text, q)
                _finish_ok(segments, summary_data, 'whisper')
                fell_back = True
            except Exception as e2:  # noqa: BLE001
                e = e2
        if not fell_back:
            taskdb.set_status(task_id, 'failed', error=str(e))
            q.put(json.dumps({
                'type': 'error',
                'message': str(e),
            }))

    finally:
        if sem is not None:
            sem.release()
        # 只在任务成功后删源音频；失败保留（Continue 补全时直接重转，不用重下载）
        row = taskdb.get(task_id)
        if row and row.get('status') == 'done':
            for path in cleanup_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
        # worker 完成后才从全局表里清掉自己，
        # 这样客户端断开/刷新后重连依然能读到队列里剩下的消息。
        tasks.pop(task_id, None)
        _task_progress.pop(task_id, None)


# ========== Pages ==========

@app.route('/')
def index():
    return render_template('index.html')


# ========== Upload & Stream ==========

def _parse_speaker_count(raw):
    """解析前端传来的预计人数；非法/空则返回 None（让阿里云自动判断）。"""
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _enqueue_task(file, engine, speaker_count=None):
    """校验并保存单个文件，建队列并提交到线程池。

    返回 (task_id, None) 成功，或 (None, error_message) 失败。
    单文件 /upload 和批量 /upload_batch 共用此逻辑。
    """
    if not file or file.filename == '':
        return None, '请选择一个音频或视频文件'

    if not allowed_file(file.filename):
        return None, f'不支持的文件格式。支持: {", ".join(config.ALLOWED_EXTENSIONS)}'

    task_id = str(uuid.uuid4())
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp3'
    filename = f"{task_id}.{ext}"
    filepath = os.path.join(config.UPLOAD_FOLDER, filename)
    file.save(filepath)

    # 先落库再入队：服务中途重启也能从 DB 找回这个任务
    taskdb.create(task_id, file.filename, engine, speaker_count, filepath)

    q = queue.Queue()
    tasks[task_id] = q

    # 模型体检：只对【云引擎】转的稿开（内容本就上了云，体检不增加隐私暴露）；
    # 本地 Whisper 转的（多半是为隐私留本地的）不送云体检。
    executor.submit(
        run_transcription, task_id, filepath, engine, file.filename, q,
        speaker_count, False, engine != 'whisper',
    )

    return task_id, None


def _resolve_local_path(raw):
    """把用户粘进来的路径规整成真实路径。

    从访达「拷贝为路径名称」或拖进终端拿到的路径，空格/括号等会被转义成 `\\ `，
    整条也可能被引号包起来。逐个候选去试，返回第一个真实存在的。
    """
    raw = (raw or '').strip()
    cands, seen = [], set()

    def add(p):
        p = os.path.expanduser(p)
        if p and p not in seen:
            seen.add(p)
            cands.append(p)

    add(raw)
    # 去掉成对引号
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        add(raw[1:-1])
    # 拆 shell 转义反斜杠：`\ ` → ` `、`\(` → `(` 等
    add(re.sub(r'\\(.)', r'\1', raw))
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        add(re.sub(r'\\(.)', r'\1', raw[1:-1]))

    for c in cands:
        if os.path.isfile(c):
            return c
    return cands[0] if cands else raw


def _enqueue_local_task(path, engine, speaker_count=None):
    """用本机已有文件建任务：软链进 uploads/（零拷贝、零上传），把软链喂给流水线。

    关键安全点：流水线结束会 os.remove(输入) —— 删的是软链，**绝不动你的原文件**
    （提取音频写的是新文件、保存结果用 copy2 读取，都不改原件）。
    返回 (task_id, None) 或 (None, error)。
    """
    if not (path or '').strip():
        return None, 'Enter a file path'
    path = _resolve_local_path(path)
    if not os.path.isfile(path):
        return None, f'File not found: {path}'
    if not allowed_file(path):
        return None, f'Unsupported format. Allowed: {", ".join(sorted(config.ALLOWED_EXTENSIONS))}'
    # macOS 会挡住进程读 Downloads/Desktop 这类隐私目录（TCC）——趁早给清楚的原因，
    # 别等 ffmpeg 报一句 "Operation not permitted"
    try:
        with open(path, 'rb') as _fh:
            _fh.read(1)
    except PermissionError:
        return None, ('macOS is blocking access to this folder (Downloads and Desktop are '
                      'privacy-protected). Move the file into Documents or a plain folder '
                      'under your home, or grant Full Disk Access to whatever launches '
                      'Verbatim in System Settings → Privacy & Security.')
    except OSError as e:
        return None, f'Cannot read the file: {e}'

    task_id = str(uuid.uuid4())
    ext = path.rsplit('.', 1)[1].lower() if '.' in path else 'mp3'
    link = os.path.join(config.UPLOAD_FOLDER, f"{task_id}.{ext}")
    try:
        os.symlink(os.path.abspath(path), link)
    except OSError as e:
        return None, f'Could not link the file: {e}'

    display = os.path.basename(path)
    taskdb.create(task_id, display, engine, speaker_count, link)
    q = queue.Queue()
    tasks[task_id] = q
    executor.submit(
        run_transcription, task_id, link, engine, display, q,
        speaker_count, False, engine != 'whisper',
    )
    return task_id, None


def recover_unfinished_tasks():
    """启动时找回上次没跑完的任务：源文件还在就重新入队，不在就标记失败。

    同时清掉 uploads/ 里不属于任何待恢复任务的孤儿文件。
    """
    rows = taskdb.unfinished()
    active_ids = set()
    recovered = 0

    for row in rows:
        task_id = row['id']
        upload_path = row.get('upload_path') or ''
        if upload_path and os.path.isfile(upload_path):
            taskdb.set_status(task_id, 'pending')
            q = queue.Queue()
            tasks[task_id] = q
            executor.submit(
                run_transcription, task_id, upload_path, row.get('engine'),
                row.get('filename'), q, row.get('speaker_count'),
            )
            active_ids.add(task_id)
            recovered += 1
        else:
            taskdb.set_status(
                task_id, 'failed', error='服务重启且源文件已丢失，请重新上传'
            )

    # 孤儿上传文件清理。只删真孤儿：taskdb 里查无此任务、或任务已 done
    # （done 的音频已复制进 results/，upload 副本没用了）。
    # failed 但音频还在的必须保留——Continue 靠它"直接重转、不用重下载"。
    cleaned = 0
    for name in os.listdir(config.UPLOAD_FOLDER):
        tid = name.split('.', 1)[0]
        row = taskdb.get(tid) if _is_valid_task_id(tid) else None
        if row and row.get('status') != 'done':
            continue                     # 未完成任务的音频：保留给 Continue
        try:
            os.remove(os.path.join(config.UPLOAD_FOLDER, name))
            cleaned += 1
        except OSError:
            pass

    if recovered or cleaned:
        print(f"[recover] 找回未完成任务 {recovered} 个，清理孤儿上传文件 {cleaned} 个")


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('audio')
    engine = request.form.get('engine', 'whisper')
    speaker_count = _parse_speaker_count(request.form.get('speaker_count'))

    task_id, error = _enqueue_task(file, engine, speaker_count)
    if error:
        return jsonify({'error': error}), 400

    return jsonify({'task_id': task_id})


@app.route('/api/transcribe_local', methods=['POST'])
def api_transcribe_local():
    """本地文件直采：给个本机路径，零上传（软链，不拷贝、不删原件）。适合大视频。"""
    body = request.get_json(silent=True) or {}
    task_id, error = _enqueue_local_task(
        body.get('path'), body.get('engine', 'whisper'),
        _parse_speaker_count(body.get('speaker_count')))
    if error:
        return jsonify({'error': error}), 400
    return jsonify({'task_id': task_id})


def _download_then_transcribe(task_id, url, engine, q, section=None, offset_sec=0):
    """单个视频链接：先下音频，再走正常转写任务（进 Library，和上传的稿一样）。

    section/offset_sec：只转某时间段（如 10:00–25:00）时，section 传给 yt-dlp
    只切那一段，offset_sec 把字幕时间戳还原成原视频真实位置。
    """
    from downloader import download_one
    dl_dir = os.path.join(config.UPLOAD_FOLDER, f'url_{task_id}')
    try:
        msg = 'Downloading clip…' if section else 'Downloading audio…'
        q.put(json.dumps({'type': 'progress', 'percent': 1, 'message': msg}))
        item = download_one({'video_url': url}, dl_dir, section=section)
        if not item or not item.get('path') or not os.path.isfile(item['path']):
            taskdb.set_status(task_id, 'failed',
                              error='Download failed — bad link, private/removed video, or geo-blocked.')
            q.put(json.dumps({'type': 'error', 'message': 'Download failed — check the link.'}))
            return
        title = item.get('title') or url
        # 复用正常转写链路：落 results/、taskdb done、SSE 推进度，全和上传一致
        run_transcription(task_id, item['path'], engine, title, q,
                          None, False, engine != 'whisper', offset_sec=offset_sec)
    except Exception as e:  # noqa: BLE001
        taskdb.set_status(task_id, 'failed', error=str(e)[:300])
        q.put(json.dumps({'type': 'error', 'message': str(e)[:200]}))
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


def _parse_url_section(line):
    """拆出行尾可选的时间段后缀 ' @start-end' → (url, section, offset_sec)。

    start/end 支持 MM:SS / HH:MM:SS / 纯秒；end 可省略（到片尾）。
    section 是 yt-dlp --download-sections 的值 '*START-END'（秒）；无后缀返回 (line, None, 0)。
    """
    from sanitize import _ts_to_seconds
    m = re.search(r'\s+@\s*([0-9:]+)\s*-\s*([0-9:]*)\s*$', line)
    if not m:
        return line.strip(), None, 0
    url = line[:m.start()].strip()
    start = _ts_to_seconds(m.group(1))
    end = _ts_to_seconds(m.group(2)) if m.group(2) else None
    if start is None or (end is not None and end <= start):
        return url, None, 0          # 无效范围：忽略后缀，转整段
    section = f"*{start}-{end if end is not None else 'inf'}"
    return url, section, start


_COLLECTION_HINTS = ('list=', '/playlist', 'space.bilibili.com', 'collectiondetail',
                     'seriesdetail', '/lists', 'youtube.com/@', '/channel/', '/c/', '/user/')


def _looks_like_collection(url):
    """像不像合集/播放列表/频道（要展开成多条视频），而不是单个视频。"""
    u = (url or '').lower()
    if '/video/bv' in u or 'watch?v=' in u or 'youtu.be/' in u:
        return 'list=' in u          # 单视频；除非同时带播放列表参数
    return any(h in u for h in _COLLECTION_HINTS)


@app.route('/api/transcribe_urls', methods=['POST'])
def api_transcribe_urls():
    """获取视频内容：贴视频链接**或合集/播放列表链接** → 各自下载+转写，成独立 Library 任务。

    合集/播放列表/频道会先枚举出里面的视频（最多 max_videos 条）再逐条转写，
    全程只转写、不做分析（要分析整个博主走 Pipeline）。
    每条链接行尾可加 ' @10:00-25:00' 只转那一段（时间戳会还原成原视频位置）。
    """
    body = request.get_json(silent=True) or {}
    engine = body.get('engine', 'whisper')
    try:
        max_videos = max(1, min(300, int(body.get('max_videos') or 20)))
    except (TypeError, ValueError):
        max_videos = 20
    lines = [u.strip() for u in re.split(r'[\n,]+', body.get('urls') or '') if u.strip()]
    if not lines:
        return jsonify({'error': 'Paste at least one video link'}), 400

    targets, errors = [], []
    for line in lines[:20]:                      # 一次最多 20 行，防手滑
        url, section, offset_sec = _parse_url_section(line)
        # 合集/播放列表 → 先枚举（带时间段后缀的按单视频处理，段落语义只对单视频成立）
        if section is None and _looks_like_collection(url):
            try:
                from downloader import probe
                items, _ch = probe(url, max_videos)
            except Exception as e:  # noqa: BLE001
                errors.append(f'{url} → {str(e)[:120]}')
                continue
            if not items:
                errors.append(f'{url} → nothing found (private, or an unsupported link)')
                continue
            for it in items[:max_videos]:
                targets.append((it.get('video_url') or url, it.get('title'), None, 0))
        else:
            targets.append((url, None, section, offset_sec))

    if not targets:
        return jsonify({'error': '; '.join(errors) or 'Nothing to transcribe'}), 400

    out = []
    for url, title, section, offset_sec in targets:
        task_id = str(uuid.uuid4())
        taskdb.create(task_id, title or url, engine, None, '')
        q = queue.Queue()
        tasks[task_id] = q
        executor.submit(_download_then_transcribe, task_id, url, engine, q,
                        section, offset_sec)
        out.append({'url': url, 'title': title, 'task_id': task_id})
    return jsonify({'tasks': out, 'errors': errors})


@app.route('/upload_batch', methods=['POST'])
def upload_batch():
    """一次接收多个文件，各自建独立任务。

    返回每个文件的 task_id（或该文件的错误）。整体只要有至少一个成功
    就返回 200；全部失败返回 400。
    """
    files = request.files.getlist('audios')
    engine = request.form.get('engine', 'whisper')
    speaker_count = _parse_speaker_count(request.form.get('speaker_count'))

    if not files:
        return jsonify({'error': '请至少选择一个文件'}), 400

    results = []
    for file in files:
        task_id, error = _enqueue_task(file, engine, speaker_count)
        results.append({
            'filename': file.filename,
            'task_id': task_id,
            'error': error,
        })

    if not any(r['task_id'] for r in results):
        return jsonify({'error': '没有可处理的文件', 'tasks': results}), 400

    return jsonify({'tasks': results})


@app.route('/stream/<task_id>')
def stream(task_id):
    # 真正的超时上限：1 小时没有任何消息才算死任务
    HARD_TIMEOUT_SECONDS = 60 * 60
    # 单次 get 短轮询间隔：没消息就发 SSE 注释保活，避免代理/浏览器断流
    POLL_INTERVAL = 15

    def event_stream():
        q = tasks.get(task_id)
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Task not found'})}\n\n"
            return

        # 注意：这里不在 finally 里 pop tasks。
        # 客户端可能中途刷新/重连，pop 由后台 worker 在写完 done/error 之后负责，
        # 这样重连时还能继续读队列里剩下的消息。
        idle_seconds = 0
        while True:
            try:
                msg = q.get(timeout=POLL_INTERVAL)
                idle_seconds = 0
                yield f"data: {msg}\n\n"
                data = json.loads(msg)
                if data['type'] in ('done', 'error'):
                    break
            except queue.Empty:
                idle_seconds += POLL_INTERVAL
                if idle_seconds >= HARD_TIMEOUT_SECONDS:
                    yield (
                        f"data: {json.dumps({'type': 'error', 'message': '转写超时（后台无响应）'})}\n\n"
                    )
                    break
                # SSE 注释行：不会触发前端 onmessage，仅用于保活
                yield ": keepalive\n\n"

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ========== History API ==========

# task_id → 所属链条的博主名。用来把 Library 里「Pipeline 跑的」和「我自己弄的」分开。
# 直接从各 chain.json 的 videos 反查，所以历史数据也能分（不需要迁移 meta）。
_chain_task_cache = {'stamp': None, 'map': {}}


def _chain_task_map():
    """{task_id: 博主名}。按 _chains 目录的 mtime 做缓存，避免每次轮询读几十个 json。"""
    try:
        stamp = max([os.path.getmtime(CHAINS_DIR)] + [
            os.path.getmtime(os.path.join(CHAINS_DIR, n, 'chain.json'))
            for n in os.listdir(CHAINS_DIR)
            if os.path.isfile(os.path.join(CHAINS_DIR, n, 'chain.json'))
        ])
    except OSError:
        return {}
    if _chain_task_cache['stamp'] == stamp:
        return _chain_task_cache['map']
    m = {}
    try:
        for n in os.listdir(CHAINS_DIR):
            cpath = os.path.join(CHAINS_DIR, n, 'chain.json')
            if not os.path.isfile(cpath):
                continue
            try:
                with open(cpath, 'r', encoding='utf-8') as f:
                    c = json.load(f)
            except Exception:  # noqa: BLE001
                continue
            author = (c.get('author') or '').strip() or '(pipeline)'
            for v in (c.get('videos') or []):
                if v.get('task_id'):
                    m[v['task_id']] = author
    except OSError:
        return _chain_task_cache['map']
    _chain_task_cache.update(stamp=stamp, map=m)
    return m


@app.route('/api/history')
def api_history():
    """List all saved transcription sessions（附带 source：pipeline / mine）。"""
    results_dir = config.RESULTS_FOLDER
    entries = []

    if not os.path.isdir(results_dir):
        return jsonify(entries)

    chain_map = _chain_task_map()
    for name in os.listdir(results_dir):
        meta_path = os.path.join(results_dir, name, 'meta.json')
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    e = json.load(f)
            except Exception:
                continue
            author = chain_map.get(e.get('id') or name)
            e['source'] = 'pipeline' if author else 'mine'
            if author:
                e['creator'] = author
            entries.append(e)

    entries.sort(key=lambda e: e.get('date', ''), reverse=True)
    return jsonify(entries)


@app.route('/api/history/<task_id>')
def api_history_detail(task_id):
    """Get full data for a saved transcription."""
    if not _is_valid_task_id(task_id):
        return jsonify({'error': 'Invalid task id'}), 400

    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    meta_path = os.path.join(task_dir, 'meta.json')
    transcript_path = os.path.join(task_dir, 'transcript.json')

    if not os.path.isfile(meta_path):
        return jsonify({'error': 'Not found'}), 404

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    segments = []
    if os.path.isfile(transcript_path):
        with open(transcript_path, 'r', encoding='utf-8') as f:
            segments = json.load(f)

    summary = None
    summary_path = os.path.join(task_dir, 'summary.json')
    if os.path.isfile(summary_path):
        with open(summary_path, 'r', encoding='utf-8') as f:
            summary = json.load(f)

    return jsonify({**meta, 'segments': segments, 'summary': summary})


@app.route('/api/history/<task_id>/audio')
def api_history_audio(task_id):
    """Serve the saved audio file for a transcription."""
    if not _is_valid_task_id(task_id):
        return jsonify({'error': 'Invalid task id'}), 400

    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    meta_path = os.path.join(task_dir, 'meta.json')

    if not os.path.isfile(meta_path):
        return jsonify({'error': 'Not found'}), 404

    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    audio_ext = meta.get('audio_ext', '.wav')
    audio_path = os.path.join(task_dir, f"audio{audio_ext}")

    if not os.path.isfile(audio_path):
        return jsonify({'error': 'Audio file not found'}), 404

    mime_map = {
        '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.flac': 'audio/flac',
        '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.webm': 'audio/webm',
    }
    resp = send_file(
        audio_path,
        mimetype=mime_map.get(audio_ext, 'audio/wav'),
        conditional=True,
    )
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/api/history/<task_id>', methods=['DELETE'])
def api_history_delete(task_id):
    """Delete a saved transcription and its files."""
    if not _is_valid_task_id(task_id):
        return jsonify({'error': 'Invalid task id'}), 400

    # 任务还在转写中就不允许删，不然后台跑完还会重新创建目录
    if task_id in tasks:
        return jsonify({'error': '任务正在转写中，请等完成后再删除'}), 409

    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    if os.path.isdir(task_dir):
        shutil.rmtree(task_dir, ignore_errors=True)
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


# ========== AI 整理（批量回填卡片元数据） ==========

# 回填是幂等的后台任务：跳过已有 ai_title 的条目，所以随时可重跑
_enrich_state = {'running': False, 'done': 0, 'total': 0, 'failed': 0}
_enrich_lock = threading.Lock()


def _run_enrich_all():
    from concurrent.futures import ThreadPoolExecutor as _TPE

    from enrich import enrich_task

    results_dir = config.RESULTS_FOLDER
    pending = []
    for name in os.listdir(results_dir):
        meta_path = os.path.join(results_dir, name, 'meta.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            continue
        if not meta.get('ai_title'):
            pending.append(os.path.join(results_dir, name))

    _enrich_state.update(done=0, failed=0, total=len(pending))

    def _one(task_dir):
        ok = False
        try:
            ok = enrich_task(task_dir)
        except Exception:
            ok = False
        with _enrich_lock:
            _enrich_state['done'] += 1
            if not ok:
                _enrich_state['failed'] += 1

    # Flash 模型轻量调用，8 并发在 Paid Tier 1 下安全
    with _TPE(max_workers=8) as pool:
        list(pool.map(_one, pending))

    _enrich_state['running'] = False


@app.route('/api/enrich_all', methods=['POST'])
def api_enrich_all():
    """后台批量给缺少 AI 标题的历史条目生成卡片元数据。"""
    if _enrich_state['running']:
        return jsonify({'ok': True, 'already_running': True, **_enrich_state})
    _enrich_state['running'] = True
    threading.Thread(target=_run_enrich_all, daemon=True).start()
    return jsonify({'ok': True, **_enrich_state})


@app.route('/api/enrich_status')
def api_enrich_status():
    return jsonify(_enrich_state)


# ========== Search ==========

@app.route('/api/search')
def api_search():
    """全文搜索：文件名 / AI标题 / 标签 / 转写正文，返回带命中片段的条目列表。"""
    query = (request.args.get('q') or '').strip().lower()
    if not query:
        return jsonify([])

    results_dir = config.RESULTS_FOLDER
    hits = []
    if not os.path.isdir(results_dir):
        return jsonify(hits)

    for name in os.listdir(results_dir):
        task_dir = os.path.join(results_dir, name)
        meta_path = os.path.join(task_dir, 'meta.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            continue

        haystacks = [
            meta.get('filename', ''),
            meta.get('ai_title', ''),
            meta.get('ai_one_line', ''),
            ' '.join(meta.get('ai_tags', []) or []),
        ]
        snippet = ''
        matched = any(query in h.lower() for h in haystacks if h)

        if not matched:
            transcript_path = os.path.join(task_dir, 'transcript.json')
            if os.path.isfile(transcript_path):
                try:
                    with open(transcript_path, 'r', encoding='utf-8') as f:
                        segs = json.load(f)
                    for s in segs:
                        text = s.get('text', '')
                        idx = text.lower().find(query)
                        if idx != -1:
                            start = max(0, idx - 20)
                            snippet = (
                                f"[{s.get('timestamp', '')}] "
                                f"...{text[start:idx + len(query) + 40]}..."
                            )
                            matched = True
                            break
                except Exception:
                    pass

        if matched:
            author = _chain_task_map().get(meta.get('id') or name)
            hits.append({**meta, 'snippet': snippet,
                         'source': 'pipeline' if author else 'mine',
                         **({'creator': author} if author else {})})

    hits.sort(key=lambda e: e.get('date', ''), reverse=True)
    return jsonify(hits)


@app.route('/api/history', methods=['DELETE'])
def api_history_clear():
    """Delete ALL saved transcriptions in one shot.

    Skips directories that belong to currently running tasks so we don't
    nuke an in-flight job's output folder.
    """
    results_dir = config.RESULTS_FOLDER
    if not os.path.isdir(results_dir):
        return jsonify({'ok': True, 'deleted': 0, 'skipped': 0})

    deleted = 0
    skipped = 0
    for name in os.listdir(results_dir):
        if not _is_valid_task_id(name):
            continue
        if name in tasks:
            skipped += 1
            continue
        task_dir = os.path.join(results_dir, name)
        if os.path.isdir(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)
            deleted += 1

    return jsonify({'ok': True, 'deleted': deleted, 'skipped': skipped})


# ========== 链条：URL → 下载音频 → 转写 → 逐期分析 → 总合成 ==========
#
# 复刻多 agent workflow 的思路（每期独立分析防丢信息 → 最后综合），
# 但用纯产品代码实现：一个链条 = 一个后台线程，状态持久化在
# results/_chains/<chain_id>/chain.json，产物（逐期分析 md + 总分析.md）同目录。

CHAINS_DIR = os.path.join(config.RESULTS_FOLDER, '_chains')
os.makedirs(CHAINS_DIR, exist_ok=True)

_CHAIN_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_MAX_CHAIN_VIDEOS = 300  # 防手滑整个频道几千个视频全下下来

# 协作式取消：/stop 往里加 chain_id，运行中的链条在安全点自查并收尾（已完成产物保留）。
_cancel_chains = set()
# chain.json 的唯一写锁：run_chain、单视频重转、backfill、详情自愈都从这里过，
# 串行化 + 原子写，防并发交错/丢更新/写一半崩溃损坏文件。可重入（RLock）以便
# 读-改-写（先持锁读、改、再调 _save_chain 写）不自锁。
_chain_write_lock = threading.RLock()


def _chain_dir(chain_id):
    return os.path.join(CHAINS_DIR, chain_id)


def _update_meta(meta_path, updates):
    """读最新 meta → 合并 → 原子写。避免和 enrich/compress 并发写丢字段（如 stats 覆盖 ai_title）。"""
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            m = json.load(f)
    except Exception:  # noqa: BLE001
        m = {}
    m.update(updates)
    tmp = meta_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(m, f, ensure_ascii=False, indent=2)
        os.replace(tmp, meta_path)
    except Exception:  # noqa: BLE001
        pass


def _save_chain(state):
    """原子写 chain.json：临时文件 + os.replace，持全局锁。所有 chain.json 写都走这。"""
    path = os.path.join(_chain_dir(state['id']), 'chain.json')
    tmp = path + '.tmp'
    with _chain_write_lock:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


_VID_IN_NAME = re.compile(r'\[([A-Za-z0-9_-]{6,20})\]')


def _video_id_index():
    """{video_id: task_id}：扫所有已完成转写，从 meta.filename 里的 [id] 回填。

    用于去重复用——同一个视频（同 video_id）之前转写过就直接拿旧结果。
    """
    idx = {}
    rd = config.RESULTS_FOLDER
    if not os.path.isdir(rd):
        return idx
    for name in os.listdir(rd):
        if name.startswith('_') or not _is_valid_task_id(name):
            continue
        d = os.path.join(rd, name)
        if not os.path.isfile(os.path.join(d, 'transcript.json')):
            continue
        try:
            with open(os.path.join(d, 'meta.json'), 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            continue
        m = _VID_IN_NAME.search(meta.get('filename', '') or '')
        if m:
            idx.setdefault(m.group(1), name)
    return idx


def _save_subtitle_task(task_id, target, segments, source):
    """把抓来的字幕当作转写结果落盘（无音频），并在 taskdb 里标记 done。

    这样它和普通转写任务一样进历史、进分析，只是引擎标为 subtitle、没有音频回放。
    """
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    os.makedirs(task_dir, exist_ok=True)
    title = target.get('title') or target.get('video_id') or 'untitled'
    display = f"{title} [{target.get('video_id', '')}]"
    meta = {
        'id': task_id,
        'filename': display,
        'engine': 'subtitle',
        'date': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_ext': None,                    # 字幕来源，无音频
        'segment_count': len(segments),
        'duration_seconds': None,
        'has_summary': False,
        'subtitle_source': source,            # manual / auto
    }
    with open(os.path.join(task_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with open(os.path.join(task_dir, 'transcript.json'), 'w', encoding='utf-8') as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)
    taskdb.create(task_id, display, 'subtitle', None, None)
    taskdb.set_status(task_id, 'done')
    try:
        from enrich import enrich_task
        enrich_task(task_dir)
    except Exception:
        pass


def _engine_used(task_id):
    """读任务落盘 meta 里的实际转写引擎（云失败落 whisper 时会与链条引擎不同）。"""
    try:
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'),
                  'r', encoding='utf-8') as f:
            return json.load(f).get('engine') or ''
    except Exception:
        return ''


def _sync_video_with_taskdb(v):
    """让一个视频条目的状态对齐 taskdb 真实状态。返回是否有改动。

    治"卡片假死"：任务级 recover 重转完成后，链条卡片仍冻在旧状态——
    这里按 task_id 查真实结果回写。
    """
    tid = v.get('task_id')
    if not tid:
        return False
    row = taskdb.get(tid)
    if not row:
        return False
    st = row.get('status')
    if st == 'done' and v.get('status') != 'done':
        v['status'] = 'done'
        v['engine_used'] = _engine_used(tid)      # 降级留痕（如落了 whisper）
        return True
    if st == 'failed' and v.get('status') not in ('done', 'failed'):
        v['status'] = 'failed'
        v['error'] = row.get('error') or ''
        return True
    if st in ('pending', 'running') and v.get('status') not in ('done', 'transcribing'):
        v['status'] = 'transcribing'     # recover 把它重新入队了
        return True
    return False


def recover_unfinished_chains():
    """启动时收尾链条：非终态链标 failed（pipeline 暂不自动恢复），
    并把所有链条的视频状态与 taskdb 真实状态对齐（修"卡片假死"）。
    """
    if not os.path.isdir(CHAINS_DIR):
        return
    for name in os.listdir(CHAINS_DIR):
        cpath = os.path.join(CHAINS_DIR, name, 'chain.json')
        if not os.path.isfile(cpath):
            continue
        try:
            with open(cpath, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            continue

        changed = False
        # 1) 视频状态对齐 taskdb（终态链也做——recover 的任务转完后要反映出来）
        for v in state.get('videos', []):
            if _sync_video_with_taskdb(v):
                changed = True

        # 2) 非终态链收尾成 failed（防前端永远轮询）
        if state.get('stage') not in ('done', 'failed', 'cancelled'):
            state['stage'] = 'failed'
            state['error'] = '服务重启，链条中断（点 Continue 续跑，已完成的会复用）'
            for v in state.get('videos', []):
                if v.get('status') not in ('done', 'failed', 'transcribing'):
                    v['status'] = 'failed'
            changed = True

        if changed:
            try:
                with open(cpath, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception:
                pass


def _review_episode_transcript(task_id, preset=None):
    """白嫖分析流程：这一集分析时顺手给它的转写做一次模型体检（模型标、代码删）。

    清洗后回写 transcript.json（原始留 transcript_raw.json），落一个 .model_reviewed
    标记避免 Continue/Re-analyze 重复体检。用分析同一个 provider（不新增隐私暴露）。
    返回清洗后的 segments；失败/已体检过/非 {timestamp} 形态 → 返回现有 segments。
    """
    d = os.path.join(config.RESULTS_FOLDER, task_id)
    tpath = os.path.join(d, 'transcript.json')
    try:
        with open(tpath, 'r', encoding='utf-8') as f:
            segs = json.load(f)
    except Exception:
        return None
    marker = os.path.join(d, '.model_reviewed')
    if os.path.exists(marker) or not segs or 'timestamp' not in (segs[0] or {}):
        return segs
    try:
        from analyze import review_transcript
        drop = review_transcript(segs, preset=preset)
    except Exception:  # noqa: BLE001
        drop = set()
    try:
        open(marker, 'w').close()   # 标记体检过（哪怕没删），避免重复花钱
    except OSError:
        pass
    if drop:
        rawp = os.path.join(d, 'transcript_raw.json')
        if not os.path.exists(rawp):
            try:
                with open(rawp, 'w', encoding='utf-8') as f:
                    json.dump(segs, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        segs = [s for i, s in enumerate(segs) if i not in drop]
        try:
            with open(tpath, 'w', encoding='utf-8') as f:
                json.dump(segs, f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return segs


def _merged_raw_text(author, videos):
    """把所有转写成功的视频拼成一份纯文本合集（无 AI 分析）。空则返回 ''。"""
    parts = []
    n = 0
    for v in videos:
        if v.get('status') != 'done' or not v.get('task_id'):
            continue
        tpath = os.path.join(config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
        if not os.path.isfile(tpath):
            continue
        try:
            with open(tpath, 'r', encoding='utf-8') as f:
                segs = json.load(f)
        except Exception:
            continue
        text = ' '.join((s.get('text') or '').strip()
                        for s in segs if (s.get('text') or '').strip()).strip()
        if not text:
            continue
        n += 1
        title = v.get('title') or f"视频{v.get('index', 0) + 1}"
        parts.append(f"## {title}\n\n{text}\n")
    if not n:
        return ''
    header = f"# {author} — 全部转写合并（{n} 期 · 纯文本，无 AI 分析）\n"
    return header + '\n' + '\n'.join(parts)


def _ensure_raw_doc(state):
    """确保终态链条有 合并原文.md，并回填 state['raw_doc']。

    功能上线前跑的老链条没有这份文档——这里按需补生成一次（转写还在，重拼即可）。
    """
    if state.get('stage') not in ('done', 'failed'):
        return
    fpath = os.path.join(_chain_dir(state['id']), '合并原文.md')
    if os.path.isfile(fpath):
        if not state.get('raw_doc'):
            state['raw_doc'] = '合并原文.md'
            _save_chain(state)
        return
    raw = _merged_raw_text(state.get('author') or '该博主', state.get('videos') or [])
    if not raw:
        return
    try:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(raw)
        state['raw_doc'] = '合并原文.md'
        _save_chain(state)
    except Exception:
        pass


def _safe_doc_name(name):
    name = name.replace('/', '-').replace('\\', '-')
    return re.sub(r'[:*?"<>|\x00-\x1f]', '_', name).strip()[:120]


def run_chain(state):
    """链条后台线程：解析 → 边下边转 → 逐期分析 → 总合成。

    下载和转写重叠进行（每个视频下完立刻提交转写），下载并发受全局闸限流；
    分析同样走全局闸。无论开多少条链，对外部的瞬时压力都封顶。
    """
    chain_id = state['id']
    chain_dir = _chain_dir(chain_id)
    dl_dir = os.path.join(chain_dir, 'downloads')
    lock = threading.Lock()

    def save():
        with lock:
            _save_chain(state)

    try:
        from downloader import (probe, download_one, fetch_subtitle, parse_srt,
                                channel_followers)

        # ---- 1. 解析目标（拿到标题 + 封面 + 频道名/头像）----
        state['stage'] = 'downloading'
        _save_chain(state)
        targets, channel = probe(state['url'], state.get('max_videos'))
        if not targets:
            raise RuntimeError('No downloadable videos at this link')

        # 用户没填 author 就用探测到的频道名；头像给卡片展示
        if channel.get('name') and state.get('author') in ('', '该博主'):
            state['author'] = channel['name']
        state['avatar'] = channel.get('avatar', '')
        # 订阅数单独取（probe 带 lang=zh-CN 时 YouTube 会返 None）
        state['followers'] = channel.get('followers', 0) or channel_followers(state['url'])
        _save_chain(state)

        # 预置视频网格：一开始就把全部目标铺出来，前端详情页能立刻看到
        videos = [{
            'index': i,
            'title': t.get('title') or t.get('video_id') or f'视频{i + 1}',
            'video_id': t.get('video_id', ''),
            'video_url': t.get('video_url', ''),   # 供 retry 重下用
            'thumbnail': t.get('thumbnail', ''),
            'view_count': int(t.get('view_count') or 0),
            'status': 'downloading',
            'task_id': None,
        } for i, t in enumerate(targets)]

        # 去重复用：之前已转写过的（同 video_id）直接复用旧结果，跳过下载+转写
        idx = _video_id_index()
        for v in videos:
            tid = v['video_id'] and idx.get(v['video_id'])
            if tid:
                v['task_id'] = tid
                v['status'] = 'done'
                v['source'] = 'reused'
        reused = sum(1 for v in videos if v.get('source') == 'reused')

        state['videos'] = videos
        state['download_total'] = len(targets)
        state['download_done'] = reused
        _save_chain(state)

        # ---- 2. 边下边转：并发下载（全局限流），每个下完立刻提交转写 ----
        state['stage'] = 'transcribing'

        def _download_and_submit(i, target):
            v = videos[i]
            if v.get('status') == 'done':        # 复用的旧结果，跳过
                return
            if chain_id in _cancel_chains:       # 已请求停止：不再开新下载
                v['status'] = 'skipped'
                save()
                return

            # Continue 补全：上次转写失败但音频还留着 → 直接重转，不重下载
            old_tid = v.get('task_id')
            if old_tid:
                row = taskdb.get(old_tid)
                # 已在跑/排队的别重投：重启 recover 可能已把它入队，重投会同 task_id 双 worker
                if (row or {}).get('status') in ('pending', 'running'):
                    v['status'] = 'transcribing'
                    save()
                    return
                up = (row or {}).get('upload_path') or ''
                if up and os.path.isfile(up):
                    taskdb.set_status(old_tid, 'pending')
                    q2 = queue.Queue()
                    tasks[old_tid] = q2
                    executor.submit(run_transcription, old_tid, up,
                                    state['engine'], row.get('filename'), q2, None,
                                    fallback_whisper=state.get('fallback_whisper', False))
                    v['status'] = 'transcribing'
                    with lock:
                        state['download_done'] = state.get('download_done', 0) + 1
                    save()
                    return
            sub_segs = sub_source = None
            with _chain_download_sem:            # 全局下载闸
                with lock:
                    state['current'] = v['title']
                # 勾了"优先字幕"：先抓已有字幕；有就完全跳过下载+转写
                if state.get('prefer_subs'):
                    sp, sub_source = fetch_subtitle(target, dl_dir)
                    if sp:
                        sub_segs = parse_srt(sp) or None
                item = None if sub_segs else download_one(target, dl_dir)
            with lock:
                state['download_done'] = state.get('download_done', 0) + 1

            # 有字幕 → 直接落转写结果，跳过音频转写（省下载/转写/API）
            if sub_segs:
                task_id = str(uuid.uuid4())
                _save_subtitle_task(task_id, target, sub_segs, sub_source)
                v['task_id'] = task_id
                v['title'] = target.get('title') or v['title']
                v['status'] = 'done'
                v['source'] = 'subtitle:' + (sub_source or '')
                save()
                return

            if not item:
                v['status'] = 'download_failed'
                save()
                return
            # 下完立刻建转写任务（复用引擎池/信号量/taskdb/历史），不等其它视频
            task_id = str(uuid.uuid4())
            ext = os.path.splitext(item['path'])[1].lstrip('.') or 'mp3'
            upload_path = os.path.join(config.UPLOAD_FOLDER, f"{task_id}.{ext}")
            shutil.move(item['path'], upload_path)
            display_name = f"{item['title']} [{item['video_id']}].mp3"
            taskdb.create(task_id, display_name, state['engine'], None, upload_path)
            q = queue.Queue()
            tasks[task_id] = q
            executor.submit(
                run_transcription, task_id, upload_path, state['engine'],
                display_name, q, None,
                fallback_whisper=state.get('fallback_whisper', False),
            )
            v['task_id'] = task_id
            v['title'] = item['title']
            v['video_id'] = item['video_id']
            if item.get('view_count'):
                v['view_count'] = int(item['view_count'])   # 下载时抓到的播放量
            v['status'] = 'transcribing'
            save()

        dl_futures = [
            _download_executor.submit(_download_and_submit, i, t)
            for i, t in enumerate(targets)
        ]
        for f in dl_futures:
            f.result()  # 等所有下载线程结束（对应转写已在后台并行跑着）

        submitted = [v for v in videos if v.get('task_id')]
        if not submitted:
            if chain_id in _cancel_chains:
                return                       # 一开始就被停，交给 finally 收尾
            raise RuntimeError('No audio downloaded — bad link, login required, or all downloads failed')

        # ---- 3. 等全部转写落定（轮询 taskdb；被停则停止等待，在飞的转写自然收尾）----
        # 只等新提交转写的；复用/字幕来源的已经是 done，不进等待队列。
        pending = {v['task_id'] for v in submitted if v.get('status') == 'transcribing'}
        while pending and chain_id not in _cancel_chains:
            time.sleep(5)
            for v in submitted:
                if v['task_id'] not in pending:
                    continue
                row = taskdb.get(v['task_id'])
                if row and row['status'] in ('done', 'failed'):
                    v['status'] = row['status']
                    if row['status'] == 'failed':
                        v['error'] = row.get('error') or ''
                    else:
                        v['engine_used'] = _engine_used(v['task_id'])  # 降级留痕
                    pending.discard(v['task_id'])
            save()

        # ---- 3.5 合并原文（纯文本，无 AI；不勾 analyze 也生成）----
        raw = _merged_raw_text(state['author'], submitted)
        if raw:
            with open(os.path.join(chain_dir, '合并原文.md'),
                      'w', encoding='utf-8') as fh:
                fh.write(raw)
            state['raw_doc'] = '合并原文.md'
            save()

        # ---- 4. 逐期分析（全局分析闸；每期一次独立调用，防丢信息）----
        if state.get('analyze') and chain_id not in _cancel_chains:
            state['stage'] = 'analyzing'
            state['analyzed_done'] = 0
            save()
            from analyze import analyze_episode, synthesize

            def _analyze_one(v):
                if v['status'] != 'done' or chain_id in _cancel_chains:
                    return None
                tpath = os.path.join(
                    config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
                if not os.path.isfile(tpath):
                    return None
                try:
                    # Continue 省钱：这期的证据卡之前抽过就直接用缓存，不再花钱。
                    # 校验 task_id：这期若被重转过（新 task），旧卡作废重抽。
                    cpath2 = os.path.join(chain_dir, f"cards_{v['index'] + 1:03d}.json")
                    if os.path.isfile(cpath2):
                        try:
                            with open(cpath2, 'r', encoding='utf-8') as fh:
                                ep = json.load(fh)
                            if ep.get('cards') and ep.get('task_id') == v['task_id']:
                                return ep
                        except Exception:
                            pass
                    # 白嫖：分析读转写时顺手做一次模型体检（模型标、代码删）
                    segs = _review_episode_transcript(
                        v['task_id'], preset=state.get('analysis_preset'))
                    if not segs:
                        return None
                    text = '\n'.join(
                        f"[{s.get('timestamp', '')}] {s.get('text', '')}"
                        for s in segs
                    )
                    with _chain_analysis_sem:    # 全局分析闸
                        ep = analyze_episode(v['title'], text, state['author'],
                                             verify=state.get('verify', False),
                                             preset=state.get('analysis_preset'))
                    fname = f"分析_{v['index'] + 1:03d}_{_safe_doc_name(v['title'])}.md"
                    with open(os.path.join(chain_dir, fname),
                              'w', encoding='utf-8') as fh:
                        fh.write(ep['markdown'])
                    ep['task_id'] = v['task_id']       # 缓存键：重转过就作废
                    with open(cpath2, 'w', encoding='utf-8') as fh:
                        json.dump(ep, fh, ensure_ascii=False)
                    return ep
                except Exception:  # noqa: BLE001  单期失败不拖垮整链
                    return None
                finally:
                    with lock:
                        state['analyzed_done'] = state.get('analyzed_done', 0) + 1
                        _save_chain(state)

            with ThreadPoolExecutor(
                max_workers=config.CHAIN_ANALYSIS_CONCURRENCY
            ) as pool:
                results = list(pool.map(_analyze_one, submitted))
            episodes = [r for r in results if r]
            state['analyzed_ok'] = len(episodes)

            # ---- 5. 总合成（只吃证据卡，不吃全文）----
            if episodes:
                state['stage'] = 'synthesizing'
                save()
                total_md = synthesize(episodes, state['author'],
                                      critique_level=state.get('critique_level', 'analytical'),
                                      preset=state.get('analysis_preset'),
                                      self_verify=state.get('self_verify', False),
                                      lang=state.get('lang', 'auto'))
                with open(os.path.join(chain_dir, '总分析.md'),
                          'w', encoding='utf-8') as fh:
                    fh.write(total_md)
                state['final_doc'] = '总分析.md'

        state['stage'] = 'cancelled' if chain_id in _cancel_chains else 'done'
    except Exception as e:  # noqa: BLE001
        state['stage'] = 'failed'
        state['error'] = str(e)
    finally:
        # 早退（开跑即停）不会走到设终态那行 → 兜底，否则永久卡在 transcribing/downloading
        if state.get('stage') not in ('done', 'failed', 'cancelled'):
            state['stage'] = 'cancelled' if chain_id in _cancel_chains else 'failed'
        _cancel_chains.discard(chain_id)
        state['finished_at'] = __import__('datetime').datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S')
        _save_chain(state)
        shutil.rmtree(dl_dir, ignore_errors=True)


def _reanalyze_chain(state):
    """只重跑分析+合成（不重下、不重转），复用已有转写。供历史链条测试新模型/核实模式。"""
    from analyze import analyze_episode, synthesize
    chain_dir = _chain_dir(state['id'])
    lock = threading.Lock()

    def save():
        with lock:
            _save_chain(state)

    try:
        videos = state.get('videos') or []
        submitted = [v for v in videos
                     if v.get('task_id') and v.get('status') == 'done']
        if not submitted:
            state['stage'] = 'failed'
            state['error'] = '没有可重新分析的已转写视频'
            save()
            return

        state['stage'] = 'analyzing'
        state['analyzed_done'] = 0
        state['analyzed_ok'] = 0
        state['final_doc'] = None
        save()

        def _analyze_one(v):
            if state['id'] in _cancel_chains:
                return None
            tpath = os.path.join(
                config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
            if not os.path.isfile(tpath):
                return None
            try:
                # 白嫖：分析读转写时顺手做一次模型体检（模型标、代码删）
                segs = _review_episode_transcript(
                    v['task_id'], preset=state.get('analysis_preset'))
                if not segs:
                    return None
                text = '\n'.join(
                    f"[{s.get('timestamp', '')}] {s.get('text', '')}" for s in segs)
                with _chain_analysis_sem:
                    ep = analyze_episode(v['title'], text, state['author'],
                                         verify=state.get('verify', False),
                                         preset=state.get('analysis_preset'))
                fname = f"分析_{v['index'] + 1:03d}_{_safe_doc_name(v['title'])}.md"
                with open(os.path.join(chain_dir, fname),
                          'w', encoding='utf-8') as fh:
                    fh.write(ep['markdown'])
                # Re-analyze 是显式重做：无视旧缓存、写入新证据卡（供以后 Continue 复用）
                ep['task_id'] = v['task_id']
                with open(os.path.join(chain_dir, f"cards_{v['index'] + 1:03d}.json"),
                          'w', encoding='utf-8') as fh:
                    json.dump(ep, fh, ensure_ascii=False)
                return ep
            except Exception:  # noqa: BLE001
                return None
            finally:
                with lock:
                    state['analyzed_done'] = state.get('analyzed_done', 0) + 1
                    _save_chain(state)

        with ThreadPoolExecutor(
                max_workers=config.CHAIN_ANALYSIS_CONCURRENCY) as pool:
            results = list(pool.map(_analyze_one, submitted))
        episodes = [r for r in results if r]
        state['analyzed_ok'] = len(episodes)

        if episodes and state['id'] not in _cancel_chains:
            state['stage'] = 'synthesizing'
            save()
            total_md = synthesize(episodes, state['author'],
                                  critique_level=state.get('critique_level', 'analytical'),
                                  preset=state.get('analysis_preset'),
                                  self_verify=state.get('self_verify', False),
                                  lang=state.get('lang', 'auto'))
            with open(os.path.join(chain_dir, '总分析.md'),
                      'w', encoding='utf-8') as fh:
                fh.write(total_md)
            state['final_doc'] = '总分析.md'

        state['stage'] = 'cancelled' if state['id'] in _cancel_chains else 'done'
        save()
    except Exception as e:  # noqa: BLE001
        state['stage'] = 'failed'
        state['error'] = f'重新分析失败：{e}'
        save()
    finally:
        _cancel_chains.discard(state['id'])


@app.route('/api/chain', methods=['POST'])
def api_chain_create():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': '请填写有效的 http(s) 链接'}), 400

    try:
        max_videos = int(data.get('max_videos') or 0)
    except (TypeError, ValueError):
        max_videos = 0
    max_videos = min(max_videos, _MAX_CHAIN_VIDEOS) if max_videos > 0 else None

    chain_id = uuid.uuid4().hex
    os.makedirs(_chain_dir(chain_id), exist_ok=True)
    state = {
        'id': chain_id,
        'url': url,
        'engine': data.get('engine') or 'gemini',
        'max_videos': max_videos,
        'analyze': bool(data.get('analyze', True)),
        'prefer_subs': bool(data.get('prefer_subs', False)),
        'fallback_whisper': bool(data.get('fallback_whisper', False)),
        'verify': bool(data.get('verify', False)),
        'self_verify': bool(data.get('self_verify', False)),
        'lang': (data.get('lang') or 'auto'),
        'critique_level': (data.get('critique_level') or 'analytical'),
        'analysis_preset': (data.get('analysis_preset') or 'gemini'),
        'author': (data.get('author') or '').strip() or '该博主',
        'stage': 'starting',
        'created_at': __import__('datetime').datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S'),
    }
    _save_chain(state)
    threading.Thread(target=run_chain, args=(state,), daemon=True).start()
    return jsonify({'chain_id': chain_id})


@app.route('/api/chains')
def api_chains():
    entries = []
    if os.path.isdir(CHAINS_DIR):
        for name in os.listdir(CHAINS_DIR):
            cpath = os.path.join(CHAINS_DIR, name, 'chain.json')
            if _CHAIN_ID_RE.match(name) and os.path.isfile(cpath):
                try:
                    with open(cpath, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    _ensure_raw_doc(state)      # 老链条按需补『合并原文.md』
                    entries.append(state)
                except Exception:
                    pass
    entries.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return jsonify(entries)


_channel_backfilling = set()   # 正在补频道信息的链条，防重复重探


def _backfill_channel(chain_id, url):
    """老链条重探一次频道元信息（名字/订阅数/头像），只取频道级、不列全部视频。"""
    try:
        from downloader import probe, channel_followers
        _, channel = probe(url, max_videos=1)
        followers = channel_followers(url)   # 单独取（不带 lang，否则 YouTube 返 None）
    except Exception:  # noqa: BLE001
        channel, followers = None, 0
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    with _chain_write_lock:
        try:
            with open(cpath, 'r', encoding='utf-8') as f:
                st = json.load(f)
            # 只在链条已终态时回写，避免覆盖 run_chain 内存里正在跑的 state
            if st.get('stage') in ('done', 'failed', 'cancelled'):
                st['followers'] = followers or (channel or {}).get('followers', 0)
                if not st.get('avatar') and (channel or {}).get('avatar'):
                    st['avatar'] = channel['avatar']
                # 名字也补：旧链创建时没存频道名，卡片只能显示裸 URL
                if (channel or {}).get('name') and st.get('author') in (None, '', '该博主'):
                    st['author'] = channel['name']
                st['followers_checked'] = True   # 探过就记住，别每次轮询都重探
                st['author_checked'] = True      # 名字也探过（取不到就是取不到，别反复探）
                _save_chain(st)
        except Exception:  # noqa: BLE001
            pass
    _channel_backfilling.discard(chain_id)


@app.route('/api/chain/<chain_id>')
def api_chain_detail(chain_id):
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    with open(cpath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # 打开详情时顺手校对：transcribing 的视频按 taskdb 真实状态回写并落盘
    # （任务级 recover 转完后，链条循环已死不会更新——靠这里自愈）
    healed = False
    for v in data.get('videos', []):
        if v.get('status') in ('transcribing', 'downloading') \
                and _sync_video_with_taskdb(v):
            healed = True
    if healed:
        try:
            _save_chain(data)
        except Exception:  # noqa: BLE001
            pass

    # 老链条补频道信息（名字/订阅数/头像）：只对已终态的链、且没探过的重探一次
    # （*_checked 标记防每次轮询重复打网络；running 中的链不碰，交给 run_chain）
    needs_followers = not data.get('followers') and not data.get('followers_checked')
    needs_author = data.get('author') in (None, '', '该博主') \
        and not data.get('author_checked')
    if (needs_followers or needs_author) \
            and data.get('stage') in ('done', 'failed', 'cancelled') \
            and data.get('url') and chain_id not in _channel_backfilling:
        _channel_backfilling.add(chain_id)
        threading.Thread(target=_backfill_channel,
                         args=(chain_id, data['url']), daemon=True).start()

    # 给正在转写的视频挂上实时进度百分比（内存里的 _task_progress）
    for v in data.get('videos', []):
        tid = v.get('task_id')
        if tid and v.get('status') == 'transcribing':
            p = _task_progress.get(tid)
            if p is not None:
                v['progress'] = p
    return jsonify(data)


@app.route('/api/chain/<chain_id>/reanalyze', methods=['POST'])
def api_chain_reanalyze(chain_id):
    """只重跑分析+合成（复用已有转写）。body: {verify: bool}。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    if state.get('stage') in ('downloading', 'transcribing', 'analyzing', 'synthesizing'):
        return jsonify({'ok': False, 'error': 'This pipeline is still running — wait for it to finish before re-analyzing'}), 409

    body = request.get_json(silent=True) or {}
    state['verify'] = bool(body.get('verify', False))
    state['self_verify'] = bool(body.get('self_verify', False))
    if body.get('lang'):
        state['lang'] = body['lang']
    if body.get('critique_level'):
        state['critique_level'] = body['critique_level']
    if body.get('analysis_preset'):
        state['analysis_preset'] = body['analysis_preset']
    state['analyze'] = True
    threading.Thread(target=_reanalyze_chain, args=(state,), daemon=True).start()
    return jsonify({'ok': True})


def _load_chain_cards(chain_id):
    """从链条目录读回所有证据卡 → episodes 列表（镜头/重合成共用）。"""
    import glob
    eps = []
    for f in sorted(glob.glob(os.path.join(_chain_dir(chain_id), 'cards_*.json'))):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            if data.get('cards'):
                eps.append(data)
        except Exception:  # noqa: BLE001
            pass
    return eps


_lens_jobs = {}  # (chain_id, lens) -> 'running' | 'done' | 'error:...'


@app.route('/api/chain/<chain_id>/lens', methods=['POST'])
def api_chain_lens(chain_id):
    """换个角度看这个博主：拿现成证据卡跑一个镜头（roast/craft/fun/...）。
    后台生成 → 存 镜头_<lens>.md；前端轮询 GET 取。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    from analyze import LENSES
    lens = (request.get_json(silent=True) or {}).get('lens')
    if lens not in LENSES:
        return jsonify({'error': '未知镜头'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    fpath = os.path.join(_chain_dir(chain_id), f'镜头_{lens}.md')
    if os.path.isfile(fpath):   # 已生成过，直接给
        with open(fpath, 'r', encoding='utf-8') as f:
            return jsonify({'ok': True, 'ready': True, 'markdown': f.read()})
    key = (chain_id, lens)
    if _lens_jobs.get(key) == 'running':
        return jsonify({'ok': True, 'ready': False, 'status': 'running'})
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    eps = _load_chain_cards(chain_id)
    if not eps:
        return jsonify({'error': 'No evidence cards yet — run analysis once first'}), 400

    def _run():
        try:
            from analyze import render_lens
            md = render_lens(eps, lens, author=state.get('author', '该博主'),
                             preset=state.get('analysis_preset'),
                             lang=state.get('lang', 'auto'))
            with open(fpath, 'w', encoding='utf-8') as fh:
                fh.write(md or '')
            _lens_jobs[key] = 'done'
        except Exception as e:  # noqa: BLE001
            _lens_jobs[key] = f'error:{e}'

    _lens_jobs[key] = 'running'
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'ready': False, 'status': 'running'})


@app.route('/api/chain/<chain_id>/lens/<lens>')
def api_chain_lens_get(chain_id, lens):
    """轮询取镜头结果。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    fpath = os.path.join(_chain_dir(chain_id), f'镜头_{lens}.md')
    if os.path.isfile(fpath):
        with open(fpath, 'r', encoding='utf-8') as f:
            return jsonify({'ready': True, 'markdown': f.read()})
    st = _lens_jobs.get((chain_id, lens), '')
    if st.startswith('error:'):
        return jsonify({'ready': False, 'error': st[6:]})
    return jsonify({'ready': False, 'status': st or 'idle'})


@app.route('/api/chain/<chain_id>/stop', methods=['POST'])
def api_chain_stop(chain_id):
    """请求停止一条运行中的链条（协作式）：已完成的转写/分析保留，不再继续推进。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    _cancel_chains.add(chain_id)
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>/retry', methods=['POST'])
def api_chain_retry(chain_id):
    """重跑这条链：重新解析 URL，去重复用已成功的、只重下重转失败/未完成的，再重新合成。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    if state.get('stage') not in ('done', 'failed', 'cancelled'):
        return jsonify({'ok': False, 'error': 'This pipeline is still running'}), 409
    body = request.get_json(silent=True) or {}
    if body.get('engine'):
        state['engine'] = body['engine']
    if body.get('analysis_preset'):
        state['analysis_preset'] = body['analysis_preset']
    _cancel_chains.discard(chain_id)          # 清掉可能残留的取消标记
    # run_chain 会重新 probe + 去重复用（已转写的跳过），只有缺的会真正重下重转
    threading.Thread(target=run_chain, args=(state,), daemon=True).start()
    return jsonify({'ok': True})


def _update_video(chain_id, index, fields):
    """读-改-写 chain.json 里第 index 个视频的字段（串行化，防并发丢更新）。"""
    with _chain_write_lock:
        cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
        try:
            with open(cpath, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            return
        vids = state.get('videos') or []
        if 0 <= index < len(vids):
            vids[index].update(fields)
            _save_chain(state)


def _video_target(v):
    """从视频条目取重下用的 target；没存 video_url 就按 id 重建。"""
    url = v.get('video_url')
    vid = v.get('video_id', '') or ''
    if not url and vid:
        if len(vid) == 11:                      # YouTube id
            url = f'https://www.youtube.com/watch?v={vid}'
        elif vid.startswith('BV'):              # Bilibili
            url = f'https://www.bilibili.com/video/{vid}'
    return {'video_url': url, 'video_id': vid, 'title': v.get('title')}


def _retranscribe_video(chain_id, index, target, engine):
    """只对一个视频：重下音频 → 用指定引擎转写 → 回写这张卡的状态。后台线程跑。"""
    from downloader import download_one
    dl_dir = os.path.join(_chain_dir(chain_id), 'downloads')
    try:
        _update_video(chain_id, index, {'status': 'downloading'})
        with _chain_download_sem:
            item = download_one(target, dl_dir)
        if not item:
            _update_video(chain_id, index, {'status': 'download_failed'})
            return

        task_id = str(uuid.uuid4())
        ext = os.path.splitext(item['path'])[1].lstrip('.') or 'mp3'
        upload_path = os.path.join(config.UPLOAD_FOLDER, f"{task_id}.{ext}")
        shutil.move(item['path'], upload_path)
        display_name = f"{item['title']} [{item['video_id']}].mp3"
        taskdb.create(task_id, display_name, engine, None, upload_path)
        q = queue.Queue()
        tasks[task_id] = q
        executor.submit(run_transcription, task_id, upload_path, engine,
                        display_name, q, None)
        _update_video(chain_id, index, {
            'task_id': task_id, 'title': item['title'],
            'video_id': item['video_id'], 'status': 'transcribing',
            'source': f'retranscribe:{engine}',
        })

        # 轮询到落定，回写状态
        while True:
            time.sleep(5)
            row = taskdb.get(task_id)
            if row and row['status'] in ('done', 'failed'):
                _update_video(chain_id, index, {'status': row['status']})
                break
    except Exception:  # noqa: BLE001
        _update_video(chain_id, index, {'status': 'failed'})
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)


@app.route('/api/chain/<chain_id>/video/<int:index>/retranscribe', methods=['POST'])
def api_chain_retranscribe(chain_id, index):
    """对链条里第 index 个视频，用指定引擎单独重下+重转（不动其它视频）。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    if state.get('stage') not in ('done', 'failed', 'cancelled'):
        return jsonify({'ok': False, 'error': 'Wait for the whole pipeline to finish before re-transcribing a single video'}), 409
    vids = state.get('videos') or []
    if not (0 <= index < len(vids)):
        return jsonify({'error': 'bad index'}), 400
    v = vids[index]
    if v.get('status') in ('downloading', 'transcribing'):
        return jsonify({'ok': False, 'error': '这个视频正在处理'}), 409

    body = request.get_json(silent=True) or {}
    engine = body.get('engine') or 'whisper'
    target = _video_target(v)
    if not target['video_url']:
        return jsonify({'ok': False, 'error': '没有可用的视频链接，无法重下'}), 400
    threading.Thread(target=_retranscribe_video,
                     args=(chain_id, index, target, engine), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>', methods=['DELETE'])
def api_chain_delete(chain_id):
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cdir = _chain_dir(chain_id)
    if not os.path.isdir(cdir):
        return jsonify({'error': 'Not found'}), 404
    # 只删链条目录（分析产物）；转写结果仍留在历史里
    shutil.rmtree(cdir, ignore_errors=True)
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>/file')
def api_chain_file(chain_id):
    """读链条目录下的 md 产物（?name=总分析.md 或某期分析）。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    name = request.args.get('name') or ''
    # 只允许目录内的 .md 文件名，杜绝路径穿越
    if '/' in name or '\\' in name or '..' in name or not name.endswith('.md'):
        return jsonify({'error': 'Invalid file name'}), 400
    fpath = os.path.join(_chain_dir(chain_id), name)
    if not os.path.isfile(fpath):
        return jsonify({'error': 'Not found'}), 404
    return send_file(fpath, mimetype='text/markdown; charset=utf-8',
                     as_attachment=False, download_name=name)


@app.route('/api/chain/<chain_id>/files')
def api_chain_files(chain_id):
    """列出链条目录下的全部 md 产物。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cdir = _chain_dir(chain_id)
    if not os.path.isdir(cdir):
        return jsonify({'error': 'Not found'}), 404
    files = sorted(f for f in os.listdir(cdir) if f.endswith('.md'))
    return jsonify(files)


def _transcript_title(task_id):
    """单期转写的标题：AI 标题优先，退回文件名，再退回 task_id。"""
    try:
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'),
                  'r', encoding='utf-8') as f:
            meta = json.load(f)
        return (meta.get('ai_title') or meta.get('filename') or task_id).strip()
    except Exception:
        return task_id


def _transcript_plain_text(task_id):
    """单期转写正文拼成纯文本（无时间戳）。取不到返回 ''。"""
    tpath = os.path.join(config.RESULTS_FOLDER, task_id, 'transcript.json')
    if not os.path.isfile(tpath):
        return ''
    try:
        with open(tpath, 'r', encoding='utf-8') as f:
            segs = json.load(f)
    except Exception:
        return ''
    return ' '.join((s.get('text') or '').strip()
                    for s in segs if (s.get('text') or '').strip()).strip()


@app.route('/api/transcripts/merge', methods=['POST'])
def api_transcripts_merge():
    """把任意一组转写（按 task_id）拼成一份纯文本 Markdown。跨博主、可挑期。

    只读：不落盘、不进 Library，前端拿去展示 + 下载。
    """
    body = request.get_json(silent=True) or {}
    ids = body.get('task_ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'Pick at least one transcript'}), 400
    # 去重保序 + 校验格式，挡路径穿越
    seen, clean = set(), []
    for tid in ids:
        if _is_valid_task_id(tid) and tid not in seen:
            seen.add(tid)
            clean.append(tid)
    if not clean:
        return jsonify({'error': 'No valid transcripts selected'}), 400

    parts, missing = [], 0
    for tid in clean:
        text = _transcript_plain_text(tid)
        if not text:
            missing += 1
            continue
        parts.append(f"## {_transcript_title(tid)}\n\n{text}\n")
    if not parts:
        return jsonify({'error': 'None of the selected transcripts had text'}), 400

    header = (f"# 合并转写（{len(parts)} 期 · 纯文本，无 AI 分析）\n"
              + (f"\n> {missing} 期没有可用转写，已跳过。\n" if missing else ''))
    markdown = header + '\n' + '\n'.join(parts)
    return jsonify({
        'markdown': markdown,
        'count': len(parts),
        'missing': missing,
        'filename': f'合并转写_{len(parts)}期.md',
    })


# ========== 个人数据展板 ==========

@app.route('/api/stats')
def api_stats():
    """聚合历史转写：总时长、产出字数、按引擎、关注领域(标签)、每日活跃度。

    字数(char_count)首次计算后写回 meta.json 缓存，之后秒回。
    """
    from collections import defaultdict

    results_dir = config.RESULTS_FOLDER
    total = 0
    total_seconds = 0.0
    total_chars = 0
    total_segments = 0
    engines = defaultdict(int)
    tag_counts = defaultdict(int)
    daily_seconds = defaultdict(float)   # 'YYYY-MM-DD' -> 秒
    daily_count = defaultdict(int)

    if os.path.isdir(results_dir):
        for name in os.listdir(results_dir):
            if name.startswith('_'):
                continue
            task_dir = os.path.join(results_dir, name)
            meta_path = os.path.join(task_dir, 'meta.json')
            if not os.path.isfile(meta_path):
                continue
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                continue

            total += 1
            total_seconds += meta.get('duration_seconds') or 0
            total_segments += meta.get('segment_count') or 0
            engines[meta.get('engine') or 'unknown'] += 1
            for t in (meta.get('ai_tags') or []):
                tag_counts[t] += 1

            # 字数：优先用缓存，没有就读 transcript 算一次并写回
            cc = meta.get('char_count')
            if cc is None:
                cc = 0
                tpath = os.path.join(task_dir, 'transcript.json')
                if os.path.isfile(tpath):
                    try:
                        with open(tpath, 'r', encoding='utf-8') as f:
                            for s in json.load(f):
                                cc += len(s.get('text', '') or '')
                    except Exception:
                        cc = 0
                # 合并写回：不覆盖 enrich 可能刚写的 ai_title/ai_tags
                _update_meta(meta_path, {'char_count': cc})
            total_chars += cc

            date = (meta.get('date') or '')[:10]
            if len(date) == 10:
                daily_seconds[date] += meta.get('duration_seconds') or 0
                daily_count[date] += 1

    # 每日时间线（按日期升序），前端据此画累计小时折线
    timeline = [
        {'date': d, 'minutes': round(daily_seconds[d] / 60, 1),
         'count': daily_count[d]}
        for d in sorted(daily_seconds)
    ]
    top_tags = sorted(tag_counts.items(), key=lambda kv: -kv[1])[:8]

    # 标签英文译名（带磁盘缓存，只对出现的标签翻一次）
    try:
        from enrich import translate_tags
        tmap = translate_tags([t for t, _ in top_tags],
                              os.path.join(results_dir, '_tagmap.json'))
    except Exception:
        tmap = {}

    return jsonify({
        'totals': {
            'transcripts': total,
            'hours': round(total_seconds / 3600, 1),
            'chars': total_chars,
            'segments': total_segments,
        },
        'engines': dict(engines),
        'top_tags': [{'tag': t, 'tag_en': tmap.get(t, t), 'count': c}
                     for t, c in top_tags],
        'timeline': timeline,
    })


# ========== Settings API ==========

def _mask_key(env_name):
    """返回 (是否已设置, 末4位提示)，绝不回传完整 key。"""
    v = (os.environ.get(env_name) or '').strip()
    if not v:
        return False, ''
    return True, '••••' + v[-4:] if len(v) >= 4 else '••••'


@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    gset, ghint = _mask_key('GEMINI_API_KEY')
    dset, dhint = _mask_key('DASHSCOPE_API_KEY')
    oset, ohint = _mask_key('OPENROUTER_API_KEY')
    out = {
        'gemini': {'set': gset, 'hint': ghint},
        'dashscope': {'set': dset, 'hint': dhint},
        'openrouter': {'set': oset, 'hint': ohint},
    }
    # 非秘密字段直接回显供编辑（模型名空 = 用默认）
    for field in _PLAIN_FIELDS:
        out[field] = (os.environ.get(_SETTING_ENV[field]) or '').strip()
    return jsonify(out)


@app.route('/api/settings', methods=['POST'])
def api_settings_save():
    body = request.get_json(silent=True) or {}
    data = _load_settings()

    # key：只有传了非空值才更新（留空 = 保持不变，避免用户没重填就被清空）
    for field in ('gemini_key', 'dashscope_key', 'openrouter_key'):
        if field in body:
            v = (body.get(field) or '').strip()
            if v:
                data[field] = v
    # 非秘密字段（base URL / 各模型名）：允许清空（空 = 删 env，回默认）
    for field in _PLAIN_FIELDS:
        if field in body:
            data[field] = (body.get(field) or '').strip()
            if not data[field]:
                os.environ.pop(_SETTING_ENV[field], None)

    try:
        with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'保存失败：{e}'}), 500

    _apply_settings_to_env(data)
    return jsonify({'ok': True})


def _diagnose_gemini_error(e):
    s = str(e).lower()
    if any(k in s for k in ('api_key_invalid', 'api key not valid', 'invalid api key',
                            'unauthorized', 'permission', '401', '403')):
        return 'API key looks invalid — double-check you copied the whole thing.'
    if any(k in s for k in ('location', 'not supported in your', 'user location',
                            'failed_precondition', 'region')):
        return 'Gemini is not available from your region — fill in a proxy Base URL below.'
    if any(k in s for k in ('timeout', 'timed out', 'connect', 'getaddrinfo',
                            'max retries', 'ssl', 'network', 'unreachable', 'refused')):
        return "Can't reach Gemini — network or proxy problem (check the Base URL)."
    if any(k in s for k in ('429', 'resource_exhausted', 'quota', 'rate limit')):
        return 'Key works, but you are rate-limited / out of quota right now.'
    return str(e)[:180]


@app.route('/api/settings/test', methods=['POST'])
def api_settings_test():
    """用用户填的 key（留空则用已保存的）打一次最小真实请求，返回能否用 + 原因。"""
    body = request.get_json(silent=True) or {}
    engine = body.get('engine')

    if engine == 'gemini':
        key_in = (body.get('gemini_key') or '').strip()
        base = (body.get('gemini_base_url') or '').strip()
        # 防外泄：自定义 base_url 必须自带 key。绝不拿已保存的真实 key 去打请求体指定的任意 URL。
        if base and not key_in:
            return jsonify({'ok': False,
                            'reason': 'A custom base URL must come with its own key — the saved key is never sent to a custom endpoint.'})
        key = key_in or os.environ.get('GEMINI_API_KEY', '')
        if not key:
            return jsonify({'ok': False, 'reason': 'No key entered or saved yet.'})
        try:
            # base_url 显式传参，不改全局 env（否则会污染并发中的转写客户端）
            client = config.make_gemini_client(key, timeout_ms=30_000, base_url=base or None)
            next(iter(client.models.list()), None)
            return jsonify({'ok': True, 'reason': 'Works — key accepted.'})
        except Exception as e:
            return jsonify({'ok': False, 'reason': _diagnose_gemini_error(e)})

    if engine == 'dashscope':
        key = (body.get('dashscope_key') or '').strip() or os.environ.get('DASHSCOPE_API_KEY', '')
        if not key:
            return jsonify({'ok': False, 'reason': 'No key entered or saved yet.'})
        try:
            from dashscope import Generation
            resp = Generation.call(
                model=config.DASHSCOPE_LLM_MODEL, api_key=key,
                messages=[{'role': 'user', 'content': 'ping'}],
                max_tokens=1,
            )
            code = getattr(resp, 'status_code', 200)
            if code == 200:
                return jsonify({'ok': True, 'reason': 'Works — key accepted.'})
            msg = getattr(resp, 'message', '') or str(code)
            if code in (401, 403):
                return jsonify({'ok': False, 'reason': 'API key looks invalid.'})
            return jsonify({'ok': False, 'reason': f'DashScope error {code}: {msg}'[:180]})
        except Exception as e:
            return jsonify({'ok': False, 'reason': str(e)[:180]})

    if engine == 'openrouter':
        key = (body.get('openrouter_key') or '').strip() or os.environ.get('OPENROUTER_API_KEY', '')
        if not key:
            return jsonify({'ok': False, 'reason': 'No key entered or saved yet.'})
        try:
            import requests as _rq
            r = _rq.get('https://openrouter.ai/api/v1/key',
                        headers={'Authorization': f'Bearer {key}'}, timeout=15)
            if r.status_code == 200:
                d = (r.json() or {}).get('data') or {}
                usage = d.get('usage')
                limit = d.get('limit')
                extra = f' Usage ${usage:.2f}' + (f' / limit ${limit:.2f}' if limit else '') \
                    if isinstance(usage, (int, float)) else ''
                return jsonify({'ok': True, 'reason': f'Works — key accepted.{extra}'})
            if r.status_code in (401, 403):
                return jsonify({'ok': False, 'reason': 'API key looks invalid.'})
            return jsonify({'ok': False, 'reason': f'OpenRouter error {r.status_code}'[:180]})
        except Exception as e:
            return jsonify({'ok': False, 'reason': str(e)[:180]})

    return jsonify({'ok': False, 'reason': 'Unknown engine.'}), 400


# ========== 存储 / 音频压缩 ==========

_compress_state = {'running': False, 'done': 0, 'total': 0, 'saved': 0, 'errors': 0}
_compress_lock = threading.Lock()


def _audio_files():
    """遍历所有任务的音频文件，产出 (路径, 是否已是 opus)。"""
    results_dir = config.RESULTS_FOLDER
    if not os.path.isdir(results_dir):
        return
    for d in os.listdir(results_dir):
        td = os.path.join(results_dir, d)
        if not (_is_valid_task_id(d) and os.path.isdir(td)):
            continue
        try:
            names = os.listdir(td)
        except OSError:
            continue
        for name in names:
            if name.startswith('audio.') and not name.endswith('.tmp'):
                yield os.path.join(td, name), name.endswith('.ogg')


@app.route('/api/storage')
def api_storage():
    total = 0
    count = 0
    compressed = 0
    for path, is_opus in _audio_files():
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
        count += 1
        if is_opus:
            compressed += 1
    return jsonify({'audio_bytes': total, 'audio_count': count,
                    'compressed_count': compressed})


def _run_compress_all():
    from audioutil import compress_task
    results_dir = config.RESULTS_FOLDER
    task_dirs = [d for d in os.listdir(results_dir)
                 if _is_valid_task_id(d)
                 and os.path.isdir(os.path.join(results_dir, d))]
    with _compress_lock:
        _compress_state.update(running=True, done=0,
                               total=len(task_dirs), saved=0, errors=0)
    for d in task_dirs:
        try:
            saved, status = compress_task(os.path.join(results_dir, d))
        except Exception:
            saved, status = 0, 'error'
        with _compress_lock:
            _compress_state['done'] += 1
            _compress_state['saved'] += saved
            if status.startswith('error'):
                _compress_state['errors'] += 1
    with _compress_lock:
        _compress_state['running'] = False


@app.route('/api/compress_all', methods=['POST'])
def api_compress_all():
    with _compress_lock:
        if _compress_state['running']:
            return jsonify({'ok': False, 'error': 'already running'}), 409
    # 串行跑（ffmpeg 吃 CPU，串行避免烧满/发热），后台线程 + 状态轮询
    threading.Thread(target=_run_compress_all, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/compress_status')
def api_compress_status():
    with _compress_lock:
        return jsonify(dict(_compress_state))


# ==== 小红书采集：Verbatim 驱动外部爬虫（uv/3.12 子进程，复用现成 xhs_pipeline.py）====
_XHS_UV = os.environ.get('XHS_UV') or '/opt/homebrew/bin/uv'
_XHS_PROJECT = os.environ.get('XHS_PROJECT') or '/Users/kapozux/Documents/XHS-Downloader'
_XHS_ROOT = os.environ.get('XHS_ROOT') or '/Users/kapozux/Documents/CODEelse'
_XHS_SCRIPT = os.path.join(_XHS_ROOT, 'xhs_pipeline.py')
_XHS_NOTES = os.path.join(_XHS_ROOT, 'xhs_dataset', 'notes')
_xhs_job = {'running': False, 'log': [], 'started': None, 'base': 0, 'kw': '',
            'proc': None, 'stopping': False}


def _xhs_notes_count():
    try:
        return sum(1 for n in os.listdir(_XHS_NOTES)
                   if os.path.isdir(os.path.join(_XHS_NOTES, n)))
    except OSError:
        return 0


def _run_xhs(keywords, max_notes, max_comments):
    env = dict(os.environ, XHS_KEYWORDS=keywords,
               XHS_MAX_NOTES=str(max_notes), XHS_MAX_COMMENTS=str(max_comments),
               PYTHONUNBUFFERED='1')          # 让脚本的 print 实时流出来（否则管道缓冲，看着像卡死）
    env.pop('VIRTUAL_ENV', None)               # 别把 Verbatim 的 3.9 venv 传给 uv/3.12（那条 warning 的根源）
    env.pop('PYTHONHOME', None)
    _xhs_job.update(running=True, log=[], base=_xhs_notes_count(),
                    proc=None, stopping=False)
    try:
        proc = subprocess.Popen(
            [_XHS_UV, 'run', '--project', _XHS_PROJECT, 'python', _XHS_SCRIPT],
            cwd=_XHS_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            start_new_session=True,   # 独立进程组：停止时可整组杀，且绝不误伤 Flask
        )
        _xhs_job['proc'] = proc
        for line in proc.stdout:
            _xhs_job['log'].append(line.rstrip()[:200])
            del _xhs_job['log'][:-60]          # 只留最后 60 行
        proc.wait()
        tail = '[已停止]' if _xhs_job.get('stopping') else f'[完成] 退出码 {proc.returncode}'
        _xhs_job['log'].append(tail)
    except Exception as e:  # noqa: BLE001
        _xhs_job['log'].append(f'[错误] {e}')
    finally:
        _xhs_job['running'] = False
        _xhs_job['proc'] = None


def _stop_xhs():
    """停止采集：给子进程整组发信号（含 chromium）。已采的每篇都已落盘，不会丢。"""
    proc = _xhs_job.get('proc')
    if not proc or proc.poll() is not None:
        return False
    _xhs_job['stopping'] = True
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)   # 整组:python + playwright chromium
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            return False
    return True


@app.route('/api/xhs/scrape', methods=['POST'])
def api_xhs_scrape():
    """填关键词 + 数量 → 后台起 uv 子进程采集。会弹出有头浏览器（首次要扫码）。"""
    if _xhs_job['running']:
        return jsonify({'ok': False, 'error': 'A scrape is already running — wait for it to finish'}), 409
    if not os.path.isfile(_XHS_SCRIPT):
        return jsonify({'ok': False, 'error': f'找不到爬虫脚本：{_XHS_SCRIPT}'}), 500
    body = request.get_json(silent=True) or {}
    kws = (body.get('keywords') or '').strip()
    if not kws:
        return jsonify({'ok': False, 'error': '至少填一个关键词'}), 400
    try:
        mn = max(1, min(300, int(body.get('max_notes') or 20)))
        mc = max(50, min(2000, int(body.get('max_comments') or 400)))
    except (ValueError, TypeError):
        mn, mc = 20, 400
    _xhs_job['started'] = __import__('datetime').datetime.now().strftime('%H:%M:%S')
    _xhs_job['kw'] = kws.replace('\n', ' / ')[:120]
    threading.Thread(target=_run_xhs, args=(kws, mn, mc), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/xhs/stop', methods=['POST'])
def api_xhs_stop():
    """停止正在跑的采集。已采的每篇都已落盘，不会丢。"""
    if not _xhs_job['running']:
        return jsonify({'ok': False, 'error': 'No scrape is running'}), 400
    ok = _stop_xhs()
    return jsonify({'ok': ok, 'error': None if ok else 'Could not signal the process'})


@app.route('/api/xhs/status')
def api_xhs_status():
    total = _xhs_notes_count()
    return jsonify({
        'running': _xhs_job['running'],
        'scraped': max(0, total - _xhs_job.get('base', 0)),
        'total': total,
        'started': _xhs_job.get('started'),
        'kw': _xhs_job.get('kw', ''),
        'log': _xhs_job.get('log', [])[-30:],
    })


# ---- 小红书分析：逐篇多模态读图+评论 → 聚合调研报告 ----
_XHS_REPORT = os.path.join(_XHS_ROOT, 'xhs_dataset', '小红书报告.md')
_xhs_an = {'running': False, 'done': 0, 'total': 0, 'error': None, 'ready': False}


def _xhs_match_dirs(keywords):
    """按 source_keyword 圈定笔记目录。keywords 非空 → 只要来源词命中的；空 → 全部。"""
    import glob as _g
    kwset = {k.strip() for k in (keywords or []) if k.strip()}
    dirs = []
    for d in sorted(_g.glob(os.path.join(_XHS_NOTES, '*'))):
        if not os.path.isdir(d):
            continue
        if not kwset:
            dirs.append(d)
            continue
        try:
            with open(os.path.join(d, 'meta.json'), 'r', encoding='utf-8') as f:
                sk = (json.load(f).get('source_keyword') or '').strip()
            if sk in kwset:
                dirs.append(d)
        except Exception:  # noqa: BLE001
            pass
    return dirs


def _run_xhs_analyze(keywords, lang='auto'):
    _xhs_an.update(running=True, done=0, total=0, error=None, ready=False)
    try:
        dirs = _xhs_match_dirs(keywords)
        _xhs_an['total'] = len(dirs)
        if not dirs:
            _xhs_an['error'] = '这些关键词下没有笔记'
            return
        from analyze import xhs_report

        def prog(done, total):
            _xhs_an['done'], _xhs_an['total'] = done, total

        report, extractions = xhs_report(dirs, title='小红书调研报告', on_progress=prog, lang=lang)
        with open(_XHS_REPORT, 'w', encoding='utf-8') as f:
            f.write(report or '')
        with open(os.path.join(_XHS_ROOT, 'xhs_dataset', '_extractions.json'),
                  'w', encoding='utf-8') as f:
            json.dump(extractions, f, ensure_ascii=False, indent=1)
        _xhs_an['ready'] = True
    except Exception as e:  # noqa: BLE001
        _xhs_an['error'] = str(e)[:200]
    finally:
        _xhs_an['running'] = False


@app.route('/api/xhs/analyze', methods=['POST'])
def api_xhs_analyze():
    if _xhs_an['running']:
        return jsonify({'ok': False, 'error': 'Analysis in progress'}), 409
    body = request.get_json(silent=True) or {}
    kws = [k.strip() for k in re.split(r'\n|\|\|', body.get('keywords') or '') if k.strip()]
    dirs = _xhs_match_dirs(kws)
    if not dirs:
        return jsonify({'ok': False,
                        'error': '这些关键词下还没有笔记 —— 先用同样的关键词采集，或清空关键词分析全部'}), 400
    threading.Thread(target=_run_xhs_analyze,
                     args=(kws, (body.get('lang') or 'auto')), daemon=True).start()
    return jsonify({'ok': True, 'matched': len(dirs)})


@app.route('/api/xhs/analyze_status')
def api_xhs_analyze_status():
    return jsonify({
        'running': _xhs_an['running'], 'done': _xhs_an['done'],
        'total': _xhs_an['total'], 'error': _xhs_an['error'],
        'ready': _xhs_an['ready'], 'has_report': os.path.isfile(_XHS_REPORT),
    })


@app.route('/api/xhs/report')
def api_xhs_report():
    if not os.path.isfile(_XHS_REPORT):
        return jsonify({'error': '还没有报告'}), 404
    with open(_XHS_REPORT, 'r', encoding='utf-8') as f:
        return jsonify({'markdown': f.read()})


if __name__ == '__main__':
    # 调试器默认关（debug=True 的 Werkzeug 调试器在公网上等于 RCE）。
    # 本地想要热重载显式 FLASK_DEBUG=1。另：HOST 非 127.0.0.1（对外暴露）时强制关 debug，
    # 防"改了 HOST 忘了关 debug"这类致命配置疏漏。
    _host = os.environ.get('HOST', '127.0.0.1')
    debug = os.environ.get('FLASK_DEBUG', '0') == '1' and _host in ('127.0.0.1', 'localhost')
    # 恢复未完成任务只在"真正服务的进程"里跑一次：
    # debug 模式有 reloader 父/子两进程，只在子进程（WERKZEUG_RUN_MAIN）跑；
    # 非 debug 只有一个进程，直接跑。
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        recover_unfinished_tasks()
        recover_unfinished_chains()
    # HOST 默认 127.0.0.1（本地只对自己开）；Docker 里设 HOST=0.0.0.0 对外暴露。
    app.run(debug=debug, threaded=True, host=_host,
            port=int(os.environ.get('PORT', 5001)))
```

---

## `config.py`

> 配置：引擎/并发/限流参数、分析大脑预设

```python
import os
from dotenv import load_dotenv

load_dotenv()

# Flask
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
RESULTS_FOLDER = os.path.join(os.path.dirname(__file__), 'results')
# 上传上限：视频（尤其 1080p 一小时）常轻松超过 500MB，之前会被 413 顶掉。
# 默认 4GB，可用 MAX_UPLOAD_MB 环境变量调。本地单用户，放宽无碍。
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '4096'))
MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'webm'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'mkv', 'avi', 'm4v'}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# 批量转录并发控制：一次可以丢进很多文件，但真正同时运行的数量按引擎区分。
# 本地 Whisper 每个任务都吃满 CPU/内存，必须保守；云引擎只是提交请求，可以放开。
ENGINE_CONCURRENCY = {
    # 本地 Whisper 并发。M 系多核（如 M4 Max 12 性能核）可开多路；配合下面的
    # WHISPER_CPU_THREADS，num_workers×cpu_threads ≈ 性能核数，避免多路互抢核。
    'whisper': int(os.environ.get('WHISPER_CONCURRENCY') or 4),
    # Paid Tier 1（~150 RPM）下 8 路并发稳妥。注意每个文件不止一次请求
    # （上传 + 长音频分段各一次），所以实际 QPS 会更高；若大量 429/空文本再下调。
    'gemini': int(os.environ.get('GEMINI_CONCURRENCY') or 12),
    'dashscope': 9,
    # Qwen-ASR：和 dashscope 同一套异步转写接口，配额也是同一个账号，给同样的并发。
    'qwenasr': 9,
    # 精准模式：单个任务内部会并发跑 Gemini 转写 + 阿里云说话人分离，最后再 Gemini 合并。
    # 一个任务实际打 2~3 次 Gemini + 1 次阿里云，所以并发压低到 4，避免叠加把两边都打爆。
    'precise': 4,
}

# 链条（URL→下载→转写→分析）的全局限流：所有链条共享，不按每条链条算。
# 这样开 1 条还是 5 条链，对 YouTube 和 Gemini 的瞬时压力恒定，多开只是排队更长、不会叠加超标。
CHAIN_DOWNLOAD_CONCURRENCY = 4   # 全部链条同时下载的视频数上限（再高易被 YouTube 限速/风控）
CHAIN_ANALYSIS_CONCURRENCY = 4   # 全部链条同时进行的逐期分析请求数上限

# yt-dlp 元数据语言偏好：拉中文标题，避免 UP 主上传的英文翻译标题被抓到
YTDLP_LANG = 'zh-CN'

# 从浏览器借 cookies 给 yt-dlp（用登录态绕过 B站 412 风控、抬高 YouTube 限额）。
# 值为浏览器名（chrome/edge/firefox/brave…）；置空则不带 cookies。
# 注意：仅本机、读你自己的浏览器 cookie；换机器或没装该浏览器时设为 '' 关闭。
YTDLP_COOKIES_FROM_BROWSER = os.environ.get('YTDLP_COOKIES_BROWSER', 'chrome')

# 访问令牌：不设置（默认）= 完全不启用鉴权，本地照常用。
# 要暴露到局域网/公网前，在 .env 里加 GETAUDIO_TOKEN=一串随机字符串，
# 然后浏览器首次访问 http://host:5001/?token=该字符串 即可（之后走 cookie）。
AUTH_TOKEN = os.environ.get('GETAUDIO_TOKEN', '')

# Whisper
# large-v3 精度最好（口语 + 专有名词多的内容值得）；M4 Max 扛得住。首次用会下载 ~3GB。
# 想快/省内存可在 Settings 或 env 改回 small/medium（WHISPER_MODEL_SIZE 运行时读 env）。
WHISPER_MODEL_SIZE = os.environ.get('WHISPER_MODEL_SIZE') or 'large-v3'
WHISPER_DEVICE = 'cpu'
# faster-whisper 每路用几个 CPU 线程 + 几个并行 worker。
# num_workers 让一个模型实例并行处理多路请求；cpu_threads 是每路的线程数。
# 目标：WHISPER_CONCURRENCY(=num_workers) × cpu_threads ≈ 性能核数（M4 Max 12）。
WHISPER_CPU_THREADS = int(os.environ.get('WHISPER_CPU_THREADS') or 3)
# None/空 = 自动检测语言（推荐，英文录音不会再被强制转成中文）；填 'zh' 可强制中文
WHISPER_LANGUAGE = os.environ.get('WHISPER_LANGUAGE') or None

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_TRANSCRIBE_MODEL') or 'gemini-2.5-flash'
# 分析/综合/核实层的模型。分层后事实判断已交给 grounding（核实模式）而非模型记忆，
# 所以默认用 2.5-pro（便宜、够用）；想要更晚的知识截止可用环境变量切到 3.x-pro。
GEMINI_ANALYSIS_MODEL = os.environ.get('GEMINI_ANALYSIS_MODEL') or 'gemini-2.5-pro'
# 逐期"抽取证据卡"是机械读写、又是调用大头（N 期 × 1）→ 用便宜的 flash（省钱大头）。
# 合成（人物画像）才用上面的 pro。advisor/orchestrator：大模型动嘴、小模型跑腿。
GEMINI_EXTRACT_MODEL = os.environ.get('GEMINI_EXTRACT_MODEL') or 'gemini-2.5-flash'
# 主模型 429/限流/挂了就自动降级到这些（flash 速率额度更宽、更便宜），保命用。
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in
    os.environ.get('GEMINI_FALLBACK_MODELS', 'gemini-2.5-flash,gemini-flash-latest').split(',')
    if m.strip()
]

# ===== 分析层「大脑」预设 =====
# 阿里云百炼一把 DashScope key 通吃 DeepSeek/Qwen/Kimi/GLM（OpenAI 兼容端点）。
# 有内容审查：只用于非敏感博主。转录不走这里（都是文本模型）。
ALIYUN_COMPAT_BASE = os.environ.get('ALIYUN_COMPAT_BASE') or \
    'https://dashscope.aliyuncs.com/compatible-mode/v1'
# OpenRouter：一把 key 通吃 Claude 等海外模型（OpenAI 兼容端点，无内容审查）。
OPENROUTER_COMPAT_BASE = os.environ.get('OPENROUTER_COMPAT_BASE') or \
    'https://openrouter.ai/api/v1'
ANALYSIS_PRESET_DEFAULT = os.environ.get('ANALYSIS_PRESET') or 'gemini'
# 预设 → (provider, 抽取模型, 合成模型)
ANALYSIS_PRESETS = {
    'gemini':   ('gemini', GEMINI_EXTRACT_MODEL, GEMINI_ANALYSIS_MODEL),
    'deepseek': ('aliyun', 'deepseek-v4-flash', 'deepseek-v4-pro'),
    'qwen':     ('aliyun', 'qwen3.7-plus', 'qwen3.7-plus'),
    'kimi':     ('aliyun', 'kimi-k2.6', 'kimi-k2.6'),
    'glm':      ('aliyun', 'glm-5.2', 'glm-5.2'),
    # Claude（走 OpenRouter，带 thinking）：贵但强，抽取+合成同模型，成本随期数线性涨
    'opus46':   ('openrouter', 'anthropic/claude-opus-4.6', 'anthropic/claude-opus-4.6'),
    'opus5':    ('openrouter', 'anthropic/claude-opus-5', 'anthropic/claude-opus-5'),
}


def resolve_analysis(preset):
    """预设名 → (provider, 抽取模型, 合成模型)。未知则回落 gemini。

    运行时读 env（Settings 保存即生效，不用重启）：gemini 预设的两档模型
    可被 GEMINI_EXTRACT_MODEL / GEMINI_ANALYSIS_MODEL 覆盖。
    """
    p = ANALYSIS_PRESETS.get(preset or ANALYSIS_PRESET_DEFAULT,
                             ANALYSIS_PRESETS['gemini'])
    if p[0] == 'gemini':
        return ('gemini',
                os.environ.get('GEMINI_EXTRACT_MODEL') or p[1],
                os.environ.get('GEMINI_ANALYSIS_MODEL') or p[2])
    return p
# 卡片元数据（标题/标签）生成用 Flash：快、便宜，质量足够
GEMINI_ENRICH_MODEL = 'gemini-2.5-flash'
GEMINI_INLINE_LIMIT = 19 * 1024 * 1024  # 19 MB, use File API above this

# DashScope (阿里云百炼)
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', '')
# 阿里云 ASR 统一走 Qwen-Audio-3.0（2026-08 起）。paraformer-v2 已被阿里官方标为
# 上一代并建议迁移；实测同一段真人录音，paraformer 会把「AI」听成「悲哀」、
# 「语音识别」听成「原因识别」，Qwen 则准确，说话人分离两者相当（都正确分出 2 人）。
# 两代接口完全一致（同异步端点、同 diarization_enabled/speaker_count 参数、
# 同 sentences[].begin_time/text/speaker_id 返回结构），所以换模型名即可。
# 单文件上限 12 小时 / 2GB。需要退回旧模型时设 DASHSCOPE_ASR_MODEL=paraformer-v2。
DASHSCOPE_ASR_MODEL = (os.environ.get('DASHSCOPE_ASR_MODEL')
                       or 'qwen-audio-3.0-asr-flash-filetrans')
DASHSCOPE_LLM_MODEL = 'qwen-plus'


def make_gemini_client(api_key, timeout_ms=600_000, base_url=None):
    """统一构造 Gemini 客户端：带超时 + 可选自定义 base_url。

    base_url 显式传入优先；否则取环境变量 GEMINI_BASE_URL（Settings 里可填），
    给国内用户挂代理用；留空则直连官方。显式传入避免测试时改动全局 env 污染并发调用。
    老 SDK 不支持 HttpOptions 时回退到最简构造。
    """
    from google import genai
    base = ((base_url if base_url is not None else os.environ.get('GEMINI_BASE_URL'))
            or '').strip() or None
    try:
        from google.genai import types
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms, base_url=base),
        )
    except Exception:
        return genai.Client(api_key=api_key)
```

---

## `taskdb.py`

> 任务持久化（SQLite）+ 重启恢复

```python
"""
任务持久化（SQLite）。

只记录任务的生命周期状态（pending/running/done/failed），
转写结果本身仍然存 results/<task_id>/ 文件目录，职责不变。

价值：服务重启后能从 DB 找回没跑完的任务并重新入队，
不再像纯内存 tasks 字典那样凭空消失。
"""

import os
import sqlite3
import threading
from datetime import datetime

# GETAUDIO_DB 允许把库挪到持久卷里（Docker 把它指到 results/ 下，
# 否则镜像重建就丢任务状态）；本地默认还是 getAudio/tasks.db 不变。
DB_PATH = (os.environ.get('GETAUDIO_DB')
           or os.path.join(os.path.dirname(__file__), 'tasks.db'))

# sqlite3 连接不跨线程共享；每次操作开新连接（量小，开销可忽略），
# 写操作用锁串行化，避免 WAL 下偶发的 database is locked。
_write_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _write_lock, _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id            TEXT PRIMARY KEY,
                filename      TEXT,
                engine        TEXT,
                speaker_count INTEGER,
                upload_path   TEXT,
                status        TEXT,
                error         TEXT,
                created_at    TEXT,
                updated_at    TEXT
            )
        ''')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def create(task_id, filename, engine, speaker_count, upload_path):
    with _write_lock, _conn() as c:
        c.execute(
            'INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)',
            (task_id, filename, engine, speaker_count, upload_path,
             'pending', None, _now(), _now()),
        )


def set_status(task_id, status, error=None):
    with _write_lock, _conn() as c:
        c.execute(
            'UPDATE tasks SET status=?, error=?, updated_at=? WHERE id=?',
            (status, error, _now(), task_id),
        )


def unfinished():
    """服务启动时调用：返回所有没跑完的任务行。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'running')"
        ).fetchall()
    return [dict(r) for r in rows]


def get(task_id):
    """按 id 取单个任务行，不存在返回 None。"""
    with _conn() as c:
        r = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    return dict(r) if r else None
```

---

## `downloader.py`

> yt-dlp 下载（probe/download_one，cookies + zh-CN 标题 + 合集展开 + B站活动页自愈）

```python
"""
yt-dlp 下载模块：URL（单视频 / 播放列表 / 频道）→ 音频文件。

调用系统 yt-dlp 二进制（brew 版，保持最新，能吃到用户全局配置/cookies），
而不是 venv 里的 python 包——后者被 Python 3.9 锁死在旧版本，
YouTube 的反爬会让旧版直接解析失败。

对外暴露：
  - probe(url, max_videos) -> [{'video_url', 'title', 'video_id', 'thumbnail'}, ...]
  - download_one(target, dest_dir) -> {'path', 'title', 'video_id', 'thumbnail'} 或 None
  - download_audios(...) 仍保留（串行全下），供非链条场景/兼容使用。
"""

import json
import os
import time
import re
import shutil
import subprocess

from config import YTDLP_LANG, YTDLP_COOKIES_FROM_BROWSER

_PROBE_TIMEOUT = 120        # 元数据解析超时（秒）
_PROBE_ATTEMPTS = 3         # B站 412 等间歇风控：解析链接也退避重试
_DOWNLOAD_TIMEOUT = 1800    # 单个视频音频下载超时（秒）

_YT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')


def _resolve_ytdlp():
    # brew 版优先：venv 的 PATH 里可能残留 pip 装的旧版 yt-dlp（py3.9 锁旧版），
    # 旧版会被 YouTube 反爬挡掉，必须避开。
    candidates = ['/opt/homebrew/bin/yt-dlp', shutil.which('yt-dlp')]
    for binary in candidates:
        if binary and os.path.exists(binary):
            return binary
    raise RuntimeError('yt-dlp not found. Install it: brew install yt-dlp')


def _lang_args():
    return ['--extractor-args', f'youtube:lang={YTDLP_LANG}'] if YTDLP_LANG else []


def _cookie_args():
    # 借浏览器登录态：绕过 B站 412 风控、抬高 YouTube 限额
    return (['--cookies-from-browser', YTDLP_COOKIES_FROM_BROWSER]
            if YTDLP_COOKIES_FROM_BROWSER else [])


# YouTube 频道主页（无栏目）→ 补 /videos，否则 yt-dlp 会把"视频/直播/短视频"
# 各个栏目 tab 当成条目返回，下载器再拿 tab 去下 = 下载整个频道，永远下不完。
_YT_CHANNEL_ROOT = re.compile(
    r'^(https?://(?:www\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?(?:[?#].*)?$'
)

# B站 UP主空间页：砍掉 /video 等子路径和 query，回到裸空间页。
# space.bilibili.com/<uid>/video 会走 BilibiliSpaceVideo 提取器，412 风控更凶；
# 裸 space.bilibili.com/<uid> 更稳。单个视频 www.bilibili.com/video/BV... 不受影响。
_BILI_SPACE = re.compile(r'^(https?://space\.bilibili\.com/\d+)(?:/.*)?$')


def _normalize_bili_list(url):
    """B站合集/系列链接 → yt-dlp 认识的形式；不是合集则返回 None。

    浏览器地址栏给的是新版 `space.bilibili.com/<uid>/lists?sid=<sid>`，yt-dlp 报
    Unsupported URL；而 _BILI_SPACE 又会把 ?sid= 连同路径一起砍掉、退化成整个空间页
    （变成下这个 UP 的全部视频，不是这个合集）。所以要在它之前转成
    channel/collectiondetail?sid=（合集）或 channel/seriesdetail?sid=（系列）。
    """
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(url or '')
    except ValueError:
        return None
    if 'space.bilibili.com' not in (u.netloc or ''):
        return None
    parts = [p for p in (u.path or '').split('/') if p]
    if not parts or not parts[0].isdigit():
        return None
    uid = parts[0]
    rest = parts[1:]
    if not rest or rest[0] not in ('lists', 'channel'):
        return None
    q = parse_qs(u.query or '')
    sid = (q.get('sid') or [''])[0]
    if not sid:                       # /lists/<sid> 这种把 sid 放在路径里的
        for seg in rest[1:]:
            if seg.isdigit():
                sid = seg
                break
    if not sid:
        return None
    kind = (q.get('type') or [''])[0].lower()
    if 'seriesdetail' in rest or kind == 'series':
        page = 'seriesdetail'
    else:
        page = 'collectiondetail'     # 合集（season）是常见情形，默认它
    return f'https://space.bilibili.com/{uid}/channel/{page}?sid={sid}'


def _normalize_url(url):
    url = url or ''
    m = _YT_CHANNEL_ROOT.match(url)
    if m:
        return m.group(1) + '/videos'
    bl = _normalize_bili_list(url)     # 合集/系列要先认，否则会被下面这条砍成整个空间页
    if bl:
        return bl
    b = _BILI_SPACE.match(url)
    if b:
        return b.group(1)
    return url


def _is_video_entry(e):
    """判断 flat 条目是不是"单个视频"——排除频道 tab / 嵌套播放列表。"""
    if e.get('_type') == 'playlist':
        return False
    if e.get('ie_key') in ('YoutubeTab', 'YoutubeChannel'):
        return False
    vid = e.get('id') or ''
    if vid.startswith('UC') and len(vid) == 24:   # YouTube 频道 ID，不是视频
        return False
    return True


def _thumbnail_for(entry):
    """从 flat 条目里取封面 URL；YouTube 用稳定的 ytimg 兜底。"""
    thumbs = entry.get('thumbnails')
    if isinstance(thumbs, list) and thumbs:
        for t in reversed(thumbs):  # 最后一个通常分辨率最高
            if t.get('url'):
                return t['url']
    if entry.get('thumbnail'):
        return entry['thumbnail']
    vid = entry.get('id') or ''
    if _YT_ID_RE.match(vid):
        return f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
    return ''


def _channel_from_info(info):
    """从 yt-dlp 信息里取频道名 + 头像 + 订阅数（尽力而为，取不到留空/0）。"""
    name = (info.get('channel') or info.get('uploader')
            or info.get('playlist_uploader') or '')
    avatar = ''
    thumbs = info.get('thumbnails')
    if isinstance(thumbs, list):
        for t in thumbs:                     # 频道头像的 thumbnail id 通常含 'avatar'
            if 'avatar' in str(t.get('id', '')).lower() and t.get('url'):
                avatar = t['url']
                break
        if not avatar and thumbs and thumbs[0].get('url'):
            avatar = thumbs[0]['url']        # 兜底：第一张缩略图
    followers = (info.get('channel_follower_count')
                 or info.get('subscriber_count') or 0)
    return {'name': (name or '').strip(), 'avatar': avatar,
            'followers': int(followers) if followers else 0}


def channel_followers(url):
    """单独取订阅数。YouTube 在 lang=zh-CN 下会把 channel_follower_count 抹成 None，
    所以这里用**不带 lang**的干净命令探一次（cookies 保留）。取不到返回 0。"""
    try:
        binary = _resolve_ytdlp()
        cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
               *_cookie_args(), '--playlist-end', '1', _normalize_url(url)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
        if r.returncode == 0 and r.stdout.strip():
            fc = json.loads(r.stdout).get('channel_follower_count') or 0
            return int(fc) if fc else 0
    except Exception:  # noqa: BLE001
        pass
    return 0


def probe(url, max_videos=None):
    """解析 URL 元数据（不下载）。

    返回 (targets, channel)：
      targets = [{'video_url','title','video_id','thumbnail'}, ...]
      channel = {'name', 'avatar'}
    """
    binary = _resolve_ytdlp()
    url = _normalize_url(url)          # 频道主页 → /videos，避免下成整个频道
    cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
           *_lang_args(), *_cookie_args()]
    if max_videos:
        cmd += ['--playlist-end', str(max_videos)]
    cmd.append(url)

    # B站 412 / 网络抖动这类间歇性风控：退避重试（download_one 早就这么干，
    # probe 之前漏了，一次 412 就把整条链判死）。
    result = None
    for attempt in range(1, _PROBE_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0 and result.stdout.strip():
            break
        if attempt < _PROBE_ATTEMPTS:
            time.sleep(4 * attempt)     # 4s, 8s 退避
    if result is None or result.returncode != 0 or not result.stdout.strip():
        err = (result.stderr or '').strip().splitlines() if result else []
        detail = err[-1][:200] if err else 'timeout / unknown error'
        raise RuntimeError(f'Could not parse link: {detail}')

    info = json.loads(result.stdout)
    channel = _channel_from_info(info)

    if info.get('_type') == 'playlist':
        # 只保留真正的视频条目，滤掉频道 tab / 嵌套播放列表（防跑飞）
        entries = [e for e in (info.get('entries') or []) if e and _is_video_entry(e)]
        if max_videos:
            entries = entries[:max_videos]
        targets = []
        for e in entries:
            targets.append({
                'video_url': e.get('url') or e.get('webpage_url') or e.get('id'),
                'title': e.get('title', ''),
                'video_id': e.get('id', ''),
                'thumbnail': _thumbnail_for(e),
                'view_count': int(e.get('view_count') or 0),  # flat 常为 0，B站等有时给
            })
        # B站空间页等 flat 探测拿不到频道名（entries 连 title 都是空的）——
        # 兜底：对第一个视频做一次全量探测，用它的 uploader 当频道名/头像。
        if not channel.get('name') and targets:
            try:
                _, ch2 = probe(targets[0]['video_url'])   # 单视频 → 走下面的全量分支
                if ch2.get('name'):
                    channel['name'] = ch2['name']
                    if not channel.get('avatar') and ch2.get('avatar'):
                        channel['avatar'] = ch2['avatar']
            except Exception:  # noqa: BLE001  探测失败不影响主流程
                pass
        return targets, channel

    return [{
        'video_url': url,
        'title': info.get('title', ''),
        'video_id': info.get('id', ''),
        'thumbnail': _thumbnail_for(info),
    }], channel


_DOWNLOAD_ATTEMPTS = 3        # B站 412 等间歇性风控：退避重试，绝大多数第二次就过


def download_one(target, dest_dir, section=None):
    """下载单个目标的音频，返回 {'path','title','video_id','thumbnail'} 或 None。

    带退避重试：B站 412 / 网络抖动这类间歇失败，隔几秒重试常能过。
    section: 可选时间段（yt-dlp --download-sections 的值，如 '*600-1500'），
             只下载/切出那一段音频，转写成本随之下降。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    outtmpl = os.path.join(dest_dir, '%(title)s [%(id)s].%(ext)s')
    section_args = ['--download-sections', section] if section else []
    cmd = [
        binary,
        '-x', '--audio-format', 'mp3', '--audio-quality', '128K',
        '-o', outtmpl,
        '--no-playlist', '--no-warnings', '--quiet',
        *section_args,
        *_lang_args(), *_cookie_args(),
        # 下载+后处理完成后打印最终文件路径和元信息，逐行读取
        '--print', 'after_move:filepath',
        '--print', 'after_move:title',
        '--print', 'after_move:id',
        '--print', 'after_move:view_count',
        '--no-simulate',
        target['video_url'],
    ]
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
            if len(lines) >= 3 and os.path.isfile(lines[0]):
                vc = 0
                if len(lines) >= 4:
                    try:
                        vc = int(lines[3])
                    except (ValueError, TypeError):
                        vc = 0
                return {
                    'path': lines[0],
                    'title': lines[1] or target.get('title') or 'untitled',
                    'video_id': lines[2] or target.get('video_id', ''),
                    'thumbnail': target.get('thumbnail', ''),
                    'view_count': vc or int(target.get('view_count') or 0),
                }
        if attempt < _DOWNLOAD_ATTEMPTS:
            time.sleep(4 * attempt)      # 4s, 8s 退避
    # B站「活动页」视频（入选盛典/榜单等）：普通 /video/BVxxx 页 yt-dlp 解析不了
    # （Unable to extract initial state），但同一支片子的活动页 URL 可以。换它再试一次。
    fest = _bili_festival_url(target.get('video_url'))
    if fest and fest != target.get('video_url'):
        return download_one({**target, 'video_url': fest}, dest_dir, section=section)
    return None


_BV_RE = re.compile(r'(BV[0-9A-Za-z]{10})')


def _bili_festival_url(url):
    """查这支 B站视频的活动页地址（官方 API 的 festival_jump_url）；没有则 None。"""
    if 'bilibili.com' not in (url or ''):
        return None
    m = _BV_RE.search(url or '')
    if not m:
        return None
    try:
        import urllib.request
        req = urllib.request.Request(
            f'https://api.bilibili.com/x/web-interface/view?bvid={m.group(1)}',
            headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return ((data.get('data') or {}).get('festival_jump_url') or '').strip() or None
    except Exception:  # noqa: BLE001
        return None


_SUB_TIMEOUT = 120


def _collect_srt(dest_dir, vid):
    try:
        names = [n for n in os.listdir(dest_dir)
                 if n.startswith('sub_') and n.endswith('.srt')
                 and (not vid or vid in n)]
    except OSError:
        return None
    return os.path.join(dest_dir, names[0]) if names else None


def _pick_sub_lang(tracks, orig):
    """从可用字幕轨里挑一条：原语言 > 英 > 中(含B站 AI字幕 ai-zh) > 任意。避免下成机翻。"""
    if not tracks:
        return None
    pref = [orig, 'en', 'en-US', 'zh-Hans', 'zh', 'zh-CN', 'zh-Hant',
            'ai-zh', 'ai-en']          # ai-zh/ai-en：B站等平台的 AI 自动字幕
    for code in [c for c in pref if c]:
        if code in tracks:
            return code
    # 兜底：任何 zh 开头(含 ai-zh)的轨优先，再不行取第一条
    for code in tracks:
        if str(code).lower().startswith(('zh', 'ai-zh')):
            return code
    return next(iter(tracks))


def fetch_subtitle(target, dest_dir):
    """抓取视频已有字幕并转 srt。返回 (srt路径, 类型) 或 (None, None)。

    先用一次 -J 拿到视频语言 + 可用字幕清单，只下『原语言那一条轨』——
    避免请求多语言触发 429，也避免下成机器翻译。人工字幕优先，其次自动字幕。
    """
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    url = target['video_url']

    # 1) 一次元数据调用，拿语言 + 字幕清单（不下载、不强制 lang，免得偏向翻译轨）
    try:
        r = subprocess.run(
            [binary, '-J', '--skip-download', '--no-playlist', '--no-warnings',
             *_cookie_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
        info = json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception:
        return None, None

    orig = (info.get('language') or '').split('-')[0]
    manual = info.get('subtitles') or {}
    autos = info.get('automatic_captions') or {}

    kind = chosen = None
    m = _pick_sub_lang(manual, orig)
    if m:
        kind, chosen = 'manual', m
    else:
        a = orig if (orig and orig in autos) else _pick_sub_lang(autos, orig)
        if a:
            kind, chosen = 'auto', a
    if not chosen:
        return None, None

    # 2) 只下这一条轨
    outtmpl = os.path.join(dest_dir, 'sub_%(id)s.%(ext)s')
    flag = '--write-subs' if kind == 'manual' else '--write-auto-subs'
    try:
        subprocess.run(
            [binary, '--skip-download', flag, '--sub-langs', chosen,
             '--convert-subs', 'srt', '-o', outtmpl,
             '--no-playlist', '--no-warnings', '--quiet', *_cookie_args(), url],
            capture_output=True, text=True, timeout=_SUB_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, None
    path = _collect_srt(dest_dir, target.get('video_id', ''))
    return (path, kind) if path else (None, None)


def _fmt_ts(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f'{h:d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


_SRT_TS = re.compile(
    r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})')


def parse_srt(path):
    """SRT → [{'timestamp','end','text'}]，格式对齐转写输出。

    YouTube 自动字幕是"滚动累积"格式（同一句反复出现、逐步补全），逐行去重：
    与上一行相同则跳过；是上一行的延伸则替换（保留最长的那版）。
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read()
    except OSError:
        return []
    segs = []
    last = None
    for block in re.split(r'\n\s*\n', raw.strip()):
        m = _SRT_TS.search(block)
        if not m:
            continue
        sh, sm, ss, _, eh, em, es, _ = m.groups()
        ts, end = _fmt_ts(int(sh) * 3600 + int(sm) * 60 + int(ss)), \
            _fmt_ts(int(eh) * 3600 + int(em) * 60 + int(es))
        for ln in block.splitlines():
            if _SRT_TS.search(ln) or ln.strip().isdigit():
                continue
            text = re.sub(r'<[^>]+>', '', ln).strip()
            if not text or text == last:
                continue
            if last and text.startswith(last):           # 滚动补全：延伸上一行
                segs[-1] = {'timestamp': segs[-1]['timestamp'], 'end': end, 'text': text}
                last = text
                continue
            if last and last.startswith(text):           # 上一行已包含它
                continue
            segs.append({'timestamp': ts, 'end': end, 'text': text})
            last = text
    return segs


def download_audios(url, dest_dir, max_videos=None, progress_cb=None):
    """（兼容旧接口）串行下载 URL 指向的所有音频，返回成功项列表。"""
    targets, _ = probe(url, max_videos)
    if not targets:
        raise RuntimeError('No downloadable videos at this link')

    total = len(targets)
    results = []
    for idx, target in enumerate(targets):
        if progress_cb:
            progress_cb(idx, total, target.get('title') or target['video_url'])
        item = download_one(target, dest_dir)
        if item:
            results.append(item)
    if progress_cb:
        progress_cb(total, total, '')
    return results
```

---

## `harness.py`

> 极简 agent 原语：fanout/agent，确定性编排、零业务依赖

```python
"""
极简 agent harness —— 确定性编排 + 模型只做叶子。

不做"自主 agent 自己决定下一步"（贵、不可控、爱跑偏）。这里只提供两块积木，
控制流全在调用方的 Python 里写死：

  fanout(items, fn, concurrency)   并发 map，保序；单个失败 → 该位置 None（不拖垮整批）
  agent(call_fn, prompt, schema=…) 一次 LLM 调用：重试 + 可选 JSON 必需键校验

call_fn 由调用方注入（如 analyze._llm），所以本模块**零业务依赖、可独立测试**，
也不会和 analyze / config 形成循环导入。
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor


def fanout(items, fn, concurrency=6):
    """并发把 fn 施加到每个 item，**保序**返回。单个抛异常 → 该位置 None。"""
    items = list(items)
    if not items:
        return []

    def _run(i):
        try:
            return i, fn(items[i])
        except Exception:  # noqa: BLE001  单个失败不拖垮整批
            return i, None

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(items)))) as ex:
        for i, r in ex.map(_run, range(len(items))):
            results[i] = r
    return results


def _extract_json(raw):
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹和前言）。"""
    m = re.search(r'```json\s*(.*?)\s*```', raw or '', re.DOTALL)
    if m:
        raw = m.group(1)
    raw = (raw or '').strip()
    s = raw.find('{')
    if s != -1:
        raw = raw[s:]
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def agent(call_fn, prompt, *, schema=None, retries=2):
    """一次 LLM 调用。

    schema=None            → 返回原始文本（失败到底返回 ''）。
    schema=可迭代的必需键名 → 解析 JSON 并校验这些键都在，缺则重试；失败到底返回 None。
    call_fn(prompt) -> str 由调用方注入（保持本模块无业务依赖）。
    """
    for _ in range(retries + 1):
        try:
            out = call_fn(prompt)
        except Exception:  # noqa: BLE001
            out = None
        if not out:
            continue
        if schema is None:
            return out
        obj = _extract_json(out)
        if isinstance(obj, dict) and all(k in obj for k in schema):
            return obj
    return None if schema is not None else ''
```

---

## `analyze.py`

> 证据卡抽取 + 跨期合成 + 证伪层 + 镜头 + 小红书多模态分析

```python
"""
博主解读：证据卡两段式 —— 逐期只做中立抽取，评判/风格采纳全部上移到合并层。

  1. analyze_episode()  逐期 → 结构化"证据卡"（原子观察 + 逐字引文 + 时间戳 + 证据层）
                        + 三个可数修辞指标（hype/hedge/tradeoff）+ 疑似 ASR 误识实体。
                        零评判、离线。
  2. synthesize()       只吃卡片（不再吃全文）→ 人物画像。母题归纳、风格采纳、批判都在这层，
                        每条结论必须能点回某张卡的引文；批判强度由 critique_level 调（只调语气，
                        不调证据松紧）。
可选 verify=True：逐期附一节联网核实脚注，只作脚注、绝不进卡片/画像结论。
"""

import json
import os
import re
import time
from datetime import datetime

from config import (GEMINI_API_KEY, GEMINI_ANALYSIS_MODEL,
                    GEMINI_EXTRACT_MODEL, GEMINI_FALLBACK_MODELS,
                    DASHSCOPE_API_KEY, ALIYUN_COMPAT_BASE,
                    OPENROUTER_COMPAT_BASE, resolve_analysis,
                    make_gemini_client)
from harness import fanout, agent

_CALIBRATION_RULES = """【校准兜底 · 必守】
- 今天是 {today}。内容可能涉及你知识截止之后的论文/模型/事件。**不认识 ≠ 不存在 ≠ 编造。**
- 不许判"造假/虚构"。涉及外部事实而无法确认的，一律记"未能核实"，不下真伪判决。
- 保留说话人的存疑标注；区分"他的主张 / 他转述来源 / 他自己标存疑"。"""

# ---- 逐期：抽取证据卡（中立、结构化、离线、零评判）----
CARDS_PROMPT = """你的任务是**中立抽取证据卡**：不评价、不判断、不润色、不下结论。从「{author}」这期《{title}》的转写抽出结构化证据。

严格只输出 JSON：
{{
  "cards": [
    {{"obs": "一条原子观察（他的一个主张／一个修辞手法／一个叙事框架，客观描述，不带评价词）",
      "quote": "支撑它的逐字原话（照抄转写，别改写）",
      "timestamp": "该原话的时间戳（从转写行首 [MM:SS] 取，取不到留空）",
      "layer": "转写自证 | 他的主张 | 外部核实"}}
  ],
  "metrics": {{
    "hype":     {{"count": 0, "examples": ["最高级/军备竞赛类原话，如 碾压/秒杀/新王/无敌/彻底取代/判死刑"]}},
    "hedge":    {{"count": 0, "examples": ["待验证/尚未/存疑/未开源/自己核 类原话"]}},
    "tradeoff": {{"tech_count": 0, "with_tradeoff": 0, "examples": ["介绍某技术时确实提到了代价/权衡的原话"]}}
  }},
  "asr_suspects": ["疑似语音识别错误的实体名，照抄听错的样子（如 千万三 / Dora-CIT）"]
}}

规则（必守）：
- layer：修辞/叙事/措辞观察 = 转写自证；他下的事实/性能/预测判断 = 他的主张；只有你确凿的外部事实核对才 = 外部核实（默认几乎不用）。
- 卡片里**不许出现评价词**（不写"浮夸/严谨/高明/盲区"），只客观描述 + 引文。评价是合并层的事。
- quote 必须是转写里的逐字原话，能让人点回原文。
- metrics：hype/hedge 的 count = 出现次数，examples 放几条代表原话；tradeoff 的 tech_count = 他介绍了几个技术，with_tradeoff = 其中几个提了代价。
- asr_suspects：只放疑似识别错误的**实体名**——这是转写质量问题，不是他的语言风格。
- 今天是 {today}，不认识的新词照抄，别判真假、别猜"正确写法"。

转写：
{transcript}"""

# ---- 可选：联网核实（仅脚注，不进卡片/画像）----
VERIFY_PROMPT = """（联网核实 · 仅作脚注）下面是从一期抽出的、依赖外部事实的 claim。用 Google 搜索逐条核对，只输出一节 Markdown：

## 事实核对（脚注 · 不改变对这个人的解读）
- 每条：`claim → 真实 / 未找到 / 有出入`，加一句依据（给得出源就给源）。
- 疑似语音识别错误的实体，先搜正确写法再核。

claims：
{claims}"""

# ---- 转写体检：模型只「标」出识别垃圾，删除交给确定性代码 ----
REVIEW_PROMPT = """下面是一段语音转写（ASR），每行 [编号] 文字。找出【明显是识别垃圾】的段：静音幻听、模型卡死的重复循环、无意义碎片、整段逐字复读。

只输出 JSON：{{"drop": [[起,止], ...]}}（闭区间编号）。
铁律：
- 只删明显垃圾；真实说话内容一律保留，再短也留（"对""OK""好"）。
- 逐字复读只删重复的那几条，保留第一条。
- 拿不准 → 不删。**不要改写任何文字**，只返回要删的编号区间。

段：
{body}"""


def review_transcript(segments, preset=None, window=180):
    """模型体检：返回「该删的段索引」集合（模型只标，代码删）。

    分窗送（每窗 window 段）控 token；用便宜的抽取模型。
    无 key / 出错 / 无结果 → 返回空集合，绝不误伤。
    """
    if not segments:
        return set()
    provider, extract_model, _ = resolve_analysis(preset)
    drop = set()
    for base in range(0, len(segments), window):
        chunk = segments[base:base + window]
        body = '\n'.join(
            "[%d] %s" % (i, re.sub(r'\s+', '', (s.get('text') or ''))[:60])
            for i, s in enumerate(chunk))
        try:
            obj = agent(lambda p: _llm(p, provider, extract_model),
                        REVIEW_PROMPT.format(body=body), schema=['drop'])
        except Exception:  # noqa: BLE001
            obj = None
        win_drop = set()
        for r in (obj or {}).get('drop', []) or []:
            try:
                a, b = int(r[0]), int(r[1])
            except (ValueError, TypeError, IndexError):
                continue
            for k in range(max(0, a), min(len(chunk), b + 1)):
                win_drop.add(base + k)
        # 代码级保险：单窗删除 > 40% 视为模型判乱了，整窗判决作废——
        # 宁可漏删噪声，绝不误删真内容（模型幻觉可能返回一整窗）
        if chunk and len(win_drop) > len(chunk) * 0.4:
            continue
        drop |= win_drop
    return drop


# ---- 合并层：人物画像（只吃卡片；批判档位只调语气）----
_TONE = {
    'descriptive': '只描述、不评判：呈现他的母题/风格/指标分布，不下价值判断、不展开"盲区/缺陷"。',
    'analytical': '可指出系统性盲区与回避，但归因克制：只依据证据说"他在 X 上回避 Y"，不猜动机、不夸大。',
    'sharp': '可以明确下判断、语气锋利直接。但证据标准丝毫不变：每条评判仍须挂证据层标签、仍须能点回引文——提升的只是语气强度，不是证据松紧。',
}

PORTRAIT_PROMPT = """你会收到「{author}」{n} 期的**证据卡 + 修辞指标**（不是全文）。基于这些卡片综合成一份人物画像（Markdown）：这个人怎么看、怎么思考他的领域。

{lang_line}（下面的小标题是示例，请按输出语言翻译。）

{rules}

【评判档位：{level}】{tone}

【硬约束 · 任何档位都必守】
- 每条评判性结论必须挂证据层标签〔转写自证〕/〔他的主张〕/〔外部核实〕，且能点回某张卡的引文；点不回引文的**不许写**。
- 他的判断/预测一律挂〔他的主张〕，**不许**洗成你自己的客观结论。
- 涉及外部事实的只标"未核实/流行说法"，**不要背书**（别把营销回声当已核实）。
- 只做行为对比、**不猜动机**（可写"他只在非开源项目上 hedge、别处浮夸"，不许写"为维持人设"）。
- 区分"他个人的选择性回避"与"这个体裁天生不做的事"，后者别算进他的盲区。
- 跨语境的态度不一致，如实描述为"不一致"（如"对开源项目 hedge、对闭源项目浮夸"）；**不许**升级成"双重标准/知行不一/虚伪"这类道德指控——证据只支撑到不一致，支撑不到诛心。
- 修辞结论用给到的 hype/hedge/tradeoff 跨期分布支撑；**不要**输出"客观性/可信度"这类合成总分，只用可数分项。
- 疑似 ASR 误识的实体名属于转写质量，放最后的「转写质量说明」里，**不许**当成他的语言风格。

# {author}：人物解读（基于 {n} 期）

## 他怎么看他的领域
他对这个领域的总体世界观/判断（记住这些是他的，挂〔他的主张〕）。

## 思维方法与母题
跨期反复出现的思考方式、判断偏好、执念。以〔转写自证〕为主。

## 叙事与修辞风格
用 hype / hedge / tradeoff 的跨期分布 + 卡片引文支撑：他在哪些事上浮夸、在哪些事上 hedge、介绍技术时提不提代价。

## 系统性盲区
（descriptive 档可略过或只陈述事实；analytical/sharp 才展开。区分个人回避 vs 体裁固有。）

## 综合印象（本节为 {level} 档下的判断，换档/换语气可能变化）
读完这些他是个怎样的创作者/思考者。分析者视角，但把"他的主张"和"你的判断"分清。尽量把总体判断锚到可数指标（hype/hedge/tradeoff 的跨期分布）上，而不是笼统的情绪定性。{impression}

## 转写质量说明
本报告基于的转写可能含语音识别错误，下列实体名可能失真（仅元数据，非其风格）：按卡片里的 asr_suspects 汇总。

证据卡与指标（JSON）：
{digest}"""

# ---- 证伪层：从画像抽论断 → 逐条 skeptic 反驳 → 撑不住就砍 ----
CLAIMS_PROMPT = """下面是一份对「{author}」的人物画像。抽出其中**评判性 / 概括性的论断**——即对这个人下的判断（他的思维偏好、修辞风格、盲区、总体印象等），不是纯转述、不是小标题、不是元说明（如"转写质量说明"）。

只输出 JSON：{{"claims": ["论断一（尽量照抄画像原句）", "论断二", "..."]}}
只抽**可被证据检验**的判断句；最多 25 条，挑最实质的。

画像：
{portrait}"""

SKEPTIC_PROMPT = """（证伪 · 唱反调）下面有一条对「{author}」的论断，和一批从他视频抽出的证据卡。你的任务是**尽力反驳它**：卡片到底撑不撑得住？

只输出 JSON：{{"verdict": "成立 | 夸大 | 不成立", "why": "一句话依据", "support_quote": "能撑住它的逐字引文，没有留空"}}

判据：
- 成立：至少一张卡的引文能直接支撑。
- 夸大：有影子但说过头了（如证据只到"前后不一致"，论断却说成"虚伪/双标"）。
- 不成立：没有卡片支撑，或与卡片矛盾。
- **拿不准 → 往"夸大/不成立"靠，别轻易放行。**

论断：{claim}

证据卡（JSON）：
{cards}"""

REVISE_PROMPT = """下面是一份人物画像，和对其中若干论断的**证伪结果**。据此修订：
- 判"不成立"的论断：**删掉**。
- 判"夸大"的论断：**改写softer**，只说到证据撑得住的程度。
- 其余不动。保持原结构、原语言、原证据层标签。

输出修订后的画像正文，并在**末尾加一节**：
## 证伪留痕
逐条列出被删/改的论断 + 判定（不成立/夸大）+ 一句依据。

原画像：
{portrait}

证伪结果（JSON）：
{verdicts}"""

_SYNTH_CHAR_LIMIT = 600_000
_VERIFY_DIGEST_CHARS = 60_000    # 证伪时每条论断重发的证据上限（控成本，见 _verify_portrait）
_MAX_ATTEMPTS = 3

# 期数超过这个就走 map-reduce：分批做中间简报再合成，
# 而不是把几百期卡片硬塞一个 prompt（旧 _digest 会砍卡片、越多期砍越狠）。
_BATCH_SIZE = 24
_BRIEF_CONCURRENCY = 6

# ---- map 阶段：把一批期压成紧凑的中间简报（便宜模型、保留代表性引文供溯源）----
BRIEF_PROMPT = """你会收到「{author}」其中 {n} 期的**证据卡 + 修辞指标**。把这一批压成一份**紧凑的中间简报**（Markdown），供后续跨全部期综合用。**只提炼、不下最终结论、不评判整个人**。

要求：
- 按**反复出现的母题 / 立场 / 修辞手法**归拢，别按期逐条罗列。
- 每个母题下保留 1~2 条**最具代表性的逐字引文 + [时间戳] + 期名**，供后续溯源。
- 保留证据层标签（〔转写自证〕/〔他的主张〕/〔外部核实〕）。
- 末尾一行汇总本批的 hype / hedge / tradeoff 计数。
- 跟随卡片语言。

证据卡与指标（JSON）：
{digest}"""


def _rules():
    return _CALIBRATION_RULES.format(today=datetime.now().strftime('%Y-%m-%d'))


# ---- 输出语言：'auto' 跟随内容 / 'en' / 'zh'。注入到所有「产出文档」的 prompt ----
_LANG_LINES = {
    'auto': '**Output language: follow the language of the source content.**',
    'en': '**Output language: write the ENTIRE output in English '
          '(headings, labels, and body). Keep verbatim quotes in their original language.**',
    'zh': '**输出语言：全文用中文（标题、标签、正文）。逐字引文保留原语言。**',
}


def _lang_line(lang):
    return _LANG_LINES.get(lang or 'auto', _LANG_LINES['auto'])


def _call_gemini(prompt, grounded=False, model=None):
    """带重试的 Gemini 调用。grounded=True 开 Google 搜索。model 缺省用合成模型。"""
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

    # 模型降级链：主模型 → 兜底模型（去重保序）。某个模型限流/挂了就换下一个。
    ladder = []
    for m in [model or GEMINI_ANALYSIS_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in ladder:
            ladder.append(m)

    last_err = None
    for m in ladder:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.models.generate_content(
                    model=m, contents=prompt, config=cfg
                )
                text = (resp.text or '').strip()
                if text:
                    return text
                last_err = RuntimeError('Gemini 返回空文本')
            except Exception as e:  # noqa: BLE001
                last_err = e
                s = str(e)
                transient = any(k in s for k in (
                    '429', 'RESOURCE_EXHAUSTED', '503', 'UNAVAILABLE',
                    'overloaded', 'deadline', 'timeout'))
                if not transient:
                    break            # 模型名错/安全拦截等：别耗重试，直接换下一个模型
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(5 * attempt)
        # 该模型跑不通 → 降级到链条里的下一个
    raise RuntimeError(f'Gemini 调用失败（已试模型 {ladder}）: {last_err}')


def _call_openai_compat(prompt, model, base_url, api_key, extra_payload=None,
                        label='模型'):
    """OpenAI 兼容端点（阿里云百炼 / OpenRouter）。带重试，返回文本或抛异常。"""
    import requests
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
    if extra_payload:
        payload.update(extra_payload)
    last_err = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=600)
            if r.status_code == 200:
                txt = ((r.json().get('choices') or [{}])[0]
                       .get('message', {}).get('content') or '').strip()
                if txt:
                    return txt
                last_err = RuntimeError('空文本')
            else:
                last_err = RuntimeError(f'{r.status_code}: {r.text[:200]}')
                if r.status_code not in (429, 500, 502, 503, 504):
                    break               # 4xx（key 错/模型名错）别耗重试
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < _MAX_ATTEMPTS:
            time.sleep(5 * attempt)
    raise RuntimeError(f'{label}({model}) 调用失败: {last_err}')


def _llm(prompt, provider, model, grounded=False):
    """按 provider 分发：gemini 走 google-genai，aliyun 走百炼，openrouter 走 OpenRouter。"""
    if provider == 'aliyun':
        key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
        if not key:
            raise RuntimeError('DASHSCOPE_API_KEY 未设置（阿里云分析需要）')
        return _call_openai_compat(prompt, model, ALIYUN_COMPAT_BASE, key,
                                   label='阿里云')
    if provider == 'openrouter':
        key = os.environ.get('OPENROUTER_API_KEY', '')
        if not key:
            raise RuntimeError('OPENROUTER_API_KEY 未设置 — 在 Settings → OpenRouter 里填 key')
        # reasoning: OpenRouter 的统一思考开关；对 Claude 4.6+/5 映射为 adaptive thinking
        return _call_openai_compat(prompt, model, OPENROUTER_COMPAT_BASE, key,
                                   extra_payload={'reasoning': {'enabled': True}},
                                   label='OpenRouter')
    return _call_gemini(prompt, grounded=grounded, model=model)


def _parse_json_obj(raw):
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


def _extract_cards(title, transcript_text, author, provider, model):
    """逐期抽取证据卡。返回 {cards, metrics, asr_suspects} 或 None。"""
    prompt = CARDS_PROMPT.format(
        author=author, title=title, transcript=transcript_text,
        today=datetime.now().strftime('%Y-%m-%d'),
    )
    try:
        data = _parse_json_obj(_llm(prompt, provider, model))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault('cards', [])
    data.setdefault('metrics', {})
    data.setdefault('asr_suspects', [])
    return data


def _cards_markdown(title, data):
    """把证据卡渲染成可读的逐期文档（可回溯，无评判）。"""
    lines = [f'# {title}', '', '## 证据卡']
    for c in data.get('cards', []):
        ts = f" [{c['timestamp']}]" if c.get('timestamp') else ''
        q = c.get('quote', '')
        lines.append(f"- 〔{c.get('layer', '?')}〕{c.get('obs', '')}"
                     + (f"  ——「{q}」{ts}" if q else ''))
    m = data.get('metrics', {}) or {}
    lines += ['', '## 修辞指标']
    h, hd, td = m.get('hype', {}), m.get('hedge', {}), m.get('tradeoff', {})
    lines.append(f"- Hype（最高级/军备竞赛词）：{h.get('count', 0)} 次")
    lines.append(f"- Hedge（待验证/存疑类）：{hd.get('count', 0)} 次")
    lines.append(f"- 提代价/权衡：介绍 {td.get('tech_count', 0)} 个技术中 {td.get('with_tradeoff', 0)} 个提了")
    asr = data.get('asr_suspects', []) or []
    if asr:
        lines += ['', '## 转写质量（疑似识别错误，非风格）', '- ' + '、'.join(asr)]
    return '\n'.join(lines)


def _external_claims(data):
    return [c.get('obs') or c.get('quote') for c in data.get('cards', [])
            if c.get('layer') == '他的主张' and (c.get('obs') or c.get('quote'))]


def analyze_episode(title, transcript_text, author='该博主', verify=False, preset=None):
    """逐期 → {title, cards, metrics, asr_suspects, markdown}。verify 时附核实脚注。"""
    provider, extract_model, _ = resolve_analysis(preset)
    data = _extract_cards(title, transcript_text, author, provider, extract_model)
    failed = not isinstance(data, dict)          # 抽取失败要留痕，别洗成"成功但空卡"
    if failed:
        data = {'cards': [], 'metrics': {}, 'asr_suspects': []}
    # 卡片必须是 dict：模型偶发返回字符串数组会让下游 .get 崩
    data['cards'] = [c for c in (data.get('cards') or []) if isinstance(c, dict)]
    md = _cards_markdown(title, data)

    if verify:
        claims = _external_claims(data)
        if claims:
            try:
                footnote = _call_gemini(
                    VERIFY_PROMPT.format(
                        claims='\n'.join(f'- {c}' for c in claims[:40])),
                    grounded=True,
                )
                md = md + '\n\n' + footnote
            except Exception:  # noqa: BLE001
                pass

    return {
        'title': title,
        'cards': data.get('cards', []),
        'metrics': data.get('metrics', {}),
        'asr_suspects': data.get('asr_suspects', []),
        'markdown': md,
        'extract_failed': failed,
    }


def _digest(episodes, budget):
    """把各期卡片压成合并层的输入（不含全文）。超预算就每期少留几张卡。"""
    keep = 100
    while keep > 3:
        items = []
        for ep in episodes:
            items.append({
                'title': ep.get('title', ''),
                'cards': (ep.get('cards') or [])[:keep],
                'metrics': ep.get('metrics', {}),
                'asr_suspects': ep.get('asr_suspects', []),
            })
        s = json.dumps(items, ensure_ascii=False, indent=1)
        if len(s) <= budget:
            return s
        keep = keep // 2
    return s  # 尽力而为


def _agg_metrics(episodes):
    """跨期把 hype/hedge/tradeoff 加成**真实总数**（纯 Python，不靠模型估）。"""
    hype = hedge = tech = trade = 0
    for ep in episodes:
        m = ep.get('metrics') or {}
        hype += (m.get('hype') or {}).get('count', 0) or 0
        hedge += (m.get('hedge') or {}).get('count', 0) or 0
        td = m.get('tradeoff') or {}
        tech += td.get('tech_count', 0) or 0
        trade += td.get('with_tradeoff', 0) or 0
    return {'hype': hype, 'hedge': hedge, 'tech': tech, 'tradeoff': trade}


def _metrics_block(agg, n):
    return (f"【跨 {n} 期真实计数 · Python 统计非模型估算】"
            f"Hype {agg['hype']} 次 · Hedge {agg['hedge']} 次 · "
            f"介绍技术 {agg['tech']} 个其中提代价 {agg['tradeoff']} 个。"
            f"（修辞结论以此为准，别自己重估）")


def _batch_brief(batch, author, provider, model):
    """map 叶子：一批期的卡片 → 一份中间简报（失败返回 None）。"""
    try:
        return _llm(BRIEF_PROMPT.format(
            author=author, n=len(batch),
            digest=_digest(batch, _SYNTH_CHAR_LIMIT),
        ), provider, model)
    except Exception:  # noqa: BLE001
        return None


def _verify_portrait(portrait, digest, author, provider, extract_model, synth_model):
    """证伪层：抽论断 → 逐条 skeptic 反驳（并发、复用画像那步的 digest）→ 撑不住就砍。

    任何一步失败/无可检验论断/全部成立 → 原样返回，绝不把画像搞没。
    """
    obj = agent(lambda p: _llm(p, provider, extract_model),
                CLAIMS_PROMPT.format(author=author, portrait=portrait),
                schema=['claims'])
    claims = [c for c in ((obj or {}).get('claims') or []) if isinstance(c, str) and c.strip()]
    if not claims:
        return portrait

    # 成本闸：每条论断都重发整个 digest，25 条 × 60 万字符是 15 倍放大。
    # 给 skeptic 一份截断的证据（够判断即可，不需全量），砍掉绝大部分重复开销。
    verify_cards = digest[:_VERIFY_DIGEST_CHARS]

    def _skeptic(claim):
        return agent(lambda p: _llm(p, provider, extract_model),
                     SKEPTIC_PROMPT.format(author=author, claim=claim, cards=verify_cards),
                     schema=['verdict'])

    verdicts = fanout(claims, _skeptic, concurrency=_BRIEF_CONCURRENCY)
    bad = [{'claim': c, 'verdict': v.get('verdict'), 'why': v.get('why', '')}
           for c, v in zip(claims, verdicts)
           if v and v.get('verdict') in ('夸大', '不成立')]
    if not bad:
        return portrait  # 全成立，不动

    # REVISE 是裸调用：失败也绝不能把已经算好（已花钱）的画像丢掉——原样返回
    try:
        revised = _llm(REVISE_PROMPT.format(
            portrait=portrait,
            verdicts=json.dumps(bad, ensure_ascii=False, indent=1),
        ), provider, synth_model)
    except Exception:  # noqa: BLE001
        return portrait
    return revised or portrait


# 各家上下文窗口不同：Gemini ~1M 能吃大 digest；阿里云 DeepSeek/Qwen 多为 128k，
# 硬塞 600k 字符必然超窗返 400。按 provider 给预算。
def _digest_budget(provider):
    return _SYNTH_CHAR_LIMIT if provider == 'gemini' else 180_000


def _build_digest(episodes, author, provider, extract_model):
    """把 N 期证据卡压成合成层的输入：少量期直接进卡片；多期走 map-reduce
    分批简报，避免几百期硬塞一个 prompt 被砍。合成和各镜头共用。"""
    budget = _digest_budget(provider)
    if len(episodes) <= _BATCH_SIZE:
        return _digest(episodes, budget)
    batches = [episodes[i:i + _BATCH_SIZE]
               for i in range(0, len(episodes), _BATCH_SIZE)]
    briefs = fanout(
        batches,
        lambda b: _batch_brief(b, author, provider, extract_model),
        concurrency=_BRIEF_CONCURRENCY,
    )
    briefs = [b for b in briefs if b]
    if briefs:
        return '\n\n---\n\n'.join(
            f'## 简报 {i + 1}/{len(briefs)}\n{b}' for i, b in enumerate(briefs))[:budget]
    return _digest(episodes, budget)  # 全批失败兜底


def synthesize(episodes, author='该博主', critique_level='analytical',
               impression_bias='', preset=None, self_verify=False, lang='auto'):
    """N 期证据卡 → 一份人物画像。critique_level: descriptive/analytical/sharp。

    self_verify=True：合成后再跑一轮证伪——抽出每条论断、逐条 skeptic 拿证据反驳，
    证据撑不住的删、夸大的改软，末尾留痕。

    impression_bias：仅供校准回归测试用——注入一条语气基线看结论会不会跟着漂。
    """
    episodes = [e for e in episodes if e and e.get('cards') is not None]
    if not episodes:
        raise RuntimeError('没有可综合的证据卡')
    # 抽取失败的期不能当"成功但沉默"喂进合成，否则模型会拿全零指标凭空编画像
    failed_n = sum(1 for e in episodes if e.get('extract_failed'))
    if failed_n >= max(1, len(episodes) * 0.5):
        raise RuntimeError(
            f'证据卡抽取失败过半（{failed_n}/{len(episodes)} 期），画像不可信，先查 API key / 限流')
    if sum(len(e.get('cards') or []) for e in episodes) == 0:
        raise RuntimeError('所有期都没抽到证据卡，无法合成画像')
    level = critique_level if critique_level in _TONE else 'analytical'
    impression = f'（综合印象的语气基线：{impression_bias}）' if impression_bias else ''
    provider, extract_model, synth_model = resolve_analysis(preset)
    agg = _agg_metrics(episodes)
    digest = _build_digest(episodes, author, provider, extract_model)

    portrait = _llm(PORTRAIT_PROMPT.format(
        author=author, n=len(episodes), rules=_rules(), lang_line=_lang_line(lang),
        level=level, tone=_TONE[level], impression=impression,
        digest=_metrics_block(agg, len(episodes)) + '\n\n' + digest,
    ), provider, synth_model)

    if self_verify:
        portrait = _verify_portrait(
            portrait, digest, author, provider, extract_model, synth_model)
    return portrait


# ==== 镜头（lenses）：同一批证据卡 + 不同的合成 prompt，边际成本≈0 ====
_LENS_GROUND = """铁律（违反就是编造真人 = 造谣，不是解读）：
- 每条判断/吐槽/结论**必须挂得回某张卡的原话**（给〔证据层〕和「原话」），点不回引文的**一律不许写**。
- 他的主张挂〔他的主张〕，别洗成客观事实；跨语境不一致如实说"不一致"，别升级成诛心。
- 只评他的【内容/主张/修辞/自相矛盾】，不捏造私德、隐私、人身。"""

LENSES = {
    'roast': """你是吐槽大会的毒舌选手。下面是「{author}」{n} 期视频的证据卡。写一段辛辣的 roast，像脱口秀 roast 那样损、刻薄、可以粗俗——**但最狠的弹药永远是"他自己打自己脸"**（跨期自相矛盾、又当又立）。

""" + _LENS_GROUND + """

结构：
## 罪状清单
（每条：一句损的话 —— 挂〔证据层〕「他的原话」，矛盾就并列两句原话）
## 总结陈词
（一段，收个狠的）

证据卡（JSON）：
{digest}""",

    'craft': """你是内容创作教练。下面是「{author}」{n} 期视频的证据卡。拆解**他是怎么做内容/写稿子的**，给想偷师、想模仿他的人一份可操作的说明书。

""" + _LENS_GROUND + """

结构（每条都挂原话举例）：
## 开头怎么钩人
## 常用结构 / 套路
## 修辞与话术手法（用 hype/hedge/tradeoff 分布佐证）
## 节奏与信息密度
## 可复制的招 vs 学不来的
证据卡（JSON）：
{digest}""",

    'fun': """你是选题/追更判断官。下面是「{author}」{n} 期视频的证据卡。回答一个问题：**这个人有意思吗？看点在哪？**

""" + _LENS_GROUND + """

结构：
## 看点在哪（幽默/反转/信息量/人设魅力，挂原话）
## 什么样的人会爱看 / 会划走
## 最出彩 vs 最无聊的部分
## 一句话结论：值不值得追
证据卡（JSON）：
{digest}""",

    'quotes': """下面是「{author}」{n} 期视频的证据卡。挑出他**最有代表性 / 最出圈 / 最能体现其风格**的原话，做一份金句集。

铁律：**只用卡片里的逐字原话**，不许改写、不许编。每条标出处集名（若有）。

结构：
## 金句集
- 按主题归类，每条：「逐字原话」—— 一句话点评它为什么有代表性
证据卡（JSON）：
{digest}""",

    'worldview': """下面是「{author}」{n} 期视频的证据卡。把他对各类事物的立场整理成一张**世界观地图**。

""" + _LENS_GROUND + """

结构：
## 世界观地图
| 议题 | 他的立场 | 证据〔层〕「原话」 |
（每行一个议题；跨期不一致的，在立场里如实写"不一致：A / B"并各挂原话）
## 底层母题
（这些立场背后反复出现的 1~3 个底层假设）
证据卡（JSON）：
{digest}""",
}

LENS_META = {
    'roast': ('🔥 锐评 / 吐槽', '拿证据损他，最狠的是他自己打脸的地方'),
    'craft': ('✍️ 写作 / 内容拆解', '他怎么做内容，给想偷师的人'),
    'fun': ('😂 看点 / 有意思吗', '值不值得追'),
    'quotes': ('💬 金句集', '他最有代表性的原话'),
    'worldview': ('🗺 世界观地图', '他对各类事的立场一张表'),
}


def render_lens(episodes, lens, author='该博主', preset=None, lang='auto'):
    """同一批证据卡 → 指定镜头的报告（Markdown）。复用合成层的证据卡逻辑。"""
    episodes = [e for e in episodes if e and e.get('cards') is not None]
    if not episodes:
        raise RuntimeError('没有可用的证据卡')
    tpl = LENSES.get(lens)
    if not tpl:
        raise ValueError(f'未知镜头：{lens}')
    provider, extract_model, synth_model = resolve_analysis(preset)
    agg = _agg_metrics(episodes)
    digest = _build_digest(episodes, author, provider, extract_model)
    return _llm(_lang_line(lang) + '\n\n' + tpl.format(
        author=author, n=len(episodes),
        digest=_metrics_block(agg, len(episodes)) + '\n\n' + digest,
    ), provider, synth_model)


# ==== 小红书笔记分析（多模态：读图 + 评论 → 逐篇结构化 → 聚合报告）====
import glob as _glob
import csv as _csv

_XHS_IMG_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                 '.webp': 'image/webp', '.heic': 'image/heic'}


def _call_gemini_mm(prompt, image_paths, model=None):
    """多模态 Gemini：文字 + 图片一起送。带模型降级链、重试。返回文本。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY 未设置')
    from google.genai import types
    client = make_gemini_client(api_key)
    parts = [prompt]
    for p in (image_paths or [])[:12]:          # 每篇最多喂 12 张，控 token
        try:
            ext = os.path.splitext(p)[1].lower()
            with open(p, 'rb') as f:
                parts.append(types.Part.from_bytes(
                    data=f.read(), mime_type=_XHS_IMG_MIME.get(ext, 'image/png')))
        except Exception:  # noqa: BLE001
            pass
    ladder = []
    for m in [model or GEMINI_EXTRACT_MODEL, *GEMINI_FALLBACK_MODELS]:
        if m and m not in ladder:
            ladder.append(m)
    last_err = None
    for m in ladder:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = client.models.generate_content(model=m, contents=parts)
                text = (resp.text or '').strip()
                if text:
                    return text
                last_err = last_err or RuntimeError('空响应（可能被内容过滤或读图失败）')
            except Exception as e:  # noqa: BLE001
                last_err = e
            if attempt < _MAX_ATTEMPTS:      # 空响应也退避重试，别零间隔连打
                time.sleep(2 * attempt)
    raise RuntimeError(f'Gemini 多模态调用失败：{last_err}')


XHS_EXTRACT_PROMPT = """这是一篇小红书笔记：图片 + 一级评论 + 元信息。**图片里往往是正文/截图/信息主体，务必仔细读图**。

只输出 JSON：
{{
  "summary": "这篇讲了什么，一两句",
  "topic": "它属于什么话题/子类",
  "key_points": ["正文（含图片内容）里的关键信息点，逐条"],
  "author_stance": "发帖人的立场/态度（读不出留空）",
  "notable_comments": ["评论区最有信息量/代表性的几条原话"],
  "entities": ["提到的具体实体：学校/公司/人名/产品/地名等"],
  "sentiment": "评论区整体情绪：正面/负面/混合/中性",
  "relevant": true
}}
铁律：只写图片/评论里**真有**的，读不出来就留空/空数组，别编。跟随内容语言。

元信息：{meta}

评论（前 {ncmt} 条）：
{comments}"""


def analyze_xhs_note(note_dir):
    """一篇笔记 → 多模态结构化抽取（读图+评论）。失败返回 None。"""
    meta = {}
    try:
        with open(os.path.join(note_dir, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    comments = []
    cpath = os.path.join(note_dir, 'comments.csv')
    if os.path.isfile(cpath):
        try:
            with open(cpath, encoding='utf-8-sig') as f:   # -sig 去掉表头 BOM
                for row in _csv.DictReader(f):
                    # 列名是 comment_text（老数据可能是 text）
                    t = (row.get('comment_text') or row.get('text') or '').strip()
                    if not t:
                        continue
                    lk = (row.get('like_count') or '').strip()
                    comments.append((int(lk) if lk.isdigit() else 0, t))
            comments.sort(key=lambda x: -x[0])             # 高赞评论排前，更有代表性
            comments = [f'(赞{lk}) {t}' if lk else t for lk, t in comments]
        except Exception:  # noqa: BLE001
            pass
    imgs = sorted(sum((_glob.glob(os.path.join(note_dir, f'*{e}'))
                       for e in _XHS_IMG_MIME), []))
    prompt = XHS_EXTRACT_PROMPT.format(
        meta=json.dumps(meta, ensure_ascii=False),
        ncmt=min(60, len(comments)),
        comments='\n'.join('- ' + c for c in comments[:60])[:8000])
    try:
        data = _parse_json_obj(_call_gemini_mm(prompt, imgs))
    except Exception:  # noqa: BLE001
        data = None
    if not isinstance(data, dict):
        return None
    data['_note_id'] = os.path.basename(note_dir.rstrip('/'))
    data['_title'] = meta.get('title', '')
    data['_n_images'] = len(imgs)
    data['_n_comments'] = len(comments)
    return data


XHS_REPORT_PROMPT = """你收到从小红书采集的 {n} 篇笔记的结构化抽取（每篇：话题/要点/立场/代表评论/实体/情绪）。这些笔记来自关键词搜索、围绕某个话题。写一份调研报告（Markdown）。
{lang_line}

要求：
- 先判断这批在聊什么主话题（可能不止一个，按簇分）。
- 按**反复出现的主题/模式**组织，别逐篇复述。
- 每个结论尽量挂**具体例子**（哪篇的要点 / 哪条评论原话），别空泛。
- 有可数的就给数字（多少篇提到 X、情绪分布）。
- 只写抽取里**真有**的，不编、不脑补。

结构（按内容灵活取舍）：
# {title}
## 概览（这批在聊什么、样本规模）
## 主要主题 / 模式
## 值得注意的案例 / 金句
## 情绪与分歧
## 小结

抽取结果（JSON）：
{digest}"""


def xhs_report(note_dirs, title='小红书调研报告', on_progress=None, lang='auto'):
    """一批笔记目录 → (报告 Markdown, 逐篇抽取列表)。逐篇多模态抽取（并发）→ 聚合。"""
    done = [0]

    def _one(d):
        r = analyze_xhs_note(d)
        done[0] += 1
        if on_progress:
            on_progress(done[0], len(note_dirs))
        return r

    extractions = [e for e in fanout(note_dirs, _one, concurrency=_BRIEF_CONCURRENCY) if e]
    if not extractions:
        raise RuntimeError('没有可分析的笔记（抽取全失败）')
    digest = json.dumps(extractions, ensure_ascii=False, indent=1)[:_SYNTH_CHAR_LIMIT]
    report = _call_gemini(XHS_REPORT_PROMPT.format(
        n=len(extractions), title=title, digest=digest,
        lang_line=_lang_line(lang)))
    return report, extractions
```

---

## `sanitize.py`

> 转写证伪层：反幻听、复读死循环折叠、时间轴修复

```python
"""
转写「证伪层」——不相信任何一段文字，除非它站得住。

ASR（Whisper / Gemini）在静音、杂乱、低置信处有三种通病：
  1. 静音幻听：对着没人说话的时段编出成片的「嗯。嗯。嗯。/ 然後。我。我。」
  2. 复读死循环：把同一句话逐字反复吐十几遍
  3. 坏时间戳：混进 [0m3s30m433ms] 之类的畸形 token

本模块在转写「之后」做后处理，把这些清掉。核心原则：
  **只有多个信号都指向「假」才删；单一信号存疑就保留。**
  绝不删除有独特实义的内容——哪怕它很短（「OK。」「对。」照样留）。

可选：传入音频的静音时间轴（detect_silence），落在静音里的可疑短段
才删——这是最硬的「证伪」证据（你说的「那 4-6 分钟大概率是空的」），
避免仅凭文字误杀真人的低声细语。

输入 / 输出都是 [{'timestamp': 'HH:MM:SS', 'text': str}, ...]（Gemini 系稿）。
"""

import os
import re
import shutil
import subprocess
from collections import Counter

# —— 阈值（保守）——
_MICRO_MAX = 4        # 「核心字数 <= 4」算微段（嗯/然後/我/对…）
_SHORT_MAX = 12       # 「核心字数 <= 12」算短句（含 `我再拍一次` 这种 5~11 字的循环词）
_FILLER_RUN_MIN = 6   # 连续微段成片 >= 6 且只在少数几个词里打转 → 整段丢
_DUP_COLLAPSE_MIN = 2 # 连续「完全相同的短核心」>= 2 → 只留 1 条
_LOOP_MIN_CORE = 12   # 长句（核心 >= 12 字）
_LOOP_MIN_COUNT = 3   # 逐字重复 >= 3 次 → 只留首次
# 「短句循环」判定：一段连续短句里，不同核心种类占比极低 = 死循环打转
_LOOP_RUN_MIN = 8         # 连续短句成片至少这么长才考虑
_LOOP_DIVERSITY = 0.35    # 不同核心数 / 段数 <= 此值 → 判为循环
_LOOP_DROP_ALL = 5        # 循环片里，某短句重复 >= 此次数 → 全删（含首条）

# 常见语气词/口水词（仅在「成片重复」时才据此判假，单独出现一律保留）
_FILLER_CHARS = set('嗯呃啊哦唔呐呗哈嘛呀么呢哎诶嗨欸')

_TS_RE = re.compile(r'^\d{1,2}:\d{2}(:\d{2})?$')
# 文字里漏出来的畸形时间戳残片，如 [0m3s30m433ms]、[00:1a]
_BROKEN_TS_IN_TEXT = re.compile(r'\[\s*\d[0-9a-zA-Z:.\s]*m?s?\]')


def _core(text):
    """去掉标点和空白，留下判断用的核心串。"""
    return re.sub(r'[\s\W_]+', '', text or '', flags=re.UNICODE)


def _is_micro(seg):
    return len(_core(seg['text'])) <= _MICRO_MAX


def _looks_filler(core):
    """核心串是否全由语气词（可含少量『然後/我/好/对』这类打转词）构成。"""
    if not core:
        return True
    return all(ch in _FILLER_CHARS for ch in core)


# 段内单字死循环：同一字符连打 >= 6 次（Gemini 解码卡死，如「他」×2199）。
# 排除数字：否则「1000000」(一百万) 会被折成「100」、手机号被折断——那是损毁真实内容。
_CHAR_LOOP = re.compile(r'([^\d])\1{5,}')
# 段内短语死循环：同一 2~8 字短语连续重复 >= 4 次（「我再拍一次我再拍一次…」在一段里）
_PHRASE_LOOP = re.compile(r'(.{2,8}?)\1{3,}')


def _fold_phrase(m):
    g = m.group(1)
    if g.isdigit():           # 纯数字串（金额/号码/序列）不折叠，保住真实内容
        return m.group(0)
    return g * 2


def _clean_text(text):
    """剥掉畸形时间戳残片 + 折叠段内死循环（单字连打 / 短语复读）；首尾清一下。"""
    text = _BROKEN_TS_IN_TEXT.sub('', text or '')
    text = _CHAR_LOOP.sub(lambda m: m.group(1) * 2, text)      # 6+ 连打 → 留 2（数字除外）
    text = _PHRASE_LOOP.sub(_fold_phrase, text)               # 短语复读 → 留 2（纯数字除外）
    return text.strip()


def _ts_to_seconds(ts):
    parts = (ts or '').split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def _in_silence(sec, silence_intervals):
    if sec is None or not silence_intervals:
        return False
    for a, b in silence_intervals:
        if a <= sec <= b:
            return True
    return False


def _fmt_ts(v):
    v = max(0, int(round(v)))
    return f"{v // 3600:02d}:{v % 3600 // 60:02d}:{v % 60:02d}"


def repair_timeline(segments, duration):
    """只修「无法解析 / 越界」的时间戳（负数、> 音频时长，如飙到 21h 的）。

    ⚠ 只碰确凿非法的。**绝不因为「比前一段小」就判坏**——文件里段的排列
    顺序不一定等于时间顺序，那样会把正确的时间戳误杀（曾把 02:06 的英文
    段一路夹到 02:50）。非法的用左右最近「合法锚点」按位置插值填回。
    返回 (segments_new, n_repaired)。时间戳都合法时是 no-op。
    """
    if not duration or duration <= 0 or not segments:
        return segments, 0
    lim = duration + 60  # 容一点边界误差
    raw = [_ts_to_seconds(s.get('timestamp', '')) for s in segments]
    n = len(segments)
    good = [v is not None and 0 <= v <= lim for v in raw]  # 只看合法性，不看单调
    gi = [i for i in range(n) if good[i]]
    if len(gi) == n or not gi:
        return segments, 0  # 都合法（或全非法无从插值）→ 不动
    out = []
    for i, s in enumerate(segments):
        if good[i]:
            out.append(s)
            continue
        L = max((j for j in gi if j < i), default=None)
        R = min((j for j in gi if j > i), default=None)
        if L is not None and R is not None and raw[R] >= raw[L]:
            v = raw[L] + (raw[R] - raw[L]) * (i - L) / (R - L)
        elif L is not None:
            v = raw[L]
        elif R is not None:
            v = raw[R]
        else:
            v = 0
        out.append({**s, 'timestamp': _fmt_ts(v)})
    return out, n - len(gi)


def clean_transcript(segments, silence_intervals=None, duration=None):
    """清洗一份转写稿，返回 (clean_segments, report)。

    report = {'removed': int, 'filler_runs': int, 'loops': int,
              'bad_ts': int, 'silence_dropped': int, 'ts_repaired': int}
    duration（音频秒数）给了就顺带重建单调时间轴，修 Precise 的坏时间戳。
    绝不原地改传入的 list。
    """
    report = {'removed': 0, 'filler_runs': 0, 'loops': 0,
              'bad_ts': 0, 'silence_dropped': 0, 'ts_repaired': 0,
              'intra_loops': 0}

    # —— 0) 逐段清文字（含段内死循环折叠）+ 修时间戳 ——
    segs = []
    for s in segments:
        raw = s.get('text', '')
        text = _clean_text(raw)
        if len(raw) - len(text) >= 40:  # 一段里折掉了一大片 → 记一次段内死循环
            report['intra_loops'] += 1
        if not text:
            report['removed'] += 1
            continue
        ts = s.get('timestamp', '')
        if not _TS_RE.match(ts or ''):
            report['bad_ts'] += 1
            # 时间戳坏了不丢内容：继承上一条的时间戳
            ts = segs[-1]['timestamp'] if segs else '00:00:00'
        segs.append({'timestamp': ts, 'text': text})

    n = len(segs)
    drop = [False] * n

    # —— 1) 静音掩码：落在静音里的「微段」大概率是幻听 ——
    if silence_intervals:
        for i, s in enumerate(segs):
            if _is_micro(s) and _in_silence(_ts_to_seconds(s['timestamp']), silence_intervals):
                drop[i] = True
                report['silence_dropped'] += 1

    # —— 2) 成片幻听 / 短句死循环：在「够长的连续短句片」里逐段判 ——
    #    片内多样性极低（翻来覆去就那几个短词）才动手：
    #      · 纯语气词（嗯/啊…）             → 丢
    #      · 高频重复（>= _LOOP_DROP_ALL 次）→ 全丢（含首条，`我再拍一次`×几百就是它）
    #      · 中频重复（2~4 次）             → 只留首条
    #    片内唯一出现的实义短句（如「好，青和」「那要評評」）一律保留。
    i = 0
    while i < n:
        if len(_core(segs[i]['text'])) > _SHORT_MAX:
            i += 1
            continue
        j = i
        while j < n and len(_core(segs[j]['text'])) <= _SHORT_MAX:
            j += 1
        run = range(i, j)
        run_len = j - i
        cnt = Counter(_core(segs[k]['text']) for k in run)
        is_loop = (run_len >= _LOOP_RUN_MIN
                   and len(cnt) / run_len <= _LOOP_DIVERSITY)
        # 旧规则兜底：短小的纯微段片（长度 6~7、多样性没那么低）
        is_micro_run = (run_len >= _FILLER_RUN_MIN
                        and all(len(_core(segs[k]['text'])) <= _MICRO_MAX for k in run))
        if is_loop or is_micro_run:
            seen = set()
            fired = False
            for k in run:
                c = _core(segs[k]['text'])
                drop_it = False
                if _looks_filler(c):
                    drop_it = True
                elif cnt[c] >= _LOOP_DROP_ALL:
                    drop_it = True
                elif cnt[c] >= 2:
                    drop_it = c in seen
                    seen.add(c)
                if drop_it and not drop[k]:
                    drop[k] = True
                    report['removed'] += 1
                    fired = True
            if fired:
                report['filler_runs'] += 1
        i = j

    # —— 3) 连续相同短核心折叠：留 1 条 ——
    run_start = 0
    for i in range(1, n + 1):
        same = (i < n and not drop[i] and not drop[run_start]
                and _core(segs[i]['text']) == _core(segs[run_start]['text'])
                and len(_core(segs[i]['text'])) <= _MICRO_MAX)
        if not same:
            if i - run_start >= _DUP_COLLAPSE_MIN:
                for k in range(run_start + 1, i):
                    if not drop[k]:
                        drop[k] = True
                        report['removed'] += 1
            run_start = i

    # —— 4) 长句复读去重：同一长核心出现 >= N 次 → 只留首次 ——
    seen = {}
    for i, s in enumerate(segs):
        if drop[i]:
            continue
        c = _core(s['text'])
        if len(c) >= _LOOP_MIN_CORE:
            seen.setdefault(c, []).append(i)
    for c, idxs in seen.items():
        if len(idxs) >= _LOOP_MIN_COUNT:
            for i in idxs[1:]:
                if not drop[i]:
                    drop[i] = True
                    report['removed'] += 1
            report['loops'] += 1

    clean = [segs[i] for i in range(n) if not drop[i]]

    # —— 5) 重建单调时间轴（修 Precise 合并的坏时间戳）——
    if duration:
        clean, report['ts_repaired'] = repair_timeline(clean, duration)

    return clean, report


def detect_silence(filepath, noise_db=-35, min_silence=2.0):
    """用 ffmpeg silencedetect 返回静音区间 [(start_sec, end_sec), ...]。

    best-effort：ffmpeg 缺失或出错就返回 []（退化为纯文字证伪）。
    noise_db 以下、持续 min_silence 秒以上判为静音。
    """
    ffmpeg = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
    if not os.path.exists(ffmpeg) or not os.path.isfile(filepath):
        return []
    try:
        proc = subprocess.run(
            [ffmpeg, '-nostats', '-i', filepath,
             '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=600,
        )
    except Exception:
        return []
    intervals = []
    start = None
    for line in (proc.stderr or '').splitlines():
        m = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        if m:
            start = float(m.group(1))
            continue
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m and start is not None:
            intervals.append((max(0.0, start), float(m.group(1))))
            start = None
    return intervals
```

---

## `summarize.py`

> AI 内容摘要

```python
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
    make_gemini_client,
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
5. 所有文字（overview / title / summary）用与转录文本相同的语言输出——转录是中文就用中文，是英文就用英文
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
        client = make_gemini_client(api_key)
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
```

---

## `enrich.py`

> AI 卡片元数据（标题/一句话/标签）

```python
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
  "tags": ["2到4个简短标签，如：访谈、课堂、播客、情感短剧、时政评论、英语、会议"]
}}

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
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return True
```

---

## `audioutil.py`

> 转写后音频 Opus 压缩归档

```python
"""转写完成后把存档音频压成 Opus 24k 单声道，省磁盘。

音频此时只剩“回放对照”用途，语音在 24k opus 单声道下依然清晰。
安全红线：只处理已经有 transcript.json 的任务；压不出更小就保留原文件。
"""

import json
import os
import shutil
import subprocess

# 目标码率可用环境变量覆盖；默认 24k（语音够清晰，体积最省）
OPUS_BITRATE = os.environ.get('AUDIO_OPUS_BITRATE', '24k')
_TARGET_EXT = '.ogg'          # opus 放 ogg 容器，浏览器 <audio> 直接能放


def _ffmpeg():
    if os.path.exists('/opt/homebrew/bin/ffmpeg'):
        return '/opt/homebrew/bin/ffmpeg'
    return shutil.which('ffmpeg') or 'ffmpeg'


def _find_audio(task_dir, meta):
    """定位当前音频文件：优先按 meta 的 audio_ext，兜底 glob audio.*。"""
    ext = meta.get('audio_ext') if meta else None
    if ext:
        p = os.path.join(task_dir, f'audio{ext}')
        if os.path.isfile(p):
            return p
    try:
        for name in os.listdir(task_dir):
            if name.startswith('audio.') and not name.endswith('.tmp'):
                return os.path.join(task_dir, name)
    except OSError:
        pass
    return None


def compress_task(task_dir):
    """把某任务的音频压成 opus。返回 (省下的字节数, 状态)。

    状态：ok / skip:no-transcript / skip:no-audio / skip:already /
          skip:not-smaller / error:...
    """
    if not os.path.isfile(os.path.join(task_dir, 'transcript.json')):
        return 0, 'skip:no-transcript'          # 没转写成功的音频绝不动

    meta_path = os.path.join(task_dir, 'meta.json')
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            meta = {}

    src = _find_audio(task_dir, meta)
    if not src:
        return 0, 'skip:no-audio'
    if src.endswith(_TARGET_EXT) or meta.get('audio_compressed'):
        return 0, 'skip:already'

    before = os.path.getsize(src)
    dst = os.path.join(task_dir, 'audio' + _TARGET_EXT)
    tmp = dst + '.tmp'
    try:
        subprocess.run(
            [_ffmpeg(), '-y', '-v', 'error', '-i', src,
             '-ac', '1', '-c:a', 'libopus', '-b:a', OPUS_BITRATE,
             '-f', 'ogg', tmp],          # 显式指定容器：tmp 后缀是 .tmp，ffmpeg 猜不出格式
            check=True, timeout=900,
        )
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return 0, f'error:{str(e)[:80]}'

    after = os.path.getsize(tmp)
    if after >= before:                          # 没压小（原本就低码率）→ 保留原文件
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0, 'skip:not-smaller'

    os.replace(tmp, dst)
    if os.path.abspath(src) != os.path.abspath(dst):
        try:
            os.remove(src)
        except OSError:
            pass

    meta['audio_ext'] = _TARGET_EXT
    meta['audio_compressed'] = True
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return max(0, before - after), 'ok'
```

---

## `transcribe_whisper.py`

> 本地引擎：mlx（Apple Silicon GPU）→ faster-whisper（CPU）→ openai-whisper

```python
"""
Whisper transcription engine (local).

后端优先级：
  1. mlx-whisper —— Apple Silicon 上跑 Metal GPU。实测 M4 Max large-v3：
     5.1 分钟音频 12.2 秒（25× 实时），比 CPU 的 faster-whisper 快约 9.5 倍。
  2. faster-whisper（CTranslate2 CPU，自带 VAD）—— 非 Apple Silicon 或 mlx 不可用时。
  3. openai-whisper —— 最后兜底。

Returns 与旧实现完全一致：list of {'start': float, 'end': float, 'text': str}。
"""

import os
import platform
import threading

from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# 型号 → mlx-community 上的 MLX 权重仓库
_MLX_REPOS = {
    'tiny': 'mlx-community/whisper-tiny-mlx',
    'base': 'mlx-community/whisper-base-mlx',
    'small': 'mlx-community/whisper-small-mlx',
    'medium': 'mlx-community/whisper-medium-mlx',
    'large-v2': 'mlx-community/whisper-large-v2-mlx',
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
    'large-v3-turbo': 'mlx-community/whisper-large-v3-turbo',
}


def _mlx_usable():
    """Apple Silicon + 装了 mlx_whisper + 没被显式关掉 → 用 GPU 后端。"""
    if os.environ.get('WHISPER_DISABLE_MLX') == '1':
        return False
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False

# 按型号缓存 (model, backend)：Settings 里换档（如 small→large-v3）保存即生效，
# 下一个任务用新型号，无需重启。
_models = {}
_model_lock = threading.Lock()

# mlx 的 Metal 命令编码器**不是线程安全的**：两个转写同时调 GPU 会触发
# AGXG16XFamilyCommandBuffer 断言 → SIGABRT，崩的是整个 Flask 进程（不只是这个任务）。
# 所以 GPU 路径全局串行。GPU 版 ~25× 实时，串行吞吐仍远高于 CPU 多路并行。
_mlx_gpu_lock = threading.Lock()


def _current_size():
    """运行时读 Whisper 型号（env 优先，Settings 保存写 env）。"""
    return (os.environ.get('WHISPER_MODEL_SIZE') or WHISPER_MODEL_SIZE).strip()


def _get_model():
    """Load Whisper model (lazy, per-size cache, thread-safe). 优先 faster-whisper。"""
    size = _current_size()
    if size not in _models:
        with _model_lock:
            if size not in _models:
                # 1) Apple Silicon：走 Metal GPU（快约 9.5 倍）。mlx 不持有模型对象，
                #    权重按 repo 名惰性加载并由 mlx_whisper 内部缓存，这里只存 repo 名。
                if _mlx_usable() and size in _MLX_REPOS:
                    _models[size] = (_MLX_REPOS[size], 'mlx')
                    return _models[size]
                try:
                    from faster_whisper import WhisperModel
                    from config import WHISPER_CPU_THREADS, ENGINE_CONCURRENCY

                    # CPU 上 int8 量化最快且精度损失可忽略
                    compute = 'int8' if WHISPER_DEVICE == 'cpu' else 'float16'
                    # num_workers=并发路数 → 一个模型实例并行处理多路；cpu_threads=每路线程数。
                    # 二者乘积贴近性能核数，避免多路互抢核导致整体变慢。
                    workers = max(1, ENGINE_CONCURRENCY.get('whisper', 1))
                    _models[size] = (WhisperModel(
                        size,
                        device=WHISPER_DEVICE,
                        compute_type=compute,
                        cpu_threads=WHISPER_CPU_THREADS,
                        num_workers=workers,
                    ), 'faster')
                except Exception:
                    # faster-whisper 不可用（未安装/模型下载失败等）→ 回退旧实现
                    import whisper

                    _models[size] = (whisper.load_model(
                        size, device=WHISPER_DEVICE
                    ), 'openai')
    return _models[size]


def get_model():
    """兼容旧调用方：只返回 model。"""
    return _get_model()[0]


def transcribe_audio(filepath, progress_callback=None):
    """
    Run Whisper transcription on an audio file.

    Args:
        filepath: Path to the audio file.
        progress_callback: Optional callable(percent: int) for progress updates.

    Returns:
        List of segment dicts with keys: start (float), end (float), text (str).
    """
    model, backend = _get_model()

    if backend == 'mlx':
        try:
            return _transcribe_mlx(model, filepath, progress_callback)
        except Exception:  # noqa: BLE001
            # GPU 路径出任何问题都别让任务失败：退回 CPU 的 faster-whisper 重跑
            _models.pop(_current_size(), None)
            os.environ['WHISPER_DISABLE_MLX'] = '1'
            model, backend = _get_model()
    if backend == 'faster':
        return _transcribe_faster(model, filepath, progress_callback)
    return _transcribe_openai(model, filepath, progress_callback)


def _transcribe_mlx(repo, filepath, progress_callback=None):
    """mlx-whisper（Apple Silicon GPU）路径。

    反幻听参数和 faster-whisper 那条对齐；**temperature 固定 0**——默认的温度回退
    会把可疑段最多重试 6 次，既慢 6 倍又正是「是是是是…」这类复读循环的来源
    （实测同一段音频：默认 79s 且出现死循环，固定 0 后 12.2s 且循环消失）。
    mlx 没有 VAD，静音段靠 no_speech_threshold + 上层 sanitize 的静音掩码兜。
    """
    import mlx_whisper

    if progress_callback:
        progress_callback(5)
    # 全局串行：并发调 Metal 会崩整个进程（见 _mlx_gpu_lock 注释）
    with _mlx_gpu_lock:
        r = mlx_whisper.transcribe(
            filepath,
            path_or_hf_repo=repo,
            language=WHISPER_LANGUAGE,          # None = 自动检测
            temperature=0.0,                    # 不做温度回退（见上）
            condition_on_previous_text=False,   # 每段独立解码 → 断掉自我喂养的复读
            compression_ratio_threshold=2.4,    # 成片重复判为幻听
            logprob_threshold=-1.0,             # 置信度过低丢弃
            no_speech_threshold=0.6,            # 判定静音就不出字
            word_timestamps=False,
        )
    results = []
    for seg in (r.get('segments') or []):
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        results.append({
            'start': float(seg.get('start') or 0.0),
            'end': float(seg.get('end') or 0.0),
            'text': text,
        })
    if progress_callback:
        progress_callback(99)
    return results


def _transcribe_faster(model, filepath, progress_callback=None):
    """faster-whisper 路径：流式产出 segment，按已处理时长报进度。"""
    segments_iter, info = model.transcribe(
        filepath,
        language=WHISPER_LANGUAGE,      # None = 自动检测
        vad_filter=True,                # 跳过静音，长音频显著提速、也少幻听
        # ↓ 反幻听 / 反复读（对着静音编「嗯嗯嗯」、卡进死循环复读整句的根因）：
        condition_on_previous_text=False,  # 每段独立解码，不被前文污染 → 断掉自我喂养的循环
        no_repeat_ngram_size=3,            # 禁止 3-gram 立刻重复 → 掐断复读
        compression_ratio_threshold=2.4,   # 压缩率过高（成片重复）判为幻听丢弃
        log_prob_threshold=-1.0,           # 置信度过低的段丢弃
        no_speech_threshold=0.6,           # 判定为静音就不出字
    )
    duration = getattr(info, 'duration', None) or 0

    results = []
    for seg in segments_iter:
        results.append({
            'start': seg.start,
            'end': seg.end,
            'text': seg.text,
        })
        if progress_callback and duration:
            progress_callback(min(99, int(seg.end / duration * 100)))
    return results


# ========== openai-whisper 回退路径（与旧实现一致） ==========

def _transcribe_openai(model, filepath, progress_callback=None):
    import whisper.transcribe as whisper_transcribe
    import tqdm as tqdm_module

    class ProgressTqdm(tqdm_module.tqdm):
        """Custom tqdm that intercepts update() calls to report progress."""

        def update(self, n=1):
            super().update(n)
            if progress_callback and self.total:
                pct = min(99, int(self.n / self.total * 100))
                progress_callback(pct)

    original_tqdm = whisper_transcribe.tqdm.tqdm
    whisper_transcribe.tqdm.tqdm = ProgressTqdm

    try:
        result = model.transcribe(
            filepath,
            language=WHISPER_LANGUAGE,
            verbose=False,
            word_timestamps=False,
            # 与 faster 路径一致的反幻听 / 反复读设置
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
    finally:
        whisper_transcribe.tqdm.tqdm = original_tqdm

    return result.get('segments', [])
```

---

## `transcribe_gemini.py`

> 云端引擎：Gemini（分块 + 重试 + 安全过滤诊断）

```python
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
```

---

## `transcribe_dashscope.py`

> 云端引擎：阿里云 Qwen-ASR

```python
"""
DashScope transcription engine (阿里云百炼).
默认走 Qwen-Audio-3.0 ASR（见 config.DASHSCOPE_ASR_MODEL）；paraformer-v2 是同接口
的上一代模型，改 env 即可退回。两者请求/返回结构一致，共用本模块。
"""

import json
import os
import time

import requests as http_requests

from config import DASHSCOPE_API_KEY, DASHSCOPE_ASR_MODEL

BASE_URL = 'https://dashscope.aliyuncs.com/api/v1'


def transcribe_audio(filepath, progress_callback=None, diarization=False,
                     speaker_count=None, model=None):
    """
    Transcribe audio using DashScope ASR (Qwen-Audio-3.0 by default) via REST API.

    Args:
        diarization: 开启说话人分离（声纹），返回的每段会带 'speaker' 字段。
        speaker_count: 已知说话人数量时传入作为提示，能显著改善聚类；
            不填（None）则让阿里云自动判断人数。
        model: 覆盖默认 ASR 模型（如 qwen-audio-3.0-asr-flash-filetrans）。
            两代模型的请求/返回结构一致，所以共用这一条链路。

    Returns:
        List of segment dicts with keys: timestamp (str), text (str),
        以及开启 diarization 时的 speaker。
    """
    api_key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            "DashScope API Key 未设置。请在 .env 文件中设置 DASHSCOPE_API_KEY。"
        )
    model = model or DASHSCOPE_ASR_MODEL

    if progress_callback:
        progress_callback(5)

    file_url = _upload_file(filepath, api_key, model)

    if progress_callback:
        progress_callback(15)

    task_id = _submit_task(file_url, api_key, diarization=diarization,
                           speaker_count=speaker_count, model=model)

    if progress_callback:
        progress_callback(20)

    result = _poll_task(task_id, api_key, progress_callback)

    if progress_callback:
        progress_callback(90)

    segments = _parse_result(result)

    if progress_callback:
        progress_callback(100)

    return segments


def _upload_file(filepath, api_key, model=None):
    """Upload a local file to DashScope temporary OSS and return oss:// URL."""
    filename = os.path.basename(filepath)

    policy_resp = http_requests.get(
        f'{BASE_URL}/uploads',
        headers={'Authorization': f'Bearer {api_key}'},
        params={'action': 'getPolicy', 'model': model or DASHSCOPE_ASR_MODEL},
        timeout=30,
    )
    policy_resp.raise_for_status()
    policy = policy_resp.json().get('data', {})

    upload_host = policy.get('upload_host')
    upload_dir = policy.get('upload_dir')
    if not upload_host or not upload_dir:
        raise RuntimeError("DashScope 文件上传凭证获取失败")

    oss_key = f"{upload_dir}/{filename}"

    with open(filepath, 'rb') as f:
        files = {
            'OSSAccessKeyId': (None, policy['oss_access_key_id']),
            'Signature': (None, policy['signature']),
            'policy': (None, policy['policy']),
            'x-oss-object-acl': (None, policy['x_oss_object_acl']),
            'x-oss-forbid-overwrite': (None, policy['x_oss_forbid_overwrite']),
            'key': (None, oss_key),
            'success_action_status': (None, '200'),
            'file': (filename, f),
        }
        upload_resp = http_requests.post(upload_host, files=files, timeout=300)
        if upload_resp.status_code not in (200, 204):
            raise RuntimeError(
                f"文件上传到 OSS 失败: HTTP {upload_resp.status_code}"
            )

    return f"oss://{oss_key}"


def _submit_task(file_url, api_key, diarization=False, speaker_count=None,
                 model=None):
    """Submit a transcription task via REST API and return task_id."""
    parameters = {
        'language_hints': ['zh', 'en'],
    }
    if diarization:
        # 声纹说话人分离（paraformer-v2 与 qwen-audio-3.0 同名参数）
        parameters['diarization_enabled'] = True
        # 传入已知人数作为提示，远场/多人场景能明显改善聚类；不填则自动判断
        if speaker_count and speaker_count > 0:
            parameters['speaker_count'] = int(speaker_count)

    resp = http_requests.post(
        f'{BASE_URL}/services/audio/asr/transcription',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'X-DashScope-Async': 'enable',
            'X-DashScope-OssResourceResolve': 'enable',
        },
        json={
            'model': model or DASHSCOPE_ASR_MODEL,
            'input': {
                'file_urls': [file_url],
            },
            'parameters': parameters,
        },
        timeout=30,
    )

    # 4xx 时阿里云会在响应体里给出真实的 code/message（如格式不支持、无音轨、时长超限），
    # 直接 raise_for_status() 会把这些吞掉只剩笼统的 "400 Bad Request"，这里手动带出来。
    if resp.status_code >= 400:
        code = message = ''
        try:
            err = resp.json()
            code = err.get('code', '')
            message = err.get('message', '')
        except ValueError:
            message = resp.text[:300]
        detail = ' '.join(p for p in (code, message) if p) or f'HTTP {resp.status_code}'
        raise RuntimeError(f"DashScope 任务提交被拒 (HTTP {resp.status_code}): {detail}")

    data = resp.json()

    task_id = data.get('output', {}).get('task_id')
    if not task_id:
        msg = data.get('message', json.dumps(data, ensure_ascii=False))
        raise RuntimeError(f"DashScope 转写任务提交失败: {msg}")

    return task_id


def _poll_task(task_id, api_key, progress_callback=None):
    """Poll transcription task via REST API until completion."""
    POLL_INTERVAL_SECONDS = 2
    # 1 小时上限，避免任务卡死时前端 / 后台线程永远挂着
    MAX_WAIT_SECONDS = 60 * 60
    TERMINAL_FAIL_STATUSES = {'FAILED', 'CANCELED', 'CANCELLED', 'UNKNOWN'}

    poll_count = 0
    waited_seconds = 0
    while True:
        try:
            resp = http_requests.get(
                f'{BASE_URL}/tasks/{task_id}',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'X-DashScope-OssResourceResolve': 'enable',
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except http_requests.RequestException as e:
            # 4xx（鉴权/参数/任务不存在）说明请求本身有问题，重试也不会好
            status_code = getattr(getattr(e, 'response', None), 'status_code', None)
            if status_code is not None and 400 <= status_code < 500:
                raise RuntimeError(
                    f"DashScope 查询任务失败 (HTTP {status_code})，请检查 API Key 是否正确"
                ) from e
            # 5xx / 网络抖动：等下一轮，靠 MAX_WAIT_SECONDS 兜底
            if waited_seconds >= MAX_WAIT_SECONDS:
                raise RuntimeError(
                    f"DashScope 查询任务状态持续失败（{MAX_WAIT_SECONDS // 60} 分钟），已超时放弃"
                ) from e
            time.sleep(POLL_INTERVAL_SECONDS)
            waited_seconds += POLL_INTERVAL_SECONDS
            continue

        status = data.get('output', {}).get('task_status', '')

        if status == 'SUCCEEDED':
            return data
        if status in TERMINAL_FAIL_STATUSES:
            msg = data.get('output', {}).get('message', '未知错误')
            raise RuntimeError(f"DashScope 转写任务失败（状态 {status}）: {msg}")

        poll_count += 1
        if progress_callback:
            pct = min(85, 20 + poll_count * 5)
            progress_callback(pct)

        if waited_seconds >= MAX_WAIT_SECONDS:
            raise RuntimeError(
                f"DashScope 转写任务超时（{MAX_WAIT_SECONDS // 60} 分钟内未完成），最后状态: {status or '未知'}"
            )

        time.sleep(POLL_INTERVAL_SECONDS)
        waited_seconds += POLL_INTERVAL_SECONDS


def _parse_result(data):
    """Parse DashScope transcription result into segment list."""
    segments = []

    results_list = data.get('output', {}).get('results', [])
    if not results_list:
        return segments

    first_result = results_list[0]
    if first_result.get('subtask_status') != 'SUCCEEDED':
        # 子任务失败别静默返空（会显示"成功但转写为空"）——抛出真实原因
        msg = first_result.get('message') or first_result.get('subtask_status') or '未知原因'
        raise RuntimeError(f'阿里云子任务失败：{msg}')

    transcription_url = first_result.get('transcription_url')
    if not transcription_url:
        return segments

    resp = http_requests.get(transcription_url, timeout=30)
    resp.raise_for_status()
    result_data = resp.json()

    transcripts = result_data.get('transcripts', [])
    for transcript in transcripts:
        sentences = transcript.get('sentences', [])
        for sent in sentences:
            # begin_time 键可能存在但值为 null → int(... or 0) 兜住，别让 None//1000 崩
            begin_ms = int(sent.get('begin_time') or 0)
            text = sent.get('text', '').strip()
            if not text:
                continue

            total_seconds = begin_ms // 1000
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            seconds = total_seconds % 60

            seg = {
                'timestamp': f"{hours:02d}:{minutes:02d}:{seconds:02d}",
                'text': text,
            }
            # 开启说话人分离时会带 speaker_id（通常是 0/1/2…）
            speaker_id = sent.get('speaker_id')
            if speaker_id is not None:
                seg['speaker'] = speaker_id
            segments.append(seg)

    if not segments and transcripts:
        full_text = transcripts[0].get('text', '').strip()
        if full_text:
            segments.append({'timestamp': '00:00:00', 'text': full_text})

    return segments
```

---

## `transcribe_precise.py`

> 精准模式：Gemini 文字 + 阿里云说话人分离，逐窗口合并校验

```python
"""
精准模式（说话人分离）合并逻辑。

思路：用每个引擎最擅长的部分，各取所长：
  - 阿里云 Qwen-ASR（diarization）负责"谁在说"——声纹分离，句级 speaker + 时间戳；
  - Gemini 负责"说了什么"——高质量文字；
  - 最后再让 Gemini 把两份稿按时间轴对齐合并，输出带说话人的成稿。

对外只暴露：
  - merge_speaker_transcript(gemini_segments, dashscope_segments) -> (segments, full_text)
  - speaker_only_segments(dashscope_segments) -> segments   # Gemini 失败时的降级
"""

import os
import re
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


_TS_RE = re.compile(r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]')


def _window_ts_ok(text, win):
    """合并输出的时间戳是否落在本窗口时间范围内。

    根因防线：LLM 合并每个窗口时可能把时间轴重置（如第 2 窗输出 [00:00:xx]）或漂移，
    拼接后整条时间线错乱——这正是历史「精准模式时间戳损坏」的来源。用阿里云稿已知的
    窗口区间校验；越界过多说明这一窗不可信，退回时间戳准确的说话人稿。
    """
    lo = win * MERGE_WINDOW_SECONDS
    hi = lo + MERGE_WINDOW_SECONDS
    slack = 90  # 容忍跨窗句子 / 尾句延续
    tss = [_ts_to_seconds(m) for m in _TS_RE.findall(text)]
    if not tss:
        return False
    good = sum(1 for t in tss if lo - slack <= t <= hi + slack)
    return good >= len(tss) * 0.6


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
                # 时间戳必须落在本窗口范围内，否则视为 LLM 重置/漂移了时间轴
                if text and _window_ts_ok(text, win):
                    return text
            except Exception:  # noqa: BLE001
                pass
            # 合并失败/返空/时间戳越界：退回阿里云说话人稿（时间戳准确），保住这段内容
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
```

---

## `templates/index.html`

> 单页结构（4 个 tab：Transcribe/Xiaohongshu/Creators/Library）

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Verbatim</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div class="container">

        <!-- ===== Main View ===== -->
        <div id="main-view">
            <header>
                <div class="topbar">
                    <div class="brand"><span class="dot"></span>Verbatim</div>
                    <div class="topbar-right">
                        <button id="global-tasks" class="global-tasks hidden" type="button"
                                title="Active tasks">
                            <span class="gt-spin">⟳</span><span id="global-tasks-count"></span>
                        </button>
                        <div class="tagline">Audio → transcript → insight · local</div>
                        <div id="global-tasks-pop" class="global-tasks-pop hidden"></div>
                    </div>
                </div>
                <nav class="main-nav">
                    <button class="nav-tab active" data-tab="transcribe">Transcribe</button>
                    <button class="nav-tab" data-tab="xhs">Xiaohongshu</button>
                    <button class="nav-tab" data-tab="creators">Creators</button>
                    <button class="nav-tab" data-tab="library">Library</button>
                </nav>
            </header>

            <!-- ===== Tab: Transcribe ===== -->
            <section id="tab-transcribe" class="tab-panel active">
                <div class="hero">
                    <h1>Transcribe<span class="accent">.</span></h1>
                    <p>Drop audio or video files — Verbatim extracts the audio and turns it into timestamped text.</p>
                </div>

                <form id="upload-form" enctype="multipart/form-data">
                    <div class="form-group">
                        <div id="drop-zone" class="drop-zone">
                            <label for="audio-file" class="file-label">
                                <span class="file-icon">📁</span>
                                <span id="file-label-text">Choose or drop audio / video files (multiple ok)</span>
                                <span id="file-info" class="file-info"></span>
                            </label>
                        </div>
                        <input type="file" id="audio-file" name="audio" multiple
                               accept=".mp3,.wav,.flac,.m4a,.ogg,.webm,.mp4,.mov,.mkv,.avi,.m4v">
                        <textarea id="mixed-input" class="url-box" rows="2"
                            placeholder="Or paste video links / local file paths — one per line, mix freely&#10;https://www.bilibili.com/video/BV...      /Users/you/Documents/lecture.mp4"></textarea>
                        <p class="local-hint">Links download &amp; transcribe (YouTube, Bilibili &amp; other yt-dlp
                            sites — for a whole channel use Creators). Local paths are read in place, nothing
                            uploaded — note: macOS blocks reading from Downloads/Desktop, keep files in
                            Documents or another folder.<br>
                            Only want part of a video? Add a time range after the link:
                            <code>…/watch?v=xxx @10:00-25:00</code> (or <code>@5:30-</code> to the end) —
                            only that clip is downloaded &amp; transcribed, cheaper, and timestamps stay at the
                            real video position.<br>
                            A collection / playlist link (e.g. a Bilibili 合集, a YouTube playlist) is expanded
                            into its videos and each one is transcribed — transcripts only, no analysis.</p>
                        <label class="collection-cap">Videos per collection
                            <input type="number" id="url-max-videos" class="search-input chain-small"
                                   min="1" max="300" value="20"
                                   title="When a link is a collection/playlist, transcribe at most this many of its videos.">
                        </label>
                        <div id="intake-list" class="intake-list"></div>
                    </div>

                    <div class="form-group">
                        <label class="group-label">Engine</label>
                        <div class="radio-group">
                            <label class="radio-option">
                                <input type="radio" name="engine" value="whisper" checked>
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>Whisper</strong>
                                    <small>Local · free · offline</small>
                                </span>
                            </label>
                            <label class="radio-option">
                                <input type="radio" name="engine" value="gemini">
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>Gemini</strong>
                                    <small>Cloud · needs API key</small>
                                </span>
                            </label>
                        </div>

                        <button type="button" id="show-mainland" class="reveal-link">
                            Show mainland-cloud engines ▾
                        </button>
                        <div class="radio-group hidden" id="mainland-engines">
                            <label class="radio-option">
                                <input type="radio" name="engine" value="qwenasr">
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>Qwen-ASR</strong>
                                    <small>Alibaba Cloud · strong zh · ⚠ moderates content</small>
                                </span>
                            </label>
                            <label class="radio-option">
                                <input type="radio" name="engine" value="precise">
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>Precise</strong>
                                    <small>Gemini text + Alibaba diarization · ⚠ moderates content</small>
                                </span>
                            </label>
                        </div>
                    </div>

                    <div class="form-group hidden" id="speaker-count-group">
                        <label class="group-label" for="speaker-count">
                            Expected speakers <small class="hint">(optional — improves accuracy; leave blank to auto-detect)</small>
                        </label>
                        <input type="number" id="speaker-count" name="speaker_count"
                               min="1" max="20" step="1" placeholder="blank = auto"
                               class="number-input">
                    </div>

                    <button type="submit" id="submit-btn" class="btn-primary">
                        Transcribe
                    </button>
                </form>

                <div id="batch-section" class="hidden">
                    <div class="batch-header">
                        <h2>Queue</h2>
                        <span id="batch-progress" class="batch-progress"></span>
                    </div>
                    <div id="batch-list" class="batch-list"></div>
                </div>

                <div id="error-section" class="hidden">
                    <div class="error-box">
                        <span class="error-icon">⚠️</span>
                        <span id="error-text"></span>
                    </div>
                </div>
            </section>



            <!-- ===== Tab: 小红书采集 ===== -->
            <section id="tab-xhs" class="tab-panel">
                <div class="hero">
                    <h1>Xiaohongshu<span class="accent">.</span></h1>
                    <p>Enter keywords and how many notes — Verbatim drives a real browser to search, download full-res images, and pull top-level comments into a dataset. <b>First run pops a browser asking for a QR login</b> (remembered afterwards).</p>
                </div>
                <form class="chain-form-shell" onsubmit="return false">
                    <div class="chain-form">
                        <textarea id="xhs-keywords" class="search-input" rows="4"
                            placeholder="One keyword per line — searched separately, de-duplicated, merged:&#10;uiuc cs admissions&#10;college essay tips&#10;对象出轨"></textarea>
                        <div class="chain-form-row">
                            <label class="xhs-num"><span>Notes to scrape</span>
                            <input type="number" id="xhs-max-notes" class="search-input chain-small"
                                   min="1" max="300" value="20"></label>
                            <label class="xhs-num"><span>Comments cap / note</span>
                            <input type="number" id="xhs-max-comments" class="search-input chain-small"
                                   min="50" max="2000" value="400"></label>
                            <button id="xhs-start" type="button" class="btn-primary chain-btn">Start scraping</button>
                        </div>
                        <p class="chain-hint">Scraping and analysis are decoupled; every note is saved as it lands (safe to interrupt). Anti-bot warning: 70+ notes in one run risks rate-limiting — go in small batches. Data lands in <code>xhs_dataset/</code>.</p>
                    </div>
                </form>
                <div id="xhs-progress" class="xhs-progress hidden"></div>

                <div class="xhs-analyze-card">
                    <div class="xhs-an-head">
                        <div>
                            <b>Analyze the notes from the keywords above → research report</b>
                            <span class="ci-hint">Scoped to notes scraped by those exact keywords (topics never mix). Reads images + comments per note, then aggregates. Empty keywords = whole dataset. Uses Gemini multimodal; costs scale with note count.</span>
                            <select id="xhs-lang" class="search-input chain-small" style="margin-top:8px">
                                <option value="auto" selected>Report: Auto (follow content)</option>
                                <option value="en">Report: English</option>
                                <option value="zh">Report: 中文</option>
                            </select>
                        </div>
                        <button id="xhs-analyze-btn" type="button" class="btn-primary chain-btn">Analyze this batch</button>
                    </div>
                    <div id="xhs-analyze-progress" class="hidden"></div>
                </div>
            </section>

            <!-- ===== Tab: 博主库 (Creators) ===== -->
            <section id="tab-creators" class="tab-panel">
                <div class="hero">
                    <h1>Creators<span class="accent">.</span></h1>
                    <p>Paste a channel, playlist, or video — Verbatim downloads and transcribes every episode, then writes a portrait. Open a card for the portrait, then re-read it through a lens — 🔥 Roast / ✍️ Craft / 😂 Watchability / 💬 Quotes / 🖼 Worldview.</p>
                </div>

                <form class="chain-form-shell">
                    <div class="chain-form">
                        <input type="url" id="chain-url" class="search-input"
                               placeholder="https://www.youtube.com/@creator  ·  a channel / homepage (for one video, use Transcribe)">
                        <!-- 主行：每次都要决定的（URL 在上；这里是 谁/用什么脑/要不要分析）-->
                        <div class="chain-form-row">
                            <input type="text" id="chain-author" class="search-input chain-small"
                                   placeholder="Author (optional)">
                            <select id="chain-engine" class="search-input chain-small"
                                    title="Transcription engine.">
                                <option value="gemini">Gemini (recommended)</option>
                                <option value="whisper">Whisper (local)</option>
                                <option value="qwenasr">Alibaba Qwen-ASR (zh ASR · no Gemini filter)</option>
                            </select>
                            <select id="chain-provider" class="search-input chain-small"
                                    title="Which model does the analysis. Aliyun (DeepSeek/Kimi/GLM/Qwen) reuses your DashScope key and dodges the Gemini quota — but it moderates content, so use it only for non-sensitive creators.">
                                <option value="gemini" selected>Analyze: Gemini</option>
                                <option value="opus5">Analyze: Claude Opus 5  (OpenRouter · thinking)</option>
                                <option value="opus46">Analyze: Claude Opus 4.6  (OpenRouter · thinking)</option>
                                <option value="deepseek">Analyze: DeepSeek  (Alibaba)</option>
                                <option value="kimi">Analyze: Kimi  (Alibaba)</option>
                                <option value="glm">Analyze: GLM  (Alibaba)</option>
                                <option value="qwen">Analyze: Qwen  (Alibaba)</option>
                            </select>
                            <button id="chain-start" type="button" class="btn-primary chain-btn">Analyze</button>
                        </div>
                        <!-- 折叠：默认设置 + 专家旋钮，多数人不动 -->
                        <details class="chain-advanced">
                            <summary>⚙ More options</summary>
                            <div class="chain-form-row chain-adv-row">
                                <input type="number" id="chain-max" class="search-input chain-small"
                                       min="1" max="300" placeholder="Max videos">
                                <select id="chain-lang" class="search-input chain-small"
                                        title="Language of the analysis documents (portrait, per-episode notes, lenses). Auto = follow the content's language. Verbatim quotes always stay original.">
                                    <option value="auto" selected>Output: Auto (follow content)</option>
                                    <option value="en">Output: English</option>
                                    <option value="zh">Output: 中文</option>
                                </select>
                                <select id="chain-critique" class="search-input chain-small"
                                        title="How sharp the persona portrait's judgment is. Evidence tagging is enforced at every level — only the tone changes.">
                                    <option value="descriptive">Descriptive</option>
                                    <option value="analytical" selected>Analytical</option>
                                    <option value="sharp">Sharp</option>
                                </select>
                                <label class="chain-check" title="If a video already has captions, use them instead of downloading + transcribing the audio (much faster, no API cost). Manual subs preferred; falls back to the video's auto-captions, then to audio.">
                                    <input type="checkbox" id="chain-prefer-subs">
                                    Prefer subtitles
                                </label>
                                <label class="chain-check" title="Let the analysis model verify papers, models, data, and mangled entity names with live Google Search before judging them. Fixes 'confidently calls real recent things fake' — but slower and uses search quota. Worth it for time-sensitive / technical content.">
                                    <input type="checkbox" id="chain-verify" checked>
                                    Verify facts (web)
                                </label>
                                <label class="chain-check" title="If the cloud engine fails (e.g. quota), fall back to local Whisper for that video instead of leaving it failed. Off by default — otherwise it just fails and you re-run on the same engine (e.g. once your quota is back).">
                                    <input type="checkbox" id="chain-fallback-whisper">
                                    Whisper fallback
                                </label>
                                <label class="chain-check" title="After building the portrait, run a self-critique pass: pull out every evaluative claim, send a skeptic per claim to refute it against the evidence cards, then cut the unsupported ones and soften the overstated ones (with a 证伪留痕 appendix). Costs extra calls; on for high-stakes reads.">
                                    <input type="checkbox" id="chain-self-verify" checked>
                                    Self-verify
                                </label>
                            </div>
                        </details>
                    </div>
                    <p class="chain-hint">YouTube tip: use <code>channel/videos</code>. Bilibili &amp; other
                       yt-dlp sites work too. <b>Prefer subtitles</b> skips download + transcription when a
                       video already has captions.</p>
                </form>

                <div id="creators-grid" class="creators-grid"></div>
                <p id="creators-empty" class="history-empty hidden">No creators yet — paste a link above to analyze one.</p>
            </section>

            <!-- ===== Tab: Library ===== -->
            <section id="tab-library" class="tab-panel">
                <div class="hero">
                    <h1>Library<span class="accent">.</span></h1>
                    <p>Every transcript and analysis document, searchable in one place.</p>
                </div>

                <div class="lib-subnav">
                    <button class="sub-tab active" data-lib="transcripts">Transcripts</button>
                    <button class="sub-tab" data-lib="docs">Analyses</button>
                </div>

                <!-- Transcripts -->
                <div id="lib-transcripts" class="lib-panel active">
                    <div id="history-section">
                        <div class="history-card">
                            <div class="history-header">
                                <h2>History <span id="history-count" class="history-count"></span></h2>
                                <div class="history-header-actions">
                                    <button id="enrich-btn" class="btn-secondary" title="Use AI to add titles &amp; tags to untitled records">
                                        Auto-title
                                    </button>
                                    <button id="clear-history-btn" class="btn-secondary btn-danger" title="Clear all history">
                                        Clear
                                    </button>
                                </div>
                            </div>
                            <div class="history-toolbar">
                                <input type="search" id="search-input" class="search-input"
                                       placeholder="Search titles, filenames, or transcript text…">
                                <div id="source-filters" class="filter-chips">
                                    <button class="chip active" data-source="">All sources</button>
                                    <button class="chip" data-source="mine">🙋 Mine</button>
                                    <button class="chip" data-source="pipeline">🎬 From Creators</button>
                                </div>
                                <div id="engine-filters" class="filter-chips">
                                    <button class="chip active" data-engine="">All</button>
                                    <button class="chip" data-engine="gemini">Gemini</button>
                                    <button class="chip" data-engine="qwenasr">Qwen-ASR</button>
                                    <button class="chip" data-engine="dashscope">DashScope</button>
                                    <button class="chip" data-engine="precise">Precise</button>
                                    <button class="chip" data-engine="whisper">Whisper</button>
                                </div>
                            </div>
                            <div id="history-list" class="history-list">
                                <p class="history-empty">No transcripts yet</p>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Analyses -->
                <div id="lib-docs" class="lib-panel">
                    <div class="history-card">
                        <div class="history-header">
                            <h2>Analyses</h2>
                            <button id="docs-refresh" class="btn-secondary" title="Refresh">Refresh</button>
                        </div>
                        <p class="chain-hint">Documents produced by creator analyses. Click to read — no download needed.</p>
                        <div id="docs-list" class="docs-list">
                            <p class="history-empty">No analysis documents yet — analyze a creator first</p>
                        </div>
                    </div>
                </div>
            </section>
        </div>

        <!-- ===== Transcript Detail View ===== -->
        <div id="detail-view" class="hidden">
            <div class="detail-top-bar">
                <button id="detail-back-btn" class="btn-secondary">← Back</button>
                <span id="detail-title" class="detail-title"></span>
            </div>

            <div id="detail-meta" class="detail-meta"></div>

            <div id="detail-player-section" class="hidden">
                <div class="player-card">
                    <audio id="detail-audio-player" controls></audio>
                </div>
            </div>

            <div id="detail-summary-section" class="hidden">
                <div class="summary-card">
                    <h2>Summary</h2>
                    <div id="detail-summary-overview" class="summary-overview"></div>
                    <div id="detail-summary-sections" class="summary-sections"></div>
                </div>
            </div>

            <div id="detail-results-section">
                <div class="results-header">
                    <h2>Transcript</h2>
                    <div class="results-actions">
                        <button id="detail-copy-btn" class="btn-secondary" title="Copy text">Copy</button>
                        <button id="detail-download-btn" class="btn-secondary" title="Download as TXT">TXT</button>
                        <button id="detail-srt-btn" class="btn-secondary" title="Download subtitles">SRT</button>
                    </div>
                </div>
                <div id="detail-segments-container" class="segments-container"></div>
            </div>
        </div>

        <!-- ===== Chain Detail View (info panel + collapsible episode grid) ===== -->
        <div id="chain-detail-view" class="hidden">
            <div class="detail-top-bar">
                <button id="chain-detail-back" class="btn-secondary">← Back</button>
                <span id="chain-detail-title" class="detail-title"></span>
            </div>
            <div id="chain-detail-meta" class="detail-meta"></div>
            <div id="chain-detail-info"></div>
            <button id="chain-episodes-toggle" class="episodes-toggle" type="button">
                Episodes <span id="chain-episodes-count"></span> ▸
            </button>
            <div id="chain-detail-grid" class="video-grid hidden"></div>
        </div>

        <!-- ===== Analysis Doc Reading View ===== -->
        <div id="doc-view" class="hidden">
            <div class="detail-top-bar">
                <button id="doc-back-btn" class="btn-secondary">← Back</button>
                <span id="doc-title" class="detail-title"></span>
                <button id="doc-download-btn" class="btn-secondary" title="Download the Markdown file">Download .md</button>
            </div>
            <div id="doc-content" class="md-body"></div>
        </div>

    </div>

    <!-- ===== Settings modal (two-pane, Claude-style) ===== -->
    <div id="settings-overlay" class="settings-overlay hidden">
        <div class="settings-modal">
            <button id="settings-close" class="settings-x" aria-label="Close">×</button>

            <nav class="settings-nav">
                <div class="settings-nav-title">Settings</div>
                <button class="settings-nav-item active" data-pane="gemini">Gemini</button>
                <button class="settings-nav-item" data-pane="dashscope">DashScope</button>
                <button class="settings-nav-item" data-pane="openrouter">OpenRouter</button>
                <button class="settings-nav-item" data-pane="models">Models</button>
                <button class="settings-nav-item" data-pane="storage">Storage</button>
                <button class="settings-nav-item" data-pane="about">About</button>
            </nav>

            <div class="settings-main">
                <div class="settings-panes">
                    <!-- Gemini -->
                    <section class="settings-pane active" data-pane="gemini">
                        <h3>Gemini</h3>
                        <p class="settings-desc">
                            Powers cloud transcription, summaries, and the pipeline analysis.
                            Everything stays on this machine except the calls to Google.
                        </p>
                        <label class="settings-field">
                            <span class="settings-label">API key
                                <a href="https://aistudio.google.com/apikey" target="_blank" rel="noopener">get one ↗</a>
                            </span>
                            <input type="password" id="set-gemini-key" class="search-input"
                                   placeholder="paste key…" autocomplete="off">
                        </label>
                        <label class="settings-field">
                            <span class="settings-label">Base URL
                                <span class="settings-opt">optional · proxy / mainland China</span>
                            </span>
                            <input type="text" id="set-gemini-base" class="search-input"
                                   placeholder="https://your-proxy.example   (blank = official endpoint)"
                                   autocomplete="off">
                        </label>
                        <div class="settings-test-row">
                            <button type="button" id="test-gemini" class="btn-secondary">Test connection</button>
                            <span id="test-gemini-res" class="test-res"></span>
                        </div>
                    </section>

                    <!-- DashScope -->
                    <section class="settings-pane" data-pane="dashscope">
                        <h3>DashScope</h3>
                        <p class="settings-desc">
                            Alibaba Cloud key — powers the <strong>Qwen-ASR</strong> engine, the speaker
                            diarization half of <strong>Precise</strong>, and the Alibaba analysis models.
                            <strong>⚠ It moderates content</strong> — don't use it for politically
                            sensitive audio; prefer local Whisper or Gemini for that.
                        </p>
                        <label class="settings-field">
                            <span class="settings-label">API key
                                <a href="https://bailian.console.aliyun.com/" target="_blank" rel="noopener">get one ↗</a>
                            </span>
                            <input type="password" id="set-dashscope-key" class="search-input"
                                   placeholder="paste key…" autocomplete="off">
                        </label>
                        <div class="settings-test-row">
                            <button type="button" id="test-dashscope" class="btn-secondary">Test connection</button>
                            <span id="test-dashscope-res" class="test-res"></span>
                        </div>
                    </section>

                    <!-- OpenRouter -->
                    <section class="settings-pane" data-pane="openrouter">
                        <h3>OpenRouter</h3>
                        <p class="settings-desc">
                            One key for Claude and other frontier models — powers the
                            <strong>Claude Opus</strong> analysis options in Creators (with thinking enabled).
                            No content moderation. Note: Opus is premium-priced and the pipeline
                            analyzes every episode, so cost scales with episode count.
                        </p>
                        <label class="settings-field">
                            <span class="settings-label">API key
                                <a href="https://openrouter.ai/settings/keys" target="_blank" rel="noopener">get one ↗</a>
                            </span>
                            <input type="password" id="set-openrouter-key" class="search-input"
                                   placeholder="paste key…" autocomplete="off">
                        </label>
                        <div class="settings-test-row">
                            <button type="button" id="test-openrouter" class="btn-secondary">Test connection</button>
                            <span id="test-openrouter-res" class="test-res"></span>
                        </div>
                    </section>

                    <!-- Models -->
                    <section class="settings-pane" data-pane="models">
                        <h3>Models</h3>
                        <p class="settings-desc">
                            Saved instantly — the next task picks them up, no restart. Leave a field
                            blank to use the default.
                        </p>
                        <label class="settings-field">
                            <span class="settings-label">Whisper model
                                <span class="settings-opt">local transcription · bigger = better zh accuracy, slower first load</span>
                            </span>
                            <select id="set-whisper-model" class="search-input">
                                <option value="">small (default)</option>
                                <option value="tiny">tiny — fastest, rough</option>
                                <option value="base">base</option>
                                <option value="small">small</option>
                                <option value="medium">medium</option>
                                <option value="large-v3">large-v3 — best (needs ~4GB RAM, fine on 48GB)</option>
                            </select>
                        </label>
                        <label class="settings-field">
                            <span class="settings-label">Gemini · transcription model
                                <span class="settings-opt">default gemini-2.5-flash</span>
                            </span>
                            <input type="text" id="set-gemini-transcribe" class="search-input"
                                   placeholder="gemini-2.5-flash (default) · e.g. gemini-2.5-pro" autocomplete="off">
                        </label>
                        <label class="settings-field">
                            <span class="settings-label">Gemini · analysis (synthesis) model
                                <span class="settings-opt">default gemini-2.5-pro</span>
                            </span>
                            <input type="text" id="set-gemini-analysis" class="search-input"
                                   placeholder="gemini-2.5-pro (default) · e.g. gemini-3.1-pro-preview" autocomplete="off">
                        </label>
                        <label class="settings-field">
                            <span class="settings-label">Gemini · extraction model
                                <span class="settings-opt">default gemini-2.5-flash · cheap mechanical step</span>
                            </span>
                            <input type="text" id="set-gemini-extract" class="search-input"
                                   placeholder="gemini-2.5-flash (default)" autocomplete="off">
                        </label>
                        <p class="settings-opt">DeepSeek / Kimi / GLM / Qwen model names are per-analysis presets (pick the model in the Creators form).</p>
                    </section>

                    <!-- Storage -->
                    <section class="settings-pane" data-pane="storage">
                        <h3>Storage</h3>
                        <p class="settings-desc">
                            After a recording is transcribed, its audio is only kept for playback.
                            Compressing it to Opus (24 kbps mono) keeps speech clear while shrinking
                            the library by roughly 70%. New transcriptions are compressed automatically.
                        </p>
                        <div class="storage-stat">
                            <div><b id="storage-size">—</b><span>audio on disk</span></div>
                            <div><b id="storage-done">—</b><span>already compressed</span></div>
                        </div>
                        <div class="settings-test-row">
                            <button type="button" id="compress-all" class="btn-secondary">Compress existing library</button>
                            <span id="compress-res" class="test-res"></span>
                        </div>
                        <p class="settings-opt">Lossy and irreversible — fine for playback, but you can't re-transcribe at original quality afterward. Chrome/Firefox play Opus natively.</p>
                    </section>

                    <!-- About -->
                    <section class="settings-pane" data-pane="about">
                        <h3>About</h3>
                        <p class="settings-desc">
                            <strong>Verbatim</strong> — a local, self-hosted tool that turns audio and video
                            into transcripts, then into cross-episode research.
                        </p>
                        <p class="settings-desc">
                            Keys you enter here are stored only in <code>settings.local.json</code> on this
                            machine and are never sent anywhere except to the provider they belong to. Leave a
                            key field blank to keep the current one.
                        </p>
                        <p class="settings-desc">
                            <a href="https://github.com/xyzxinlu-max/getAudio" target="_blank" rel="noopener">Source on GitHub ↗</a>
                        </p>
                    </section>
                </div>

                <div class="settings-foot">
                    <span id="settings-msg" class="settings-msg"></span>
                    <button type="button" id="settings-save" class="btn-primary">Save</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ===== 左下角个人数据展板 ===== -->
    <div id="stats-fab" class="stats-fab">
        <div id="stats-panel" class="stats-panel hidden">
            <div class="stats-head">Your library</div>
            <div class="stats-nums">
                <div class="stat-num"><b id="stat-hours">—</b><span>hours transcribed</span></div>
                <div class="stat-num"><b id="stat-chars">—</b><span>characters</span></div>
                <div class="stat-num"><b id="stat-count">—</b><span>transcripts</span></div>
            </div>
            <div class="stats-block">
                <div class="stats-label">Cumulative hours</div>
                <div id="stats-chart"></div>
            </div>
            <div class="stats-block">
                <div class="stats-label stats-label-row">
                    <span>Fields you follow</span>
                    <button id="stats-lang" class="stats-lang" type="button"
                            title="Toggle tag language">EN</button>
                </div>
                <div id="stats-tags"></div>
            </div>
        </div>
        <div class="fab-row">
            <button id="stats-toggle" class="stats-toggle" title="Your stats">
                <span class="stats-dot"></span><span>Your stats</span>
            </button>
            <button id="settings-open" class="stats-toggle" title="Settings">
                <span class="fab-gear">⚙</span><span>Settings</span>
            </button>
        </div>
    </div>

    <script src="/static/app.js"></script>
</body>
</html>
```

---

## `static/app.js`

> 前端逻辑（上传/SSE/链条/镜头/小红书/统计/Markdown 渲染）

```javascript
/**
 * Client-side logic for audio/video transcription app.
 * Supports batch (multi-file) transcription: drop many files at once, each
 * runs as an independent task with its own progress row. Completed rows link
 * into the existing history detail view. Results persist server-side.
 */

// ========== DOM: Main view ==========
const mainView = document.getElementById('main-view');
const form = document.getElementById('upload-form');
const fileInput = document.getElementById('audio-file');
const fileLabel = document.querySelector('.file-label');
const fileLabelText = document.getElementById('file-label-text');
const fileInfo = document.getElementById('file-info');
const submitBtn = document.getElementById('submit-btn');
const dropZone = document.getElementById('drop-zone');

const batchSection = document.getElementById('batch-section');
const batchList = document.getElementById('batch-list');
const batchProgress = document.getElementById('batch-progress');

const errorSection = document.getElementById('error-section');
const errorText = document.getElementById('error-text');

const clearHistoryBtn = document.getElementById('clear-history-btn');
const historyList = document.getElementById('history-list');
const historyCount = document.getElementById('history-count');
const searchInput = document.getElementById('search-input');
const engineFilters = document.getElementById('engine-filters');
const enrichBtn = document.getElementById('enrich-btn');

// ========== DOM: Detail view ==========
const detailView = document.getElementById('detail-view');
const detailBackBtn = document.getElementById('detail-back-btn');
const detailTitle = document.getElementById('detail-title');
const detailMeta = document.getElementById('detail-meta');
const detailPlayerSection = document.getElementById('detail-player-section');
const detailAudioPlayer = document.getElementById('detail-audio-player');
const detailSummarySection = document.getElementById('detail-summary-section');
const detailSummaryOverview = document.getElementById('detail-summary-overview');
const detailSummarySections = document.getElementById('detail-summary-sections');
const detailSegmentsContainer = document.getElementById('detail-segments-container');
const detailCopyBtn = document.getElementById('detail-copy-btn');
const detailDownloadBtn = document.getElementById('detail-download-btn');
const detailSrtBtn = document.getElementById('detail-srt-btn');

// ========== State ==========
let detailSegments = [];
let batchTotal = 0;
let batchFinished = 0;

// ========== Helpers ==========
const ENGINE_LABELS = {
    whisper: 'Whisper',
    gemini: 'Gemini',
    // dashscope = 2026-08 之前用 paraformer-v2 转的老稿，保留标签让历史记录如实显示
    dashscope: 'DashScope (Paraformer)',
    qwenasr: 'Qwen-ASR',
    precise: 'Precise (diarization)',
};

function engineLabel(engine) {
    return ENGINE_LABELS[engine] || (engine || 'Unknown');
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '';
    const s = Math.round(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
    return `${m}:${String(sec).padStart(2, '0')}`;
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ========== 统一入口：文件 + 链接 + 本地路径 ==========
// 文件的事实来源是 intakeFiles（可多次追加、单条删除）；
// 文本行的事实来源是 #mixed-input 的内容（一行一条，实时解析出预览）。
let intakeFiles = [];
let intakeSubmitting = false;
const mixedInput = document.getElementById('mixed-input');
const intakeList = document.getElementById('intake-list');

fileInput.addEventListener('change', () => {
    addFiles(fileInput.files);
    fileInput.value = '';   // 清掉原生选择，允许再次添加同名文件；预览列表才是事实来源
});

function addFiles(files) {
    for (const f of files || []) {
        // 同名同大小视为重复，跳过
        if (!intakeFiles.some(x => x.name === f.name && x.size === f.size)) {
            intakeFiles.push(f);
        }
    }
    renderIntake();
}

function updateFileLabel() {
    if (intakeFiles.length === 0) {
        fileLabelText.textContent = 'Choose or drop audio / video files (multiple ok)';
        fileInfo.textContent = '';
        fileLabel.classList.remove('has-file');
        return;
    }
    let totalSize = 0;
    for (const f of intakeFiles) totalSize += f.size;
    fileLabelText.textContent = intakeFiles.length === 1
        ? intakeFiles[0].name : `${intakeFiles.length} files added`;
    fileInfo.textContent = formatFileSize(totalSize);
    fileLabel.classList.add('has-file');
}

// 逐行分类：http(s):// → 链接；/ 或 ~ 开头（允许引号包裹）→ 本地路径；其余非空行 → invalid
function parseTextLines() {
    const out = [];
    (mixedInput.value || '').split('\n').forEach((raw, line) => {
        const s = raw.trim();
        if (!s) return;
        let kind = 'invalid';
        if (/^https?:\/\//i.test(s)) kind = 'link';
        else if (/^[\/~]/.test(s.replace(/^['"]/, ''))) kind = 'path';
        // 链接可带行尾时间段后缀 " @10:00-25:00"（只转那一段）；预览里拆出来显示
        let label = s, clip = '';
        if (kind === 'link') {
            const m = s.match(/\s+@\s*([0-9:]+)\s*-\s*([0-9:]*)\s*$/);
            if (m) {
                label = s.slice(0, m.index).trim();
                clip = `${m[1]}–${m[2] || 'end'}`;
            }
        }
        out.push({ kind, text: s, label, clip, line });
    });
    return out;
}

const INTAKE_BADGES = { file: 'File', link: 'Link', path: 'Local path', invalid: '?' };

function buildIntakeRow(r) {
    const row = document.createElement('div');
    row.className = 'intake-row' + (r.kind === 'invalid' ? ' intake-invalid' : '');

    const badge = document.createElement('span');
    badge.className = `intake-badge intake-badge-${r.kind}`;
    badge.textContent = INTAKE_BADGES[r.kind];
    row.appendChild(badge);

    const name = document.createElement('span');
    name.className = 'intake-name';
    name.textContent = r.label;
    name.title = r.label;
    row.appendChild(name);

    if (r.sub || r.kind === 'invalid') {
        const sub = document.createElement('span');
        sub.className = 'intake-sub';
        sub.textContent = r.kind === 'invalid'
            ? 'Not a link or a path — will be skipped' : r.sub;
        row.appendChild(sub);
    }

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'intake-del';
    del.textContent = '×';
    del.title = 'Remove';
    del.addEventListener('click', () => removeIntakeRow(r));
    row.appendChild(del);
    return row;
}

function removeIntakeRow(r) {
    if (r.kind === 'file') {
        intakeFiles.splice(r.fileIndex, 1);
    } else {
        const lines = mixedInput.value.split('\n');
        lines.splice(r.line, 1);
        mixedInput.value = lines.join('\n');
    }
    renderIntake();
}

function renderIntake() {
    const rows = [];
    intakeFiles.forEach((f, i) => rows.push(
        { kind: 'file', label: f.name, sub: formatFileSize(f.size), fileIndex: i }));
    parseTextLines().forEach(t => rows.push({
        kind: t.kind, label: t.label || t.text, line: t.line,
        sub: t.clip ? `⏱ ${t.clip}` : '',
    }));
    intakeList.innerHTML = '';
    rows.forEach(r => intakeList.appendChild(buildIntakeRow(r)));
    updateFileLabel();
    updateSubmitBtn();
}

function intakeValidCount() {
    return intakeFiles.length
        + parseTextLines().filter(t => t.kind !== 'invalid').length;
}

function updateSubmitBtn() {
    if (intakeSubmitting) { submitBtn.disabled = true; return; }
    const n = intakeValidCount();
    submitBtn.textContent = n > 0
        ? `Transcribe ${n} item${n === 1 ? '' : 's'}` : 'Transcribe';
    submitBtn.disabled = n === 0;
}

let mixedInputTimer = null;
if (mixedInput) mixedInput.addEventListener('input', () => {
    clearTimeout(mixedInputTimer);
    mixedInputTimer = setTimeout(renderIntake, 250);
});

// ========== Drag & Drop (multi-file) ==========
['dragenter', 'dragover'].forEach(evt => {
    dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add('drag-over');
    });
});

['dragleave', 'drop'].forEach(evt => {
    dropZone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove('drag-over');
    });
});

dropZone.addEventListener('drop', (e) => {
    const files = e.dataTransfer.files;
    if (files && files.length > 0) addFiles(files);
});

// ========== Engine selection: toggle 预计人数 (precise only) ==========
const speakerCountGroup = document.getElementById('speaker-count-group');
document.querySelectorAll('input[name="engine"]').forEach(radio => {
    radio.addEventListener('change', () => {
        const isPrecise = document.querySelector('input[name="engine"]:checked').value === 'precise';
        speakerCountGroup.classList.toggle('hidden', !isPrecise);
    });
});

// ========== 国内云引擎（DashScope / Precise）：默认隐藏 + 风险确认 ==========
// 这两个引擎把音频送阿里云，阿里强制内容审核 —— 敏感/政治内容会被拒或篡改。
const showMainlandBtn = document.getElementById('show-mainland');
const mainlandEngines = document.getElementById('mainland-engines');
if (showMainlandBtn && mainlandEngines) {
    showMainlandBtn.addEventListener('click', () => {
        mainlandEngines.classList.toggle('hidden');
        showMainlandBtn.classList.toggle('open');
    });
}
const MAINLAND_WARNING =
    'Qwen-ASR / Precise send your audio to Alibaba Cloud (mainland China), which runs ' +
    'mandatory content moderation.\n\n' +
    'Do NOT use them for politically sensitive material — it may be refused, garbled, or altered. ' +
    'For sensitive content use Whisper (local, private) or Gemini.\n\nUse this engine anyway?';
document.querySelectorAll('input[name="engine"][value="qwenasr"], input[name="engine"][value="precise"]')
    .forEach(radio => {
        radio.addEventListener('change', () => {
            if (radio.checked && !confirm(MAINLAND_WARNING)) {
                // 拒绝 → 退回 Whisper
                const w = document.querySelector('input[name="engine"][value="whisper"]');
                w.checked = true;
                w.dispatchEvent(new Event('change'));
            }
        });
    });

// ========== Form submission (batch) ==========
// 同时上传的文件数。逐个上传是为了绕开单请求体积上限（几百个文件塞一个请求会超限被拒），
// 上传本身也限流，避免一次性发起过多大文件上传拖垮网络/内存。
// 真正的转写并发由服务器端每引擎的信号量控制（见 config.ENGINE_CONCURRENCY）。
const UPLOAD_CONCURRENCY = 3;

// 混合批次提交：文件 → /upload（上传池），链接 → /api/transcribe_urls（整批），
// 本地路径 → /api/transcribe_local（逐条）。三路并发，共用一个 Queue。
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (intakeSubmitting) return;
    const files = intakeFiles.slice();
    const texts = parseTextLines();
    const linkItems = texts.filter(t => t.kind === 'link');
    const links = linkItems.map(t => t.text);   // 带 @后缀，服务器端解析
    const paths = texts.filter(t => t.kind === 'path').map(t => t.text);
    const total = files.length + links.length + paths.length;
    if (!total) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;
    errorSection.classList.add('hidden');
    batchSection.classList.remove('hidden');
    batchList.innerHTML = '';
    batchTotal = total;
    batchFinished = 0;
    updateBatchProgress();

    intakeSubmitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Transcribing…';

    // 按预览顺序先建行（文件 → 链接 → 路径），invalid 行不进队列
    const fileJobs = files.map(file => {
        const row = createBatchRow(file.name);
        setRowStatus(row, 'Queued', 'queued');
        batchList.appendChild(row);
        return { file, row };
    });
    const linkRows = linkItems.map(t => {
        // Queue 里显示干净 URL + 时间段徽标，别露出 @后缀
        const row = createBatchRow(t.clip ? `${t.label}  (⏱ ${t.clip})` : t.label);
        setRowStatus(row, 'Queued', 'queued');
        batchList.appendChild(row);
        return row;
    });
    const pathRows = paths.map(p => {
        const row = createBatchRow(p.split('/').pop() || p);
        setRowStatus(row, 'Queued', 'queued');
        batchList.appendChild(row);
        return row;
    });

    // 提交即清空输入区（invalid 行留在文本框里，用户可改）
    intakeFiles = [];
    const keptLines = mixedInput.value.split('\n')
        .filter(l => { const s = l.trim(); return s && !(/^https?:\/\//i.test(s)) && !(/^[\/~]/.test(s.replace(/^['"]/, ''))); });
    mixedInput.value = keptLines.join('\n');
    renderIntake();

    await Promise.all([
        runUploadPool(fileJobs, engine, UPLOAD_CONCURRENCY),
        submitLinks(links, linkRows, engine),
        submitPaths(paths, pathRows, engine),
    ]);
});

async function runUploadPool(jobs, engine, concurrency) {
    let cursor = 0;

    async function worker() {
        while (cursor < jobs.length) {
            const job = jobs[cursor++];
            await uploadOne(job.file, job.row, engine);
        }
    }

    const workers = [];
    for (let i = 0; i < Math.min(concurrency, jobs.length); i++) {
        workers.push(worker());
    }
    await Promise.all(workers);
}

// —— 链接批：一次 POST，服务器按行返回 tasks（与 rows 顺序一一对应，上限 20）——
async function submitLinks(links, rows, engine) {
    if (!links.length) return;
    try {
        const resp = await fetch('/api/transcribe_urls', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                urls: links.join('\n'), engine,
                max_videos: parseInt((document.getElementById('url-max-videos') || {}).value, 10) || 20,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.error) {
            rows.forEach(row => {
                setRowStatus(row, data.error || `Failed (${resp.status})`, 'error');
                onTaskFinished();
            });
            return;
        }
        // 注意：一行**合集/播放列表**链接会展开成多个任务，所以 tasks 可能比 rows 多。
        // 多出来的当场补行（用服务器返回的标题），别让它们没有进度显示。
        data.tasks.forEach((t, i) => {
            let row = rows[i];
            if (!row) {
                row = createBatchRow(t.title || t.url);
                batchList.appendChild(row);
                batchTotal += 1;
                updateBatchProgress();
            } else if (t.title) {
                const nameEl = row.querySelector('.batch-item-name');
                if (nameEl) nameEl.textContent = t.title;   // 展开后用真实标题替掉合集URL
            }
            setRowStatus(row, 'Downloading…', 'running');
            connectBatchSSE(t.task_id, row);
        });
        // 超出服务器单批上限被截掉的行，明确标出而不是悄悄消失
        for (let i = data.tasks.length; i < rows.length; i++) {
            setRowStatus(rows[i], 'Skipped — max 20 links per batch', 'error');
            onTaskFinished();
        }
        // 合集枚举失败之类的问题，提示出来而不是静默
        if (data.errors && data.errors.length) {
            showToast(`Some links could not be expanded: ${data.errors[0]}`);
        }
    } catch (err) {
        rows.forEach(row => {
            setRowStatus(row, `Failed: ${err.message}`, 'error');
            onTaskFinished();
        });
    }
}

// —— 本地路径批：逐条 POST（服务器软链读盘，零上传）——
async function submitPaths(paths, rows, engine) {
    for (let i = 0; i < paths.length; i++) {
        const row = rows[i];
        setRowStatus(row, 'Reading local file…', 'running');
        const body = { path: paths[i], engine };
        const speakerEl = document.getElementById('speaker-count');
        if (engine === 'precise' && speakerEl && speakerEl.value.trim()) {
            body.speaker_count = speakerEl.value.trim();
        }
        try {
            const resp = await fetch('/api/transcribe_local', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok || data.error) {
                setRowStatus(row, data.error || `Failed (${resp.status})`, 'error');
                onTaskFinished();
            } else {
                connectBatchSSE(data.task_id, row);
            }
        } catch (err) {
            setRowStatus(row, `Failed: ${err.message}`, 'error');
            onTaskFinished();
        }
    }
}

async function uploadOne(file, row, engine) {
    setRowStatus(row, 'Uploading…', 'running');

    const formData = new FormData();
    formData.append('audio', file);
    formData.append('engine', engine);

    // 精准模式下把"预计人数"一起带上（留空则不带，服务器自动判断）
    const speakerCountEl = document.getElementById('speaker-count');
    if (engine === 'precise' && speakerCountEl && speakerCountEl.value.trim()) {
        formData.append('speaker_count', speakerCountEl.value.trim());
    }

    try {
        const resp = await fetch('/upload', { method: 'POST', body: formData });
        if (!resp.ok) {
            let msg = `Upload failed (${resp.status})`;
            try {
                const d = await resp.json();
                if (d.error) msg = d.error;
            } catch { /* 413 等可能不是 JSON */ }
            setRowStatus(row, `${msg}`, 'error');
            onTaskFinished();
            return;
        }

        const data = await resp.json();
        if (data.error) {
            setRowStatus(row, `${data.error}`, 'error');
            onTaskFinished();
            return;
        }

        // 上传成功 → 服务器已排队，接 SSE 看进度
        connectBatchSSE(data.task_id, row);
    } catch (err) {
        setRowStatus(row, `Upload failed: ${err.message}`, 'error');
        onTaskFinished();
    }
}

function resetSubmitBtn() {
    intakeSubmitting = false;
    updateSubmitBtn();   // 恢复 "Transcribe N items"（批次跑完后输入区通常已空 → 禁用）
}

// ========== Batch rows ==========
function createBatchRow(filename) {
    const row = document.createElement('div');
    row.className = 'batch-item';

    const info = document.createElement('div');
    info.className = 'batch-item-info';

    const name = document.createElement('span');
    name.className = 'batch-item-name';
    name.textContent = filename;

    const status = document.createElement('span');
    status.className = 'batch-item-status';
    status.textContent = 'Queued';

    info.appendChild(name);
    info.appendChild(status);

    const bar = document.createElement('div');
    bar.className = 'batch-item-progress';
    const fill = document.createElement('div');
    fill.className = 'batch-item-progress-fill';
    bar.appendChild(fill);

    const actions = document.createElement('div');
    actions.className = 'batch-item-actions';

    row.appendChild(info);
    row.appendChild(bar);
    row.appendChild(actions);
    return row;
}

function setRowStatus(row, text, state) {
    const status = row.querySelector('.batch-item-status');
    status.textContent = text;
    status.className = 'batch-item-status';
    if (state) status.classList.add(`status-${state}`);
    // 所有队列行的状态变化都经过这里 → 顺手刷新全局任务指示器
    updateGlobalIndicator('transcribe', collectQueueTasks());
}

// Queue 的事实来源就是 #batch-list 的行：未完成 = queued / running
function collectQueueTasks() {
    return [...batchList.querySelectorAll('.batch-item')]
        .filter(r => r.querySelector('.status-queued, .status-running'))
        .map(r => ({
            label: r.querySelector('.batch-item-name').textContent,
            progress: r.querySelector('.batch-item-status').textContent,
            tab: 'transcribe',
        }));
}

function setRowProgress(row, percent) {
    row.querySelector('.batch-item-progress-fill').style.width = `${percent}%`;
}

function connectBatchSSE(taskId, row) {
    const actions = row.querySelector('.batch-item-actions');
    const source = new EventSource(`/stream/${taskId}`);

    source.onmessage = (event) => {
        const msg = JSON.parse(event.data);

        switch (msg.type) {
            case 'queued':
                setRowStatus(row, 'Queued', 'queued');
                break;

            case 'progress':
                setRowStatus(row, `Transcribing ${msg.percent}%`, 'running');
                setRowProgress(row, msg.percent);
                break;

            // segment / summary 事件在此忽略：结果自动进历史，点“查看”看详情
            case 'segment':
            case 'summary':
                break;

            case 'done':
                setRowStatus(row, 'Done', 'done');
                setRowProgress(row, 100);
                addViewButton(actions, taskId);
                source.close();
                onTaskFinished();
                renderHistory();
                break;

            case 'error':
                setRowStatus(row, `${msg.message}`, 'error');
                source.close();
                onTaskFinished();
                break;
        }
    };

    source.onerror = () => {
        // SSE 会自动重连；只有队列里已经推完 done/error 才真正结束。
        // 这里不主动报错，避免瞬时断连误报。
    };
}

function addViewButton(actions, taskId) {
    if (actions.querySelector('.batch-view-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'btn-secondary btn-small batch-view-btn';
    btn.textContent = 'View';
    btn.addEventListener('click', () => openDetailView(taskId));
    actions.appendChild(btn);
}

function onTaskFinished() {
    batchFinished += 1;
    updateBatchProgress();
    if (batchFinished >= batchTotal) {
        resetSubmitBtn();
    }
}

function updateBatchProgress() {
    batchProgress.textContent = `${batchFinished} / ${batchTotal} done`;
}

// ========== Segment rendering (shared with detail view) ==========
function parseTimestampToSeconds(ts) {
    const parts = ts.split(':').map(Number);
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    return 0;
}

function setupTimeSync(player, container, stateObj) {
    player.addEventListener('timeupdate', () => {
        const currentTime = player.currentTime;
        const segs = container.querySelectorAll('.segment');
        if (segs.length === 0) return;

        let active = null;
        for (let i = segs.length - 1; i >= 0; i--) {
            if (currentTime >= parseFloat(segs[i].dataset.startSec)) {
                active = segs[i];
                break;
            }
        }

        if (active === stateObj.last) return;
        if (stateObj.last) stateObj.last.classList.remove('active');
        if (active) {
            active.classList.add('active');
            active.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
        stateObj.last = active;
    });
}

const detailSyncState = { last: null };
setupTimeSync(detailAudioPlayer, detailSegmentsContainer, detailSyncState);

function appendSegment(seg, container, player) {
    const div = document.createElement('div');
    div.className = 'segment';
    div.dataset.startSec = parseTimestampToSeconds(seg.timestamp);

    const ts = document.createElement('span');
    ts.className = 'timestamp clickable';
    ts.textContent = seg.timestamp;
    ts.title = 'Jump to this point';
    ts.addEventListener('click', () => {
        if (!player.src) return;
        player.currentTime = parseTimestampToSeconds(seg.timestamp);
        player.play();
    });

    const text = document.createElement('span');
    text.className = 'segment-text';
    text.textContent = seg.text;

    div.appendChild(ts);
    div.appendChild(text);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
}

function showError(message) {
    errorSection.classList.remove('hidden');
    errorText.textContent = message;
}

function renderSummary(section, overviewEl, sectionsEl, data) {
    if (!data || !data.overview) return;

    section.classList.remove('hidden');
    overviewEl.textContent = data.overview;
    sectionsEl.innerHTML = '';

    if (data.sections && data.sections.length > 0) {
        data.sections.forEach(sec => {
            const block = document.createElement('div');
            block.className = 'summary-section-item';

            const header = document.createElement('div');
            header.className = 'summary-section-header';

            const title = document.createElement('span');
            title.className = 'summary-section-title';
            title.textContent = sec.title;
            header.appendChild(title);

            if (sec.time_range) {
                const time = document.createElement('span');
                time.className = 'summary-section-time';
                time.textContent = sec.time_range;
                header.appendChild(time);
            }

            const desc = document.createElement('p');
            desc.className = 'summary-section-desc';
            desc.textContent = sec.summary;

            block.appendChild(header);
            block.appendChild(desc);
            sectionsEl.appendChild(block);
        });
    }
}

// ========== Copy & Download helpers ==========
function segmentLines(segs) {
    return segs.map(seg => `[${seg.timestamp}] ${seg.text}`);
}

// ========== Copy & Download (detail view) ==========
detailCopyBtn.addEventListener('click', () => {
    copyToClipboard(segmentLines(detailSegments).join('\n'));
});

detailDownloadBtn.addEventListener('click', () => {
    downloadFile(segmentLines(detailSegments).join('\n'),
        `transcription_${dateStr()}.txt`, 'text/plain;charset=utf-8');
    showToast('Downloaded TXT');
});

detailSrtBtn.addEventListener('click', () => {
    if (detailSegments.length === 0) return;
    downloadFile(buildSRT(detailSegments),
        `subtitles_${dateStr()}.srt`, 'text/plain;charset=utf-8');
    showToast('Downloaded SRT');
});

// ========== SRT ==========
function buildSRT(segments) {
    const lines = [];
    for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const startSec = parseTimestampToSeconds(seg.timestamp);
        const endSec = (i + 1 < segments.length)
            ? parseTimestampToSeconds(segments[i + 1].timestamp)
            : startSec + 5;
        lines.push(String(i + 1));
        lines.push(`${formatSRTTime(startSec)} --> ${formatSRTTime(endSec)}`);
        lines.push(seg.text);
        lines.push('');
    }
    return lines.join('\n');
}

function formatSRTTime(totalSeconds) {
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    return `${pad2(h)}:${pad2(m)}:${pad2(s)},000`;
}

function pad2(n) { return String(n).padStart(2, '0'); }

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast('Copied to clipboard');
    }).catch(() => {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('Copied to clipboard');
    });
}

function downloadFile(content, filename, mimeType) {
    const blob = new Blob([content], { type: mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function dateStr() {
    return new Date().toISOString().slice(0, 10);
}

// ========== History (server API) ==========
// 搜索 + 引擎筛选状态
let activeEngineFilter = '';
let activeSourceFilter = '';    // '' | 'mine' | 'pipeline'
let searchTimer = null;

async function renderHistory() {
    const query = (searchInput.value || '').trim();
    try {
        const url = query
            ? `/api/search?q=${encodeURIComponent(query)}`
            : '/api/history';
        const resp = await fetch(url);
        let entries = await resp.json();

        if (activeEngineFilter) {
            entries = entries.filter(e => e.engine === activeEngineFilter);
        }
        if (activeSourceFilter) {
            entries = entries.filter(e => (e.source || 'mine') === activeSourceFilter);
        }

        historyCount.textContent = entries.length ? `(${entries.length})` : '';

        if (entries.length === 0) {
            historyList.innerHTML = query
                ? '<p class="history-empty">No matching records</p>'
                : '<p class="history-empty">No transcripts yet</p>';
            return;
        }

        historyList.innerHTML = '';
        entries.forEach(entry => historyList.appendChild(buildHistoryCard(entry)));
    } catch {
        historyList.innerHTML = '<p class="history-empty">Failed to load history</p>';
    }
}

function buildHistoryCard(entry) {
    const item = document.createElement('div');
    item.className = 'history-item';

    const info = document.createElement('div');
    info.className = 'history-item-info';

    // 第一行：AI 标题（没有则退回文件名）+ 引擎徽章
    const titleRow = document.createElement('div');
    titleRow.className = 'history-item-title-row';

    const name = document.createElement('span');
    name.className = 'history-item-name';
    name.textContent = entry.ai_title || entry.filename;
    name.title = entry.filename;
    titleRow.appendChild(name);

    const badge = document.createElement('span');
    badge.className = `engine-badge engine-${entry.engine || 'unknown'}`;
    badge.textContent = engineLabel(entry.engine);
    titleRow.appendChild(badge);

    // Pipeline 跑出来的挂上博主名，一眼看出这条不是我自己传的
    if (entry.source === 'pipeline') {
        const src = document.createElement('span');
        src.className = 'source-badge';
        src.textContent = `🎬 ${entry.creator || 'Creators'}`;
        src.title = 'From a Creators pipeline run';
        titleRow.appendChild(src);
    }

    info.appendChild(titleRow);

    // 第二行：一句话简介（或搜索命中片段）
    const oneLineText = entry.snippet || entry.ai_one_line;
    if (oneLineText) {
        const oneLine = document.createElement('span');
        oneLine.className = entry.snippet
            ? 'history-item-oneline snippet' : 'history-item-oneline';
        oneLine.textContent = oneLineText;
        info.appendChild(oneLine);
    }

    // 第三行：标签 + 元信息
    const meta = document.createElement('span');
    meta.className = 'history-item-meta';
    const parts = [];
    (entry.ai_tags || []).forEach(t => parts.push(`#${t}`));
    parts.push(entry.date);
    if (entry.duration_seconds) parts.push(formatDuration(entry.duration_seconds));
    parts.push(`${entry.segment_count} segments`);
    if (entry.ai_title) parts.push(entry.filename);
    meta.textContent = parts.join(' · ');
    info.appendChild(meta);

    const actions = document.createElement('div');
    actions.className = 'history-item-actions';

    const viewBtn = document.createElement('button');
    viewBtn.className = 'btn-secondary btn-small';
    viewBtn.textContent = 'View';
    viewBtn.addEventListener('click', () => openDetailView(entry.id));

    const delBtn = document.createElement('button');
    delBtn.className = 'btn-secondary btn-small btn-danger';
    delBtn.textContent = 'Delete';
    delBtn.addEventListener('click', async () => {
        const label = entry.ai_title || entry.filename;
        if (!confirm(`Delete "${label}"? This cannot be undone.`)) return;
        try {
            const resp = await fetch(`/api/history/${entry.id}`, { method: 'DELETE' });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                showToast(data.error || 'Delete failed');
                return;
            }
            renderHistory();
            showToast('Deleted');
        } catch {
            showToast('Delete failed');
        }
    });

    actions.appendChild(viewBtn);
    actions.appendChild(delBtn);

    item.appendChild(info);
    item.appendChild(actions);
    return item;
}

// 搜索：输入防抖 300ms
searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(renderHistory, 300);
});

// 引擎筛选 chips
engineFilters.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    engineFilters.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    activeEngineFilter = chip.dataset.engine || '';
    renderHistory();
});

// 来源筛选 chips：我自己弄的 vs Creators 流水线跑的
const sourceFilters = document.getElementById('source-filters');
if (sourceFilters) sourceFilters.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    sourceFilters.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    chip.classList.add('active');
    activeSourceFilter = chip.dataset.source || '';
    renderHistory();
});

// ========== AI 整理（批量生成标题/标签） ==========
enrichBtn.addEventListener('click', async () => {
    enrichBtn.disabled = true;
    enrichBtn.textContent = 'Enriching…';
    try {
        await fetch('/api/enrich_all', { method: 'POST' });
        pollEnrichStatus();
    } catch {
        showToast('Failed to start auto-titling');
        resetEnrichBtn();
    }
});

function resetEnrichBtn() {
    enrichBtn.disabled = false;
    enrichBtn.textContent = 'Auto-title';
}

let enrichWasPolling = false;   // 后台标签暂停 enrich 轮询时的续跑标记

async function pollEnrichStatus() {
    try {
        const resp = await fetch('/api/enrich_status');
        const st = await resp.json();
        if (st.running) {
            enrichBtn.textContent = `${st.done}/${st.total}`;
            // 每整理完几条就刷新列表，让标题逐步冒出来
            if (st.done > 0 && st.done % 10 === 0) renderHistory();
            if (!document.hidden) setTimeout(pollEnrichStatus, 1500);
            else enrichWasPolling = true;      // 后台暂停，回前台再续
        } else {
            resetEnrichBtn();
            renderHistory();
            if (st.total > 0) {
                showToast(`Auto-title done: ${st.total - st.failed}/${st.total} succeeded`);
            } else {
                showToast('All records already have titles');
            }
        }
    } catch {
        resetEnrichBtn();
    }
}

// ========== Detail View ==========
async function openDetailView(taskId) {
    try {
        const resp = await fetch(`/api/history/${taskId}`);
        if (!resp.ok) throw new Error('Not found');
        const data = await resp.json();

        // 隐藏其它可能正在显示的顶层视图（可能是从链条详情/文档页点进来的）
        mainView.classList.add('hidden');
        if (typeof chainDetailView !== 'undefined' && chainDetailView) {
            chainDetailView.classList.add('hidden');
            clearTimeout(chainDetailTimer);
        }
        if (typeof docView !== 'undefined' && docView) docView.classList.add('hidden');
        detailView.classList.remove('hidden');
        window.scrollTo({ top: 0 });

        detailTitle.textContent = data.filename;
        const segCount = (data.segments || []).length;
        const parts = [data.date, engineLabel(data.engine)];
        if (data.duration_seconds) parts.push(formatDuration(data.duration_seconds));
        parts.push(`${segCount} segments`);
        detailMeta.textContent = parts.join(' · ');

        detailAudioPlayer.pause();
        detailAudioPlayer.currentTime = 0;
        detailAudioPlayer.src = `/api/history/${taskId}/audio`;
        detailPlayerSection.classList.remove('hidden');

        detailSegments = data.segments || [];
        detailSegmentsContainer.innerHTML = '';
        detailSyncState.last = null;
        detailSegments.forEach(seg => {
            appendSegment(seg, detailSegmentsContainer, detailAudioPlayer);
        });

        if (data.summary && data.summary.overview) {
            renderSummary(detailSummarySection, detailSummaryOverview,
                detailSummarySections, data.summary);
        } else {
            detailSummarySection.classList.add('hidden');
        }
    } catch {
        showToast('Could not load record');
    }
}

function closeDetailView() {
    detailView.classList.add('hidden');
    mainView.classList.remove('hidden');
    detailAudioPlayer.pause();
    detailAudioPlayer.src = '';
    detailSegmentsContainer.innerHTML = '';
    detailSummaryOverview.textContent = '';
    detailSummarySections.innerHTML = '';
    detailSummarySection.classList.add('hidden');
    detailSegments = [];
    detailSyncState.last = null;
    window.scrollTo({ top: 0 });
}

detailBackBtn.addEventListener('click', closeDetailView);

clearHistoryBtn.addEventListener('click', async () => {
    if (!confirm('Clear all history? This deletes every saved audio file and transcript.')) return;

    try {
        const resp = await fetch('/api/history', { method: 'DELETE' });
        const data = await resp.json();
        if (!resp.ok) {
            showToast(data.error || 'Clear failed');
            return;
        }
        renderHistory();
        let msg = `Cleared ${data.deleted || 0}`;
        if (data.skipped) msg += ` (${data.skipped} in-progress skipped)`;
        showToast(msg);
    } catch {
        showToast('Clear failed');
    }
});

// ========== Toast ==========
function showToast(message) {
    const existing = document.querySelector('.toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 2000);
}

// ========== Init ==========
renderHistory();
renderIntake();   // 初始化提交按钮状态（0 条 → 禁用）

// ========== 链条：URL → 下载 → 转写 → 分析 → 总合成 ==========
const chainUrl = document.getElementById('chain-url');
const chainAuthor = document.getElementById('chain-author');
const chainMax = document.getElementById('chain-max');
const chainEngine = document.getElementById('chain-engine');
const chainPreferSubs = document.getElementById('chain-prefer-subs');
const chainVerify = document.getElementById('chain-verify');
const chainSelfVerify = document.getElementById('chain-self-verify');
const chainFallbackWhisper = document.getElementById('chain-fallback-whisper');
const chainCritique = document.getElementById('chain-critique');
const chainProvider = document.getElementById('chain-provider');
const chainStartBtn = document.getElementById('chain-start');

// 分析模型的用户可读名（analysis_preset 是内部字段，展示层别裸露）
const BRAIN_LABELS = { gemini: 'Gemini', deepseek: 'DeepSeek', kimi: 'Kimi',
    glm: 'GLM', qwen: 'Qwen',
    opus5: 'Claude Opus 5', opus46: 'Claude Opus 4.6' };
function brainLabel(preset) {
    return BRAIN_LABELS[preset || 'gemini'] || preset;
}

const CHAIN_STAGE_LABELS = {
    starting: 'Starting',
    downloading: 'Downloading',
    transcribing: 'Transcribing',
    analyzing: 'Analyzing',
    synthesizing: 'Synthesizing',
    done: 'Done',
    failed: 'Failed',
    cancelled: 'Stopped',
};

let chainPollTimer = null;

let chainSubmitting = false;
chainStartBtn.addEventListener('click', async () => {
    if (chainSubmitting) return;               // 防连点重复建链（每条都烧钱）
    const url = (chainUrl.value || '').trim();
    if (!url) { chainUrl.focus(); return; }
    // 花钱确认：分析/合成会按视频数调用付费模型
    {
        const extra = chainVerify.checked ? '\n+ Web fact-check uses extra Google Search quota.' : '';
        if (!confirm('This analyzes every video of the creator + synthesizes one portrait, spending paid model quota that scales with video count (can add up).'
            + extra + '\n\nJust want the transcript of one or a few videos? Cancel and use the Transcribe tab instead.\n\nContinue?')) {
            return;
        }
    }
    chainSubmitting = true;
    chainStartBtn.disabled = true;
    try {
        const resp = await fetch('/api/chain', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url,
                author: chainAuthor.value.trim(),
                max_videos: parseInt(chainMax.value, 10) || 0,
                engine: chainEngine.value,
                analyze: true,
                prefer_subs: chainPreferSubs.checked,
                fallback_whisper: chainFallbackWhisper.checked,
                verify: chainVerify.checked,
                self_verify: chainSelfVerify.checked,
                lang: (document.getElementById('chain-lang') || {}).value || 'auto',
                critique_level: chainCritique.value,
                analysis_preset: chainProvider.value,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Failed to create');
        chainUrl.value = '';
        loadChains();
    } catch (err) {
        alert('Failed to start the analysis: ' + err.message);
    } finally {
        chainSubmitting = false;
        chainStartBtn.disabled = false;
    }
});

function chainProgressText(c) {
    const vids = c.videos || [];
    const by = s => vids.filter(v => v.status === s).length;
    const parts = [];

    if (c.download_total) {
        const f = by('download_failed');
        parts.push(`Downloaded ${c.download_done || 0}/${c.download_total}` +
            (f ? ` (${f} failed)` : ''));
    }

    // 转写：显示已完成 + 正在转写 + 失败，让进度"会动"（不只在转完才 +1）
    const submitted = vids.filter(v => v.status !== 'download_failed');
    const done = by('done'), transcribing = by('transcribing'), failed = by('failed');
    if (submitted.length && (done || transcribing || failed || c.stage !== 'downloading')) {
        const extra = [];
        if (transcribing) extra.push(`${transcribing} in progress`);
        if (failed) extra.push(`${failed} failed`);
        parts.push(`Transcribed ${done}/${submitted.length}` +
            (extra.length ? ` (${extra.join(', ')})` : ''));
    }

    if (c.analyze && c.analyzed_done != null && submitted.length) {
        parts.push(`Analyzed ${c.analyzed_done}/${submitted.length}`);
    }
    return parts.join(' · ');
}

// ===== URL 归一化：仅用于卡片归组比较，不改提交逻辑和存储 =====
// 去名单方式：只删已知跟踪参数，其余参数一律保留 ——
// YouTube 的 watch?v= / playlist?list= 是内容标识，误删会把不同内容合成一张卡。
const TRACKING_PARAMS = new Set([
    // Bilibili 分享链接
    'share_source', 'share_medium', 'share_plat', 'share_session_id',
    'share_tag', 'share_from', 'share_times', 'unique_k',
    'vd_source', 'from_spmid', 'spm_id_from', 'spm', 'from_source',
    'buvid', 'trackid', 'plat_id', 'is_story_h5', '-arouter',
    // YouTube 分享链接
    'si', 'feature',
    // 通用
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
]);

function normalizeChainUrl(raw) {
    try {
        const u = new URL(String(raw || '').trim());
        const kept = [...u.searchParams.entries()]
            .filter(([k]) => !TRACKING_PARAMS.has(k.toLowerCase()));
        kept.sort((a, b) => a[0].localeCompare(b[0]) || a[1].localeCompare(b[1]));
        const q = kept.map(([k, v]) => `${k}=${v}`).join('&');
        return u.hostname.toLowerCase().replace(/^www\./, '')
            + u.pathname.replace(/\/+$/, '')
            + (q ? '?' + q : '');
    } catch {
        return String(raw || '').trim();
    }
}

// 进行中卡片的细进度条：按 下载/转写/分析 三步的完成数粗估百分比（纯展示）
function chainPercent(c) {
    const vids = c.videos || [];
    const total = c.download_total || vids.length;
    if (!total) return 2;
    const steps = c.analyze === false ? 2 : 3;
    const done = vids.filter(v => v.status === 'done').length;
    const num = (c.download_done || 0) + done
        + (c.analyze === false ? 0 : (c.analyzed_done || 0));
    return Math.max(2, Math.min(99, Math.round(num / (total * steps) * 100)));
}

// 一条 chain → 一张卡。四态：进行中（进度） / 完成（现状不变） / 失败（错误+Retry）
// / 停止或中断（Stopped+Continue，别让旧数据从界面消失）。
function buildChainCard(c) {
    const active = !['done', 'failed', 'cancelled'].includes(c.stage);
    const author = (c.author && c.author !== '该博主') ? c.author : (c.url || 'Creator');
    const vids = c.videos || [];
    const img = c.avatar || (vids.find(v => v.thumbnail) || {}).thumbnail || '';
    const thumb = img
        ? `<div class="creator-thumb" style="background-image:url('${escapeHtml(img).replace(/[()'"\\]/g, '')}')"></div>`
        : `<div class="creator-thumb creator-noimg">▷</div>`;
    const name = `<div class="creator-name">${escapeHtml(String(author).slice(0, 60))}</div>`;

    let body;
    if (active) {
        const prog = chainProgressText(c) || (CHAIN_STAGE_LABELS[c.stage] || c.stage);
        body = `<div class="creator-meta">${escapeHtml(prog)}</div>
            <div class="creator-progressbar"><i style="width:${chainPercent(c)}%"></i></div>`
            + (c.current ? `<div class="creator-current">${escapeHtml(c.current.slice(0, 60))}</div>` : '');
    } else if (c.stage === 'done' && c.final_doc) {
        const nEp = vids.filter(v => v.status === 'done').length || vids.length;
        body = `<div class="creator-meta">${nEp} episode${nEp === 1 ? '' : 's'} · ${brainLabel(c.analysis_preset)}</div>`;
    } else if (c.stage === 'failed') {
        body = `<div class="creator-meta creator-error">⚠ ${escapeHtml(String(c.error || 'Failed').slice(0, 90))}</div>
            <button class="btn-secondary btn-small creator-retry"
                onclick="continueChain('${c.id}', event)">Retry</button>`;
    } else {
        // cancelled，或 done 但没产出画像（中断/未合成）
        const prog = chainProgressText(c);
        body = `<div class="creator-meta">Stopped${prog ? ' · ' + escapeHtml(prog) : ''}</div>
            <button class="btn-secondary btn-small creator-retry"
                onclick="continueChain('${c.id}', event)">Continue</button>`;
    }
    return `<div class="creator-card ${active ? 'creator-running' : ''}"
        onclick="openChainDetail('${c.id}')" title="${escapeHtml(author)}">
        ${thumb}<div class="creator-body">${name}${body}</div></div>`;
}

function renderChains(chains) {
    const grid = document.getElementById('creators-grid');
    const empty = document.getElementById('creators-empty');
    if (!grid) return;
    // 同一 URL（去跟踪参数后）只留最新一条：卡片代表"这个博主"，显示最新一次分析；
    // 旧 run 的文档仍在 Library → Analyses。/api/chains 已按 created_at 倒序。
    const seen = new Set();
    const latest = [];
    for (const c of chains) {
        const key = normalizeChainUrl(c.url);
        if (seen.has(key)) continue;
        seen.add(key);
        latest.push(c);
    }
    if (empty) empty.classList.toggle('hidden', latest.length > 0);
    grid.innerHTML = latest.map(buildChainCard).join('');
}

// ========== 链条详情：视频封面网格 + 每个视频状态 ==========
const chainDetailView = document.getElementById('chain-detail-view');
const chainDetailBack = document.getElementById('chain-detail-back');
const chainDetailTitle = document.getElementById('chain-detail-title');
const chainDetailMeta = document.getElementById('chain-detail-meta');
const chainDetailGrid = document.getElementById('chain-detail-grid');

const VIDEO_STATUS = {
    downloading: { label: 'Downloading', cls: 'vs-active' },
    transcribing: { label: 'Transcribing', cls: 'vs-active' },
    pending: { label: 'Queued', cls: 'vs-active' },
    done: { label: 'Transcribed', cls: 'vs-done' },
    failed: { label: 'Failed', cls: 'vs-fail' },
    download_failed: { label: 'Download failed', cls: 'vs-fail' },
};

let chainDetailId = null;
let chainDetailTimer = null;

async function openChainDetail(id) {
    chainDetailId = id;
    mainView.classList.add('hidden');
    chainDetailView.classList.remove('hidden');
    // 每次打开先收起分集网格：先看设置/模型/进度，要看每期再展开
    chainDetailGrid.classList.add('hidden');
    const tg = document.getElementById('chain-episodes-toggle');
    if (tg) tg.classList.remove('open');
    window.scrollTo({ top: 0 });
    renderMergeBar();   // 购物车里有跨博主选的期 → 进来就显操作条
    await refreshChainDetail();
}

// Episodes 折叠开关
const episodesToggle = document.getElementById('chain-episodes-toggle');
if (episodesToggle) episodesToggle.addEventListener('click', () => {
    const open = chainDetailGrid.classList.toggle('hidden');
    episodesToggle.classList.toggle('open', !open);
});

async function refreshChainDetail() {
    if (!chainDetailId) return;
    let c;
    try {
        const resp = await fetch(`/api/chain/${chainDetailId}`);
        if (!resp.ok) throw new Error('not found');
        c = await resp.json();
    } catch {
        chainDetailGrid.innerHTML = '<p class="history-empty">Could not load</p>';
        return;
    }
    chainDetailTitle.textContent = 'Creators';   // 顶栏只当面包屑，名字在下面的封面里
    chainDetailMeta.textContent = '';          // 卡片已含状态，别重复这行灰字

    const vids = c.videos || [];
    const chainTerminal = ['done', 'failed', 'cancelled'].includes(c.stage);

    // ===== Info 面板：设置 / 模型（含降级留痕）/ 操作 =====
    const onoff = b => b ? 'on' : 'off';
    const fell = vids.filter(v => v.status === 'done' && v.engine_used
        && v.engine_used !== c.engine).length;
    const fellNote = fell
        ? `<div class="cd-alert">⚠ ${fell} episode(s) fell back to a different engine
            (cloud failed → actual engine recorded per episode)</div>` : '';
    const err = c.error
        ? `<div class="cd-alert">⚠ ${String(c.error).replace(/</g, '&lt;').slice(0, 180)}</div>` : '';
    const actions = chainTerminal
        ? `<button class="btn-primary ci-btn" onclick="continueChain('${c.id}')">Continue</button>
           <button class="btn-secondary ci-btn" onclick="reanalyzeChain('${c.id}')">Re-analyze</button>
           <button class="btn-secondary ci-btn" onclick="closeChainDetail();gotoDocs()">Episode docs</button>
           <button class="btn-secondary ci-btn btn-danger" onclick="deleteChain('${c.id}', true)">Delete</button>
           <span class="ci-hint">Continue = fill whatever is missing (reuses everything done).
           Re-analyze = redo analysis only, with the Creators form's brain/level settings.</span>`
        : `<button class="btn-secondary ci-btn" onclick="stopChain('${c.id}')">Stop</button>`;
    // ===== 布局原则：主角是「这个博主 + 读他的解读」；运维细节全部折叠 =====
    const author = (c.author && c.author !== '该博主') ? c.author : '';
    const doneN = vids.filter(v => v.status === 'done').length;
    // 主 CTA：读画像 / 合并原文（核心内容，做大）
    let ctas = '';
    if (c.final_doc) ctas += `<button class="btn-primary cd-cta"
        onclick="openDocView('${c.id}','${encodeURIComponent(c.final_doc)}')">📖 Report</button>`;
    if (c.raw_doc) ctas += `<button class="btn-secondary cd-cta"
        onclick="openDocView('${c.id}','${encodeURIComponent(c.raw_doc)}')">📜 Full transcript</button>`;
    // 镜头：核心动作，大 chip
    const LENSES = [['roast', '🔥 Roast'], ['craft', '✍️ Craft'],
        ['fun', '😂 Watchability'], ['quotes', '💬 Quotes'], ['worldview', '🗺 Worldview']];
    const lensBlock = (chainTerminal && c.analyze !== false)
        ? `<div class="cd-lens-title">Read him through a lens <span class="ci-hint">same evidence cards, different angle — nearly free</span></div>
           <div class="cd-lens-row">`
          + LENSES.map(([k, label]) =>
              `<button class="lens-btn" onclick="runLens('${c.id}','${k}')">${label}</button>`).join('')
          + `</div>`
        : '';
    // 运维细节 + 次要操作：折叠（活跃时展开显进度）
    const opsOpen = chainTerminal ? '' : ' open';
    // 真头像（取不到/加载失败 → 名字首字的珊瑚章）+ 真数据条
    const ch = (author || c.url || '?').trim().slice(0, 1) || '?';
    const avatarHtml = `<div class="cd-avatar">${ch}${c.avatar
        ? `<img class="cd-avatar-img" src="${escapeHtml(c.avatar || '')}" alt="" onerror="this.remove()">`
        : ''}</div>`;
    const totalViews = vids.reduce((s, v) => s + (v.view_count || 0), 0);
    const stats = [`<div class="cd-stat"><div class="n">${doneN}</div><div class="l">episodes read</div></div>`];
    if (c.followers) stats.push(`<div class="cd-stat"><div class="n">${fmtCount(c.followers)}</div><div class="l">followers</div></div>`);
    if (totalViews) stats.push(`<div class="cd-stat"><div class="n">${fmtCount(totalViews)}</div><div class="l">total plays</div></div>`);
    stats.push(`<div class="cd-stat"><div class="n">${brainLabel(c.analysis_preset)}</div><div class="l">analysis model</div></div>`);
    document.getElementById('chain-detail-info').innerHTML = `
        <div class="cd-cover">
            <div class="cd-cover-top">
                ${avatarHtml}
                <div class="cd-id">
                    <div class="cd-eyebrow">CREATOR READ · ${doneN} EPISODES</div>
                    <div class="cd-name">${escapeHtml((author || c.url || 'Creator').slice(0, 60))}</div>
                    <div class="cd-sub">${CHAIN_STAGE_LABELS[c.stage] || c.stage}${c.finished_at ? ' · ' + c.finished_at : ''}</div>
                </div>
            </div>
            <div class="cd-stats">${stats.join('')}</div>
            <div class="cd-body">
                <div class="cd-ctas">${ctas}</div>
                ${lensBlock}
            </div>
        </div>
        ${fellNote}
        <details class="chain-ops"${opsOpen}>
            <summary>⚙ Run details & actions${err ? ' · <span class="ops-flag">has errors</span>' : ''}</summary>
            <div class="chain-info">
                ${err}
                <div class="ci-row"><span class="ci-k">Source</span>
                    <span class="ci-v"><a href="${safeUrl(c.url)}" target="_blank" rel="noopener">${escapeHtml((c.url || '').slice(0, 80))}</a></span></div>
                <div class="ci-row"><span class="ci-k">Settings</span>
                    <span class="ci-v">engine <b>${c.engine || '-'}</b> · analyze <b>${onoff(c.analyze)}</b>
                    · model <b>${brainLabel(c.analysis_preset)}</b> · level <b>${c.critique_level || 'analytical'}</b>
                    · subs-first <b>${onoff(c.prefer_subs)}</b> · web-verify <b>${onoff(c.verify)}</b>
                    · self-verify <b>${onoff(c.self_verify)}</b>
                    · whisper-fallback <b>${onoff(c.fallback_whisper)}</b></span></div>
                <div class="ci-row"><span class="ci-k">Progress</span>
                    <span class="ci-v">${chainProgressText(c)}</span></div>
                <div class="ci-actions">${actions}</div>
            </div>
        </details>`;

    document.getElementById('chain-episodes-count').textContent = `(${vids.length})`;

    chainDetailGrid.innerHTML = vids.map((v, idx) => {
        const st = VIDEO_STATUS[v.status] || { label: v.status, cls: '' };
        const clickable = v.status === 'done' && v.task_id;
        const thumb = v.thumbnail
            ? `<img class="vg-thumb" src="${v.thumbnail}" loading="lazy" alt=""
                 onerror="this.style.display='none'">`
            : '<div class="vg-thumb vg-noimg">▷</div>';
        // 转写中且有进度 → 封面上盖珊瑚半透明板 + 大号百分比
        const pct = (v.status === 'transcribing' && typeof v.progress === 'number')
            ? v.progress : null;
        const overlay = pct != null
            ? `<div class="vg-prog" style="--p:${pct}%"><span>${pct}%</span></div>` : '';
        const onclick = clickable
            ? ` onclick="openDetailView('${v.task_id}')" title="View transcript"` : '';
        // 降级留痕：这期实际用的引擎和链条引擎不同（如 gemini 链落了 whisper）
        const engBadge = (v.engine_used && v.engine_used !== c.engine)
            ? `<span class="vg-eng" title="cloud engine failed; actually transcribed with ${v.engine_used}">${v.engine_used}</span>` : '';
        // 只有完成的分集能加入合并购物车；勾选框吞掉点击，不触发打开转写
        const pick = clickable
            ? `<label class="vg-pick" onclick="event.stopPropagation()" title="Add to merge">
                 <input type="checkbox" ${mergeCart.has(v.task_id) ? 'checked' : ''}
                   onchange="toggleMergePick('${v.task_id}', this.checked)">
               </label>` : '';
        return `<div class="vg-card ${clickable ? 'vg-clickable' : ''} ${mergeCart.has(v.task_id) ? 'vg-picked' : ''}"${onclick}>
            <div class="vg-thumb-wrap">${thumb}${overlay}${pick}</div>
            <div class="vg-badge ${st.cls}">${st.label}${engBadge}</div>
            <div class="vg-title" title="${escapeHtml(v.title || '')}">${escapeHtml(v.title || '')}</div>
        </div>`;
    }).join('') || '<p class="history-empty">Resolving episode list…</p>';

    clearTimeout(chainDetailTimer);
    // 链条在跑、或有单个视频在重转中 → 继续轮询刷新
    const anyBusy = vids.some(v => ['downloading', 'transcribing'].includes(v.status));
    if ((!chainTerminal || anyBusy) && !document.hidden) {
        chainDetailTimer = setTimeout(refreshChainDetail, 4000);
    }
}

function closeChainDetail() {
    clearTimeout(chainDetailTimer);
    chainDetailId = null;
    chainDetailView.classList.add('hidden');
    mainView.classList.remove('hidden');
    window.scrollTo({ top: 0 });
}

// ========== 合并转写「购物车」：跨博主挑期，按 task_id 攒着 ==========
// 选择单位是单期转写，与博主解耦；切博主不清空，最后一起合并成一份纯文本。
const mergeCart = new Set();

function toggleMergePick(taskId, checked) {
    if (checked) mergeCart.add(taskId); else mergeCart.delete(taskId);
    // 同步卡片高亮（重绘时也会带上 vg-picked）
    const box = document.querySelector(`.vg-pick input[onchange*="${taskId}"]`);
    if (box) box.closest('.vg-card')?.classList.toggle('vg-picked', checked);
    renderMergeBar();
}

function renderMergeBar() {
    let bar = document.getElementById('merge-bar');
    if (!mergeCart.size) { if (bar) bar.remove(); return; }
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'merge-bar';
        bar.className = 'merge-bar';
        document.body.appendChild(bar);
    }
    const n = mergeCart.size;
    bar.innerHTML = `
        <span class="merge-bar-count">${n} transcript${n === 1 ? '' : 's'} selected</span>
        <button class="btn-secondary btn-small" onclick="clearMergeCart()">Clear</button>
        <button class="btn-primary btn-small" onclick="runMergeTranscripts()">📄 Merge</button>`;
}

function clearMergeCart() {
    mergeCart.clear();
    document.querySelectorAll('.vg-card.vg-picked').forEach(c => {
        c.classList.remove('vg-picked');
        const box = c.querySelector('.vg-pick input');
        if (box) box.checked = false;
    });
    renderMergeBar();
}

async function runMergeTranscripts() {
    if (!mergeCart.size) return;
    const btn = document.querySelector('#merge-bar .btn-primary');
    if (btn) { btn.disabled = true; btn.textContent = 'Merging…'; }
    try {
        const r = await (await fetch('/api/transcripts/merge', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task_ids: [...mergeCart] }),
        })).json();
        if (r.error) { alert(r.error); return; }
        // 复用现成的内存文档视图（和小红书报告同一条路）
        currentDoc = { chainId: null, name: r.filename || '合并转写.md', raw: r.markdown };
        docTitle.textContent = `Merged transcript · ${r.count} episode${r.count === 1 ? '' : 's'}`;
        docContent.innerHTML = renderMarkdown(r.markdown);
        docReturnTo = (chainDetailView && !chainDetailView.classList.contains('hidden'))
            ? 'chainDetail' : 'main';
        mainView.classList.add('hidden');
        if (chainDetailView) chainDetailView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
        if (r.missing) showToast(`${r.missing} skipped (no transcript text)`);
    } catch {
        alert('Merge failed');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📄 Merge'; }
    }
}

if (chainDetailBack) chainDetailBack.addEventListener('click', closeChainDetail);

async function loadChains() {
    try {
        const resp = await fetch('/api/chains');
        const chains = await resp.json();
        renderChains(chains);
        updateGlobalIndicator('chains', chains
            .filter(c => !['done', 'failed', 'cancelled'].includes(c.stage))
            .map(c => ({
                label: (c.author && c.author !== '该博主') ? c.author : c.url,
                progress: chainProgressText(c) || (CHAIN_STAGE_LABELS[c.stage] || c.stage),
                tab: 'creators',
                chainId: c.id,
            })));
        const anyActive = chains.some(c => !['done', 'failed', 'cancelled'].includes(c.stage));
        clearTimeout(chainPollTimer);
        // 只在有活跃链条、且标签页在前台时才继续轮询：
        // 后台标签不空转；也不再每 4 秒全量重绘历史（几百条卡片重绘会烧满渲染进程）。
        if (anyActive && !document.hidden) {
            chainPollTimer = setTimeout(loadChains, 4000);
        }
    } catch (e) { /* 服务重启瞬间的抖动，忽略 */ }
}

// 标签页切到后台：停掉所有轮询，别在后台烧电；切回前台再恢复。
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearTimeout(chainPollTimer);
        clearTimeout(chainDetailTimer);
        clearTimeout(xhsTimer);
        clearTimeout(xhsAnTimer);
    } else {
        loadChains();
        if (chainDetailId) refreshChainDetail();
        if (enrichWasPolling) { enrichWasPolling = false; pollEnrichStatus(); }
        // XHS：仅在上次已知有活跃任务时恢复（各自查一次，idle 就地停，不空转）
        if (activeTasks['xhs-scrape'].length) pollXhs();
        if (activeTasks['xhs-analyze'].length) pollXhsAnalyze();
    }
});

// 跳到「资料库 → 分析文档」子分区
function gotoDocs() {
    switchTab('library');
    switchLib('docs');
}

// Continue：补全一切缺失——已完成的复用，没下的下，转写失败的（音频在就直接重转、
// 云引擎失败自动落 Whisper），最后补分析 + 合成。用上面表单的引擎/分析大脑设置。
async function continueChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    if (!confirm('Continue this analysis?\nReuses everything finished; only fills gaps: downloads what is missing, re-transcribes failures, then completes analysis + synthesis, using the settings in the Creators form.\n\nNote: videos content-blocked by Gemini (RECITATION/safety) auto-fall-back to local Whisper; other failures only fall back if Whisper fallback is checked.')) return;
    try {
        const r = await (await fetch(`/api/chain/${chainId}/retry`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ engine: chainEngine.value, analysis_preset: chainProvider.value }),
        })).json();
        if (!r.ok) { alert(r.error || 'Could not continue'); }
    } catch { alert('Could not continue'); }
    loadChains();
}

async function stopChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    if (!confirm('Stop this analysis? Finished transcripts & analyses are kept; it just stops going further.')) return;
    try { await fetch(`/api/chain/${chainId}/stop`, { method: 'POST' }); } catch { /* ignore */ }
    loadChains();
}

async function deleteChain(chainId, fromDetail) {
    if (!confirm('Delete this creator analysis’s documents? (transcripts stay in the library)')) return;
    await fetch(`/api/chain/${chainId}`, { method: 'DELETE' });
    if (fromDetail) closeChainDetail();
    loadChains();
}

// Re-analyze：只对已有转写重跑分析 + 合成（不碰转写）。用上面表单的分析大脑 / 档位 / 核实。
async function reanalyzeChain(chainId, ev) {
    if (ev) ev.stopPropagation();
    const verify = chainVerify.checked;
    const selfVerify = chainSelfVerify.checked;
    if (!confirm('Re-analyze: rerun AI analysis on every transcribed video'
        + (verify ? ' + web fact-check (extra search quota)' : '')
        + (selfVerify ? ' + self-verify (extra calls)' : '')
        + ', using the analysis model selected in the Creators form. Transcripts untouched. Continue?')) return;
    try {
        const r = await (await fetch(`/api/chain/${chainId}/reanalyze`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ verify, self_verify: selfVerify,
                                   lang: (document.getElementById('chain-lang') || {}).value || 'auto',
                                   critique_level: chainCritique.value,
                                   analysis_preset: chainProvider.value }),
        })).json();
        if (!r.ok) { alert(r.error || 'Could not start re-analysis'); }
    } catch { alert('Could not start re-analysis'); }
    loadChains();   // stage 变 analyzing → 轮询自动接管显示进度
}

loadChains();

// ========== Tab 切换（转写 / 链条 / 资料库） ==========
const navTabs = document.querySelectorAll('.nav-tab');
const tabPanels = {
    transcribe: document.getElementById('tab-transcribe'),
    xhs: document.getElementById('tab-xhs'),
    creators: document.getElementById('tab-creators'),
    library: document.getElementById('tab-library'),
};

function switchTab(name) {
    navTabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    Object.entries(tabPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'library') { renderHistory(); }
    if (name === 'creators') { loadChains(); }
    if (name === 'xhs') { pollXhs(); pollXhsAnalyze(); }
}

// ========== 小红书采集 ==========
let xhsTimer = null;
async function pollXhs() {
    const box = document.getElementById('xhs-progress');
    if (!box) return;
    let s;
    try { s = await (await fetch('/api/xhs/status')).json(); }
    catch { return; }
    clearTimeout(xhsTimer);
    updateGlobalIndicator('xhs-scrape', s.running
        ? [{ label: s.kw || 'Scraping notes', progress: `${s.scraped} scraped`, tab: 'xhs' }]
        : []);
    if (!s.running && !s.log?.length) { box.classList.add('hidden'); return; }
    box.classList.remove('hidden');
    const dot = s.running ? '<span class="xhs-live">● Scraping</span>' : '<span class="xhs-done">✓ Stopped</span>';
    const stopBtn = s.running ? '<button class="lens-btn" onclick="stopXhs()">■ Stop</button>' : '';
    box.innerHTML = `
        <div class="xhs-head">${dot}
            <span>scraped <b>${s.scraped}</b> this run · ${s.total} in dataset${s.kw ? ' · ' + escapeHtml(s.kw) : ''}</span>
            ${stopBtn}</div>
        <pre class="xhs-log">${(s.log || []).map(l => escapeHtml(l)).join('\n')}</pre>`;
    const startBtn = document.getElementById('xhs-start');
    if (startBtn) startBtn.disabled = s.running;
    if (s.running && !document.hidden) xhsTimer = setTimeout(pollXhs, 3000);
}

async function stopXhs() {
    if (!confirm('Stop the scrape? Notes already saved are kept — only the rest is skipped.')) return;
    try {
        const r = await (await fetch('/api/xhs/stop', { method: 'POST' })).json();
        if (!r.ok) alert(r.error || 'Could not stop');
    } catch { alert('Could not stop'); }
    setTimeout(pollXhs, 500);
}

// —— 分析：逐篇读图+评论 → 聚合报告 ——
let xhsAnTimer = null;
async function pollXhsAnalyze() {
    const box = document.getElementById('xhs-analyze-progress');
    const btn = document.getElementById('xhs-analyze-btn');
    if (!box) return;
    let s;
    try { s = await (await fetch('/api/xhs/analyze_status')).json(); }
    catch { return; }
    clearTimeout(xhsAnTimer);
    updateGlobalIndicator('xhs-analyze', s.running
        ? [{ label: 'Notes analysis', progress: `${s.done}/${s.total} notes`, tab: 'xhs' }]
        : []);
    if (s.running) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-live">● Analyzing</span> read <b>${s.done}</b>/${s.total} notes… (images + comments)`;
        if (btn) btn.disabled = true;
        if (!document.hidden) xhsAnTimer = setTimeout(pollXhsAnalyze, 2000);
        return;
    }
    if (btn) btn.disabled = false;
    if (s.error) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-done">Analysis failed: ${String(s.error).replace(/</g, '&lt;')}</span>`;
    } else if (s.has_report) {
        box.classList.remove('hidden');
        box.innerHTML = `<span class="xhs-done">✓ Report ready</span>
            <button class="lens-btn" onclick="openXhsReport()">📖 Read report</button>`;
    } else {
        box.classList.add('hidden');
    }
}
async function openXhsReport() {
    try {
        const r = await (await fetch('/api/xhs/report')).json();
        if (r.markdown) showXhsReport(r.markdown); else alert(r.error || 'No report yet');
    } catch { alert('Could not load the report'); }
}
function showXhsReport(md) {
    currentDoc = { chainId: null, name: '小红书报告.md', raw: md };
    docTitle.textContent = 'Xiaohongshu research report';
    docContent.innerHTML = renderMarkdown(md);
    docReturnTo = 'main';
    mainView.classList.add('hidden');
    if (chainDetailView) chainDetailView.classList.add('hidden');
    docView.classList.remove('hidden');
    window.scrollTo({ top: 0 });
}
const xhsAnalyzeBtn = document.getElementById('xhs-analyze-btn');
if (xhsAnalyzeBtn) xhsAnalyzeBtn.addEventListener('click', async () => {
    const keywords = (document.getElementById('xhs-keywords').value || '').trim();
    const scopeMsg = keywords
        ? 'Only notes scraped by the keywords above will be analyzed (topics never mix).'
        : '⚠ Keywords empty = analyze the WHOLE dataset (topics will mix).';
    if (!confirm(scopeMsg + '\nReads images + comments per note, then aggregates into a report. Gemini cost scales with note count. Continue?')) return;
    xhsAnalyzeBtn.disabled = true;
    try {
        const r = await (await fetch('/api/xhs/analyze', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keywords,
                lang: (document.getElementById('xhs-lang') || {}).value || 'auto' }),
        })).json();
        if (!r.ok) { alert(r.error || 'Failed to start'); xhsAnalyzeBtn.disabled = false; return; }
        pollXhsAnalyze();
    } catch { alert('Failed to start'); xhsAnalyzeBtn.disabled = false; }
});

const xhsStartBtn = document.getElementById('xhs-start');
if (xhsStartBtn) xhsStartBtn.addEventListener('click', async () => {
    const keywords = document.getElementById('xhs-keywords').value.trim();
    if (!keywords) { document.getElementById('xhs-keywords').focus(); return; }
    if (!confirm('Start scraping? A browser window will pop up (first run asks for a Xiaohongshu QR login).\n'
        + 'High volume risks rate-limiting — keep batches small. Continue?')) return;
    xhsStartBtn.disabled = true;
    try {
        const r = await (await fetch('/api/xhs/scrape', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                keywords,
                max_notes: parseInt(document.getElementById('xhs-max-notes').value, 10) || 20,
                max_comments: parseInt(document.getElementById('xhs-max-comments').value, 10) || 400,
            }),
        })).json();
        if (!r.ok) { alert(r.error || 'Failed to start'); xhsStartBtn.disabled = false; return; }
        pollXhs();
    } catch { alert('Failed to start'); xhsStartBtn.disabled = false; }
});

// 页面加载各查一次：刷新页面时若有爬取/分析仍在跑（服务端进行中），
// 不用点进 XHS tab 也能接上轮询、点亮全局指示器；idle 则就地停，不是常驻轮询。
pollXhs();
pollXhsAnalyze();

// ========== 全局任务指示器（顶栏 ⟳N + popover）==========
// 极简聚合，不新建任何轮询：三处现有刷新（setRowStatus / loadChains / pollXhs*）
// 每次拿到新数据后调 updateGlobalIndicator(source, tasks) 更新这里并重绘。
const activeTasks = { transcribe: [], chains: [], 'xhs-scrape': [], 'xhs-analyze': [] };
const GT_SOURCE_LABEL = { transcribe: 'Transcribe', chains: 'Creator',
    'xhs-scrape': 'Xiaohongshu', 'xhs-analyze': 'Xiaohongshu' };
const gtBtn = document.getElementById('global-tasks');
const gtCount = document.getElementById('global-tasks-count');
const gtPop = document.getElementById('global-tasks-pop');

function updateGlobalIndicator(source, tasks) {
    activeTasks[source] = tasks || [];
    const total = Object.values(activeTasks).reduce((s, a) => s + a.length, 0);
    if (!total) {
        gtBtn.classList.add('hidden');
        gtPop.classList.add('hidden');
        return;
    }
    gtBtn.classList.remove('hidden');
    gtCount.textContent = total;
    if (!gtPop.classList.contains('hidden')) renderGtPop();   // 打开时跟着刷新
}

function renderGtPop() {
    gtPop.innerHTML = '';
    Object.entries(activeTasks).forEach(([source, tasks]) => {
        tasks.forEach(t => {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'gt-row';
            const kind = document.createElement('span');
            kind.className = 'gt-kind';
            kind.textContent = GT_SOURCE_LABEL[source];
            const name = document.createElement('span');
            name.className = 'gt-name';
            name.textContent = t.label;
            name.title = t.label;
            const prog = document.createElement('span');
            prog.className = 'gt-prog';
            prog.textContent = t.progress || '';
            row.appendChild(kind);
            row.appendChild(name);
            row.appendChild(prog);
            row.addEventListener('click', () => {
                gtPop.classList.add('hidden');
                switchTab(t.tab);
                if (t.chainId) flashCreatorCard(t.chainId);
            });
            gtPop.appendChild(row);
        });
    });
}

// 从 popover 点进 Creators：滚动到对应卡片并短暂高亮
function flashCreatorCard(chainId) {
    const card = document.querySelector(`.creator-card[onclick*="${chainId}"]`);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('card-flash');
    setTimeout(() => card.classList.remove('card-flash'), 1600);
}

if (gtBtn) gtBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const nowHidden = gtPop.classList.toggle('hidden');
    if (!nowHidden) renderGtPop();
});
document.addEventListener('click', (e) => {
    if (!gtPop.classList.contains('hidden')
        && !gtPop.contains(e.target) && !gtBtn.contains(e.target)) {
        gtPop.classList.add('hidden');
    }
});

// 大数字人性化：万 / 亿
function fmtCount(n) {
    n = Number(n) || 0;
    if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, '') + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1).replace(/\.0$/, '') + '万';
    return String(n);
}

navTabs.forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

// ========== 资料库子分区（转写记录 / 分析文档） ==========
const subTabs = document.querySelectorAll('.sub-tab');
const libPanels = {
    transcripts: document.getElementById('lib-transcripts'),
    docs: document.getElementById('lib-docs'),
};

function switchLib(name) {
    subTabs.forEach(b => b.classList.toggle('active', b.dataset.lib === name));
    Object.entries(libPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'docs') { loadDocs(); }
}

subTabs.forEach(btn => {
    btn.addEventListener('click', () => switchLib(btn.dataset.lib));
});

// ========== 分析文档浏览 ==========
const docsList = document.getElementById('docs-list');
const docsRefreshBtn = document.getElementById('docs-refresh');
const docView = document.getElementById('doc-view');
const docBackBtn = document.getElementById('doc-back-btn');
const docTitle = document.getElementById('doc-title');
const docContent = document.getElementById('doc-content');
const docDownloadBtn = document.getElementById('doc-download-btn');

let currentDoc = { chainId: null, name: null, raw: '' };

if (docsRefreshBtn) docsRefreshBtn.addEventListener('click', loadDocs);

async function loadDocs() {
    try {
        const chains = await (await fetch('/api/chains')).json();
        // 只列出有产物的链条（分析过的）
        const withDocs = chains.filter(c => c.analyze);
        if (!withDocs.length) {
            docsList.innerHTML = '<p class="history-empty">No analysis documents yet — analyze a creator first</p>';
            return;
        }
        const blocks = await Promise.all(withDocs.map(async c => {
            let files = [];
            try { files = await (await fetch(`/api/chain/${c.id}/files`)).json(); }
            catch { files = []; }
            files = (Array.isArray(files) ? files : []).filter(f => f.endsWith('.md'));
            if (!files.length) return '';
            // 排序：Report(总分析) > Full transcript(合并原文) > 其余按名
            const rank = f => f === '总分析.md' ? 0 : f === '合并原文.md' ? 1 : 2;
            files.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
            const title = (c.author && c.author !== '该博主') ? c.author : c.url;
            const stageBadge = c.stage === 'done' ? ''
                : `<span class="doc-stage">（${CHAIN_STAGE_LABELS[c.stage] || c.stage}）</span>`;
            const items = files.map(f => {
                const isTotal = f === '总分析.md';
                const isRaw = f === '合并原文.md';
                const label = isTotal ? 'Report' : isRaw ? 'Full transcript'
                    : f.replace(/^分析_\d+_/, '').replace(/\.md$/, '');
                const cls = isTotal ? 'doc-total' : isRaw ? 'doc-raw' : '';
                return `<button class="doc-item ${cls}"
                    onclick="openDocView('${c.id}','${encodeURIComponent(f)}')">${label}</button>`;
            }).join('');
            return `<div class="doc-group">
                <div class="doc-group-title">${escapeHtml(title.slice(0, 70))} ${stageBadge}
                    <span class="doc-count">${files.length} 篇</span></div>
                <div class="doc-items">${items}</div>
            </div>`;
        }));
        const html = blocks.filter(Boolean).join('');
        docsList.innerHTML = html || '<p class="history-empty">No analysis documents yet</p>';
    } catch (e) {
        docsList.innerHTML = '<p class="history-empty">Failed to load</p>';
    }
}

let docReturnTo = 'main';   // 打开文档前在哪：'main' | 'chainDetail'

async function openDocView(chainId, encName) {
    const name = decodeURIComponent(encName);
    try {
        const resp = await fetch(`/api/chain/${chainId}/file?name=${encodeURIComponent(name)}`);
        if (!resp.ok) throw new Error('not found');
        const raw = await resp.text();
        currentDoc = { chainId, name, raw };
        docTitle.textContent = name.replace(/\.md$/, '');
        docContent.innerHTML = renderMarkdown(raw);
        // 记住来源并把它藏掉（之前只藏 mainView，从详情页打开会两个视图叠在一起）
        docReturnTo = (chainDetailView && !chainDetailView.classList.contains('hidden'))
            ? 'chainDetail' : 'main';
        mainView.classList.add('hidden');
        if (chainDetailView) chainDetailView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
    } catch {
        showToast('Could not load document');
    }
}

function closeDocView() {
    docView.classList.add('hidden');
    if (docReturnTo === 'chainDetail' && chainDetailView) {
        chainDetailView.classList.remove('hidden');   // 回到博主详情，不是回主页
    } else {
        mainView.classList.remove('hidden');
    }
    docContent.innerHTML = '';
    window.scrollTo({ top: 0 });
}

// 换个角度看博主：拿现成证据卡跑一个镜头 → 后台生成 → 轮询 → 用 openDocView 展示
async function runLens(chainId, lens) {
    const open = () => openDocView(chainId, encodeURIComponent(`镜头_${lens}.md`));
    showToast('Generating… (same cards, new lens)');
    try {
        const r = await (await fetch(`/api/chain/${chainId}/lens`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lens }),
        })).json();
        if (r.error) { showToast(r.error); return; }
        if (r.ready) { open(); return; }
        let n = 40;   // 最多轮询 ~2 分钟（大链条 map-reduce 要点时间）
        const poll = async () => {
            if (n-- <= 0) { showToast('Still generating — check the doc list shortly'); return; }
            try {
                const g = await (await fetch(`/api/chain/${chainId}/lens/${lens}`)).json();
                if (g.ready) { open(); return; }
                if (g.error) { showToast('Generation failed: ' + g.error); return; }
            } catch { /* 抖动忽略，继续轮 */ }
            setTimeout(poll, 3000);
        };
        setTimeout(poll, 3000);
    } catch { showToast('Generation failed'); }
}

if (docBackBtn) docBackBtn.addEventListener('click', closeDocView);
if (docDownloadBtn) docDownloadBtn.addEventListener('click', () => {
    if (currentDoc.raw) {
        downloadFile(currentDoc.raw, currentDoc.name || 'document.md',
            'text/markdown;charset=utf-8');
        showToast('Downloaded .md');
    }
});

// ========== 轻量 Markdown 渲染（无外部依赖） ==========
function escapeHtml(s) {
    // & < > 以及引号都转义，这样对「文本」和「属性值」两种上下文都安全
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 只放行安全协议的 URL，挡住 javascript:/data: 等注入
function safeUrl(u) {
    const s = String(u || '').trim();
    return /^(https?:|mailto:|\/|#)/i.test(s) ? s : '#';
}

function renderInline(s) {
    // 先转义，再套内联格式
    s = escapeHtml(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, txt, url) =>
        `<a href="${safeUrl(url)}" target="_blank" rel="noopener">${txt}</a>`);
    return s;
}

function renderMarkdown(md) {
    const lines = md.replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
        let line = lines[i];

        // 代码块
        if (/^```/.test(line)) {
            const buf = [];
            i++;
            while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
            i++;
            out.push('<pre><code>' + escapeHtml(buf.join('\n')) + '</code></pre>');
            continue;
        }
        // 标题
        const h = line.match(/^(#{1,6})\s+(.*)$/);
        if (h) {
            const lvl = h[1].length;
            out.push(`<h${lvl}>${renderInline(h[2])}</h${lvl}>`);
            i++; continue;
        }
        // 分割线
        if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
        // 表格
        if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i + 1]) && /\|/.test(lines[i + 1])) {
            const parseRow = r => r.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
            const header = parseRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) {
                rows.push(parseRow(lines[i])); i++;
            }
            let t = '<table><thead><tr>' + header.map(c => `<th>${renderInline(c)}</th>`).join('') + '</tr></thead><tbody>';
            t += rows.map(r => '<tr>' + r.map(c => `<td>${renderInline(c)}</td>`).join('') + '</tr>').join('');
            t += '</tbody></table>';
            out.push(t); continue;
        }
        // 引用
        if (/^>\s?/.test(line)) {
            const buf = [];
            while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i++; }
            out.push('<blockquote>' + renderInline(buf.join(' ')) + '</blockquote>');
            continue;
        }
        // 无序列表
        if (/^\s*[-*]\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
                buf.push('<li>' + renderInline(lines[i].replace(/^\s*[-*]\s+/, '')) + '</li>'); i++;
            }
            out.push('<ul>' + buf.join('') + '</ul>');
            continue;
        }
        // 有序列表
        if (/^\s*\d+\.\s+/.test(line)) {
            const buf = [];
            while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
                buf.push('<li>' + renderInline(lines[i].replace(/^\s*\d+\.\s+/, '')) + '</li>'); i++;
            }
            out.push('<ol>' + buf.join('') + '</ol>');
            continue;
        }
        // 空行
        if (!line.trim()) { i++; continue; }
        // 段落（合并连续非空行）
        const buf = [line];
        i++;
        while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|>\s?|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i]) && !/^\s*([-*_])\1{2,}\s*$/.test(lines[i])) {
            buf.push(lines[i]); i++;
        }
        out.push('<p>' + renderInline(buf.join(' ')) + '</p>');
    }
    return out.join('\n');
}

// ========== 左下角个人数据展板 ==========
const statsToggle = document.getElementById('stats-toggle');
const statsPanel = document.getElementById('stats-panel');
let statsLoaded = false;
let statsTags = [];
let statsTagLang = 'zh';   // 'zh' | 'en'

// 关注领域横向条：按当前语言渲染，不重新拉数据
function renderStatsTags() {
    const el = document.getElementById('stats-tags');
    if (!statsTags.length) {
        el.innerHTML = '<div class="spark-empty">No tags yet — run “Auto-title”</div>';
        return;
    }
    const max = statsTags[0].count || 1;
    el.innerHTML = statsTags.map(t => {
        const label = statsTagLang === 'en' ? (t.tag_en || t.tag) : t.tag;
        return `<div class="stat-tag">
            <span class="stat-tag-name" title="${escapeHtml(label)}">${escapeHtml(label)}</span>
            <span class="stat-tag-bar"><i style="width:${Math.max(6, t.count / max * 100)}%"></i></span>
            <span class="stat-tag-num">${t.count}</span>
        </div>`;
    }).join('');
}

function fmtBig(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
}

// 自绘 SVG 折线（面积填充 + 端点强调），不依赖外部库
function sparkline(values, w = 296, h = 84) {
    if (!values || values.length < 2) {
        return '<div class="spark-empty">Not enough data yet</div>';
    }
    const pad = 6;
    const maxY = Math.max(...values, 1);
    const X = i => pad + (i / (values.length - 1)) * (w - 2 * pad);
    const Y = v => h - pad - (v / maxY) * (h - 2 * pad);
    const pts = values.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    const area = `${X(0)},${h - pad} ${pts} ${X(values.length - 1)},${h - pad}`;
    const last = values.length - 1;
    return `<svg viewBox="0 0 ${w} ${h}" class="spark" preserveAspectRatio="none">
        <defs><linearGradient id="sparkfill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="var(--coral)" stop-opacity="0.18"/>
            <stop offset="1" stop-color="var(--coral)" stop-opacity="0"/>
        </linearGradient></defs>
        <polygon points="${area}" fill="url(#sparkfill)"/>
        <polyline points="${pts}" fill="none" stroke="var(--coral)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${X(last)}" cy="${Y(values[last])}" r="3" fill="var(--coral)"/>
    </svg>`;
}

async function loadStats() {
    try {
        const s = await (await fetch('/api/stats')).json();
        document.getElementById('stat-hours').textContent = s.totals.hours;
        document.getElementById('stat-chars').textContent = fmtBig(s.totals.chars);
        document.getElementById('stat-count').textContent = s.totals.transcripts;

        // 累计小时折线
        let cum = 0;
        const cumHours = (s.timeline || []).map(p => { cum += p.minutes / 60; return cum; });
        document.getElementById('stats-chart').innerHTML = sparkline(cumHours);

        // 关注领域：横向条（中/EN 切换见 renderStatsTags）
        statsTags = s.top_tags || [];
        renderStatsTags();
        statsLoaded = true;
    } catch {
        document.getElementById('stats-chart').innerHTML = '<div class="spark-empty">Failed to load</div>';
    }
}

statsToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const nowHidden = statsPanel.classList.toggle('hidden');
    if (!nowHidden) loadStats();                   // 变为可见才刷新（一次）
});
// 标签语言切换：按钮上显示的是"点了会切到的语言"
const statsLang = document.getElementById('stats-lang');
statsLang.addEventListener('click', (e) => {
    e.stopPropagation();
    statsTagLang = statsTagLang === 'zh' ? 'en' : 'zh';
    statsLang.textContent = statsTagLang === 'zh' ? 'EN' : '中';
    renderStatsTags();
});
// 点面板外部关闭
document.addEventListener('click', (e) => {
    if (!statsPanel.classList.contains('hidden') &&
        !document.getElementById('stats-fab').contains(e.target)) {
        statsPanel.classList.add('hidden');
    }
});


// ===== Settings modal =====
const settingsOverlay = document.getElementById('settings-overlay');
const setGeminiKey = document.getElementById('set-gemini-key');
const setGeminiBase = document.getElementById('set-gemini-base');
const setDashKey = document.getElementById('set-dashscope-key');
const setOpenrouterKey = document.getElementById('set-openrouter-key');
const settingsMsg = document.getElementById('settings-msg');

async function openSettings() {
    // 拉当前状态：填回 base URL、用占位符提示 key 是否已存在
    try {
        const s = await (await fetch('/api/settings')).json();
        setGeminiBase.value = s.gemini_base_url || '';
        setGeminiKey.value = '';
        setDashKey.value = '';
        setOpenrouterKey.value = '';
        setGeminiKey.placeholder = s.gemini.set
            ? `saved ${s.gemini.hint} · leave blank to keep` : 'paste key…';
        setDashKey.placeholder = s.dashscope.set
            ? `saved ${s.dashscope.hint} · leave blank to keep` : 'paste key…';
        setOpenrouterKey.placeholder = (s.openrouter && s.openrouter.set)
            ? `saved ${s.openrouter.hint} · leave blank to keep` : 'paste key…';
        // Models（空 = 默认）
        document.getElementById('set-whisper-model').value = s.whisper_model || '';
        document.getElementById('set-gemini-transcribe').value = s.gemini_transcribe_model || '';
        document.getElementById('set-gemini-analysis').value = s.gemini_analysis_model || '';
        document.getElementById('set-gemini-extract').value = s.gemini_extract_model || '';
    } catch { /* 打开即可，拉取失败不阻塞 */ }
    document.getElementById('test-gemini-res').textContent = '';
    document.getElementById('test-dashscope-res').textContent = '';
    document.getElementById('test-openrouter-res').textContent = '';
    settingsMsg.textContent = '';
    settingsOverlay.classList.remove('hidden');
}
function closeSettings() { settingsOverlay.classList.add('hidden'); }

// 左侧导航切换面板
function showSettingsPane(name) {
    document.querySelectorAll('.settings-nav-item').forEach(b =>
        b.classList.toggle('active', b.dataset.pane === name));
    document.querySelectorAll('.settings-pane').forEach(p =>
        p.classList.toggle('active', p.dataset.pane === name));
    if (name === 'storage') loadStorage();
}

function fmtBytes(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
    if (n >= 1e6) return (n / 1e6).toFixed(0) + ' MB';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + ' KB';
    return (n || 0) + ' B';
}

let compressPollTimer = null;

async function loadStorage() {
    const sizeEl = document.getElementById('storage-size');
    const doneEl = document.getElementById('storage-done');
    sizeEl.textContent = '…';
    try {
        const s = await (await fetch('/api/storage')).json();
        sizeEl.textContent = fmtBytes(s.audio_bytes);
        doneEl.textContent = `${s.compressed_count} / ${s.audio_count}`;
    } catch { sizeEl.textContent = '—'; }
    // 若已有批量压缩在跑，接着显示进度
    try {
        const st = await (await fetch('/api/compress_status')).json();
        if (st.running) {
            document.getElementById('compress-all').disabled = true;
            pollCompress();
        }
    } catch { /* ignore */ }
}

async function pollCompress() {
    const res = document.getElementById('compress-res');
    const btn = document.getElementById('compress-all');
    clearTimeout(compressPollTimer);
    try {
        const s = await (await fetch('/api/compress_status')).json();
        if (s.running) {
            res.className = 'test-res testing';
            res.textContent = `Compressing ${s.done}/${s.total}… saved ${fmtBytes(s.saved)}`;
            if (!document.hidden) compressPollTimer = setTimeout(pollCompress, 2000);
        } else {
            btn.disabled = false;
            if (s.total > 0) {
                res.className = 'test-res ok';
                res.textContent = `Done — reclaimed ${fmtBytes(s.saved)}`
                    + (s.errors ? ` (${s.errors} errors)` : '');
                loadStorage();
            }
        }
    } catch {
        res.className = 'test-res bad';
        res.textContent = 'Status check failed';
        btn.disabled = false;
    }
}

document.getElementById('compress-all').addEventListener('click', async () => {
    const res = document.getElementById('compress-res');
    const btn = document.getElementById('compress-all');
    res.className = 'test-res testing';
    res.textContent = 'Starting…';
    try {
        const r = await (await fetch('/api/compress_all', { method: 'POST' })).json();
        if (!r.ok) {
            res.className = 'test-res bad';
            res.textContent = r.error || 'Could not start';
            return;
        }
    } catch {
        res.className = 'test-res bad';
        res.textContent = 'Could not start';
        return;
    }
    btn.disabled = true;
    pollCompress();
});
document.querySelectorAll('.settings-nav-item').forEach(b =>
    b.addEventListener('click', () => showSettingsPane(b.dataset.pane)));

document.getElementById('settings-open').addEventListener('click', () => {
    showSettingsPane('gemini');       // 每次打开回到第一栏
    openSettings();
});
document.getElementById('settings-close').addEventListener('click', closeSettings);
settingsOverlay.addEventListener('click', (e) => {
    if (e.target === settingsOverlay) closeSettings();      // 点遮罩关闭
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !settingsOverlay.classList.contains('hidden')) closeSettings();
});

async function testEngine(engine, resEl, payload) {
    resEl.className = 'test-res testing';
    resEl.textContent = 'Testing…';
    try {
        const r = await (await fetch('/api/settings/test', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({engine, ...payload}),
        })).json();
        resEl.className = 'test-res ' + (r.ok ? 'ok' : 'bad');
        resEl.textContent = (r.ok ? '✓ ' : '✗ ') + (r.reason || '');
    } catch {
        resEl.className = 'test-res bad';
        resEl.textContent = '✗ Request failed';
    }
}

document.getElementById('test-gemini').addEventListener('click', () => {
    testEngine('gemini', document.getElementById('test-gemini-res'), {
        gemini_key: setGeminiKey.value.trim(),
        gemini_base_url: setGeminiBase.value.trim(),
    });
});
document.getElementById('test-dashscope').addEventListener('click', () => {
    testEngine('dashscope', document.getElementById('test-dashscope-res'), {
        dashscope_key: setDashKey.value.trim(),
    });
});
document.getElementById('test-openrouter').addEventListener('click', () => {
    testEngine('openrouter', document.getElementById('test-openrouter-res'), {
        openrouter_key: setOpenrouterKey.value.trim(),
    });
});

document.getElementById('settings-save').addEventListener('click', async () => {
    const body = {
        gemini_base_url: setGeminiBase.value.trim(),
        whisper_model: document.getElementById('set-whisper-model').value.trim(),
        gemini_transcribe_model: document.getElementById('set-gemini-transcribe').value.trim(),
        gemini_analysis_model: document.getElementById('set-gemini-analysis').value.trim(),
        gemini_extract_model: document.getElementById('set-gemini-extract').value.trim(),
    };
    if (setGeminiKey.value.trim()) body.gemini_key = setGeminiKey.value.trim();
    if (setDashKey.value.trim()) body.dashscope_key = setDashKey.value.trim();
    if (setOpenrouterKey.value.trim()) body.openrouter_key = setOpenrouterKey.value.trim();
    settingsMsg.className = 'settings-msg';
    settingsMsg.textContent = 'Saving…';
    try {
        const r = await (await fetch('/api/settings', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        })).json();
        if (r.ok) {
            settingsMsg.className = 'settings-msg ok';
            settingsMsg.textContent = 'Saved ✓';
            openSettings();                    // 刷新占位符、清空已输入的 key
            settingsMsg.textContent = 'Saved ✓';
        } else {
            settingsMsg.className = 'settings-msg bad';
            settingsMsg.textContent = r.error || 'Save failed';
        }
    } catch {
        settingsMsg.className = 'settings-msg bad';
        settingsMsg.textContent = 'Save failed';
    }
});
```

---

## `static/style.css`

> Claude 风格设计系统

```css
/* ============================================================
   Verbatim — Claude / Anthropic 风格设计系统
   衬线大标题 × 苹果无衬线小正文 · 暖象牙白 · Claude 珊瑚 #D97757
   参数：4px 基准间距、明确字号阶梯、分层柔和阴影、≥40px 控件、150ms ease。
   ============================================================ */

:root {
  --paper:#FAF9F5; --card:#FFFFFF;
  --ink:#141413; --ink-soft:#3D3A34; --muted:#79756C; --faint:#A8A398;
  --line:#ECEAE1; --line-2:#E1DED2;
  --coral:#D97757; --coral-deep:#BE5D3E; --coral-soft:#FBEEE7; --coral-line:#F0D8CC;
  --ok:#4F7A5B; --ok-bg:#EBF1EC; --run:#B07A3C; --run-bg:#F7EDDD; --bad:#B24A3B; --bad-bg:#F7E5E1;
  --code-bg:#F1EFE8;

  --r-xs:6px; --r-sm:8px; --r-md:9px; --r-lg:12px; --r-xl:14px;
  --e1:0 1px 2px rgba(50,42,28,.04);
  --e2:0 6px 20px rgba(60,50,30,.06), 0 1px 3px rgba(50,42,28,.04);

  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Hiragino Sans GB","Segoe UI",sans-serif;
  --serif:"Hoefler Text","Iowan Old Style","Palatino Linotype",Palatino,"Songti SC",Georgia,serif;
  --mono:ui-monospace,"SF Mono","Menlo","Consolas",monospace;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: var(--sans); font-size: 13.5px; line-height: 1.6;
  color: var(--ink); background: var(--paper); min-height: 100vh;
  letter-spacing: -0.003em;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
}
.tnum { font-variant-numeric: tabular-nums; }

.container { max-width: 1000px; margin: 0 auto; padding: 0 32px 76px; }

/* === Header：品牌 + tagline + 导航 === */
header { margin-bottom: 4px; }
.topbar { display: flex; align-items: center; justify-content: space-between; height: 58px; }
.brand { display: flex; align-items: center; gap: 8px; font-size: 15px; font-weight: 600; letter-spacing: -.01em; }
.brand .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--coral); }
.tagline { font-size: 12px; color: var(--muted); }

/* 顶栏全局任务指示器：有活动任务才出现 */
.topbar-right { position: relative; display: flex; align-items: center; gap: 12px; }
.global-tasks {
  display: inline-flex; align-items: center; gap: 5px;
  border: 1px solid var(--coral-line); background: var(--coral-soft);
  color: var(--coral-deep); border-radius: 999px; padding: 3px 10px;
  font-size: 12px; font-weight: 600; cursor: pointer; font-family: inherit;
  font-variant-numeric: tabular-nums;
}
.gt-spin { display: inline-block; animation: gtspin 1.2s linear infinite; }
@keyframes gtspin { to { transform: rotate(360deg); } }
.global-tasks-pop {
  position: absolute; top: calc(100% + 8px); right: 0; z-index: 200;
  min-width: 300px; max-width: 380px; max-height: 320px; overflow-y: auto;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  box-shadow: var(--e2); padding: 6px;
}
.gt-row {
  display: flex; align-items: center; gap: 8px; width: 100%;
  border: none; background: none; cursor: pointer; font-family: inherit;
  padding: 8px 10px; border-radius: var(--r-sm); text-align: left; font-size: 12.5px;
}
.gt-row:hover { background: var(--code-bg); }
.gt-kind {
  flex: none; font-size: 10px; font-weight: 700; letter-spacing: .05em;
  text-transform: uppercase; color: var(--coral-deep); background: var(--coral-soft);
  padding: 2px 7px; border-radius: 999px;
}
.gt-name {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: var(--ink);
}
.gt-prog { flex: none; color: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
/* 从 popover 点进 Creators 时的卡片定位高亮 */
.card-flash { animation: cardflash 1.5s ease; }
@keyframes cardflash {
  0%, 60% { box-shadow: 0 0 0 3px var(--coral); }
  100% { box-shadow: none; }
}

.main-nav { display: flex; gap: 28px; border-bottom: 1px solid var(--line); }
.nav-tab {
  appearance: none; background: none; border: none; font-family: inherit;
  font-size: 13.5px; color: var(--muted); cursor: pointer;
  padding: 13px 0; border-bottom: 1.5px solid transparent; margin-bottom: -1px;
  transition: color .15s ease;
}
.nav-tab:hover { color: var(--ink); }
.nav-tab.active { color: var(--ink); font-weight: 500; border-bottom-color: var(--coral); }

.tab-panel { display: none; }
.tab-panel.active { display: block; animation: fadeIn .2s ease; }

/* === 衬线大标题（学 Claude 官网）=== */
.hero { padding: 46px 0 28px; }
.hero h1 {
  font-family: var(--serif); font-size: 50px; font-weight: 600;
  letter-spacing: -.015em; line-height: 1.05; color: var(--ink); margin-bottom: 12px;
  text-wrap: balance;
}
.hero h1 .accent { color: var(--coral); }
.hero p { font-size: 15px; color: var(--muted); max-width: 580px; line-height: 1.55; }

.section-eyebrow {
  font-size: 11px; letter-spacing: .13em; text-transform: uppercase;
  color: var(--faint); font-weight: 600; margin: 34px 0 12px;
}

/* === Form / 卡片 === */
form, .card-shell {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: 24px; box-shadow: var(--e1); margin-bottom: 18px;
}
.form-group { margin-bottom: 20px; }
.form-group:last-child { margin-bottom: 0; }
.group-label { display: block; font-weight: 600; font-size: 12.5px; color: var(--ink-soft); margin-bottom: 10px; }
.group-label .hint { font-weight: 400; color: var(--faint); font-size: 12px; }

/* File input & Drag-drop */
input[type="file"] { display: none; }
.drop-zone { border-radius: var(--r-md); transition: all .15s ease; }
.drop-zone.drag-over .file-label { border-color: var(--coral); background: var(--coral-soft); box-shadow: 0 0 0 3px var(--coral-soft); }
.file-label {
  display: flex; align-items: center; gap: 12px; padding: 18px 20px; min-height: 60px;
  border: 1.5px dashed var(--line-2); border-radius: var(--r-md);
  cursor: pointer; transition: all .15s ease; background: var(--paper);
}
.file-label:hover { border-color: var(--coral); background: var(--coral-soft); }
.file-label.has-file { border-color: var(--ok); border-style: solid; background: var(--ok-bg); }
.file-icon { font-size: 22px; }
#file-label-text { font-size: 13.5px; color: var(--ink-soft); }
.file-info { font-size: 12px; color: var(--faint); margin-left: auto; font-variant-numeric: tabular-nums; }

/* Radio（引擎选择）*/
.radio-group { display: flex; flex-wrap: wrap; gap: 10px; }
.radio-option {
  flex: 1; min-width: 190px; display: flex; align-items: center; gap: 10px;
  padding: 13px 15px; border: 1px solid var(--line-2); border-radius: var(--r-md);
  cursor: pointer; transition: all .15s ease; background: var(--card);
}
.radio-option:hover { border-color: var(--coral); }
.radio-option:has(input:checked) { border-color: var(--coral); background: var(--coral-soft); }
.radio-option input[type="radio"] { display: none; }
.radio-custom { width: 18px; height: 18px; border: 1.5px solid var(--line-2); border-radius: 50%; flex-shrink: 0; position: relative; transition: all .15s ease; }
.radio-option:has(input:checked) .radio-custom { border-color: var(--coral); }
.radio-option:has(input:checked) .radio-custom::after { content:''; position:absolute; top:3px; left:3px; width:8px; height:8px; background:var(--coral); border-radius:50%; }
.radio-label { display: flex; flex-direction: column; gap: 2px; }
.radio-label strong { font-size: 13.5px; font-weight: 600; color: var(--ink); }
.radio-label small { font-size: 12px; color: var(--muted); }

/* === Buttons === */
.btn-primary {
  display: block; width: 100%; min-height: 44px; padding: 12px 22px;
  background: var(--coral); color: #fff; border: 1px solid var(--coral);
  border-radius: var(--r-md); font-family: inherit; font-size: 14px; font-weight: 500;
  cursor: pointer; transition: background .15s ease;
}
.btn-primary:hover { background: var(--coral-deep); border-color: var(--coral-deep); }
.btn-primary:disabled { background: var(--faint); border-color: var(--faint); cursor: not-allowed; }

.btn-secondary {
  padding: 8px 15px; min-height: 36px; background: var(--card); color: var(--ink-soft);
  border: 1px solid var(--line-2); border-radius: var(--r-sm);
  font-family: inherit; font-size: 13px; cursor: pointer; white-space: nowrap;
  transition: border-color .15s ease, background .15s ease;
}
.btn-secondary:hover { border-color: var(--ink); background: var(--paper); }
.btn-small { padding: 5px 12px; min-height: 30px; font-size: 12px; }
.btn-danger { color: var(--bad); }
.btn-danger:hover { border-color: var(--bad); background: var(--bad-bg); }

button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible {
  outline: 2px solid var(--coral); outline-offset: 2px;
}

/* === Inputs === */
.search-input, .number-input, select.search-input {
  width: 100%; font-family: inherit; font-size: 13.5px; color: var(--ink);
  background: var(--card); border: 1px solid var(--line-2); border-radius: var(--r-md);
  padding: 11px 13px; min-height: 40px;
  transition: border-color .15s ease, box-shadow .15s ease;
}
.search-input::placeholder, .number-input::placeholder { color: var(--faint); }
.search-input:focus, .number-input:focus, select.search-input:focus {
  outline: none; border-color: var(--coral); box-shadow: 0 0 0 3px var(--coral-soft);
}
.number-input { width: 140px; font-variant-numeric: tabular-nums; }

/* === Progress === */
#progress-section { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 22px 24px; box-shadow: var(--e1); margin-bottom: 18px; }
.progress-bar { width: 100%; height: 8px; background: #EBE7DC; border-radius: 999px; overflow: hidden; }
.progress-fill { height: 100%; width: 0%; background: var(--coral); border-radius: 999px; transition: width .4s ease; }
.progress-text { margin-top: 10px; font-size: 13px; color: var(--muted); text-align: center; }

/* === Audio Player === */
.player-card { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 16px 20px; box-shadow: var(--e1); margin-bottom: 18px; }
.player-card audio { width: 100%; }

/* === Summary === */
#summary-section { margin-bottom: 18px; }
.summary-card { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 22px 24px; box-shadow: var(--e1); }
.summary-card h2 { font-family: var(--serif); font-size: 20px; font-weight: 600; color: var(--ink); margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.summary-overview { font-size: 14px; line-height: 1.75; color: var(--ink-soft); margin-bottom: 18px; padding: 14px 16px; background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-sm); }
.summary-sections { display: flex; flex-direction: column; gap: 10px; }
.summary-section-item { padding: 14px 16px; background: var(--paper); border-radius: var(--r-sm); border: 1px solid var(--line); animation: fadeIn .3s ease; }
.summary-section-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.summary-section-title { font-size: 14px; font-weight: 600; color: var(--ink); }
.summary-section-time { font-family: var(--mono); font-size: 12px; color: var(--coral); background: var(--coral-soft); padding: 2px 8px; border-radius: var(--r-xs); white-space: nowrap; font-variant-numeric: tabular-nums; }
.summary-section-desc { font-size: 13px; line-height: 1.7; color: var(--muted); }

/* === Results === */
#results-section, #detail-results-section { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 22px 24px; box-shadow: var(--e1); margin-bottom: 18px; }
.results-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.results-header h2 { font-family: var(--serif); font-size: 20px; font-weight: 600; color: var(--ink); }
.results-actions { display: flex; gap: 8px; }
.segments-container { max-height: 520px; overflow-y: auto; }
.segment { display: flex; gap: 14px; align-items: flex-start; padding: 10px 0; border-bottom: 1px solid var(--line); animation: fadeIn .3s ease; }
.segment:last-child { border-bottom: none; }
.segment.active { background: var(--coral-soft); border-radius: var(--r-sm); padding: 10px 12px; margin-left: -12px; padding-left: 12px; box-shadow: inset 3px 0 0 var(--coral); }
.segment.active .segment-text { color: var(--coral-deep); }
.segment.active .timestamp { background: var(--coral); color: #fff; }
.timestamp { font-family: var(--mono); font-size: 12px; color: var(--coral); background: var(--coral-soft); padding: 3px 9px; border-radius: var(--r-xs); white-space: nowrap; flex-shrink: 0; font-variant-numeric: tabular-nums; }
.timestamp.clickable { cursor: pointer; transition: all .15s ease; }
.timestamp.clickable:hover { background: var(--coral); color: #fff; }
.segment-text { flex: 1; font-size: 14px; line-height: 1.75; color: var(--ink-soft); }

/* === History === */
.history-card { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 22px 24px; box-shadow: var(--e1); }
.history-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.history-header h2 { font-family: var(--serif); font-size: 20px; font-weight: 600; color: var(--ink); }
.history-count { font-size: 13px; font-weight: 400; color: var(--faint); font-variant-numeric: tabular-nums; }
.history-header-actions { display: flex; gap: 8px; }
.history-list { display: flex; flex-direction: column; gap: 11px; max-height: 440px; overflow-y: auto; }
.history-empty { text-align: center; color: var(--faint); font-size: 14px; padding: 28px 0; }
.history-item { display: flex; align-items: flex-start; justify-content: space-between; padding: 16px 19px; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); transition: border-color .15s ease, box-shadow .15s ease; }
.history-item:hover { border-color: var(--line-2); box-shadow: var(--e2); }
.history-item-info { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.history-item-name { font-size: 15px; font-weight: 600; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; letter-spacing: -.01em; }
.history-item-meta { font-size: 11.5px; color: var(--faint); font-variant-numeric: tabular-nums; }
.history-item-actions { display: flex; gap: 6px; flex-shrink: 0; margin-left: 12px; }
.history-toolbar { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
.filter-chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { padding: 5px 15px; font-size: 12.5px; border: 1px solid var(--line-2); border-radius: 999px; background: var(--card); color: var(--muted); cursor: pointer; transition: all .15s ease; }
.chip:hover { border-color: var(--ink); color: var(--ink); }
.chip.active { background: var(--ink); border-color: var(--ink); color: var(--paper); }
.history-item-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.engine-badge { flex-shrink: 0; font-size: 10px; padding: 3px 8px; border-radius: 5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; white-space: nowrap; }
.engine-gemini    { background: var(--coral-soft); color: var(--coral-deep); }
.engine-dashscope { background: var(--run-bg); color: var(--run); }
.engine-precise   { background: #EFE9F2; color: #7A5A8E; }
.engine-whisper   { background: var(--ok-bg); color: var(--ok); }
.engine-unknown   { background: var(--code-bg); color: var(--muted); }
.history-item-oneline { display: -webkit-box; font-size: 13px; color: var(--ink-soft); margin-top: 4px; overflow: hidden; text-overflow: ellipsis; -webkit-line-clamp: 2; -webkit-box-orient: vertical; line-height: 1.55; }
.history-item-oneline.snippet { color: var(--run); background: var(--run-bg); border-radius: var(--r-xs); padding: 3px 8px; }

/* === Batch Queue === */
#batch-section { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 20px 24px; box-shadow: var(--e1); margin-bottom: 18px; }
.batch-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.batch-header h2 { font-family: var(--serif); font-size: 18px; margin: 0; }
.batch-progress { font-size: 13px; color: var(--muted); font-weight: 500; font-variant-numeric: tabular-nums; }
.batch-list { display: flex; flex-direction: column; gap: 10px; max-height: 460px; overflow-y: auto; }
.batch-item { display: grid; grid-template-columns: 1fr auto; grid-template-areas: "info actions" "bar actions"; align-items: center; gap: 6px 12px; padding: 12px 14px; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-md); }
.batch-item-info { grid-area: info; display: flex; align-items: baseline; justify-content: space-between; gap: 12px; min-width: 0; }
.batch-item-name { font-size: 14px; font-weight: 500; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.batch-item-status { font-size: 12px; color: var(--faint); flex-shrink: 0; white-space: nowrap; }
.batch-item-status.status-queued  { color: var(--run); }
.batch-item-status.status-running { color: var(--coral); }
.batch-item-status.status-done    { color: var(--ok); }
.batch-item-status.status-error   { color: var(--bad); }
.batch-item-progress { grid-area: bar; width: 100%; height: 5px; background: #EBE7DC; border-radius: 999px; overflow: hidden; }
.batch-item-progress-fill { height: 100%; width: 0%; background: var(--coral); border-radius: 999px; transition: width .4s ease; }
.batch-item-actions { grid-area: actions; display: flex; gap: 6px; flex-shrink: 0; }

/* === Detail 顶栏 === */
.detail-top-bar { display: flex; align-items: center; gap: 14px; margin: 20px 0 18px; padding: 14px 18px; background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg); box-shadow: var(--e1); }
.detail-top-bar > button { flex-shrink: 0; }
.detail-title { flex: 1; min-width: 0; font-size: 15px; font-weight: 600; color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.detail-meta { font-size: 13px; color: var(--muted); margin-bottom: 16px; padding: 0 4px; font-variant-numeric: tabular-nums; }

/* === Error === */
.error-box { display: flex; align-items: center; gap: 10px; background: var(--bad-bg); border: 1px solid #EBCEC7; border-radius: var(--r-md); padding: 14px 18px; color: var(--bad); font-size: 14px; }
.error-icon { font-size: 18px; }

/* === Utilities === */
.hidden { display: none !important; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

.toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%); background: var(--ink); color: var(--paper); padding: 10px 22px; border-radius: var(--r-md); font-size: 14px; z-index: 1000; box-shadow: var(--e2); animation: toastIn .3s ease, toastOut .3s ease 1.5s forwards; }
@keyframes toastIn { from { opacity: 0; transform: translateX(-50%) translateY(10px); } to { opacity: 1; transform: translateX(-50%) translateY(0); } }
@keyframes toastOut { from { opacity: 1; } to { opacity: 0; } }

/* === 链条分析 === */
.chain-hint { color: var(--muted); font-size: 12.5px; margin: 4px 0 14px; line-height: 1.55; }
.chain-hint code { background: var(--code-bg); padding: 1px 6px; border-radius: var(--r-xs); font-family: var(--mono); font-size: 11.5px; }
.chain-form { display: flex; flex-direction: column; gap: 10px; }
.chain-form-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.chain-small { flex: 1; min-width: 130px; }
.chain-check { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--muted); white-space: nowrap; }
.chain-btn { padding: 11px 22px; width: auto; min-height: 40px; }

/* === 资料库子分区 === */
.lib-subnav { display: flex; gap: 8px; margin-bottom: 16px; }
.sub-tab { background: none; border: 1px solid transparent; padding: 6px 14px; border-radius: 999px; cursor: pointer; font-family: inherit; font-size: 12.5px; color: var(--muted); transition: all .15s ease; }
.sub-tab:hover { color: var(--ink); background: var(--code-bg); }
.sub-tab.active { background: var(--ink); color: var(--paper); }
.lib-panel { display: none; }
.lib-panel.active { display: block; }

/* === 分析文档列表 === */
.docs-list { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
.doc-group { border: 1px solid var(--line); border-radius: var(--r-lg); padding: 14px 16px; background: var(--card); }
.doc-group-title { font-size: 14px; font-weight: 600; color: var(--ink); margin-bottom: 10px; }
.doc-count { color: var(--faint); font-weight: 400; font-size: 12px; margin-left: 6px; }
.doc-stage { color: var(--run); font-weight: 400; font-size: 12px; }
.doc-items { display: flex; flex-wrap: wrap; gap: 8px; }
.doc-item { background: var(--card); border: 1px solid var(--line-2); border-radius: var(--r-sm); padding: 8px 13px; font-size: 13px; cursor: pointer; color: var(--ink-soft); text-align: left; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: border-color .15s ease, color .15s ease; }
.doc-item:hover { border-color: var(--coral); color: var(--coral); }
.doc-item.doc-total { background: var(--coral-soft); border-color: var(--coral-line); font-weight: 600; color: var(--coral-deep); }
.doc-item.doc-raw { background: var(--code-bg); border-color: var(--line-2); font-weight: 600; color: var(--ink-soft); }
.doc-item.doc-raw:hover { border-color: var(--ink); color: var(--ink); }

/* === Markdown 阅读排版（衬线标题 · 编辑感）=== */
#doc-view { max-width: 780px; margin: 0 auto; }
.md-body { background: var(--card); border: 1px solid var(--line); border-radius: var(--r-xl); padding: 40px 46px; margin-top: 8px; line-height: 1.78; color: var(--ink-soft); font-size: 15px; box-shadow: var(--e1); }
.md-body h1 { font-family: var(--serif); font-size: 32px; font-weight: 600; color: var(--ink); margin: 4px 0 20px; padding-bottom: 14px; border-bottom: 1px solid var(--line); letter-spacing: -.012em; line-height: 1.14; text-wrap: balance; }
.md-body h2 { font-family: var(--serif); font-size: 23px; font-weight: 600; color: var(--ink); margin: 34px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--line); letter-spacing: -.008em; }
.md-body h3 { font-family: var(--serif); font-size: 18px; font-weight: 600; color: var(--ink); margin: 26px 0 10px; }
.md-body h4 { font-size: 14px; font-weight: 600; color: var(--ink-soft); margin: 18px 0 8px; }
.md-body p { margin: 13px 0; }
.md-body ul, .md-body ol { margin: 13px 0; padding-left: 24px; }
.md-body li { margin: 6px 0; }
.md-body strong { color: var(--ink); font-weight: 600; }
.md-body code { background: var(--code-bg); padding: 2px 6px; border-radius: var(--r-xs); font-size: 13px; font-family: var(--mono); color: var(--coral-deep); }
.md-body pre { background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-sm); padding: 14px 16px; overflow-x: auto; margin: 14px 0; }
.md-body pre code { background: none; color: var(--ink-soft); padding: 0; }
.md-body blockquote { border-left: 3px solid var(--coral-line); margin: 14px 0; padding: 4px 16px; color: var(--muted); background: var(--paper); border-radius: 0 var(--r-xs) var(--r-xs) 0; }
.md-body hr { border: none; border-top: 1px solid var(--line); margin: 26px 0; }
.md-body table { border-collapse: collapse; width: 100%; margin: 16px 0; font-size: 14px; display: block; overflow-x: auto; }
.md-body th, .md-body td { border: 1px solid var(--line); padding: 8px 12px; text-align: left; }
.md-body th { background: var(--paper); font-weight: 600; }
.md-body a { color: var(--coral-deep); }
#doc-download-btn { margin-left: auto; }

/* === 链条详情：视频封面网格 === */
#chain-detail-view { max-width: 1000px; margin: 0 auto; }
.video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(196px, 1fr)); gap: 16px; margin-top: 12px; }
.vg-card { border: 1px solid var(--line); border-radius: var(--r-lg); overflow: hidden; background: var(--card); display: flex; flex-direction: column; }
.vg-clickable { cursor: pointer; transition: border-color .15s ease, box-shadow .15s ease; }
.vg-clickable:hover { border-color: var(--ink); box-shadow: var(--e2); }
.vg-picked { border-color: var(--coral); box-shadow: 0 0 0 1px var(--coral); }
/* 合并勾选框：封面左上角，白底小方块，好点 */
.vg-pick {
  position: absolute; top: 7px; left: 7px; z-index: 3;
  width: 26px; height: 26px; border-radius: 7px;
  background: rgba(255,255,255,.92); border: 1px solid var(--line-2);
  display: flex; align-items: center; justify-content: center; cursor: pointer;
  box-shadow: var(--e1);
}
.vg-pick input { width: 15px; height: 15px; accent-color: var(--coral); cursor: pointer; margin: 0; }

/* 底部浮动合并操作条：购物车非空才出现 */
.merge-bar {
  position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%);
  z-index: 500; display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; border-radius: 999px;
  background: var(--ink); color: var(--paper); box-shadow: var(--e2);
}
.merge-bar-count { font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.merge-bar .btn-small { min-height: 32px; }
.vg-thumb { width: 100%; aspect-ratio: 16 / 9; object-fit: cover; display: block; background: linear-gradient(135deg, #EEEAE0, #E4DFD1); }
.vg-noimg { display: flex; align-items: center; justify-content: center; font-size: 26px; color: var(--faint); }
.vg-badge { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 6px 12px; }
.vs-active { color: var(--run); background: var(--run-bg); }
.vs-done   { color: var(--ok);  background: var(--ok-bg); }
.vs-fail   { color: var(--bad); background: var(--bad-bg); }
.vg-title {
  font-size: 12.5px; color: var(--ink-soft); padding: 9px 12px 12px; line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; word-break: break-word;
  /* 兜底裁剪：即使个别浏览器 line-clamp 不生效，也不会露出第 4 行 */
  max-height: calc(1.5em * 3 + 21px);
}
.vg-retry {
  font-size: 11.5px; color: var(--faint); padding: 0 12px 11px; margin-top: -4px;
}
.vg-retry a { color: var(--coral); cursor: pointer; }
.vg-retry a:hover { text-decoration: underline; }

/* ===== 链条详情：info 面板 + Episodes 折叠 ===== */
.chain-info {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: 18px 22px; margin-bottom: 16px; box-shadow: var(--e1);
}
.ci-row { display: flex; gap: 14px; margin: 5px 0; font-size: 13px; line-height: 1.6; }
.ci-k { flex: none; width: 68px; color: var(--faint); font-size: 11.5px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; padding-top: 2px; }
.ci-v { color: var(--ink-soft); min-width: 0; overflow-wrap: anywhere; }
.ci-v b { color: var(--ink); font-weight: 600; }
.ci-v a { color: var(--coral); text-decoration: none; }
.ci-v a:hover { text-decoration: underline; }
.ci-warn { margin: 8px 0 0 82px; font-size: 12.5px; color: var(--coral-deep); }
.ci-actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
.ci-btn { padding: 8px 18px; }
.ci-hint { font-size: 11.5px; color: var(--faint); line-height: 1.5; max-width: 480px; }

.episodes-toggle {
  display: inline-flex; align-items: center; gap: 6px; margin-bottom: 14px;
  background: none; border: none; font-family: inherit; font-size: 13.5px;
  font-weight: 600; color: var(--ink-soft); cursor: pointer; padding: 4px 0;
}
.episodes-toggle:hover { color: var(--coral); }
.episodes-toggle.open { color: var(--ink); }

/* 链条表单：折叠的高级选项（原生 <details>，无 JS） */
.chain-advanced { margin-top: 10px; }
.chain-advanced > summary {
  cursor: pointer; list-style: none;
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12.5px; font-weight: 600; color: var(--faint);
  padding: 4px 2px; user-select: none;
}
.chain-advanced > summary::-webkit-details-marker { display: none; }
.chain-advanced > summary::before { content: '▸'; font-size: 10px; }
.chain-advanced[open] > summary::before { content: '▾'; }
.chain-advanced > summary:hover { color: var(--coral); }
.chain-advanced[open] > summary { color: var(--ink-soft); margin-bottom: 8px; }

/* 小红书采集进度 */
.xhs-progress {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: 16px 20px; margin-top: 16px; box-shadow: var(--e1);
}
.xhs-head { font-size: 13.5px; color: var(--ink-soft); display: flex; gap: 12px;
  align-items: center; flex-wrap: wrap; }
.xhs-head b { color: var(--ink); font-variant-numeric: tabular-nums; }
.xhs-live { color: var(--coral); font-weight: 700; }
.xhs-done { color: var(--muted); font-weight: 700; }
.xhs-log {
  margin-top: 12px; max-height: 260px; overflow: auto; background: var(--code-bg);
  border-radius: var(--r-sm); padding: 12px 14px; font-family: var(--mono);
  font-size: 12px; line-height: 1.6; color: var(--ink-soft); white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.url-box { width: 100%; resize: vertical; min-height: 52px; font-family: var(--mono); font-size: 12.5px; line-height: 1.6;
  padding: 10px 12px; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--paper); color: var(--ink); }
.local-hint { font-size: 12.5px; color: var(--ink-faint); margin: 2px 0 12px; line-height: 1.5; }

/* 统一入口：文件/链接/路径 解析预览列表 */
#mixed-input { margin-top: 10px; }
.source-badge {
  font-size: 11px; padding: 2px 8px; border-radius: 999px; white-space: nowrap;
  background: var(--surface-2, #EDEFF3); color: var(--ink-soft);
  border: 1px solid var(--line); font-family: var(--mono);
}
.collection-cap { display: inline-flex; align-items: center; gap: 9px; margin: 0 0 12px;
  font-size: 12.5px; color: var(--ink-soft); }
.collection-cap input { width: 84px; }
.intake-list { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.intake-row {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px;
  border: 1px solid var(--line); border-radius: var(--r-md);
  background: var(--card); font-size: 12.5px;
}
.intake-badge {
  flex: none; font-size: 10.5px; font-weight: 600; letter-spacing: .04em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 999px;
  background: var(--code-bg); color: var(--muted); white-space: nowrap;
}
.intake-badge-link { background: var(--coral-soft); color: var(--coral-deep); }
.intake-badge-path { background: var(--ok-bg); color: var(--ok); }
.intake-name {
  flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: var(--ink); font-family: var(--mono); font-size: 12px;
}
.intake-sub { flex: none; color: var(--faint); font-size: 11.5px; }
.intake-invalid { border-color: var(--bad); background: var(--bad-bg); }
.intake-invalid .intake-badge { background: var(--bad); color: #fff; }
.intake-invalid .intake-name, .intake-invalid .intake-sub { color: var(--bad); }
.intake-del {
  flex: none; border: none; background: none; cursor: pointer;
  color: var(--muted); font-size: 17px; line-height: 1; padding: 0 4px;
}
.intake-del:hover { color: var(--bad); }
#xhs-keywords { resize: vertical; min-height: 84px; line-height: 1.6; }
.xhs-analyze-card {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: 18px 22px; margin-top: 16px; box-shadow: var(--e1);
}
.xhs-an-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.xhs-an-head b { font-size: 14.5px; color: var(--ink); }
.xhs-an-head .ci-hint { display: block; margin-top: 3px; }
#xhs-analyze-progress { margin-top: 14px; font-size: 13.5px; color: var(--ink-soft); }
#xhs-analyze-progress b { font-variant-numeric: tabular-nums; color: var(--ink); }

/* 博主库（Creators gallery）*/
.creators-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px; margin-top: 8px;
}
.creator-card {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-lg);
  overflow: hidden; cursor: pointer; box-shadow: var(--e1);
  transition: transform .12s, box-shadow .12s;
}
.creator-card:hover { transform: translateY(-3px); box-shadow: var(--e2); }
.creator-thumb {
  height: 120px; background-size: cover; background-position: center;
  background-color: var(--coral-soft);
}
.creator-noimg {
  display: flex; align-items: center; justify-content: center;
  font-size: 32px; color: var(--coral); background: var(--coral-soft);
}
.creator-body { padding: 12px 14px; }
.creator-name {
  font-weight: 700; font-size: 14.5px; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.creator-meta { margin-top: 4px; font-size: 12px; color: var(--faint); }

/* 进行中 / 失败 / 中断的分析：同一张卡，原地换状态 */
.creator-progressbar {
  margin-top: 8px; height: 4px; border-radius: 2px;
  background: var(--coral-soft); overflow: hidden;
}
.creator-progressbar i {
  display: block; height: 100%; border-radius: 2px;
  background: var(--coral); transition: width .6s ease;
}
.creator-current {
  margin-top: 6px; font-size: 11.5px; color: var(--faint);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.creator-error { color: var(--bad); }
.creator-retry { margin-top: 8px; }

/* ===== 博主详情页：档案封面（头像 + 大名 + 真数据），运维折叠 ===== */
.cd-cover {
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-xl);
  box-shadow: var(--e2); overflow: hidden; margin-bottom: 14px;
}
.cd-cover-top { display: flex; gap: 24px; padding: 30px 32px 24px; align-items: center; }
.cd-avatar {
  position: relative; flex: none; width: 96px; height: 96px; border-radius: 50%;
  overflow: hidden; display: flex; align-items: center; justify-content: center;
  font-family: var(--serif); font-size: 44px; color: #fff;
  background: radial-gradient(120% 120% at 30% 25%, #F6C9B4 0%, var(--coral) 55%, var(--coral-deep) 100%);
  box-shadow: inset 0 2px 6px rgba(255,255,255,.25), 0 6px 18px rgba(217,119,87,.26);
}
.cd-avatar-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.cd-id { min-width: 0; flex: 1; }
.cd-eyebrow {
  font-size: 11px; font-weight: 700; letter-spacing: .13em; text-transform: uppercase;
  color: var(--coral-deep);
}
.cd-name {
  font-family: var(--serif); font-size: 40px; font-weight: 600; color: var(--ink);
  line-height: 1.08; margin: 7px 0 3px; overflow-wrap: anywhere; text-wrap: balance;
}
.cd-sub { font-size: 13px; color: var(--muted); font-variant-numeric: tabular-nums; }

.cd-stats { display: flex; flex-wrap: wrap; border-top: 1px solid var(--line); background: #FCFBF8; }
/* 左对齐、按内容宽——2 个也不会被拉出中间的大空档 */
.cd-stat { padding: 14px 26px; border-right: 1px solid var(--line); }
.cd-stat:last-child { border-right: none; }
.cd-stat .n {
  font-family: var(--mono); font-size: 21px; font-weight: 600; color: var(--ink);
  font-variant-numeric: tabular-nums; line-height: 1;
}
.cd-stat .l { font-size: 11.5px; color: var(--muted); margin-top: 6px; }

.cd-body { padding: 22px 32px 26px; }
.cd-ctas { display: flex; gap: 12px; flex-wrap: wrap; }
.cd-cta {
  display: inline-flex; align-items: center; justify-content: center;
  width: auto;              /* 盖掉 .btn-primary 的 width:100%，别拉成通栏 */
  font-size: 15px; font-weight: 700; padding: 12px 26px; border-radius: var(--r-md);
  cursor: pointer;
}

/* 详情页的报错/降级提示：装进正经的警示盒，别裸奔 */
.cd-alert {
  background: var(--bad-bg); border: 1px solid #EBCEC7; border-radius: var(--r-md);
  padding: 12px 16px; margin: 0 0 14px; font-size: 13px; color: var(--bad);
  line-height: 1.55; overflow-wrap: anywhere;
}
.cd-lens-title {
  margin-top: 22px; font-size: 13px; font-weight: 700; color: var(--ink-soft);
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
}
.cd-lens-row { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px; }

/* 镜头按钮（核心动作，做大）*/
.lens-btn {
  font-family: inherit; font-size: 15px; font-weight: 600;
  color: var(--ink); background: var(--coral-soft);
  border: 1px solid var(--line); border-radius: 999px;
  padding: 10px 20px; cursor: pointer; transition: all .12s;
}
.lens-btn:hover { color: #fff; background: var(--coral); border-color: var(--coral);
  transform: translateY(-1px); }

/* 运维细节折叠 */
.chain-ops { margin-bottom: 16px; }
.chain-ops > summary {
  cursor: pointer; list-style: none; user-select: none;
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 12.5px; font-weight: 600; color: var(--faint); padding: 4px 2px;
}
.chain-ops > summary::-webkit-details-marker { display: none; }
.chain-ops > summary::before { content: '▸'; font-size: 10px; }
.chain-ops[open] > summary::before { content: '▾'; }
.chain-ops > summary:hover { color: var(--coral); }
.chain-ops[open] > summary { margin-bottom: 8px; }
.ops-flag { color: var(--bad); font-weight: 600; }
/* 运维里的报错盒（从主区收进来的）*/
.chain-ops .cd-alert { margin: 0 0 12px; }

.vg-eng {
  margin-left: 7px; font-size: 9.5px; font-weight: 600; letter-spacing: .03em;
  color: var(--coral-deep); background: var(--coral-soft);
  padding: 2px 7px; border-radius: 999px; text-transform: none;
}

/* === Responsive === */
@media (max-width: 640px) {
  .container { padding: 0 16px 48px; }
  .hero h1 { font-size: 36px; }
  form { padding: 20px; }
  .radio-option { min-width: 100%; }
  .results-header { flex-direction: column; align-items: flex-start; gap: 12px; }
  .history-item { flex-direction: column; align-items: flex-start; gap: 8px; }
  .history-item-actions { margin-left: 0; }
  .md-body { padding: 26px 22px; }
  .topbar { flex-direction: column; align-items: flex-start; gap: 4px; height: auto; padding: 16px 0; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; transition-duration: .001ms !important; }
}

/* ========== 左下角个人数据展板 ========== */
.stats-fab { position: fixed; left: 20px; bottom: 20px; z-index: 900; }
.stats-toggle {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--card); border: 1px solid var(--line-2); border-radius: 999px;
  padding: 8px 15px; font-family: inherit; font-size: 12.5px; color: var(--ink-soft);
  cursor: pointer; box-shadow: var(--e1); transition: border-color .15s, box-shadow .15s;
}
.stats-toggle:hover { border-color: var(--ink); box-shadow: var(--e2); }
.stats-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--coral); flex: none; }

.stats-panel {
  position: absolute; left: 0; bottom: 46px; width: 340px;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--r-xl);
  box-shadow: 0 12px 40px rgba(50,42,28,.14); padding: 20px 22px;
  animation: fadeIn .16s ease;
}
.stats-head { font-size: 11px; letter-spacing: .12em; text-transform: uppercase; color: var(--faint); font-weight: 600; margin-bottom: 14px; }
.stats-nums { display: flex; gap: 18px; margin-bottom: 20px; }
.stat-num { display: flex; flex-direction: column; gap: 2px; }
.stat-num b { font-family: var(--serif); font-size: 27px; font-weight: 600; letter-spacing: -.02em; color: var(--ink); font-variant-numeric: tabular-nums; line-height: 1; }
.stat-num span { font-size: 11px; color: var(--muted); }

.stats-block { margin-top: 16px; }
.stats-label { font-size: 11px; color: var(--faint); font-weight: 500; margin-bottom: 8px; }
.stats-label-row { display: flex; align-items: center; justify-content: space-between; }
.stats-lang {
  font-family: inherit; font-size: 10px; font-weight: 600; letter-spacing: .04em;
  color: var(--muted); background: var(--card); cursor: pointer;
  border: 1px solid var(--line-2); border-radius: 999px; padding: 2px 8px; line-height: 1.4;
  transition: border-color .15s ease, color .15s ease;
}
.stats-lang:hover { border-color: var(--coral); color: var(--coral); }
.spark { width: 100%; height: 84px; display: block; }
.spark-empty { font-size: 12px; color: var(--faint); padding: 14px 0; }

.stat-tag { display: flex; align-items: center; gap: 10px; margin: 7px 0; }
.stat-tag-name { font-size: 12.5px; color: var(--ink-soft); width: 78px; flex: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stat-tag-bar { flex: 1; height: 6px; background: var(--code-bg); border-radius: 999px; overflow: hidden; }
.stat-tag-bar i { display: block; height: 100%; background: var(--coral); border-radius: 999px; }
.stat-tag-num { font-size: 12px; color: var(--muted); width: 26px; text-align: right; flex: none; font-variant-numeric: tabular-nums; }

@media (max-width: 640px) { .stats-panel { width: 300px; } }

/* 转写进度：封面上盖同色系（珊瑚）半透明板 + 大号百分比 + 底部进度条 */
.vg-thumb-wrap { position: relative; line-height: 0; }
.vg-prog {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(217, 119, 87, 0.64);          /* --coral，半透明，封面透出来 */
}
.vg-prog span {
  font-family: var(--serif); font-size: 27px; font-weight: 600; color: #fff;
  letter-spacing: -.02em; font-variant-numeric: tabular-nums;
  text-shadow: 0 1px 6px rgba(120, 50, 20, .45);
}
.vg-prog::after {                                 /* 底部进度填充条 */
  content: ''; position: absolute; left: 0; bottom: 0; height: 4px;
  width: var(--p, 0%); background: #fff; opacity: .92; transition: width .4s ease;
}

/* 国内云引擎"展开"链接 */
.reveal-link {
  display: inline-block; margin: -6px 0 14px; padding: 0;
  background: none; border: none; font-family: inherit; font-size: 12.5px;
  color: var(--muted); cursor: pointer; transition: color .15s ease;
}
.reveal-link:hover { color: var(--ink); }
.reveal-link.open { color: var(--ink); }

/* ===== 左下角按钮行（Your stats + Settings）===== */
.fab-row { display: flex; gap: 8px; }
.fab-gear { font-size: 13px; line-height: 1; }

/* ===== Settings 弹窗（两栏：左导航 + 右内容，仿 Claude）===== */
.settings-overlay {
  position: fixed; inset: 0; z-index: 1000;
  display: flex; align-items: center; justify-content: center; padding: 24px;
  background: rgba(20, 20, 19, .38); backdrop-filter: blur(2px);
}
.settings-modal {
  position: relative; width: 100%; max-width: 840px;
  height: 78vh; min-height: 480px; max-height: 88vh;
  display: flex; background: var(--paper);
  border: 1px solid var(--line); border-radius: var(--r-lg);
  box-shadow: 0 24px 64px rgba(20, 20, 19, .24); overflow: hidden;
}
.settings-x {
  position: absolute; top: 14px; right: 16px; z-index: 3;
  width: 30px; height: 30px; font-size: 22px; line-height: 1; color: var(--muted);
  background: none; border: none; cursor: pointer; border-radius: var(--r-sm);
}
.settings-x:hover { color: var(--ink); background: var(--card); }

.settings-nav {
  width: 194px; flex: none; background: var(--card);
  border-right: 1px solid var(--line); padding: 22px 12px;
  display: flex; flex-direction: column; gap: 2px;
}
.settings-nav-title {
  font-family: var(--serif); font-size: 23px; letter-spacing: -.01em;
  color: var(--ink); padding: 0 10px 16px;
}
.settings-nav-item {
  text-align: left; font-family: inherit; font-size: 13.5px; color: var(--ink-soft);
  background: none; border: none; border-radius: var(--r-sm); padding: 9px 11px;
  cursor: pointer; transition: background .12s ease, color .12s ease;
}
.settings-nav-item:hover { background: var(--paper); }
.settings-nav-item.active { background: var(--coral-soft); color: var(--coral-deep); font-weight: 600; }

.settings-main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.settings-panes { flex: 1; overflow-y: auto; padding: 34px 36px; }
.settings-pane { display: none; }
.settings-pane.active { display: block; animation: fadeIn .16s ease; }
.settings-pane h3 { font-size: 20px; font-weight: 600; letter-spacing: -.01em; color: var(--ink); margin-bottom: 8px; }
.settings-desc { font-size: 13px; color: var(--muted); line-height: 1.6; margin-bottom: 18px; }
.settings-desc a { color: var(--coral); text-decoration: none; }
.settings-desc a:hover { text-decoration: underline; }
.settings-desc code { font-size: 12px; background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }

.settings-field { display: block; margin-bottom: 16px; }
.settings-label {
  display: flex; align-items: baseline; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--ink-soft); margin-bottom: 8px;
}
.settings-label a { font-size: 12px; font-weight: 500; color: var(--coral); text-decoration: none; }
.settings-label a:hover { text-decoration: underline; }
.settings-opt { font-size: 12px; font-weight: 400; color: var(--faint); }

.storage-stat { display: flex; gap: 32px; margin: 6px 0 20px; }
.storage-stat > div { display: flex; flex-direction: column; gap: 3px; }
.storage-stat b { font-family: var(--serif); font-size: 24px; color: var(--ink); letter-spacing: -.01em; font-variant-numeric: tabular-nums; }
.storage-stat span { font-size: 11.5px; color: var(--muted); }

.settings-test-row { display: flex; align-items: center; gap: 12px; margin-top: 6px; }
.test-res { font-size: 12.5px; line-height: 1.45; }
.test-res.testing { color: var(--muted); }
.test-res.ok { color: #4a7c4e; }
.test-res.bad { color: var(--coral-deep); }

.settings-foot {
  display: flex; align-items: center; justify-content: flex-end; gap: 14px;
  padding: 15px 36px; border-top: 1px solid var(--line); background: var(--card);
}
.settings-msg { font-size: 12.5px; color: var(--muted); margin-right: auto; }
.settings-msg.ok { color: #4a7c4e; }
.settings-msg.bad { color: var(--coral-deep); }

@media (max-width: 680px) {
  .settings-overlay { padding: 12px; }
  .settings-modal { flex-direction: column; height: 90vh; max-height: 90vh; }
  .settings-nav {
    width: auto; flex-direction: row; gap: 6px; overflow-x: auto;
    border-right: none; border-bottom: 1px solid var(--line); padding: 12px;
  }
  .settings-nav-title { display: none; }
  .settings-nav-item { white-space: nowrap; }
  .settings-panes { padding: 24px; }
  .settings-foot { padding: 14px 24px; }
}
```

---
