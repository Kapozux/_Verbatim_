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
import statistics
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
SETTINGS_PATH = os.path.join(config.DATA_DIR, 'settings.local.json')
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


# ===== 演示工作区（给外界看的只读实例）=====
# VERBATIM_DEMO=1 + GETAUDIO_DATA_DIR 指向 seed_demo.py 生成的目录 + 另一个端口。
# 顶栏挂「演示」标识，删除记录 / 改设置一律 403——展示用的数据别被误删、key 别被改。
DEMO_MODE = os.environ.get('VERBATIM_DEMO') == '1'


@app.before_request
def _demo_readonly_guard():
    if not DEMO_MODE:
        return None
    path = request.path or ''
    if request.method == 'DELETE' or (request.method == 'POST' and path.startswith('/api/settings')):
        return jsonify({'error': 'demo workspace is read-only'}), 403
    return None


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
    return config.FFMPEG_BIN


def resolve_ffprobe_binary():
    return config.FFPROBE_BIN


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


def extract_audio_from_video(video_path, output_path, compressed=False):
    """从视频抽音轨。compressed=False → 16k 单声道 WAV（本地 Whisper 用，无损）；
    compressed=True → Opus 单声道 16k（云引擎用：一小时 115MB 的 WAV 变成 ~21MB，
    上传快，Gemini 15 分钟一块也能走内联）。output_path 的后缀由调用方按此给 .wav/.ogg。"""
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

    if compressed:
        codec = ['-c:a', 'libopus', '-b:a', config.CLOUD_AUDIO_BITRATE, '-f', 'ogg']
    else:
        codec = ['-c:a', 'pcm_s16le']
    cmd = [
        ffmpeg_bin, '-y', '-v', 'error',
        '-i', video_path,
        '-vn', '-ac', '1', '-ar', '16000', *codec,
        output_path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        # 把 ffmpeg 真正的报错（stderr 末几行）带出来，别只显示一句命令
        lines = [ln for ln in (r.stderr or '').strip().splitlines() if ln.strip()]
        reason = ' / '.join(lines[-3:])[:300] if lines else f'exit code {r.returncode}'
        raise RuntimeError(f'ffmpeg 提取音频失败：{reason}')
    return output_path


# ===== 转写速度：按引擎统计历史耗时，给预估和统计面板用 =====
_speed_cache = {'stamp': 0.0, 'table': {}}
_SPEED_SAMPLE = 50        # 每个引擎只看最近这么多条：引擎/并发调过之后，老数据别拖累预估
_SPEED_TTL = 60           # 秒；扫 results/ 一遍约 0.1s，没必要每次请求都扫


def _speed_table():
    """{engine: {'n', 'ratio', 'proc_ratio', 'min_per_hour', 'speed_x', 'approx': {...}}}

    ratio = 转写本体秒 ÷ 音频秒（中位数）；proc_ratio 同理但用不含排队的全处理时间（预估用）。
    approx = 只有 taskdb 时间差、含排队的老记录（backfill_timing.py 回填的），单独给出、不混入。
    兜底 Whisper 的记录（timing.fallback_from）耗时里混着云端失败的那段，不计入任何引擎。
    """
    now = time.time()
    if now - _speed_cache['stamp'] < _SPEED_TTL and _speed_cache['table']:
        return _speed_cache['table']
    exact, approx = {}, {}
    root = config.RESULTS_FOLDER
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in names:
        if name.startswith('_'):
            continue
        try:
            with open(os.path.join(root, name, 'meta.json'), 'r', encoding='utf-8') as f:
                m = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        t = m.get('timing') or {}
        dur = m.get('duration_seconds') or t.get('audio_s')
        eng = m.get('engine') or ''
        if not t or not dur or dur < 30 or not eng or eng == 'subtitle':
            continue
        date = m.get('date') or ''
        if t.get('approx'):
            if t.get('wall_s'):
                approx.setdefault(eng, []).append((date, t['wall_s'] / dur))
            continue
        if t.get('fallback_from') or not t.get('transcribe_s'):
            continue
        proc = t.get('processing_s') or t['transcribe_s']
        exact.setdefault(eng, []).append((date, t['transcribe_s'] / dur, proc / dur))
    table = {}
    for eng, rows in exact.items():
        rows.sort(reverse=True)
        rows = rows[:_SPEED_SAMPLE]
        ratio = statistics.median(r[1] for r in rows)
        proc = statistics.median(r[2] for r in rows)
        table[eng] = {
            'n': len(rows),
            'ratio': round(ratio, 4),
            'proc_ratio': round(proc, 4),
            'min_per_hour': round(proc * 60, 1),        # 「1 小时音频 ≈ X 分钟」按全处理时间算
            'speed_x': round(1 / ratio, 1) if ratio else None,
        }
    for eng, rows in approx.items():
        rows.sort(reverse=True)
        rows = rows[:_SPEED_SAMPLE * 4]
        med = statistics.median(r[1] for r in rows)
        table.setdefault(eng, {})['approx'] = {'n': len(rows), 'min_per_hour': round(med * 60, 1)}
    _speed_cache.update(stamp=now, table=table)
    return table


def _estimate_seconds(engine, audio_seconds):
    """预计处理秒数；样本不足 3 条就不猜（返回 None，前端只显示已用时长）。"""
    if not audio_seconds:
        return None
    row = _speed_table().get(engine) or {}
    if (row.get('n') or 0) < 3 or not row.get('proc_ratio'):
        return None
    return int(row['proc_ratio'] * audio_seconds)


@app.route('/api/speed')
def api_speed():
    """按引擎的转写速度（给引擎选择处的提示 + 统计面板）。"""
    return jsonify(_speed_table())


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
                  segments, summary, model_review=False, extra_meta=None):
    """Persist transcription results to results/<task_id>/。

    extra_meta：额外并进 meta.json 的字段（如 source_url / video_id，
    来自链接的转写才有）——资料库靠它们做「贴链接找转录」。

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
    for k, v in (extra_meta or {}).items():
        if v:
            meta[k] = v
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
                      model_review=False, offset_sec=0, extra_meta=None,
                      timing=None):
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

    # 分阶段计时（秒）：queued / extract / transcribe / summary / save / enrich，
    # 外加调用方可能先填好的 download / subs_check。最后并进 meta.json 的 timing 字段，
    # 供详情页显示、统计面板按引擎算速度、以及给后来的任务估「还要多久」。
    timing = dict(timing or {})
    t_enq = time.monotonic()

    # 排队等待本引擎的并发额度
    sem = _engine_semaphores.get(engine)
    q.put(json.dumps({'type': 'queued', 'message': 'Queued...'}))
    if sem is not None:
        sem.acquire()
    timing['queued_s'] = round(time.monotonic() - t_enq, 1)
    t_tx = time.monotonic()           # 抽音频前就开始算；下面抽完会重置

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
            # 本地 Whisper 要无损 WAV；云引擎走压缩 Opus（上传体积小、Gemini 能内联）
            compressed = engine != 'whisper'
            audio_path = os.path.join(
                app.config['UPLOAD_FOLDER'],
                f"{task_id}_audio.{'ogg' if compressed else 'wav'}",
            )
            t0 = time.monotonic()
            input_path = extract_audio_from_video(filepath, audio_path,
                                                  compressed=compressed)
            timing['extract_s'] = round(time.monotonic() - t0, 1)
            cleanup_paths.append(input_path)

        # 预估耗时：同引擎最近的历史速度 × 这条音频的时长，推给前端显示「还要多久」
        audio_dur = probe_audio_duration_seconds(input_path)
        if audio_dur:
            timing['audio_s'] = round(audio_dur, 1)
        q.put(json.dumps({'type': 'eta', 'duration_seconds': audio_dur,
                          'expected_s': _estimate_seconds(engine, audio_dur)}))
        t_tx = time.monotonic()

        segments = []
        summary_data = None

        def _summary(full_text, use_qwen=False):
            """转写本体到这里为止计时，再单独计总结的时间。"""
            timing.setdefault('transcribe_s', round(time.monotonic() - t_tx, 1))
            t0 = time.monotonic()
            try:
                return _run_summary(full_text, q, use_qwen=use_qwen)
            finally:
                timing['summary_s'] = round(time.monotonic() - t0, 1)

        def _finish_ok(segs, summary, engine_used):
            """成功收尾：落盘 + enrich + 标记 done + 压缩音频（主路径/兜底路径共用）。"""
            # 片段截取（如只转 10:00–25:00）：把 0 起的时间戳整体加偏移，
            # 落盘 + done 事件都带真实位置。放这里是所有引擎/兜底路径的唯一收口。
            segs = _offset_segments(segs, offset_sec)
            t0 = time.monotonic()
            _save_results(task_id, original_filename, engine_used, input_path,
                          segs, summary, model_review=model_review,
                          extra_meta=extra_meta)
            timing['save_s'] = round(time.monotonic() - t0, 1)
            t0 = time.monotonic()
            try:
                from enrich import enrich_task
                enrich_task(os.path.join(config.RESULTS_FOLDER, task_id))
            except Exception:
                pass
            timing['enrich_s'] = round(time.monotonic() - t0, 1)
            # 汇总：wall = 排队 + 处理（含下载/字幕探测这些前置阶段）；processing 不含排队；
            # speed_x = 音频时长 ÷ 转写本体，「16×」就是 16 倍实时
            timing['wall_s'] = round(time.monotonic() - t_enq
                                     + timing.get('download_s', 0)
                                     + timing.get('subs_check_s', 0), 1)
            timing['processing_s'] = round(timing['wall_s'] - timing.get('queued_s', 0), 1)
            if timing.get('audio_s') and timing.get('transcribe_s'):
                timing['speed_x'] = round(timing['audio_s'] / timing['transcribe_s'], 1)
            _update_meta(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'),
                         {'timing': timing})
            _speed_cache['stamp'] = 0.0            # 有新样本，速度表下次重算
            _reflect_touch()
            taskdb.set_status(task_id, 'done')
            q.put(json.dumps({
                'type': 'done',
                'task_id': task_id,
                'segments': segs,
                'summary': summary,
                'timing': timing,
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
            summary_data = _summary(full_text)

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

            summary_data = _summary(full_text)

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
            summary_data = _summary(full_text, use_qwen=True)

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

            summary_data = _summary(full_text)

        elif engine == 'gemini35':
            # Gemini 3.5 Transcribe：专用 ASR 模型，原生说话人分离 + 词级时间戳，
            # 一次调用出结果——不用像精准模式那样两个引擎分别转、再合并。
            from transcribe_gemini35 import transcribe_audio as _gemini35_tx

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': '正在上传文件到 Gemini 3.5 Transcribe...',
            }))

            segments = _gemini35_tx(
                input_path, progress_callback=progress_cb,
                speaker_count=speaker_count,
            )

            for seg in segments:
                q.put(json.dumps({'type': 'segment', **seg}))

            full_text = "\n".join(
                f"[{s['timestamp']}] {s['text']}" for s in segments
            )
            summary_data = _summary(full_text)

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
                timing['fallback_from'] = engine
                timing.pop('transcribe_s', None)      # 重新计：含云端失败 + Whisper 两段
                segments, full_text = _whisper_transcribe()
                summary_data = _summary(full_text)
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

def _static_version():
    """静态资源版本号：取 app.js/style.css 里最新的修改时间戳。

    /static/app.js 直接按文件名引用、没有 hash/查询参数，浏览器缓存了旧版本
    后不会主动去问有没有更新——改完前端代码用户刷新页面照样看到旧行为
    （下载按钮明明改了还是老样子，就是这个）。用 mtime 当查询参数：
    文件一变这串数字就变，浏览器才会当成新资源重新拉取。
    """
    try:
        paths = [os.path.join(app.static_folder, name) for name in ('app.js', 'style.css')]
        return str(int(max(os.path.getmtime(p) for p in paths if os.path.isfile(p))))
    except (ValueError, OSError):
        return '0'


@app.route('/')
def index():
    return render_template('index.html', static_version=_static_version(), demo=DEMO_MODE)


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


_SUB_MODE_RE = re.compile(r'^[a-z]{2,3}$')


def _clean_sub_mode(raw):
    """前端传来的字幕偏好：auto / off / 两三位语言码（zh、en、ja…）。别的一律按 auto。"""
    v = (raw or 'auto').strip().lower()
    if v in ('auto', 'off') or _SUB_MODE_RE.match(v):
        return v
    return 'auto'


def _download_then_transcribe(task_id, url, engine, q, section=None, offset_sec=0,
                              sub_mode='auto'):
    """单个视频链接：先下音频，再走正常转写任务（进 Library，和上传的稿一样）。

    section/offset_sec：只转某时间段（如 10:00–25:00）时，section 传给 yt-dlp
    只切那一段，offset_sec 把字幕时间戳还原成原视频真实位置。

    先自动探测有没有现成字幕（YouTube/B站官方或 AI 自动字幕）——有就直接拿来用，
    完全跳过下载音频和转写，省时间也省 API 调用；没有才落回原来的下载+转写。
    只在「转整段」时探测：切片(section)是原视频里的一段，字幕时间戳对不上切片
    起点，硬套上去时间轴是错的，这种情况直接走转写。
    """
    from downloader import download_one, fetch_subtitle, parse_srt
    dl_dir = os.path.join(config.UPLOAD_FOLDER, f'url_{task_id}')
    timing = {}                       # 下载/字幕探测阶段的耗时，交给 run_transcription 一起落 meta
    try:
        # sub_mode：'auto' = 只用视频原语言的字幕；'off' = 从不用字幕、一律转写；
        # 'zh'/'en'/… = 只用这种语言的字幕。找不到想要的语言就转写，绝不换一种语言凑合。
        if not section and (sub_mode or 'auto') != 'off':
            q.put(json.dumps({'type': 'progress', 'percent': 1, 'message': 'Checking for existing subtitles…'}))
            t0 = time.monotonic()
            sub_path, sub_kind, sub_meta = fetch_subtitle({'video_url': url, 'video_id': ''}, dl_dir,
                                                          lang=sub_mode or 'auto')
            timing['subs_check_s'] = round(time.monotonic() - t0, 1)
            sub_segs = parse_srt(sub_path) if sub_path else None
            if sub_segs:
                target = {'video_url': url, 'title': (sub_meta or {}).get('title') or url,
                         'video_id': (sub_meta or {}).get('video_id', '')}
                sub_lang = (sub_meta or {}).get('sub_lang')
                q.put(json.dumps({'type': 'progress', 'percent': 90,
                                  'message': f'Found existing {sub_kind} subtitles ({sub_lang}) — skipping download & transcription'}))
                _save_subtitle_task(task_id, target, sub_segs, sub_kind, lang=sub_lang,
                                    timing={'subs_check_s': timing['subs_check_s'],
                                            'wall_s': timing['subs_check_s']})
                q.put(json.dumps({'type': 'done', 'task_id': task_id,
                                  'segments': sub_segs, 'summary': None,
                                  'subtitle_lang': sub_lang}))
                return

        msg = 'Downloading clip…' if section else 'Downloading audio…'
        q.put(json.dumps({'type': 'progress', 'percent': 1, 'message': msg}))
        t0 = time.monotonic()
        item = download_one({'video_url': url}, dl_dir, section=section)
        timing['download_s'] = round(time.monotonic() - t0, 1)
        if not item or not item.get('path') or not os.path.isfile(item['path']):
            taskdb.set_status(task_id, 'failed',
                              error='Download failed — bad link, private/removed video, or geo-blocked.')
            q.put(json.dumps({'type': 'error', 'message': 'Download failed — check the link.'}))
            return
        title = item.get('title') or url
        # 复用正常转写链路：落 results/、taskdb done、SSE 推进度，全和上传一致
        run_transcription(task_id, item['path'], engine, title, q,
                          None, False, engine != 'whisper', offset_sec=offset_sec,
                          extra_meta={'source_url': url,
                                      'video_id': item.get('video_id') or _video_id_from_url(url),
                                      'creator': item.get('uploader')},
                          timing=timing)
    except Exception as e:  # noqa: BLE001
        taskdb.set_status(task_id, 'failed', error=str(e)[:300])
        q.put(json.dumps({'type': 'error', 'message': str(e)[:200]}))
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
        # 字幕命中的快速路径不经过 run_transcription，它自己的 finally 里那份
        # tasks.pop 不会跑到——不清这里，任务会永远卡在「进行中」，
        # /api/history DELETE 那边 `if task_id in tasks` 的判断会一直挡着删不掉。
        # 正常路径这里重复 pop 一次是安全的（键已经被清过，pop(..., None) 不报错）。
        tasks.pop(task_id, None)
        _task_progress.pop(task_id, None)


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
    sub_mode = _clean_sub_mode(body.get('subs'))
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
                        section, offset_sec, sub_mode)
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
    """{'by_task': {task_id: 博主名}, 'by_video_id': {video_id: 博主名}}。

    by_task 只覆盖链条【当前】指向的 task_id——一旦某集被重转/Continue，
    chain.json 里的 task_id 会被换成新的，旧结果留在 results/ 里变孤儿，
    单靠 by_task 会把它错判成"我自己的"。by_video_id 兜底：只要这个
    video_id 曾经在任何一条链条里出现过（不论是不是"当前"那条），
    就认它是 pipeline 产物。按 _chains 目录的 mtime 缓存，避免每次轮询读几十个 json。
    """
    try:
        stamp = max([os.path.getmtime(CHAINS_DIR)] + [
            os.path.getmtime(os.path.join(CHAINS_DIR, n, 'chain.json'))
            for n in os.listdir(CHAINS_DIR)
            if os.path.isfile(os.path.join(CHAINS_DIR, n, 'chain.json'))
        ])
    except OSError:
        return {'by_task': {}, 'by_video_id': {}}
    if _chain_task_cache['stamp'] == stamp:
        return _chain_task_cache['map']
    by_task, by_video_id = {}, {}
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
                    by_task[v['task_id']] = author
                if v.get('video_id'):
                    by_video_id.setdefault(v['video_id'], author)
    except OSError:
        return _chain_task_cache['map']
    m = {'by_task': by_task, 'by_video_id': by_video_id}
    _chain_task_cache.update(stamp=stamp, map=m)
    return m


def _chain_author_for(task_id, filename):
    """给一条 results/ 记录判定来源：先按 task_id 精确匹配，
    再从文件名里的 [video_id] 兜底匹配（救回重转后留下的孤儿结果）。"""
    cm = _chain_task_map()
    author = cm['by_task'].get(task_id)
    if author:
        return author
    m = _VID_IN_NAME.search(filename or '')
    if m:
        return cm['by_video_id'].get(m.group(1))
    return None


@app.route('/api/task/<task_id>')
def api_task_status(task_id):
    """单个转写任务的状态快照（普通 JSON，不是 SSE）。

    网页端靠 /stream/<id> 的 SSE 实时流；但脚本、agent、MCP 这类客户端只想
    轮询问一句「好了没」，为此专门开一条 SSE 连接既别扭又容易泄漏连接。
    转写完成前 /api/history/<id> 是 404（meta.json 最后才写），所以那条路
    也当不了状态查询。这里直接读 taskdb + 内存里的进度百分比。
    """
    if not _is_valid_task_id(task_id):
        return jsonify({'error': 'Invalid task id'}), 400
    row = taskdb.get(task_id)
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'task_id': task_id,
        'status': row.get('status'),          # pending / running / done / failed
        'filename': row.get('filename'),
        'engine': row.get('engine'),
        'error': row.get('error'),
        'progress': _task_progress.get(task_id),   # 0-100，没在跑时为 null
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
    })


@app.route('/api/history')
def api_history():
    """List all saved transcription sessions（附带 source：pipeline / mine）。"""
    results_dir = config.RESULTS_FOLDER
    entries = []

    if not os.path.isdir(results_dir):
        return jsonify(entries)

    for name in os.listdir(results_dir):
        meta_path = os.path.join(results_dir, name, 'meta.json')
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    e = json.load(f)
            except Exception:
                continue
            author = _chain_author_for(e.get('id') or name, e.get('filename'))
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

    # 老记录 meta 里没存博主名：链条任务从 chain.json 反查，给下载文件名用
    if not meta.get('creator'):
        author = _chain_author_for(task_id, meta.get('filename'))
        if author:
            meta['creator'] = author
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
        '.m4a': 'audio/mp4', '.ogg': 'audio/ogg', '.opus': 'audio/ogg',
        '.webm': 'audio/webm',
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
    """全文搜索：文件名 / AI标题 / 标签 / 转写正文，返回带命中片段的条目列表。

    贴一条视频链接（YouTube / B站 / b23 短链 / 抖音）也能搜：抠出视频 id，
    去对每条记录的 video_id / 文件名里的 [id] / 记下来的 source_url，
    带一堆 ?spm_id_from=… 之类跟踪参数也照样命中。
    """
    raw_query = (request.args.get('q') or '').strip()
    query = raw_query.lower()
    if not query:
        return jsonify([])

    results_dir = config.RESULTS_FOLDER
    hits = []
    if not os.path.isdir(results_dir):
        return jsonify(hits)

    link_mode = _looks_like_url(raw_query)
    link_vid = _video_id_from_url(raw_query, resolve_short=True).lower() if link_mode else ''
    # 没抠出 id 的链接（未知平台）：退化成整串子串匹配（只去掉协议/www/末尾斜杠，
    # query 必须保留——YouTube 的 id 就在 ?v= 里，砍掉就成了 youtube.com/watch 匹配一切）
    link_plain = ''
    if link_mode and not link_vid:
        link_plain = re.sub(r'^https?://(www\.)?', '', query).rstrip('/')

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

        snippet = ''
        if link_mode:
            src = (meta.get('source_url') or '')
            matched = bool(
                (link_vid and link_vid in _link_ids(meta))
                or (link_plain and link_plain in src.lower())
            )
            if matched:
                snippet = '🔗 ' + (src or meta.get('video_id') or raw_query)
                author = _chain_author_for(meta.get('id') or name, meta.get('filename'))
                hits.append({**meta, 'snippet': snippet,
                             'source': 'pipeline' if author else 'mine',
                             **({'creator': author} if author else {})})
            continue

        haystacks = [
            meta.get('filename', ''),
            meta.get('ai_title', ''),
            meta.get('ai_one_line', ''),
            ' '.join(meta.get('ai_tags', []) or []),
            meta.get('video_id', '') or '',
            meta.get('source_url', '') or '',
        ]
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
            author = _chain_author_for(meta.get('id') or name, meta.get('filename'))
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


def _reflect_touch():
    """转写完成后通知回顾模块：防抖后后台重算，用户打开面板时已经是新的。"""
    try:
        import reflect
        reflect.touch(config.RESULTS_FOLDER)
    except Exception:  # noqa: BLE001
        pass


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

# 从各平台链接里抠视频 id 的规则（按顺序试，先中先得）
_URL_ID_PATTERNS = (
    re.compile(r'[?&]v=([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])'),      # youtube.com/watch?v=
    re.compile(r'youtu\.be/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])'),  # youtu.be/
    re.compile(r'youtube\.com/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])'),
    re.compile(r'(BV[0-9A-Za-z]{10})'),                             # B站 BV 号
    re.compile(r'bilibili\.com/video/(av\d+)'),                     # B站 av 号
    re.compile(r'bilibili\.com/bangumi/play/((?:ep|ss)\d+)'),       # B站番剧
    re.compile(r'douyin\.com/video/(\d{15,})'),
)
_SHORT_LINK_HOSTS = ('b23.tv/', 'youtu.be/')      # youtu.be 本身就带 id，不用解析；b23.tv 要跟跳转


def _looks_like_url(text):
    t = (text or '').strip().lower()
    return t.startswith(('http://', 'https://', 'www.')) or any(
        h in t for h in ('bilibili.com/', 'youtube.com/', 'youtu.be/', 'b23.tv/', 'douyin.com/'))


def _resolve_short_link(url, timeout=5):
    """b23.tv 这类短链：跟一次 302 拿真实地址（只读响应头，不下正文）。失败原样返回。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, method='HEAD',
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310
            return r.geturl() or url
    except Exception:  # noqa: BLE001
        return url


