# Verbatim — 完整源代码合并文档

> 生成时间：2026-07-06 21:19　·　音视频转写 + 博主观点研究台（本地自托管 Flask）

## 目录

- [`app.py`](#apppy) — Flask 主应用：路由 · 转写任务 · 链条编排 · 鉴权 · 统计（1261 行）
- [`config.py`](#configpy) — 配置与并发/限流参数（61 行）
- [`taskdb.py`](#taskdbpy) — 任务持久化（SQLite）+ 重启恢复（81 行）
- [`downloader.py`](#downloaderpy) — yt-dlp 下载（probe/download_one，cookies + zh-CN 标题 + 封面）（163 行）
- [`analyze.py`](#analyzepy) — 逐期分析 + 跨期总合成（Gemini）（147 行）
- [`summarize.py`](#summarizepy) — AI 内容摘要（Gemini / Qwen）（154 行）
- [`enrich.py`](#enrichpy) — AI 卡片元数据（标题/一句话/标签）（130 行）
- [`transcribe_whisper.py`](#transcribewhisperpy) — 本地引擎：faster-whisper（回退 openai-whisper）（116 行）
- [`transcribe_gemini.py`](#transcribegeminipy) — 云端引擎：Gemini 2.5 Pro（389 行）
- [`transcribe_dashscope.py`](#transcribedashscopepy) — 云端引擎：阿里云 Paraformer（265 行）
- [`transcribe_precise.py`](#transcribeprecisepy) — 精准模式：Gemini 文字 + 阿里云说话人分离合并（169 行）
- [`templates/index.html`](#templatesindexhtml) — 单页结构（Tab / 详情 / 文档阅读 / 统计面板）（294 行）
- [`static/app.js`](#staticappjs) — 前端逻辑（上传/SSE/历史/链条/文档/统计/Markdown 渲染）（1302 行）
- [`static/style.css`](#staticstylecss) — Claude 风格设计系统（衬线大标题 · 珊瑚色 · 暖白）（385 行）
- [`requirements.txt`](#requirementstxt) — Python 依赖（9 行）
- [`run.sh`](#runsh) — 启动脚本（10 行）

**合计 4936 行源码。**

---


<a id="apppy"></a>
## `app.py`

> Flask 主应用：路由 · 转写任务 · 链条编排 · 鉴权 · 统计

```python
"""
Flask application for audio/video transcription.
Supports Whisper (local) and Gemini (cloud) engines with SSE progress streaming.
Persists results (audio + transcript + summary) to disk for history playback.
"""

import json
import os
import queue
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

os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(config.RESULTS_FOLDER, exist_ok=True)

taskdb.init()

tasks = {}


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
    if supplied == config.AUTH_TOKEN:
        return None
    return jsonify({'error': 'unauthorized：请在 URL 加 ?token=你的令牌'}), 401


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

    cmd = [
        ffmpeg_bin, '-y', '-v', 'error',
        '-i', video_path,
        '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
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


def _save_results(task_id, original_filename, engine, audio_source_path,
                  segments, summary):
    """Persist transcription results to results/<task_id>/."""
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    os.makedirs(task_dir, exist_ok=True)

    ext = os.path.splitext(audio_source_path)[1].lower()
    audio_dest = os.path.join(task_dir, f"audio{ext}")
    shutil.copy2(audio_source_path, audio_dest)

    duration = probe_audio_duration_seconds(audio_dest)

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
    with open(os.path.join(task_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(task_dir, 'transcript.json'), 'w', encoding='utf-8') as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    if summary:
        with open(os.path.join(task_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


def run_transcription(task_id, filepath, engine, original_filename, q,
                      speaker_count=None):
    """Background worker: runs transcription, saves results, pushes events.

    每个引擎有独立信号量限流。任务提交后可能先排队（quota 已满），
    抢到信号量后才真正开跑，所以先推一条 queued，再推 progress。
    """

    def progress_cb(percent):
        q.put(json.dumps({
            'type': 'progress',
            'percent': percent,
            'message': f'转写中... {percent}%',
        }))

    cleanup_paths = [filepath]
    input_path = filepath

    # 排队等待本引擎的并发额度
    sem = _engine_semaphores.get(engine)
    q.put(json.dumps({'type': 'queued', 'message': '排队中...'}))
    if sem is not None:
        sem.acquire()

    try:
        taskdb.set_status(task_id, 'running')
        q.put(json.dumps({
            'type': 'progress',
            'percent': 1,
            'message': '开始转写...',
        }))
        if is_video_file(filepath):
            q.put(json.dumps({
                'type': 'progress',
                'percent': 2,
                'message': '正在从视频中提取音频...',
            }))
            audio_path = os.path.join(
                app.config['UPLOAD_FOLDER'],
                f"{task_id}_audio.wav",
            )
            input_path = extract_audio_from_video(filepath, audio_path)
            cleanup_paths.append(input_path)

        segments = []
        summary_data = None

        if engine == 'whisper':
            from transcribe_whisper import transcribe_audio

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': '正在加载 Whisper 模型（首次可能需要下载）...',
            }))

            raw_segments = transcribe_audio(input_path, progress_callback=progress_cb)

            for seg in raw_segments:
                item = {
                    'timestamp': format_seconds(seg['start']),
                    'end': format_seconds(seg['end']),
                    'text': seg['text'].strip(),
                }
                segments.append(item)
                q.put(json.dumps({'type': 'segment', **item}))

            full_text = "\n".join(
                f"[{s['timestamp']}] {s['text']}" for s in segments
            )
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

        elif engine == 'dashscope':
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
                    'message': '阿里云分离未成功，仅输出文字（无说话人）',
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

        _save_results(task_id, original_filename, engine, input_path,
                      segments, summary_data)

        # 生成列表卡片元数据（AI 标题/一句话/标签），失败不影响主流程
        try:
            from enrich import enrich_task
            enrich_task(os.path.join(config.RESULTS_FOLDER, task_id))
        except Exception:
            pass

        taskdb.set_status(task_id, 'done')
        q.put(json.dumps({
            'type': 'done',
            'task_id': task_id,
            'segments': segments,
            'summary': summary_data,
        }))

    except Exception as e:
        taskdb.set_status(task_id, 'failed', error=str(e))
        q.put(json.dumps({
            'type': 'error',
            'message': str(e),
        }))

    finally:
        if sem is not None:
            sem.release()
        for path in cleanup_paths:
            try:
                os.remove(path)
            except OSError:
                pass
        # worker 完成后才从全局表里清掉自己，
        # 这样客户端断开/刷新后重连依然能读到队列里剩下的消息。
        tasks.pop(task_id, None)


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

    executor.submit(
        run_transcription, task_id, filepath, engine, file.filename, q,
        speaker_count,
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

    # 孤儿上传文件清理（不属于任何已恢复任务的残留）
    cleaned = 0
    for name in os.listdir(config.UPLOAD_FOLDER):
        if not any(name.startswith(tid) for tid in active_ids):
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

@app.route('/api/history')
def api_history():
    """List all saved transcription sessions."""
    results_dir = config.RESULTS_FOLDER
    entries = []

    if not os.path.isdir(results_dir):
        return jsonify(entries)

    for name in os.listdir(results_dir):
        meta_path = os.path.join(results_dir, name, 'meta.json')
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    entries.append(json.load(f))
            except Exception:
                pass

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
            hits.append({**meta, 'snippet': snippet})

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


def _chain_dir(chain_id):
    return os.path.join(CHAINS_DIR, chain_id)


def _save_chain(state):
    with open(os.path.join(_chain_dir(state['id']), 'chain.json'),
              'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
        from downloader import probe, download_one

        # ---- 1. 解析目标（拿到标题 + 封面）----
        state['stage'] = 'downloading'
        _save_chain(state)
        targets = probe(state['url'], state.get('max_videos'))
        if not targets:
            raise RuntimeError('No downloadable videos at this link')

        # 预置视频网格：一开始就把全部目标铺出来，前端详情页能立刻看到
        videos = [{
            'index': i,
            'title': t.get('title') or t.get('video_id') or f'视频{i + 1}',
            'video_id': t.get('video_id', ''),
            'thumbnail': t.get('thumbnail', ''),
            'status': 'downloading',
            'task_id': None,
        } for i, t in enumerate(targets)]
        state['videos'] = videos
        state['download_total'] = len(targets)
        state['download_done'] = 0
        _save_chain(state)

        # ---- 2. 边下边转：并发下载（全局限流），每个下完立刻提交转写 ----
        state['stage'] = 'transcribing'

        def _download_and_submit(i, target):
            v = videos[i]
            with _chain_download_sem:            # 全局下载闸
                with lock:
                    state['current'] = v['title']
                item = download_one(target, dl_dir)
            with lock:
                state['download_done'] = state.get('download_done', 0) + 1
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
            )
            v['task_id'] = task_id
            v['title'] = item['title']
            v['video_id'] = item['video_id']
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
            raise RuntimeError('No audio downloaded — bad link, login required, or all downloads failed')

        # ---- 3. 等全部转写落定（轮询 taskdb）----
        pending = {v['task_id'] for v in submitted}
        while pending:
            time.sleep(5)
            for v in submitted:
                if v['task_id'] not in pending:
                    continue
                row = taskdb.get(v['task_id'])
                if row and row['status'] in ('done', 'failed'):
                    v['status'] = row['status']
                    if row['status'] == 'failed':
                        v['error'] = row.get('error') or ''
                    pending.discard(v['task_id'])
            save()

        # ---- 4. 逐期分析（全局分析闸；每期一次独立调用，防丢信息）----
        if state.get('analyze'):
            state['stage'] = 'analyzing'
            state['analyzed_done'] = 0
            save()
            from analyze import analyze_transcript, synthesize

            def _analyze_one(v):
                if v['status'] != 'done':
                    return None
                tpath = os.path.join(
                    config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
                if not os.path.isfile(tpath):
                    return None
                try:
                    with open(tpath, 'r', encoding='utf-8') as fh:
                        segs = json.load(fh)
                    text = '\n'.join(
                        f"[{s.get('timestamp', '')}] {s.get('text', '')}"
                        for s in segs
                    )
                    with _chain_analysis_sem:    # 全局分析闸
                        md = analyze_transcript(v['title'], text, state['author'])
                    fname = f"分析_{v['index'] + 1:03d}_{_safe_doc_name(v['title'])}.md"
                    with open(os.path.join(chain_dir, fname),
                              'w', encoding='utf-8') as fh:
                        fh.write(md)
                    return (v['title'], md)
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
            analyses = [r for r in results if r]
            state['analyzed_ok'] = len(analyses)

            # ---- 5. 总合成 ----
            if analyses:
                state['stage'] = 'synthesizing'
                save()
                total_md = synthesize(analyses, state['author'])
                with open(os.path.join(chain_dir, '总分析.md'),
                          'w', encoding='utf-8') as fh:
                    fh.write(total_md)
                state['final_doc'] = '总分析.md'

        state['stage'] = 'done'
    except Exception as e:  # noqa: BLE001
        state['stage'] = 'failed'
        state['error'] = str(e)
    finally:
        state['finished_at'] = __import__('datetime').datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S')
        _save_chain(state)
        shutil.rmtree(dl_dir, ignore_errors=True)


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
                        entries.append(json.load(f))
                except Exception:
                    pass
    entries.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return jsonify(entries)


@app.route('/api/chain/<chain_id>')
def api_chain_detail(chain_id):
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    with open(cpath, 'r', encoding='utf-8') as f:
        return jsonify(json.load(f))


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
                meta['char_count'] = cc
                try:
                    with open(meta_path, 'w', encoding='utf-8') as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
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

    return jsonify({
        'totals': {
            'transcripts': total,
            'hours': round(total_seconds / 3600, 1),
            'chars': total_chars,
            'segments': total_segments,
        },
        'engines': dict(engines),
        'top_tags': [{'tag': t, 'count': c} for t, c in top_tags],
        'timeline': timeline,
    })


if __name__ == '__main__':
    # debug 模式下 werkzeug 起两个进程（reloader 父进程 + 真正服务的子进程），
    # 恢复逻辑只在服务子进程（WERKZEUG_RUN_MAIN=true）里跑，避免同一任务被跑两遍。
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        recover_unfinished_tasks()
    app.run(debug=True, threaded=True,
            port=int(os.environ.get('PORT', 5001)))

```


<a id="configpy"></a>
## `config.py`

> 配置与并发/限流参数

```python
import os
from dotenv import load_dotenv

load_dotenv()

# Flask
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
RESULTS_FOLDER = os.path.join(os.path.dirname(__file__), 'results')
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB max upload
AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'webm'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'mkv', 'avi', 'm4v'}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# 批量转录并发控制：一次可以丢进很多文件，但真正同时运行的数量按引擎区分。
# 本地 Whisper 每个任务都吃满 CPU/内存，必须保守；云引擎只是提交请求，可以放开。
ENGINE_CONCURRENCY = {
    'whisper': 2,
    # Paid Tier 1（~150 RPM）下 8 路并发稳妥。注意每个文件不止一次请求
    # （上传 + 长音频分段各一次），所以实际 QPS 会更高；若大量 429/空文本再下调。
    'gemini': 8,
    'dashscope': 9,
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
WHISPER_MODEL_SIZE = 'small'
WHISPER_DEVICE = 'cpu'
# None/空 = 自动检测语言（推荐，英文录音不会再被强制转成中文）；填 'zh' 可强制中文
WHISPER_LANGUAGE = os.environ.get('WHISPER_LANGUAGE') or None

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = 'gemini-2.5-pro'
# 卡片元数据（标题/标签）生成用 Flash：快、便宜，质量足够
GEMINI_ENRICH_MODEL = 'gemini-2.5-flash'
GEMINI_INLINE_LIMIT = 19 * 1024 * 1024  # 19 MB, use File API above this

# DashScope (阿里云百炼)
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', '')
DASHSCOPE_ASR_MODEL = 'paraformer-v2'
DASHSCOPE_LLM_MODEL = 'qwen-plus'

```


<a id="taskdbpy"></a>
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

DB_PATH = os.path.join(os.path.dirname(__file__), 'tasks.db')

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


<a id="downloaderpy"></a>
## `downloader.py`

> yt-dlp 下载（probe/download_one，cookies + zh-CN 标题 + 封面）

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
import re
import shutil
import subprocess

from config import YTDLP_LANG, YTDLP_COOKIES_FROM_BROWSER

_PROBE_TIMEOUT = 120        # 元数据解析超时（秒）
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


def probe(url, max_videos=None):
    """解析 URL 元数据（不下载），返回目标视频列表。

    每个元素：{'video_url', 'title', 'video_id', 'thumbnail'}
    """
    binary = _resolve_ytdlp()
    cmd = [binary, '--flat-playlist', '-J', '--no-warnings',
           *_lang_args(), *_cookie_args()]
    if max_videos:
        cmd += ['--playlist-end', str(max_videos)]
    cmd.append(url)

    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
    )
    if result.returncode != 0 or not result.stdout.strip():
        err = (result.stderr or '').strip().splitlines()
        detail = err[-1][:200] if err else 'unknown error'
        raise RuntimeError(f'Could not parse link: {detail}')

    info = json.loads(result.stdout)

    if info.get('_type') == 'playlist':
        entries = [e for e in (info.get('entries') or []) if e]
        if max_videos:
            entries = entries[:max_videos]
        targets = []
        for e in entries:
            targets.append({
                'video_url': e.get('url') or e.get('webpage_url') or e.get('id'),
                'title': e.get('title', ''),
                'video_id': e.get('id', ''),
                'thumbnail': _thumbnail_for(e),
            })
        return targets

    return [{
        'video_url': url,
        'title': info.get('title', ''),
        'video_id': info.get('id', ''),
        'thumbnail': _thumbnail_for(info),
    }]


def download_one(target, dest_dir):
    """下载单个目标的音频，返回 {'path','title','video_id','thumbnail'} 或 None。"""
    os.makedirs(dest_dir, exist_ok=True)
    binary = _resolve_ytdlp()
    outtmpl = os.path.join(dest_dir, '%(title)s [%(id)s].%(ext)s')
    cmd = [
        binary,
        '-x', '--audio-format', 'mp3', '--audio-quality', '128K',
        '-o', outtmpl,
        '--no-playlist', '--no-warnings', '--quiet',
        *_lang_args(), *_cookie_args(),
        # 下载+后处理完成后打印最终文件路径和元信息，逐行读取
        '--print', 'after_move:filepath',
        '--print', 'after_move:title',
        '--print', 'after_move:id',
        '--no-simulate',
        target['video_url'],
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_DOWNLOAD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    path, real_title, video_id = lines[0], lines[1], lines[2]
    if not os.path.isfile(path):
        return None
    return {
        'path': path,
        'title': real_title or target.get('title') or 'untitled',
        'video_id': video_id or target.get('video_id', ''),
        'thumbnail': target.get('thumbnail', ''),
    }


def download_audios(url, dest_dir, max_videos=None, progress_cb=None):
    """（兼容旧接口）串行下载 URL 指向的所有音频，返回成功项列表。"""
    targets = probe(url, max_videos)
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


<a id="analyzepy"></a>
## `analyze.py`

> 逐期分析 + 跨期总合成（Gemini）

```python
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

```


<a id="summarizepy"></a>
## `summarize.py`

> AI 内容摘要（Gemini / Qwen）

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

```


<a id="enrichpy"></a>
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

from config import GEMINI_API_KEY, GEMINI_ENRICH_MODEL

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

要求：中文输出；标题要具体（宁可写"杜甫生平纪录片解说"也不要写"历史内容"）；
若内容明显是废稿/空白/无意义，title 写"（内容为空或无效）"。"""


def generate_card_meta(filename, content):
    """生成 {title, one_line, tags}；失败返回 None（调用方自行兜底）。"""
    api_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not api_key or not content or len(content.strip()) < 10:
        return None

    # 控制输入长度：overview 本来就短；退回正文时只取开头
    content = content.strip()[:3000]

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=GEMINI_ENRICH_MODEL,
            contents=ENRICH_PROMPT.format(filename=filename, content=content),
        )
        raw = (resp.text or '').strip()
    except Exception:
        return None

    return _parse_json(raw)


def _parse_json(raw):
    m = re.search(r'```json\s*(.*?)\s*```', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    raw = raw.strip()
    if not raw.startswith('{'):
        start = raw.find('{')
        if start != -1:
            raw = raw[start:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
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


<a id="transcribewhisperpy"></a>
## `transcribe_whisper.py`

> 本地引擎：faster-whisper（回退 openai-whisper）

```python
"""
Whisper transcription engine (local).

优先使用 faster-whisper（CTranslate2 实现：同精度、约 4 倍速度、内存减半，
且自带 VAD 跳过静音段）；未安装或初始化失败时自动回退到 openai-whisper，
保证本地始终可用。

Returns 与旧实现完全一致：list of {'start': float, 'end': float, 'text': str}。
"""

import threading

from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# Lazy singleton model
_model = None
_backend = None  # 'faster' | 'openai'
_model_lock = threading.Lock()


def get_model():
    """Load Whisper model (lazy, thread-safe singleton). 优先 faster-whisper。"""
    global _model, _backend
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel

                    # CPU 上 int8 量化最快且精度损失可忽略
                    compute = 'int8' if WHISPER_DEVICE == 'cpu' else 'float16'
                    _model = WhisperModel(
                        WHISPER_MODEL_SIZE,
                        device=WHISPER_DEVICE,
                        compute_type=compute,
                    )
                    _backend = 'faster'
                except Exception:
                    # faster-whisper 不可用（未安装/模型下载失败等）→ 回退旧实现
                    import whisper

                    _model = whisper.load_model(
                        WHISPER_MODEL_SIZE, device=WHISPER_DEVICE
                    )
                    _backend = 'openai'
    return _model


def transcribe_audio(filepath, progress_callback=None):
    """
    Run Whisper transcription on an audio file.

    Args:
        filepath: Path to the audio file.
        progress_callback: Optional callable(percent: int) for progress updates.

    Returns:
        List of segment dicts with keys: start (float), end (float), text (str).
    """
    model = get_model()

    if _backend == 'faster':
        return _transcribe_faster(model, filepath, progress_callback)
    return _transcribe_openai(model, filepath, progress_callback)


def _transcribe_faster(model, filepath, progress_callback=None):
    """faster-whisper 路径：流式产出 segment，按已处理时长报进度。"""
    segments_iter, info = model.transcribe(
        filepath,
        language=WHISPER_LANGUAGE,  # None = 自动检测
        vad_filter=True,            # 跳过静音，长音频显著提速
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
        )
    finally:
        whisper_transcribe.tqdm.tqdm = original_tqdm

    return result.get('segments', [])

```


<a id="transcribegeminipy"></a>
## `transcribe_gemini.py`

> 云端引擎：Gemini 2.5 Pro

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
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_INLINE_LIMIT

TRANSCRIPTION_PROMPT = """请对这段音频进行精确的逐字转录。

要求：
1. 严格保留音频原始语言，绝对不要翻译：中文就输出中文，英文就输出英文，日文就输出日文，多语种混杂则按说话人实际使用的语言原样转录。
2. 每隔约 30 秒在新一行的开头插入一个时间戳，格式为 [HH:MM:SS]（例如 [00:00:00]、[00:01:45]）。
3. 完整保留标点符号和说话人的语气。
4. 专有名词、人名、地名、品牌名保留原文拼写，不要音译。
5. 不要输出任何解释、说明、或 Markdown 包裹，只输出纯转录文本。

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
    try:
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=600_000),
        )
    except Exception:
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

        while uploaded.state.name == "PROCESSING":
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
                model=GEMINI_MODEL,
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

```


<a id="transcribedashscopepy"></a>
## `transcribe_dashscope.py`

> 云端引擎：阿里云 Paraformer

```python
"""
DashScope transcription engine (阿里云百炼).
Uses Paraformer ASR via REST API for speech-to-text.
"""

import json
import os
import time

import requests as http_requests

from config import DASHSCOPE_API_KEY, DASHSCOPE_ASR_MODEL

BASE_URL = 'https://dashscope.aliyuncs.com/api/v1'


def transcribe_audio(filepath, progress_callback=None, diarization=False,
                     speaker_count=None):
    """
    Transcribe audio using DashScope Paraformer via REST API.

    Args:
        diarization: 开启说话人分离（声纹），返回的每段会带 'speaker' 字段。
        speaker_count: 已知说话人数量时传入作为提示，能显著改善聚类；
            不填（None）则让阿里云自动判断人数。

    Returns:
        List of segment dicts with keys: timestamp (str), text (str),
        以及开启 diarization 时的 speaker。
    """
    api_key = DASHSCOPE_API_KEY or os.environ.get('DASHSCOPE_API_KEY', '')
    if not api_key:
        raise RuntimeError(
            "DashScope API Key 未设置。请在 .env 文件中设置 DASHSCOPE_API_KEY。"
        )

    if progress_callback:
        progress_callback(5)

    file_url = _upload_file(filepath, api_key)

    if progress_callback:
        progress_callback(15)

    task_id = _submit_task(file_url, api_key, diarization=diarization,
                           speaker_count=speaker_count)

    if progress_callback:
        progress_callback(20)

    result = _poll_task(task_id, api_key, progress_callback)

    if progress_callback:
        progress_callback(90)

    segments = _parse_result(result)

    if progress_callback:
        progress_callback(100)

    return segments


def _upload_file(filepath, api_key):
    """Upload a local file to DashScope temporary OSS and return oss:// URL."""
    filename = os.path.basename(filepath)

    policy_resp = http_requests.get(
        f'{BASE_URL}/uploads',
        headers={'Authorization': f'Bearer {api_key}'},
        params={'action': 'getPolicy', 'model': DASHSCOPE_ASR_MODEL},
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


def _submit_task(file_url, api_key, diarization=False, speaker_count=None):
    """Submit a transcription task via REST API and return task_id."""
    parameters = {
        'language_hints': ['zh', 'en'],
    }
    if diarization:
        # paraformer-v2 声纹说话人分离
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
            'model': DASHSCOPE_ASR_MODEL,
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
        return segments

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
            begin_ms = sent.get('begin_time', 0)
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


<a id="transcribeprecisepy"></a>
## `transcribe_precise.py`

> 精准模式：Gemini 文字 + 阿里云说话人分离合并

```python
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

from config import GEMINI_API_KEY, GEMINI_MODEL
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
    try:
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=600_000),
        )
    except Exception:
        return genai.Client(api_key=api_key)


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

```


<a id="templatesindexhtml"></a>
## `templates/index.html`

> 单页结构（Tab / 详情 / 文档阅读 / 统计面板）

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
                    <div class="tagline">Audio → transcript → insight · local</div>
                </div>
                <nav class="main-nav">
                    <button class="nav-tab active" data-tab="transcribe">Transcribe</button>
                    <button class="nav-tab" data-tab="chain">Pipeline</button>
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
                               accept=".mp3,.wav,.flac,.m4a,.ogg,.webm,.mp4,.mov,.mkv,.avi,.m4v" required>
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
                            <label class="radio-option">
                                <input type="radio" name="engine" value="dashscope">
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>DashScope</strong>
                                    <small>Paraformer ASR · cheap · Chinese-tuned</small>
                                </span>
                            </label>
                            <label class="radio-option">
                                <input type="radio" name="engine" value="precise">
                                <span class="radio-custom"></span>
                                <span class="radio-label">
                                    <strong>Precise</strong>
                                    <small>Gemini text + DashScope diarization · slow</small>
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

            <!-- ===== Tab: Pipeline ===== -->
            <section id="tab-chain" class="tab-panel">
                <div class="hero">
                    <h1>Analyze a <span class="accent">creator.</span></h1>
                    <p>Paste a channel, playlist, or video. Verbatim downloads the audio, transcribes every
                       episode, writes a per-episode analysis, and synthesizes one document.</p>
                </div>

                <form class="chain-form-shell">
                    <div class="chain-form">
                        <input type="url" id="chain-url" class="search-input"
                               placeholder="https://www.youtube.com/@creator/videos  ·  or a single video">
                        <div class="chain-form-row">
                            <input type="text" id="chain-author" class="search-input chain-small"
                                   placeholder="Author (optional)">
                            <input type="number" id="chain-max" class="search-input chain-small"
                                   min="1" max="300" placeholder="Max videos">
                            <select id="chain-engine" class="search-input chain-small">
                                <option value="gemini">Gemini (recommended)</option>
                                <option value="dashscope">DashScope</option>
                                <option value="whisper">Whisper (local)</option>
                            </select>
                            <label class="chain-check">
                                <input type="checkbox" id="chain-analyze" checked>
                                Analyze &amp; synthesize
                            </label>
                            <button id="chain-start" type="button" class="btn-primary chain-btn">Start</button>
                        </div>
                    </div>
                    <p class="chain-hint">YouTube tip: use <code>channel/videos</code>. Bilibili &amp; other
                       yt-dlp sites work too.</p>
                </form>

                <div id="chain-list" class="chain-list"></div>
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
                                <div id="engine-filters" class="filter-chips">
                                    <button class="chip active" data-engine="">All</button>
                                    <button class="chip" data-engine="gemini">Gemini</button>
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
                        <p class="chain-hint">Documents produced by the Pipeline. Click to read — no download needed.</p>
                        <div id="docs-list" class="docs-list">
                            <p class="history-empty">No analysis documents yet — run a Pipeline</p>
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

        <!-- ===== Chain Detail View (episode grid) ===== -->
        <div id="chain-detail-view" class="hidden">
            <div class="detail-top-bar">
                <button id="chain-detail-back" class="btn-secondary">← Back</button>
                <span id="chain-detail-title" class="detail-title"></span>
            </div>
            <div id="chain-detail-meta" class="detail-meta"></div>
            <div id="chain-detail-grid" class="video-grid"></div>
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
                <div class="stats-label">Fields you follow</div>
                <div id="stats-tags"></div>
            </div>
        </div>
        <button id="stats-toggle" class="stats-toggle" title="Your stats">
            <span class="stats-dot"></span><span>Your stats</span>
        </button>
    </div>

    <script src="/static/app.js"></script>
</body>
</html>

```


<a id="staticappjs"></a>
## `static/app.js`

> 前端逻辑（上传/SSE/历史/链条/文档/统计/Markdown 渲染）

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
    dashscope: 'DashScope',
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

// ========== File input (multi-file) ==========
fileInput.addEventListener('change', () => {
    updateFileLabel(fileInput.files);
});

function updateFileLabel(files) {
    if (!files || files.length === 0) {
        fileLabelText.textContent = 'Choose or drop audio / video files (multiple ok)';
        fileInfo.textContent = '';
        fileLabel.classList.remove('has-file');
        return;
    }

    let totalSize = 0;
    for (const f of files) totalSize += f.size;

    if (files.length === 1) {
        fileLabelText.textContent = files[0].name;
    } else {
        fileLabelText.textContent = `${files.length} files selected`;
    }
    fileInfo.textContent = formatFileSize(totalSize);
    fileLabel.classList.add('has-file');
}

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
    if (files && files.length > 0) {
        fileInput.files = files;
        updateFileLabel(files);
    }
});

// ========== Engine selection: toggle 预计人数 (precise only) ==========
const speakerCountGroup = document.getElementById('speaker-count-group');
document.querySelectorAll('input[name="engine"]').forEach(radio => {
    radio.addEventListener('change', () => {
        const isPrecise = document.querySelector('input[name="engine"]:checked').value === 'precise';
        speakerCountGroup.classList.toggle('hidden', !isPrecise);
    });
});

// ========== Form submission (batch) ==========
// 同时上传的文件数。逐个上传是为了绕开单请求体积上限（几百个文件塞一个请求会超限被拒），
// 上传本身也限流，避免一次性发起过多大文件上传拖垮网络/内存。
// 真正的转写并发由服务器端每引擎的信号量控制（见 config.ENGINE_CONCURRENCY）。
const UPLOAD_CONCURRENCY = 3;

form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!fileInput.files.length) return;

    const engine = document.querySelector('input[name="engine"]:checked').value;
    const files = Array.from(fileInput.files);

    errorSection.classList.add('hidden');
    batchSection.classList.remove('hidden');
    batchList.innerHTML = '';
    batchTotal = files.length;
    batchFinished = 0;
    updateBatchProgress();

    submitBtn.disabled = true;
    submitBtn.textContent = 'Transcribing…';

    // 按选择顺序为每个文件先建一行，再用上传池逐个提交到 /upload
    const jobs = files.map(file => {
        const row = createBatchRow(file.name);
        setRowStatus(row, 'Queued', 'queued');
        batchList.appendChild(row);
        return { file, row };
    });

    await runUploadPool(jobs, engine, UPLOAD_CONCURRENCY);
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
    submitBtn.disabled = false;
    submitBtn.textContent = 'Transcribe';
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

async function pollEnrichStatus() {
    try {
        const resp = await fetch('/api/enrich_status');
        const st = await resp.json();
        if (st.running) {
            enrichBtn.textContent = `${st.done}/${st.total}`;
            // 每整理完几条就刷新列表，让标题逐步冒出来
            if (st.done > 0 && st.done % 10 === 0) renderHistory();
            setTimeout(pollEnrichStatus, 1500);
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

// ========== 链条：URL → 下载 → 转写 → 分析 → 总合成 ==========
const chainUrl = document.getElementById('chain-url');
const chainAuthor = document.getElementById('chain-author');
const chainMax = document.getElementById('chain-max');
const chainEngine = document.getElementById('chain-engine');
const chainAnalyze = document.getElementById('chain-analyze');
const chainStartBtn = document.getElementById('chain-start');
const chainList = document.getElementById('chain-list');

const CHAIN_STAGE_LABELS = {
    starting: 'Starting',
    downloading: 'Downloading',
    transcribing: 'Transcribing',
    analyzing: 'Analyzing',
    synthesizing: 'Synthesizing',
    done: 'Done',
    failed: 'Failed',
};

let chainPollTimer = null;

chainStartBtn.addEventListener('click', async () => {
    const url = (chainUrl.value || '').trim();
    if (!url) { chainUrl.focus(); return; }
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
                analyze: chainAnalyze.checked,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Failed to create');
        chainUrl.value = '';
        loadChains();
    } catch (err) {
        alert('Failed to start pipeline: ' + err.message);
    } finally {
        chainStartBtn.disabled = false;
    }
});

function chainProgressText(c) {
    const parts = [];
    const vids = c.videos || [];
    if (c.download_total) {
        const dlFail = vids.filter(v => v.status === 'download_failed').length;
        parts.push(`Downloaded ${c.download_done || 0}/${c.download_total}` +
            (dlFail ? ` (${dlFail} failed)` : ''));
    }
    // 转写基数只算下载成功、真正提交了转写的视频
    const submitted = vids.filter(v => v.status !== 'download_failed');
    if (submitted.length) {
        const done = submitted.filter(v => v.status === 'done').length;
        const failed = submitted.filter(v => v.status === 'failed').length;
        parts.push(`Transcribed ${done}/${submitted.length}` + (failed ? ` (${failed} failed)` : ''));
    }
    if (c.analyze && c.analyzed_done != null && submitted.length) {
        parts.push(`Analyzed ${c.analyzed_done}/${submitted.length}`);
    }
    return parts.join(' · ');
}

function renderChains(chains) {
    if (!chains.length) {
        chainList.innerHTML = '';
        return;
    }
    chainList.innerHTML = chains.map(c => {
        const stage = CHAIN_STAGE_LABELS[c.stage] || c.stage;
        const active = !['done', 'failed'].includes(c.stage);
        const prog = chainProgressText(c);
        let links = '';
        if (c.final_doc) {
            links += `<a class="chain-doc-link" href="#"
                onclick="openDocView('${c.id}','${encodeURIComponent(c.final_doc)}');return false;">Read synthesis</a>`;
        }
        if (['done', 'failed'].includes(c.stage)) {
            links += `<a class="chain-doc-link" href="#"
                onclick="gotoDocs();return false;">All documents</a>
                <a class="chain-doc-link chain-del" href="#"
                onclick="deleteChain('${c.id}');return false;">Delete</a>`;
        }
        const err = c.stage === 'failed'
            ? `<div class="chain-error">${(c.error || '').slice(0, 200)}</div>` : '';
        const cur = active && c.current
            ? `<div class="chain-current">${c.current.slice(0, 60)}</div>` : '';
        return `<div class="chain-item ${active ? 'chain-active' : ''}"
                onclick="openChainDetail('${c.id}')" title="点击查看每个视频的进度">
            <div class="chain-item-top">
                <span class="chain-stage">${stage}</span>
                <span class="chain-url" title="${c.url}">${c.author !== '该博主' ? c.author + ' · ' : ''}${c.url.slice(0, 60)}</span>
                <span class="chain-links" onclick="event.stopPropagation()">${links}</span>
            </div>
            <div class="chain-progress">${prog}</div>
            ${cur}${err}
        </div>`;
    }).join('');
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
    window.scrollTo({ top: 0 });
    await refreshChainDetail();
}

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
    chainDetailTitle.textContent =
        (c.author && c.author !== '该博主') ? c.author : (c.url || 'Pipeline');
    chainDetailMeta.textContent =
        `${CHAIN_STAGE_LABELS[c.stage] || c.stage} · ${chainProgressText(c)}`;

    const vids = c.videos || [];
    chainDetailGrid.innerHTML = vids.map(v => {
        const st = VIDEO_STATUS[v.status] || { label: v.status, cls: '' };
        const clickable = v.status === 'done' && v.task_id;
        const thumb = v.thumbnail
            ? `<img class="vg-thumb" src="${v.thumbnail}" loading="lazy" alt=""
                 onerror="this.style.display='none'">`
            : '<div class="vg-thumb vg-noimg">▷</div>';
        const onclick = clickable
            ? ` onclick="openDetailView('${v.task_id}')" title="View transcript"` : '';
        return `<div class="vg-card ${clickable ? 'vg-clickable' : ''}"${onclick}>
            ${thumb}
            <div class="vg-badge ${st.cls}">${st.label}</div>
            <div class="vg-title" title="${(v.title || '').replace(/"/g, '&quot;')}">${v.title || ''}</div>
        </div>`;
    }).join('') || '<p class="history-empty">Resolving episode list…</p>';

    clearTimeout(chainDetailTimer);
    if (!['done', 'failed'].includes(c.stage)) {
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

if (chainDetailBack) chainDetailBack.addEventListener('click', closeChainDetail);

async function loadChains() {
    try {
        const resp = await fetch('/api/chains');
        const chains = await resp.json();
        renderChains(chains);
        const anyActive = chains.some(c => !['done', 'failed'].includes(c.stage));
        clearTimeout(chainPollTimer);
        if (anyActive) {
            chainPollTimer = setTimeout(loadChains, 4000);
            // 转写阶段会往历史里落新条目，顺手刷新历史
            if (typeof renderHistory === 'function') renderHistory();
        }
    } catch (e) { /* 服务重启瞬间的抖动，忽略 */ }
}

// 跳到「资料库 → 分析文档」子分区
function gotoDocs() {
    switchTab('library');
    switchLib('docs');
}

async function deleteChain(chainId) {
    if (!confirm('Delete this pipeline’s documents? (transcripts stay in the library)')) return;
    await fetch(`/api/chain/${chainId}`, { method: 'DELETE' });
    loadChains();
}

loadChains();

// ========== Tab 切换（转写 / 链条 / 资料库） ==========
const navTabs = document.querySelectorAll('.nav-tab');
const tabPanels = {
    transcribe: document.getElementById('tab-transcribe'),
    chain: document.getElementById('tab-chain'),
    library: document.getElementById('tab-library'),
};

function switchTab(name) {
    navTabs.forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    Object.entries(tabPanels).forEach(([k, el]) => {
        if (el) el.classList.toggle('active', k === name);
    });
    if (name === 'library') { renderHistory(); }
    if (name === 'chain') { loadChains(); }
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
            docsList.innerHTML = '<p class="history-empty">No analysis documents yet — run a Pipeline</p>';
            return;
        }
        const blocks = await Promise.all(withDocs.map(async c => {
            let files = [];
            try { files = await (await fetch(`/api/chain/${c.id}/files`)).json(); }
            catch { files = []; }
            files = (Array.isArray(files) ? files : []).filter(f => f.endsWith('.md'));
            if (!files.length) return '';
            // 总分析置顶，其余按名排序
            files.sort((a, b) => (a === '总分析.md' ? -1 : b === '总分析.md' ? 1 : a.localeCompare(b)));
            const title = (c.author && c.author !== '该博主') ? c.author : c.url;
            const stageBadge = c.stage === 'done' ? ''
                : `<span class="doc-stage">（${CHAIN_STAGE_LABELS[c.stage] || c.stage}）</span>`;
            const items = files.map(f => {
                const isTotal = f === 'total' || f === '总分析.md';
                const label = f === '总分析.md' ? 'Synthesis' : '' + f.replace(/^分析_\d+_/, '').replace(/\.md$/, '');
                return `<button class="doc-item ${isTotal ? 'doc-total' : ''}"
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

async function openDocView(chainId, encName) {
    const name = decodeURIComponent(encName);
    try {
        const resp = await fetch(`/api/chain/${chainId}/file?name=${encodeURIComponent(name)}`);
        if (!resp.ok) throw new Error('not found');
        const raw = await resp.text();
        currentDoc = { chainId, name, raw };
        docTitle.textContent = name.replace(/\.md$/, '');
        docContent.innerHTML = renderMarkdown(raw);
        mainView.classList.add('hidden');
        docView.classList.remove('hidden');
        window.scrollTo({ top: 0 });
    } catch {
        showToast('Could not load document');
    }
}

function closeDocView() {
    docView.classList.add('hidden');
    mainView.classList.remove('hidden');
    docContent.innerHTML = '';
    window.scrollTo({ top: 0 });
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
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function renderInline(s) {
    // 先转义，再套内联格式
    s = escapeHtml(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener">$1</a>');
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

        // 关注领域：横向条
        const tags = s.top_tags || [];
        const max = tags.length ? tags[0].count : 1;
        document.getElementById('stats-tags').innerHTML = tags.length
            ? tags.map(t => `<div class="stat-tag">
                    <span class="stat-tag-name" title="${t.tag}">${t.tag}</span>
                    <span class="stat-tag-bar"><i style="width:${Math.max(6, t.count / max * 100)}%"></i></span>
                    <span class="stat-tag-num">${t.count}</span>
                </div>`).join('')
            : '<div class="spark-empty">No tags yet — run “Auto-title”</div>';
        statsLoaded = true;
    } catch {
        document.getElementById('stats-chart').innerHTML = '<div class="spark-empty">Failed to load</div>';
    }
}

statsToggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = statsPanel.classList.toggle('hidden');
    if (!open && !statsLoaded) loadStats();      // 首次打开才拉数据
    if (!open) loadStats();                        // 每次打开刷新
});
// 点面板外部关闭
document.addEventListener('click', (e) => {
    if (!statsPanel.classList.contains('hidden') &&
        !document.getElementById('stats-fab').contains(e.target)) {
        statsPanel.classList.add('hidden');
    }
});

```


<a id="staticstylecss"></a>
## `static/style.css`

> Claude 风格设计系统（衬线大标题 · 珊瑚色 · 暖白）

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
.chain-list { margin-top: 14px; display: flex; flex-direction: column; gap: 11px; }
.chain-item { display: flex; flex-direction: column; gap: 9px; padding: 16px 19px; border: 1px solid var(--line); border-radius: var(--r-lg); background: var(--card); cursor: pointer; transition: border-color .15s ease, box-shadow .15s ease; }
.chain-item:hover { border-color: var(--line-2); box-shadow: var(--e2); }
.chain-item.chain-active { border-color: var(--coral-line); }
.chain-item-top { display: flex; gap: 13px; align-items: center; flex-wrap: wrap; }
.chain-stage { font-size: 10.5px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; white-space: nowrap; color: var(--muted); background: var(--code-bg); padding: 4px 10px; border-radius: 999px; }
.chain-active .chain-stage { color: var(--run); background: var(--run-bg); }
.chain-url { color: var(--ink); font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; flex: 1; min-width: 100px; white-space: nowrap; }
.chain-links { display: flex; gap: 12px; }
.chain-doc-link { font-size: 12.5px; text-decoration: none; color: var(--coral); }
.chain-doc-link:hover { text-decoration: underline; }
.chain-del { color: var(--muted); }
.chain-del:hover { color: var(--bad); }
.chain-progress { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.chain-current { font-size: 12px; color: var(--faint); margin-top: 2px; }
.chain-error { font-size: 12px; color: var(--bad); margin-top: 4px; }

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
.vg-thumb { width: 100%; aspect-ratio: 16 / 9; object-fit: cover; display: block; background: linear-gradient(135deg, #EEEAE0, #E4DFD1); }
.vg-noimg { display: flex; align-items: center; justify-content: center; font-size: 26px; color: var(--faint); }
.vg-badge { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 6px 12px; }
.vs-active { color: var(--run); background: var(--run-bg); }
.vs-done   { color: var(--ok);  background: var(--ok-bg); }
.vs-fail   { color: var(--bad); background: var(--bad-bg); }
.vg-title { font-size: 12.5px; color: var(--ink-soft); padding: 8px 12px 12px; line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

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
.spark { width: 100%; height: 84px; display: block; }
.spark-empty { font-size: 12px; color: var(--faint); padding: 14px 0; }

.stat-tag { display: flex; align-items: center; gap: 10px; margin: 7px 0; }
.stat-tag-name { font-size: 12.5px; color: var(--ink-soft); width: 78px; flex: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stat-tag-bar { flex: 1; height: 6px; background: var(--code-bg); border-radius: 999px; overflow: hidden; }
.stat-tag-bar i { display: block; height: 100%; background: var(--coral); border-radius: 999px; }
.stat-tag-num { font-size: 12px; color: var(--muted); width: 26px; text-align: right; flex: none; font-variant-numeric: tabular-nums; }

@media (max-width: 640px) { .stats-panel { width: 300px; } }

```


<a id="requirementstxt"></a>
## `requirements.txt`

> Python 依赖

```text
flask>=3.0
python-dotenv>=1.0
faster-whisper>=1.0
openai-whisper>=20231117  # faster-whisper 不可用时的回退引擎
google-genai>=1.0
dashscope>=1.20
requests>=2.28
torch>=2.0  # 仅 openai-whisper 回退路径需要
# yt-dlp 用系统 brew 版（brew install yt-dlp），勿装 pip 版（py3.9 锁旧版会被 YouTube 反爬）

```


<a id="runsh"></a>
## `run.sh`

> 启动脚本

```bash
#!/bin/bash
cd /Users/kapozux/Documents/CODEelse/getAudio

# Load environment variables from .env
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

source ../venv/bin/activate
python app.py

```
