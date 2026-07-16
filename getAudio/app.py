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
                      model_review=False):
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

# 协作式取消：/stop 往里加 chain_id，运行中的链条在安全点自查并收尾（已完成产物保留）。
_cancel_chains = set()
# 单视频重转会从别的线程改 chain.json；读-改-写用它串行化，防并发丢更新。
_chain_write_lock = threading.Lock()


def _chain_dir(chain_id):
    return os.path.join(CHAINS_DIR, chain_id)


def _save_chain(state):
    with open(os.path.join(_chain_dir(state['id']), 'chain.json'),
              'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


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
    """老链条重探一次频道元信息（订阅数/头像），只取频道级、不列全部视频。"""
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
            st['followers'] = followers or (channel or {}).get('followers', 0)
            if not st.get('avatar') and (channel or {}).get('avatar'):
                st['avatar'] = channel['avatar']
            with open(cpath, 'w', encoding='utf-8') as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
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
        with _chain_write_lock:
            try:
                with open(cpath, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # 老链条补频道信息（订阅数/头像）：followers 缺失或为 0（早期 bug 存的）都重探
    if not data.get('followers') and data.get('url') \
            and chain_id not in _channel_backfilling:
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
        return jsonify({'ok': False, 'error': '这条链还在跑，等它结束再重分析'}), 409

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
        return jsonify({'error': '这条链还没有证据卡，先跑一次分析'}), 400

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
        return jsonify({'ok': False, 'error': '这条链还在跑'}), 409
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
        return jsonify({'ok': False, 'error': '等这条链整体跑完再单独重转'}), 409
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
    out = {
        'gemini': {'set': gset, 'hint': ghint},
        'dashscope': {'set': dset, 'hint': dhint},
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
    for field in ('gemini_key', 'dashscope_key'):
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
        key = (body.get('gemini_key') or '').strip() or os.environ.get('GEMINI_API_KEY', '')
        base = (body.get('gemini_base_url') or '').strip()
        if not key:
            return jsonify({'ok': False, 'reason': 'No key entered or saved yet.'})
        # 临时用传入的 base URL 测（不改动已保存设置）
        prev = os.environ.get('GEMINI_BASE_URL')
        if base:
            os.environ['GEMINI_BASE_URL'] = base
        elif 'gemini_base_url' in body:
            os.environ.pop('GEMINI_BASE_URL', None)
        try:
            client = config.make_gemini_client(key, timeout_ms=30_000)
            next(iter(client.models.list()), None)
            return jsonify({'ok': True, 'reason': 'Works — key accepted.'})
        except Exception as e:
            return jsonify({'ok': False, 'reason': _diagnose_gemini_error(e)})
        finally:
            if prev is None:
                os.environ.pop('GEMINI_BASE_URL', None)
            else:
                os.environ['GEMINI_BASE_URL'] = prev

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
_xhs_job = {'running': False, 'log': [], 'started': None, 'base': 0, 'kw': ''}


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
    _xhs_job.update(running=True, log=[], base=_xhs_notes_count())
    try:
        proc = subprocess.Popen(
            [_XHS_UV, 'run', '--project', _XHS_PROJECT, 'python', _XHS_SCRIPT],
            cwd=_XHS_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            _xhs_job['log'].append(line.rstrip()[:200])
            del _xhs_job['log'][:-60]          # 只留最后 60 行
        proc.wait()
        _xhs_job['log'].append(f'[完成] 退出码 {proc.returncode}')
    except Exception as e:  # noqa: BLE001
        _xhs_job['log'].append(f'[错误] {e}')
    finally:
        _xhs_job['running'] = False


@app.route('/api/xhs/scrape', methods=['POST'])
def api_xhs_scrape():
    """填关键词 + 数量 → 后台起 uv 子进程采集。会弹出有头浏览器（首次要扫码）。"""
    if _xhs_job['running']:
        return jsonify({'ok': False, 'error': '已有采集在跑，等它结束'}), 409
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
        return jsonify({'ok': False, 'error': '分析进行中'}), 409
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
    # 生产（Docker/公网）用 FLASK_DEBUG=0 关掉调试器（debug=True 的 Werkzeug 调试器
    # 在公网上等于 RCE 漏洞）。本地默认开 debug（热重载方便）。
    debug = os.environ.get('FLASK_DEBUG', '1') == '1'
    # 恢复未完成任务只在"真正服务的进程"里跑一次：
    # debug 模式有 reloader 父/子两进程，只在子进程（WERKZEUG_RUN_MAIN）跑；
    # 非 debug 只有一个进程，直接跑。
    if not debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        recover_unfinished_tasks()
        recover_unfinished_chains()
    # HOST 默认 127.0.0.1（本地只对自己开）；Docker 里设 HOST=0.0.0.0 对外暴露。
    app.run(debug=debug, threaded=True,
            host=os.environ.get('HOST', '127.0.0.1'),
            port=int(os.environ.get('PORT', 5001)))