def _video_id_from_url(url, resolve_short=False):
    """链接 → 平台视频 id（YouTube 11 位 / BV 号 / av / ep / 抖音数字），认不出返回 ''。

    resolve_short=True 时 b23.tv 短链会先跟一次跳转（走网络，搜索时才开；
    回填旧记录时不开，避免启动期间卡在网络上）。
    """
    u = (url or '').strip()
    if not u:
        return ''
    if resolve_short and 'b23.tv/' in u.lower():
        u = _resolve_short_link(u)
    for pat in _URL_ID_PATTERNS:
        m = pat.search(u)
        if m:
            return m.group(1)
    return ''


def _url_for_video_id(vid):
    """只有 id 没存链接的旧记录：按 id 形态拼一个能打开的地址（和 _video_target 同一套规则）。"""
    vid = (vid or '').strip()
    if not vid:
        return ''
    if vid.startswith('BV') or vid.startswith('av'):
        return f'https://www.bilibili.com/video/{vid}'
    if vid.startswith(('ep', 'ss')) and vid[2:].isdigit():
        return f'https://www.bilibili.com/bangumi/play/{vid}'
    if len(vid) == 11:
        return f'https://www.youtube.com/watch?v={vid}'
    if vid.isdigit() and len(vid) >= 15:
        return f'https://www.douyin.com/video/{vid}'
    return ''


def _link_ids(meta):
    """一条记录能被哪些视频 id 找到：meta.video_id + 文件名里的 [id] + source_url 里抠出来的 id。全小写。"""
    ids = set()
    if meta.get('video_id'):
        ids.add(str(meta['video_id']).lower())
    m = _VID_IN_NAME.search(meta.get('filename') or '')
    if m:
        ids.add(m.group(1).lower())
    v = _video_id_from_url(meta.get('source_url') or '')
    if v:
        ids.add(v.lower())
    return ids


def _backfill_source_links():
    """给老记录补 source_url / video_id（一次性，之后每次启动只是空扫）。

    三个来源，按可信度：chain.json 里这条 task 的 video_id/video_url；
    taskdb 里 filename 就是链接的（单链接转写早期只把 url 存在这）；
    文件名里的 [id]。有 id 没链接就按 id 拼一个。只在真有新字段时才写盘。
    """
    rd = config.RESULTS_FOLDER
    if not os.path.isdir(rd):
        return
    by_task = {}
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
            for v in (c.get('videos') or []):
                if v.get('task_id'):
                    by_task.setdefault(v['task_id'], v)
    except OSError:
        pass

    fixed = 0
    for name in os.listdir(rd):
        if name.startswith('_') or not _is_valid_task_id(name):
            continue
        meta_path = os.path.join(rd, name, 'meta.json')
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if meta.get('source_url') and meta.get('video_id'):
            continue
        vid = meta.get('video_id') or ''
        url = meta.get('source_url') or ''
        cv = by_task.get(name) or {}
        vid = vid or cv.get('video_id') or ''
        url = url or cv.get('video_url') or ''
        if not url:
            row = taskdb.get(name)
            fn = (row or {}).get('filename') or ''
            if fn.startswith(('http://', 'https://')):
                url = fn
        if not vid:
            m = _VID_IN_NAME.search(meta.get('filename') or '')
            vid = (m.group(1) if m else '') or _video_id_from_url(url)
        if not url:
            url = _url_for_video_id(vid)
        updates = {}
        if vid and not meta.get('video_id'):
            updates['video_id'] = vid
        if url and not meta.get('source_url'):
            updates['source_url'] = url
        if updates:
            _update_meta(meta_path, updates)
            fixed += 1
    if fixed:
        print(f'[backfill] source links filled for {fixed} transcripts')


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
        if meta.get('video_id'):
            idx.setdefault(meta['video_id'], name)
        m = _VID_IN_NAME.search(meta.get('filename', '') or '')
        if m:
            idx.setdefault(m.group(1), name)
    return idx


def _save_subtitle_task(task_id, target, segments, source, lang=None, timing=None):
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
    if lang:
        meta['subtitle_lang'] = lang          # 实际用的字幕轨语言码（zh-Hans / en-orig / ai-zh…）
    if timing:
        meta['timing'] = timing
    if target.get('video_url'):
        meta['source_url'] = target['video_url']
    if target.get('video_id'):
        meta['video_id'] = target['video_id']
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
    _reflect_touch()


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
                    sp, sub_source, _sub_meta = fetch_subtitle(
                        target, dl_dir, lang=state.get('sub_lang') or 'auto')
                    if sp:
                        sub_segs = parse_srt(sp) or None
                item = None if sub_segs else download_one(target, dl_dir)
            with lock:
                state['download_done'] = state.get('download_done', 0) + 1

            # 有字幕 → 直接落转写结果，跳过音频转写（省下载/转写/API）
            if sub_segs:
                task_id = str(uuid.uuid4())
                _save_subtitle_task(task_id, target, sub_segs, sub_source,
                                    lang=(_sub_meta or {}).get('sub_lang'))
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
            display_name = f"{item['title']} [{item['video_id']}].{ext}"
            taskdb.create(task_id, display_name, state['engine'], None, upload_path)
            q = queue.Queue()
            tasks[task_id] = q
            executor.submit(
                run_transcription, task_id, upload_path, state['engine'],
                display_name, q, None,
                fallback_whisper=state.get('fallback_whisper', False),
                extra_meta={'source_url': target.get('video_url'),
                            'video_id': item.get('video_id'),
                            'creator': state.get('author') or item.get('uploader')},
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
        'sub_lang': _clean_sub_mode(data.get('sub_lang')),   # 字幕语言：auto / zh / en …
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
        display_name = f"{item['title']} [{item['video_id']}].{ext}"
        taskdb.create(task_id, display_name, engine, None, upload_path)
        q = queue.Queue()
        tasks[task_id] = q
        executor.submit(run_transcription, task_id, upload_path, engine,
                        display_name, q, None,
                        extra_meta={'source_url': target.get('video_url'),
                                    'video_id': item.get('video_id'),
                                    'creator': item.get('uploader')})
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
        'speed': _speed_table(),
        'engines': dict(engines),
        'top_tags': [{'tag': t, 'tag_en': tmap.get(t, t), 'count': c}
                     for t, c in top_tags],
        'timeline': timeline,
    })


@app.route('/api/reflect')
def api_reflect():
    """回顾面板：这段时间在听什么。?range=1m|3m|6m|12m &lang=zh|en &refresh=1 强制重写叙事。"""
    import reflect
    rng = request.args.get('range', '1m')
    if rng not in reflect.RANGES:
        rng = '1m'
    lang = request.args.get('lang', 'zh')
    refresh = request.args.get('refresh') in ('1', 'true')
    return jsonify(reflect.build(config.RESULTS_FOLDER, rng, lang, refresh))


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
        threading.Thread(target=_backfill_source_links, daemon=True).start()
        try:
            import reflect
            reflect.start_scheduler(config.RESULTS_FOLDER)   # 回顾提前算好，打开不用等
        except Exception:  # noqa: BLE001
            pass
    # HOST 默认 127.0.0.1（本地只对自己开）；Docker 里设 HOST=0.0.0.0 对外暴露。
    app.run(debug=debug, threaded=True, host=_host,
            port=int(os.environ.get('PORT', 5001)))
