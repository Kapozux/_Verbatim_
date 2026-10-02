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
from datetime import datetime
import statistics
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request, send_file

import config
import taskdb
import timecode
import usage


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
    # Storage：转写完成后是否把音频留在 results/ 里供回放。'1' = 留；空 = 不留（默认）。
    'keep_audio': 'KEEP_AUDIO',
    # Storage：备份目录（空 = 自动找本机 Google Drive）
    'backup_dir': 'BACKUP_DIR',
}

# 非秘密、可清空（空 = 回默认）的设置字段
_PLAIN_FIELDS = ('gemini_base_url', 'whisper_model', 'gemini_transcribe_model',
                 'gemini_analysis_model', 'gemini_extract_model', 'keep_audio', 'backup_dir')


def _keep_audio():
    """转写完是否保留音频。默认不留：实测音频占 results/ 的 99%，而回放几乎没人用；
    转写、摘要、搜索、导出、重转写都不依赖它（重转写是重新下载）。"""
    return (os.environ.get('KEEP_AUDIO') or '').strip() == '1'


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


_DEMO_LENS_POST = re.compile(r'^/api/chain/([0-9a-f]{32})/lens$')
_DEMO_ASK_POST = re.compile(r'^/api/chain/[0-9a-f]{32}/ask(/stream)?$')
DEMO_ASK = DEMO_MODE and os.environ.get('VERBATIM_DEMO_ASK') == '1'


@app.before_request
def _demo_readonly_guard():
    """演示实例只读、不花钱：写操作一律 403，只放行两样不花钱的——
    合并转写（纯拼文本）和已经预生成好的镜头（直接读文件）。"""
    if not DEMO_MODE or request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    path = request.path or ''
    if request.method == 'POST':
        if path == '/api/transcripts/merge':
            return None
        # 问证据卡要花钱（每问一两美分）：演示实例默认不开，VERBATIM_DEMO_ASK=1 才放行
        if DEMO_ASK and (_DEMO_ASK_POST.match(path) or path == '/api/radar'):
            return None
        m = _DEMO_LENS_POST.match(path)
        if m:
            lens = (request.get_json(silent=True) or {}).get('lens') or ''
            if re.fullmatch(r'[a-z]+', lens) and os.path.isfile(
                    os.path.join(_chain_dir(m.group(1)), f'镜头_{lens}.md')):
                return None
    return jsonify({'error': 'This is a read-only demo.'}), 403


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

# 批量转录：**每个引擎一个线程池**，池子大小就是该引擎的并发上限。
#
# 以前是所有引擎共用一个大池（44 个线程），靠每个引擎的信号量限流。问题是
# 信号量是在 worker 线程**里面**等的：一条 158 期的 Whisper 链条一次提交，
# 44 个线程瞬间被占满（4 个真在转、40 个占着线程干等 Whisper 信号量），
# 后面提交的任务——哪怕是走云端、本该立刻开跑的——只能排在池子队列里。
# 实测现象：用户传了个 .mov 选 Gemini 3.5，任务卡在 pending 两小时没动静。
# 分池之后，Whisper 的队再长也只占它自己那 4 个线程。
_engine_executors = {
    engine: ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix=f'tx-{engine}')
    for engine, n in config.ENGINE_CONCURRENCY.items()
}
# 没在 ENGINE_CONCURRENCY 里的引擎名（老任务、手填的）走这个兜底池
_other_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='tx-other')


def submit_transcription(engine, fn, *args, **kwargs):
    """按引擎分池提交转写任务。"""
    pool = _engine_executors.get(engine) or _other_executor
    return pool.submit(fn, *args, **kwargs)


# 信号量保留：跨池的路径仍要它兜底（如云引擎失败后落 Whisper，
# 那条任务占着云引擎的线程，却要排 Whisper 的额度）。
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


def _offset_segments(segments, offset_sec):
    """片段转写（只截了 10:00–25:00）出来的时间戳从 0 起 → 加偏移显示成原视频真实位置。"""
    if not offset_sec:
        return segments
    for seg in segments:
        if seg.get('timestamp'):
            seg['timestamp'] = timecode.shift(seg['timestamp'], offset_sec)
        if seg.get('end'):
            seg['end'] = timecode.shift(seg['end'], offset_sec)
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
    keep_audio = _keep_audio()
    if keep_audio:
        audio_dest = os.path.join(task_dir, f"audio{ext}")
        shutil.copy2(audio_source_path, audio_dest)
    # 时长照常探测（Reflect / 统计 / 速度表都靠它），只是不再落一份音频副本
    duration = probe_audio_duration_seconds(audio_source_path)

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
        'audio_ext': ext if keep_audio else '',
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


def _chain_stopped(chain_id, task_id=None):
    """这条链条是不是已经被用户停掉了（内存标记 + 落盘终态都算）。

    _cancel_chains 只在链条线程活着的时候有值（finally 里就 discard 了），而按下停止
    之后队列里可能还压着几十个已提交的转写——它们得自己看一眼 chain.json 的终态，
    否则用户按了停止，最贵的部分照样一个个跑完。
    """
    if not chain_id:
        return False
    if chain_id in _cancel_chains:
        return True
    cdir = _chain_dir(chain_id)
    if not os.path.isdir(cdir):
        return True          # 链条被删了：队列里剩下的别再转、别再花钱
    if task_id and task_id in _manual_tasks:
        return False         # 用户在已停止的链上手动重转某一期：终态 cancelled 不拦它
    try:
        with open(os.path.join(cdir, 'chain.json'), 'r', encoding='utf-8') as f:
            return json.load(f).get('stage') == 'cancelled'
    except Exception:  # noqa: BLE001  读不到就当没停，宁可多转一条也不误杀
        return False


_manual_tasks = set()   # 单期重转提交的 task_id（见 _retranscribe_video）


# ---- 断网断路器 ----
# 每条转写各自独立：断网时队首那条撞一下失败、标 failed，下一条接着撞……没人发现
# "大家都在因为同一个原因失败"，几分钟就把整个队列烧成失败（09-23 一小时 574 条）。
# 现在：网络类错误不判失败而是重新排队；连续几条都是网络错就判定断网，后面的云端
# 任务在开跑前原地等，后台隔一阵探一次，网络回来自动放行。
_NETWORK_ERROR_MARKERS = (
    'nodename nor servname',                  # macOS DNS 解析失败（[Errno 8]）
    'Name or service not known',              # Linux DNS
    'Temporary failure in name resolution',
    'getaddrinfo failed',
    'EOF occurred in violation of protocol',  # 线路/代理断了，TLS 被掐
    'Connection reset by peer',
    'Connection refused',                     # 常见于代理进程退了但系统代理还指着它
    'Connection aborted',
    'Network is unreachable',
    'No route to host',
    'Failed to establish a new connection',
)
_NET_TRIP_AFTER = 3        # 连续这么多条任务都是网络错 → 判定断网
_NET_PROBE_EVERY = 30      # 断网期间每隔多少秒探一次
_NET_MAX_REQUEUE = 5       # 单条任务因网络错最多重新排队几次，再失败就照常判失败
# 探测打引擎自己的 API 域名（走 requests，和引擎一样认代理设置）；有 HTTP 响应就算通
_NET_PROBE_URLS = {
    'dashscope': 'https://dashscope.aliyuncs.com/',
    'qwenasr': 'https://dashscope.aliyuncs.com/',
}
_NET_PROBE_DEFAULT = 'https://generativelanguage.googleapis.com/'


def _is_network_error(err):
    """是不是连不上（DNS / TLS 被掐 / 连接被重置）这类和内容无关的网络错误。"""
    seen = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        if isinstance(err, ConnectionError):
            return True
        s = str(err)
        if any(m in s for m in _NETWORK_ERROR_MARKERS):
            return True
        err = err.__cause__ or err.__context__
    return False


def _network_reachable(url):
    import requests
    try:
        requests.head(url, timeout=10)
        return True
    except Exception:  # noqa: BLE001
        return False


class _NetworkBreaker:
    def __init__(self):
        self._lock = threading.Lock()
        self._up = threading.Event()
        self._up.set()
        self._streak = 0
        self._since = 0.0

    def is_open(self):
        return not self._up.is_set()

    def record_ok(self):
        with self._lock:
            self._streak = 0

    def record_network_error(self, engine):
        with self._lock:
            self._streak += 1
            if self._streak < _NET_TRIP_AFTER or not self._up.is_set():
                return
            self._up.clear()
            self._since = time.time()
        url = _NET_PROBE_URLS.get(engine, _NET_PROBE_DEFAULT)
        print(f'[net] 连续 {_NET_TRIP_AFTER} 条转写都是网络错误，判定断网：'
              f'暂停云端转写，每 {_NET_PROBE_EVERY}s 探测一次 {url}')
        threading.Thread(target=self._probe_until_up, args=(url,), daemon=True,
                         name='net-probe').start()

    def _probe_until_up(self, url):
        while True:
            time.sleep(_NET_PROBE_EVERY)
            if _network_reachable(url):
                with self._lock:
                    self._streak = 0
                    self._up.set()
                print(f'[net] 网络恢复（断了约 {int(time.time() - self._since)}s），排队的转写继续')
                return

    def wait(self, should_abort):
        """断网期间挡住云端任务开跑，网络回来或 should_abort() 为真时返回。"""
        while not self._up.wait(timeout=5):
            if should_abort():
                return


_net_breaker = _NetworkBreaker()


class _EmptyTranscript(RuntimeError):
    """转写「看着成功了但其实什么都没有」：0 段、全是音乐标记、或纯静音幻听。

    当成普通失败抛出去，好处是自动吃到既有的兜底逻辑——云引擎空转就换 Whisper
    再试一次，Whisper 也拿不到东西才把任务判失败，并把原因写给用户。
    """


def _check_transcript(segments, duration, audio_path):
    """转写出口校验：真的没内容就抛 _EmptyTranscript。判据见 sanitize.transcript_quality。

    先跑一遍清洗层的纯文字清洗再判——落盘走的是清洗后的稿，而清洗会把
    「（音乐）×200 折成一段再删掉」这类内容清成 0 段。只看原始稿会漏判：
    校验通过、清洗之后却存了一份空转写。
    """
    try:
        from sanitize import clean_transcript, transcript_quality
    except Exception:  # noqa: BLE001  校验层不可用绝不影响正常转写
        return
    checked, report = segments, None
    try:
        if segments and 'timestamp' in (segments[0] or {}):
            checked, report = clean_transcript(segments, duration=duration)
    except Exception:  # noqa: BLE001
        checked, report = segments, None
    try:
        ok, reason, stats = transcript_quality(checked, duration, audio_path,
                                               clean_report=report)
    except Exception:  # noqa: BLE001
        return
    if not ok:
        raise _EmptyTranscript(f'{reason}（{stats}）')


class _OnBattery(Exception):
    """拔了电不让跑本地 Whisper：4 workers × 3 threads 是照着插电时把 M4 Max
    性能核心喂满设计的（见 config.ENGINE_CONCURRENCY 注释），带出门用电池扛
    这个几个小时，电量掉得肉眼可见（2026-09-22 真事故：链条转到一半电脑被
    强制关机）。"""


def _on_battery():
    """当前是否在吃电池（未接电源）。查不到（非 Mac / pmset 不在）就当作接着电源，
    别因为探测不到就把转写堵死。"""
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:  # noqa: BLE001
        return False
    return "'Battery Power'" in out


def run_transcription(task_id, filepath, engine, original_filename, q,
                      speaker_count=None, fallback_whisper=False,
                      model_review=False, offset_sec=0, extra_meta=None,
                      timing=None, net_retry=0, summarize=True):
    """记账归属：这个 worker 线程里所有模型调用（转写、摘要、enrich）都记到 task_id，
    链条里的转写再带上 chain_id（extra_meta 里由 run_chain 塞入）。"""
    with usage.scope(ref=task_id, chain=(extra_meta or {}).get('chain_id')):
        return _run_transcription(task_id, filepath, engine, original_filename, q,
                                  speaker_count=speaker_count,
                                  fallback_whisper=fallback_whisper,
                                  model_review=model_review, offset_sec=offset_sec,
                                  extra_meta=extra_meta, timing=timing,
                                  net_retry=net_retry, summarize=summarize)


def _run_transcription(task_id, filepath, engine, original_filename, q,
                       speaker_count=None, fallback_whisper=False,
                       model_review=False, offset_sec=0, extra_meta=None,
                       timing=None, net_retry=0, summarize=True):
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

    # 链条已被停止 → 队列里剩下的这些别再开跑（停止前可能已经提交了几十个）。
    # 音频留在 uploads/ 不删，Continue 会直接拿它重转，不用重下载。
    chain_id = (extra_meta or {}).get('chain_id')
    if _chain_stopped(chain_id, task_id):
        taskdb.set_status(task_id, 'failed', error='已随链条停止，未开始转写')
        q.put(json.dumps({'type': 'error', 'message': '已随链条停止，未开始转写'}))
        tasks.pop(task_id, None)
        return

    # 分阶段计时（秒）：queued / extract / transcribe / summary / save / enrich，
    # 外加调用方可能先填好的 download / subs_check。最后并进 meta.json 的 timing 字段，
    # 供详情页显示、统计面板按引擎算速度、以及给后来的任务估「还要多久」。
    timing_in = dict(timing or {})    # 网络错重新排队时原样带过去（下载/字幕探测的耗时）
    timing = dict(timing_in)
    t_enq = time.monotonic()
    requeued = False

    # 已判定断网：云端任务先在这儿等网络回来，别去白撞一次失败。
    # 链条被停止也会放出来，下面拿到额度后那道检查会把它标成"随链条停止"。
    if engine != 'whisper' and _net_breaker.is_open():
        q.put(json.dumps({'type': 'queued', 'message': '网络断开，恢复后自动继续...'}))
        _net_breaker.wait(lambda: _chain_stopped(chain_id, task_id))

    # 排队等待本引擎的并发额度
    sem = _engine_semaphores.get(engine)
    q.put(json.dumps({'type': 'queued', 'message': 'Queued...'}))
    if sem is not None:
        sem.acquire()
    timing['queued_s'] = round(time.monotonic() - t_enq, 1)
    t_tx = time.monotonic()           # 抽音频前就开始算；下面抽完会重置

    # 排队期间（可能几十分钟）用户按了停止 → 到自己这一轮时再确认一次
    if _chain_stopped(chain_id, task_id):
        if sem is not None:
            sem.release()
        taskdb.set_status(task_id, 'failed', error='已随链条停止，未开始转写')
        q.put(json.dumps({'type': 'error', 'message': '已随链条停止，未开始转写'}))
        tasks.pop(task_id, None)
        return

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

        def _verify(segs):
            """出口校验：在花钱做总结 / 落盘之前，先确认这稿不是空的或纯幻听。

            放在 _summary 之前，空稿就不会再白付一次总结 + enrich 的钱。
            """
            _check_transcript(segs, audio_dur, input_path)

        def _summary(full_text, use_qwen=False):
            """转写本体到这里为止计时，再单独计总结的时间。"""
            timing.setdefault('transcribe_s', round(time.monotonic() - t_tx, 1))
            if not summarize:        # 博主链默认不出摘要（每期约 1 美分，上千期很可观）
                return None
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
            if engine_used != 'whisper':
                _net_breaker.record_ok()           # 云端调通了一条，断网计数清零
            _reflect_touch()
            taskdb.set_status(task_id, 'done')
            if (extra_meta or {}).get('project_id'):          # 在项目里直接传的录音：转完加进那个项目
                try:
                    _project_on_transcribed(extra_meta['project_id'], task_id)
                except Exception as e:  # noqa: BLE001  加不进项目不影响转写本身
                    print(f'[project] add {task_id[:8]} failed: {e}')
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
            # 用电池时原地等插电，不直接判失败：以前一拔电，几百期的 Whisper 链几秒内
            # 全部被标成失败，插电后还得手动 Continue。等久了（12 小时）或链被停了才放弃。
            if _on_battery():
                q.put(json.dumps({'type': 'queued',
                                  'message': 'On battery — waiting for power before running local Whisper…'}))
                deadline = time.monotonic() + 12 * 3600
                while _on_battery():
                    if time.monotonic() > deadline or _chain_stopped(chain_id, task_id):
                        raise _OnBattery('电脑正在用电池供电，本地 Whisper 会把 CPU 全部性能核心跑满——'
                                         '插上电源再转，或者这条改用云端引擎（Gemini 等）')
                    time.sleep(30)
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
                    'timestamp': timecode.clock(seg['start']),
                    'end': timecode.clock(seg['end']),
                    'text': seg['text'].strip(),
                }
                segs.append(item)
                q.put(json.dumps({'type': 'segment', **item}))
            full = "\n".join(f"[{s['timestamp']}] {s['text']}" for s in segs)
            return segs, full

        if engine == 'whisper':
            segments, full_text = _whisper_transcribe()
            _verify(segments)
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

            _verify(segments)
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

            # 开说话人分离，并像 gemini35 一样把「说话人N：」写进正文——
            # 剔除连麦嘉宾、分析、搜索都靠这个前缀认人
            segments = transcribe_audio(
                input_path, progress_callback=progress_cb, diarization=True
            )
            for seg in segments:
                if seg.get('speaker') is not None:
                    seg['text'] = f"说话人{int(seg['speaker']) + 1}：{seg['text']}"
            # 阿里云 ASR 按音频时长计费，没有 token 数：记秒数，价格表里配 per_audio_hour 才算钱
            usage.record('dashscope', config.DASHSCOPE_ASR_MODEL, 'asr',
                         audio_seconds=audio_dur or 0)

            for seg in segments:
                q.put(json.dumps({'type': 'segment', **seg}))

            full_text = "\n".join(
                f"[{s['timestamp']}] {s['text']}" for s in segments
            )
            _verify(segments)
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
                    usage.record('dashscope', config.DASHSCOPE_ASR_MODEL, 'asr',
                                 audio_seconds=audio_dur or 0)
                except Exception as e:  # noqa: BLE001
                    holder['dashscope_err'] = e

            # usage.bound：两个子线程也归到这条 task 的账上
            tg = threading.Thread(target=usage.bound(_do_gemini))
            td = threading.Thread(target=usage.bound(_do_dashscope))
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

            _verify(segments)
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
            _verify(segments)
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
        # 同理：**云引擎交了白卷**（0 段 / 全是音乐标记 / 纯静音幻听）也无视开关落
        # Whisper——云端已经在引擎内部重试过了，再试一遍还是白卷。
        # 网络类错误（DNS / TLS 被掐 / 连接重置）跟这条音频本身无关：不判失败、不落
        # Whisper，放回队尾重排（finally 里提交）；断网期间断路器会挡着它先别开跑。
        if engine != 'whisper' and _is_network_error(e):
            _net_breaker.record_network_error(engine)
            if net_retry < _NET_MAX_REQUEUE:
                requeued = True
                taskdb.set_status(task_id, 'pending')
                q.put(json.dumps({
                    'type': 'queued',
                    'message': f'网络出错，重新排队（第 {net_retry + 1} 次）：{str(e)[:60]}',
                }))
        content_block = _is_content_block(e)
        empty_out = isinstance(e, _EmptyTranscript)
        fell_back = False
        if not requeued and engine != 'whisper' and (fallback_whisper or content_block or empty_out):
            try:
                why = ('Content-blocked by Gemini (deterministic)' if content_block
                       else f'{engine} 没转出有效内容' if empty_out
                       else f'{engine} failed ({str(e)[:50]})')
                q.put(json.dumps({
                    'type': 'progress', 'percent': 0,
                    'message': f'{why} — falling back to local Whisper...',
                }))
                # 换并发闸：放掉云引擎额度，改排 Whisper 的队（本地并发见
                # config.ENGINE_CONCURRENCY['whisper']，不然十几路 whisper 同时烧 CPU）。finally 里统一释放当前 sem。
                if sem is not None:
                    sem.release()
                sem = _engine_semaphores.get('whisper')
                if sem is not None:
                    sem.acquire()
                timing['fallback_from'] = engine
                timing.pop('transcribe_s', None)      # 重新计：含云端失败 + Whisper 两段
                segments, full_text = _whisper_transcribe()
                _verify(segments)
                summary_data = _summary(full_text)
                _finish_ok(segments, summary_data, 'whisper')
                fell_back = True
            except Exception as e2:  # noqa: BLE001
                # 两段都失败：把两边的原因都留着。只报兜底那一条会把"云引擎为什么失败"
                # 这个更有用的信息盖掉（用户看到的只剩 Whisper 的解码报错）。
                e = (e2 if str(e2) == str(e)
                     else RuntimeError(f'{engine} 失败：{e}；Whisper 兜底也失败：{e2}'))
        if not fell_back and not requeued:
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
            _voices_keep(task_id, filepath, offset_sec)      # 删源文件之前：留试听音频、排声纹
            for path in cleanup_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if requeued:
            # 放在最后提交：新 worker 用的还是同一个 q，tasks 表里的登记不能被上面清掉
            submit_transcription(engine, run_transcription, task_id, filepath, engine,
                                 original_filename, q, speaker_count,
                                 fallback_whisper=fallback_whisper,
                                 model_review=model_review, offset_sec=offset_sec,
                                 extra_meta=extra_meta, timing=timing_in,
                                 net_retry=net_retry + 1, summarize=summarize)
        else:
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
        paths = [os.path.join(app.static_folder, name) for name in ('app.js', 'style.css', 'i18n.js', 'explore.js', 'tools.js', 'projects.js', 'study.js', 'voices.js')]
        return str(int(max(os.path.getmtime(p) for p in paths if os.path.isfile(p))))
    except (ValueError, OSError):
        return '0'


@app.route('/')
def index():
    import citations
    return render_template('index.html', static_version=_static_version(), demo=DEMO_MODE, cite_id=citations.ID,
                           demo_ask=DEMO_ASK)


# ========== Upload & Stream ==========

def _parse_speaker_count(raw):
    """解析前端传来的预计人数；非法/空则返回 None（让阿里云自动判断）。"""
    try:
        n = int(raw)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _enqueue_task(file, engine, speaker_count=None, extra_meta=None):
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
    submit_transcription(
        engine, run_transcription, task_id, filepath, engine, file.filename, q,
        speaker_count, False, engine != 'whisper', extra_meta=extra_meta,
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
    submit_transcription(
        engine, run_transcription, task_id, link, engine, display, q,
        speaker_count, False, engine != 'whisper',
    )
    return task_id, None


def _chain_task_context():
    """task_id → 重投需要的链条上下文（chain.json 里的 videos[].task_id 反查）。
    taskdb 只存了位置参数那几列；链条任务少了 chain_id 等于点停止拦不住、
    钱记不到链上，summarize 回到默认 True 还会给每期白出一份摘要。"""
    ctx = {}
    try:
        names = os.listdir(CHAINS_DIR)
    except OSError:
        return ctx
    for name in names:
        try:
            with open(os.path.join(CHAINS_DIR, name, 'chain.json'), 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        chain_id = st.get('id') or name
        for v in st.get('videos') or []:
            tid = v.get('task_id')
            if not tid:
                continue
            ctx[tid] = dict(
                fallback_whisper=st.get('fallback_whisper', False),
                summarize=st.get('summarize', False),
                extra_meta={'source_url': v.get('video_url'),
                            'video_id': v.get('video_id'),
                            'creator': st.get('author') or v.get('uploader'),
                            'chain_id': chain_id},
            )
    return ctx


_ORPHAN_MIN_AGE_S = 600   # 比这新的 uploads 条目不当孤儿：可能正在 file.save / shutil.move，taskdb 行还没建


def _clean_orphan_uploads(keep_ids=()):
    """删 uploads/ 里不属于任何未完成任务的文件和残留下载目录。

    认 task_id 时去掉 `_audio` 之类的后缀（worker 抽音轨的中间文件 `<tid>_audio.ogg`）
    和 `url_` 前缀（单链接下载目录）。failed 但音频还在的保留——Continue 靠它直接重转。"""
    cleaned = 0
    now = time.time()
    for name in os.listdir(config.UPLOAD_FOLDER):
        path = os.path.join(config.UPLOAD_FOLDER, name)
        try:
            if now - os.lstat(path).st_mtime < _ORPHAN_MIN_AGE_S:
                continue
        except OSError:
            continue
        stem = name.split('.', 1)[0]
        if stem.startswith('url_'):
            stem = stem[4:]
        tid = stem.split('_', 1)[0]
        if tid in keep_ids:
            continue
        row = taskdb.get(tid) if _is_valid_task_id(tid) else None
        if row and row.get('status') != 'done':
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            cleaned += 1
        except OSError:
            pass
    return cleaned


def recover_unfinished_tasks():
    """启动时找回上次没跑完的任务：源文件还在就重新入队，不在就标记失败。

    先清孤儿再重投：反过来的话，刚恢复的视频任务可能已开始抽音轨，
    中间文件会被当孤儿删掉。
    """
    rows = taskdb.unfinished()
    cleaned = _clean_orphan_uploads(keep_ids={r['id'] for r in rows})
    chain_ctx = _chain_task_context() if rows else {}
    recovered = 0

    for row in rows:
        task_id = row['id']
        upload_path = row.get('upload_path') or ''
        if upload_path and os.path.isfile(upload_path):
            taskdb.set_status(task_id, 'pending')
            q = queue.Queue()
            tasks[task_id] = q
            engine = row.get('engine')
            kwargs = chain_ctx.get(task_id) or {'model_review': engine != 'whisper'}
            submit_transcription(
                engine, run_transcription, task_id, upload_path,
                engine, row.get('filename'), q, row.get('speaker_count'), **kwargs,
            )
            recovered += 1
        else:
            taskdb.set_status(
                task_id, 'failed', error='服务重启且源文件已丢失，请重新上传'
            )

    if recovered or cleaned:
        print(f"[recover] 找回未完成任务 {recovered} 个，清理孤儿上传文件 {cleaned} 个")


@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('audio')
    engine = request.form.get('engine', 'gemini35')
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
        body.get('path'), body.get('engine', 'gemini35'),
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
                              sub_mode='auto', project_id=None):
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
                # 残缺字幕（中途断掉 / 下载被截断）不算数：回落到下载 + 转写
                from downloader import subtitle_usable
                usable, why = subtitle_usable(sub_segs, (sub_meta or {}).get('duration'))
                if not usable:
                    q.put(json.dumps({'type': 'progress', 'percent': 1,
                                      'message': f'Subtitles found but {why} — transcribing the audio instead'}))
                    sub_segs = None
            if sub_segs:
                target = {'video_url': url, 'title': (sub_meta or {}).get('title') or url,
                         'video_id': (sub_meta or {}).get('video_id', '')}
                sub_lang = (sub_meta or {}).get('sub_lang')
                q.put(json.dumps({'type': 'progress', 'percent': 90,
                                  'message': f'Found existing {sub_kind} subtitles ({sub_lang}) — skipping download & transcription'}))
                try:
                    saved = _save_subtitle_task(task_id, target, sub_segs, sub_kind, lang=sub_lang,
                                                timing={'subs_check_s': timing['subs_check_s'],
                                                        'wall_s': timing['subs_check_s']})
                    q.put(json.dumps({'type': 'done', 'task_id': task_id,
                                      'segments': saved, 'summary': None,
                                      'subtitle_lang': sub_lang}))
                    if project_id:                 # 在项目里贴的链接：字幕直接落盘这条路也要进项目
                        _project_on_transcribed(project_id, task_id)
                    return
                except Exception as e:  # noqa: BLE001  存字幕失败也别让任务死：转写兜底
                    q.put(json.dumps({'type': 'progress', 'percent': 1,
                                      'message': f'Could not save subtitles ({str(e)[:60]}) — transcribing the audio instead'}))

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
        # 音频挪出 dl_dir（finally 会删它）：云引擎遇网络错时 worker 会用同一个文件
        # 重新排队，放在 dl_dir 里的话这边一返回就被删了，重排必败。
        # 整段下载的登记进 taskdb，重启也能恢复；片段（section）恢复时拿不到 offset，不登记。
        ext = os.path.splitext(item['path'])[1].lstrip('.') or 'mp3'
        audio_path = os.path.join(config.UPLOAD_FOLDER, f'{task_id}.{ext}')
        shutil.move(item['path'], audio_path)
        if not section:
            taskdb.set_upload_path(task_id, audio_path)
        # 复用正常转写链路：落 results/、taskdb done、SSE 推进度，全和上传一致
        run_transcription(task_id, audio_path, engine, title, q,
                          None, False, engine != 'whisper', offset_sec=offset_sec,
                          extra_meta={'source_url': url,
                                      'video_id': item.get('video_id') or _video_id_from_url(url),
                                      'creator': item.get('uploader'),
                                      **({'project_id': project_id} if project_id else {})},
                          timing=timing)
    except Exception as e:  # noqa: BLE001
        taskdb.set_status(task_id, 'failed', error=str(e)[:300])
        q.put(json.dumps({'type': 'error', 'message': str(e)[:200]}))
    finally:
        shutil.rmtree(dl_dir, ignore_errors=True)
        status = (taskdb.get(task_id) or {}).get('status')
        if status in ('pending', 'running'):
            return       # 网络错已重新排队：新 worker 还要用这个 q 和音频
        if section or status != 'failed':
            # 片段音频没法按原 offset 恢复，删掉；done 的 worker 已经删过。
            # 失败的整段音频留着，和上传任务一样可以直接重转、不用重下载。
            for name in os.listdir(config.UPLOAD_FOLDER):
                if name.startswith(task_id + '.'):
                    try:
                        os.remove(os.path.join(config.UPLOAD_FOLDER, name))
                    except OSError:
                        pass
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
    m = re.search(r'\s+@\s*([0-9:]+)\s*-\s*([0-9:]*)\s*$', line)
    if not m:
        return line.strip(), None, 0
    url = line[:m.start()].strip()
    start = timecode.parse(m.group(1), bare_seconds=True)
    end = timecode.parse(m.group(2), bare_seconds=True) if m.group(2) else None
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
    engine = body.get('engine', 'gemini35')
    sub_mode = _clean_sub_mode(body.get('subs'))
    project_id = str(body.get('project_id') or '')          # 在项目里贴的：转完加进那个项目
    project_id = project_id if _chain_ok(project_id) else None
    try:
        max_videos = max(1, min(_MAX_CHAIN_VIDEOS, int(body.get("max_videos") or 20)))   # 和博主链同一个上限
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
        submit_transcription(engine, _download_then_transcribe, task_id, url, engine, q,
                        section, offset_sec, sub_mode, project_id)
        out.append({'url': url, 'title': title, 'task_id': task_id})
    return jsonify({'tasks': out, 'errors': errors})


@app.route('/upload_batch', methods=['POST'])
def upload_batch():
    """一次接收多个文件，各自建独立任务。

    返回每个文件的 task_id（或该文件的错误）。整体只要有至少一个成功
    就返回 200；全部失败返回 400。
    """
    files = request.files.getlist('audios')
    engine = request.form.get('engine', 'gemini35')
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
    has_audio = bool(meta.get('audio_ext')) and os.path.isfile(
        os.path.join(task_dir, f"audio{meta.get('audio_ext')}"))
    return jsonify({**meta, 'segments': segments, 'summary': summary,
                    'has_audio': has_audio, 'cost': usage.cost_for(ref=task_id)})


_summary_jobs = set()   # 正在补摘要的 task_id，防连点重复花钱
_summary_jobs_lock = threading.Lock()


@app.route('/api/history/<task_id>/summary', methods=['POST'])
def api_history_summary(task_id):
    """给一期补摘要（博主链默认不出摘要，想看哪期点一下再生成）。同步返回，十几秒。"""
    if not _is_valid_task_id(task_id):
        return jsonify({'error': 'Invalid task id'}), 400
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    summary_path = os.path.join(task_dir, 'summary.json')
    if os.path.isfile(summary_path):
        with open(summary_path, 'r', encoding='utf-8') as f:
            return jsonify({'ok': True, 'summary': json.load(f)})
    try:
        with open(os.path.join(task_dir, 'transcript.json'), 'r', encoding='utf-8') as f:
            segments = json.load(f)
    except Exception:  # noqa: BLE001
        return jsonify({'error': 'Transcript not found'}), 404
    with _summary_jobs_lock:        # 查-加要原子：同一毫秒双击不能发两次模型调用
        if task_id in _summary_jobs:
            return jsonify({'error': 'Already generating'}), 409
        _summary_jobs.add(task_id)
    try:
        from summarize import summarize_transcript
        full_text = '\n'.join(f"[{s.get('timestamp', '')}] {s.get('text', '')}"
                              for s in segments if isinstance(s, dict))
        # 固定走 Gemini：用户手动点的这一下可能是敏感内容，不往阿里云发
        with usage.scope(ref=task_id):
            summary = summarize_transcript(full_text)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': f'Summary failed: {str(e)[:200]}'}), 502
    finally:
        _summary_jobs.discard(task_id)
    if not summary:
        return jsonify({'error': 'Summary failed'}), 502
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    _update_meta(os.path.join(task_dir, 'meta.json'), {'has_summary': True})
    return jsonify({'ok': True, 'summary': summary})


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

    audio_ext = meta.get('audio_ext') or '.wav'
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
_MAX_CHAIN_VIDEOS = 600  # 防手滑整个频道几千个视频全下下来
_CHAIN_PROBE_COOLDOWN = 1800  # 同一条链两次真探测（打列表接口）之间的最短间隔（秒）：
                               # 防连点 Continue 把出口 IP 敲脏，已有完整缓存时冷却内直接复用

# 协作式取消：/stop 往里加 chain_id，运行中的链条在安全点自查并收尾（已完成产物保留）。
_cancel_chains = set()
# chain.json 的唯一写锁：run_chain、单视频重转、backfill、详情自愈都从这里过，
# 串行化 + 原子写，防并发交错/丢更新/写一半崩溃损坏文件。可重入（RLock）以便
# 读-改-写（先持锁读、改、再调 _save_chain 写）不自锁。
_chain_write_lock = threading.RLock()


def _chain_dir(chain_id):
    return os.path.join(CHAINS_DIR, chain_id)


def _reflect_touch():
    """转写完成后通知回顾模块（防抖后后台重算）和备份模块（防抖后同步到 Google Drive）。"""
    try:
        import reflect
        reflect.touch(config.RESULTS_FOLDER)
    except Exception:  # noqa: BLE001
        pass
    if DEMO_MODE:
        return
    try:
        import backup
        backup.touch(config.RESULTS_FOLDER)
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
    cdir = _chain_dir(state['id'])
    if not os.path.isdir(cdir):
        return       # 链条已被删除：还在收尾的线程别再把它写回来（也别抛异常）
    path = os.path.join(cdir, 'chain.json')
    tmp = path + '.tmp'
    with _chain_write_lock:
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except FileNotFoundError:
            if os.path.isdir(cdir):
                raise        # 目录还在却写不了，是真问题；目录没了就是刚被删，静默放过


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


def _video_id_index(require_speakers=False):
    """{video_id: task_id}：扫所有已完成转写，从 meta.filename 里的 [id] 回填。

    用于去重复用——同一个视频（同 video_id）之前转写过就直接拿旧结果。
    require_speakers：只认带说话人标签的旧结果（要按说话人剔除连麦嘉宾时，
    旧 whisper / 字幕 / 老 gemini 引擎的结果不算数，得重转）。
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
        if require_speakers and not _has_speaker_labels(d, meta):
            continue
        if meta.get('video_id'):
            idx.setdefault(meta['video_id'], name)
        m = _VID_IN_NAME.search(meta.get('filename', '') or '')
        if m:
            idx.setdefault(m.group(1), name)
    return idx


def _has_speaker_labels(result_dir, meta):
    if meta.get('engine') == 'gemini35':            # 这个引擎总是开着 diarization
        return True
    # 看每段开头有没有「说话人N：」前缀，不在全文里找子串——正文里恰好说到「说话人」三个字的稿会被误判
    try:
        with open(os.path.join(result_dir, 'transcript.json'), 'r', encoding='utf-8') as f:
            segs = json.load(f)
    except Exception:
        return False
    return any(isinstance(sg, dict) and _SPEAKER_PREFIX.match(sg.get('text') or '')
               for sg in (segs or [])[:200])


_SPEAKER_PREFIX = re.compile(r'^\s*说话人\s*\d+\s*[：:]')


def _save_subtitle_task(task_id, target, segments, source, lang=None, timing=None,
                        summarize=True, chain_id=None):
    """把抓来的字幕当作转写结果落盘（无音频），并在 taskdb 里标记 done。

    这样它和普通转写任务一样进历史、进分析，只是引擎标为 subtitle、没有音频回放。
    返回落盘的句子级片段（实时推给前端的也该是这份，和 Library 里一致）。
    摘要 / enrich 的花费记到这个 task（和链条）上——这条路径不经过 run_transcription 的 scope。
    """
    with usage.scope(ref=task_id, chain=chain_id):
        return _save_subtitle_task_inner(task_id, target, segments, source, lang, timing,
                                         summarize)


def _save_subtitle_task_inner(task_id, target, segments, source, lang, timing, summarize):
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    os.makedirs(task_dir, exist_ok=True)
    from downloader import merge_caption_cues
    segments = merge_caption_cues(segments)          # 句子级片段，不是两秒一行的字幕 cue
    title = target.get('title') or target.get('video_id') or 'untitled'
    display = f"{title} [{target.get('video_id', '')}]"
    # 字幕来源也出摘要：Library 卡片、详情页 Summary 区跟普通转写一致
    summary = None
    if summarize:
        try:
            from summarize import summarize_transcript
            summary = summarize_transcript('\n'.join(s.get('text', '') for s in segments))
        except Exception:  # noqa: BLE001
            summary = None
    meta = {
        'id': task_id,
        'filename': display,
        'engine': 'subtitle',
        'date': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_ext': None,                    # 字幕来源，无音频
        'segment_count': len(segments),
        'duration_seconds': None,
        'has_summary': bool(summary),
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
    if summary:
        with open(os.path.join(task_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    taskdb.create(task_id, display, 'subtitle', None, None)
    taskdb.set_status(task_id, 'done')
    try:
        from enrich import enrich_task
        enrich_task(task_dir)
    except Exception:
        pass
    _reflect_touch()
    return segments


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


def _task_duration(task_id):
    """这条转写对应音频的时长（秒）；取不到返回 None。"""
    try:
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'),
                  'r', encoding='utf-8') as f:
            return json.load(f).get('duration_seconds')
    except Exception:  # noqa: BLE001
        return None


def _unusable_transcript(task_id):
    """这一期的转写是不是废稿（返回原因，可用则返回 None）。

    放在逐期分析的最前面：废稿连模型体检和抽卡都不该花钱，而且抽卡模型对着
    幻听文本照样能编出几十张「证据卡」，一路流进最终画像。
    """
    try:
        from sanitize import transcript_quality
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'transcript.json'),
                  'r', encoding='utf-8') as f:
            segs = json.load(f)
        if not isinstance(segs, list):
            segs = segs.get('segments') or []
        import glob as _glob
        audio = sorted(_glob.glob(os.path.join(
            config.RESULTS_FOLDER, task_id, 'audio.*')))       # 留了音频副本就一起验静音
        ok, reason, _stats = transcript_quality(
            segs, _task_duration(task_id), audio[0] if audio else None)
        return None if ok else reason
    except Exception:  # noqa: BLE001  判不了就当可用，别挡住正常分析
        return None


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
    """记账归属：链条线程里的分析 / 合成调用都记到 chain_id。"""
    with usage.scope(ref=state['id'], chain=state['id']):
        return _run_chain(state)


def _run_chain(state):
    """链条后台线程：解析 → 边下边转 → 逐期分析 → 总合成。

    下载和转写重叠进行（每个视频下完立刻提交转写），下载并发受全局闸限流；
    分析同样走全局闸。无论开多少条链，对外部的瞬时压力都封顶。
    """
    import glob as _glob
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
        prev_videos = list(state.get('videos') or [])   # 上一轮的视频表（Continue 要用它接回 task_id）
        have_full_cache = bool(prev_videos) and all(v.get('video_url') for v in prev_videos)
        # 上限调大了（想多拿几期）时旧列表不够用，冷却窗口内也得真探测一次；
        # 老链没记 probed_max，就按旧列表条数算
        probed_max = state.get('probed_max') or len(prev_videos)
        cap_raised = (state.get('max_videos') or 10 ** 9) > probed_max

        def _targets_from_prev():
            targets = [{
                'video_url': v['video_url'],
                'title': v.get('title', ''),
                'video_id': v.get('video_id', ''),
                'thumbnail': v.get('thumbnail', ''),
                'view_count': int(v.get('view_count') or 0),
                'upload_date': v.get('upload_date') or '',
            } for v in prev_videos]
            channel = {'name': state.get('author', ''), 'avatar': state.get('avatar', ''),
                       'followers': state.get('followers', 0)}
            return targets, channel

        # 冷却：同一条链短时间内被连点 Continue，没必要每次都真打一次探测接口——
        # 一次探测本身就带内部重试+代理兜底，密集重复触发是把 IP 敲脏的元凶
        # （马督工那次就是这么敲了一天被封的）。有完整缓存时，冷却窗口内直接复用。
        last_probe = state.get('last_probe_at') or 0
        cooldown_left = _CHAIN_PROBE_COOLDOWN - (time.time() - last_probe)
        syncing = chain_id in _sub_runs
        fixed = state.get('fixed_targets')
        if fixed and not syncing:
            # 建的时候在预览里亲手挑的那几期：只处理它们（Continue 也一样），不去探测「最新 N 期」
            targets = [dict(t) for t in fixed]
            channel = {'name': state.get('author', ''), 'avatar': state.get('avatar', ''),
                       'followers': state.get('followers', 0)}
        elif have_full_cache and cooldown_left > 0 and not cap_raised and not syncing:
            print(f'[chain {chain_id[:8]}] 距上次探测不到 {int(_CHAIN_PROBE_COOLDOWN / 60)} 分钟'
                  f'（还剩 {int(cooldown_left)}s），跳过重新探测，沿用缓存列表')
            targets, channel = _targets_from_prev()
        else:
            state['last_probe_at'] = time.time()
            _save_chain(state)
            try:
                if syncing:              # 定期同步：只看最新几十期有没有新的，旧的一律不碰
                    targets, channel = probe(state['url'], SYNC_PROBE_N)
                else:
                    targets, channel = probe(state['url'], state.get('max_videos'))
                    state['probed_max'] = state.get('max_videos') or 10 ** 9
            except Exception as probe_err:
                # 列表探测本身被风控封锁（B站 412 等）：上一轮如果已经拿到过完整目标
                # 列表，没必要陪它一起判死，沿用旧列表接着下/转，只是暂时发现不了新
                # 视频。真正的第一次探测（没有缓存）该失败还是失败。
                if have_full_cache:
                    print(f'[chain {chain_id[:8]}] probe 失败（{probe_err}），'
                          f'改用上一轮缓存的 {len(prev_videos)} 条目标列表续跑')
                    targets, channel = _targets_from_prev()
                else:
                    raise
        if not targets:
            raise RuntimeError('No downloadable videos at this link')

        # 用户没填 author 就用探测到的频道名；头像给卡片展示
        if channel.get('name') and state.get('author') in ('', '该博主'):
            state['author'] = channel['name']
        state['avatar'] = channel.get('avatar', '') or state.get('avatar', '')
        # 订阅数单独取（probe 带 lang=zh-CN 时 YouTube 会返 None）；沿用旧列表时
        # 探测本来就被封了，别再打一次 channel_followers() 陪绑。
        if channel.get('followers'):
            state['followers'] = channel['followers']
        elif not prev_videos:
            state['followers'] = channel_followers(state['url'])
        _save_chain(state)

        # 预置视频网格：一开始就把全部目标铺出来，前端详情页能立刻看到
        videos = [{
            'index': i,
            'title': t.get('title') or t.get('video_id') or f'视频{i + 1}',
            'video_id': t.get('video_id', ''),
            'video_url': t.get('video_url', ''),   # 供 retry 重下用
            'thumbnail': t.get('thumbnail', ''),
            'view_count': int(t.get('view_count') or 0),
            'upload_date': t.get('upload_date') or '',
            'duration': int(t.get('duration') or 0),
            'status': 'downloading',
            'task_id': None,
        } for i, t in enumerate(targets)]
        # 探测没给日期的（YouTube flat 列表多半不给）：沿用上一轮记下的
        _prev_dates = {pv.get('video_id'): pv.get('upload_date') for pv in prev_videos
                       if pv.get('video_id') and pv.get('upload_date')}
        for v in videos:
            if not v['upload_date']:
                v['upload_date'] = _prev_dates.get(v['video_id'], '')

        # 定期同步：探测只看了最新几十期。把它们里真正新的放前面，上一轮的整个列表原样接在后面——
        # 总库只增不减；旧的那些期连状态带 task_id 一个字不改，也不会被重新下载 / 转写 / 抽卡。
        if syncing and prev_videos:
            prev_keys = {pv.get('video_id') for pv in prev_videos if pv.get('video_id')} | \
                        {pv.get('video_url') for pv in prev_videos if pv.get('video_url')}
            # 「新」= 比库里最新那期还新的：频道列表新的在前，碰到第一期认识的就停。
            # （否则一个当初只取了最新 6 期的博主，第一次同步会把最近 30 期里另外 24 期全当成新的下载。）
            fresh_videos = []
            for v in videos:
                if v.get('video_id') in prev_keys or v.get('video_url') in prev_keys:
                    break
                fresh_videos.append(v)
            fresh_keys = {v.get('video_id') or v.get('video_url') for v in fresh_videos}
            videos = fresh_videos + [dict(pv) for pv in prev_videos]
            for i, v in enumerate(videos):
                v['index'] = i
            targets = [t for t in targets if (t.get('video_id') or t.get('video_url')) in fresh_keys] + \
                      [{'video_url': v.get('video_url', ''), 'title': v.get('title', ''),
                        'video_id': v.get('video_id', ''), 'thumbnail': v.get('thumbnail', ''),
                        'view_count': int(v.get('view_count') or 0), 'upload_date': v.get('upload_date', '')}
                       for v in prev_videos]
            state['sync_new'] = [v.get('video_id') or v.get('video_url') for v in fresh_videos]
            print(f'[chain {chain_id[:8]}] 同步：新视频 {len(fresh_videos)} 期，总库保留 {len(prev_videos)} 期')

        # 去重复用：之前已转写过的（同 video_id）直接复用旧结果，跳过下载+转写
        idx = _video_id_index(require_speakers=state.get('require_speakers', False))
        for v in videos:
            tid = v['video_id'] and idx.get(v['video_id'])
            if tid:
                v['task_id'] = tid
                v['status'] = 'done'
                v['source'] = 'reused'
        reused = sum(1 for v in videos if v.get('source') == 'reused')

        # Continue 复用上一轮**失败但音频还在**的任务：上面这份 videos 是按 probe
        # 结果新建的（task_id 全是 None），而去重索引只认已经成功落盘的转写，
        # 失败那几期的 task_id 就此丢掉 → 会重新下载一遍。这里把旧 task_id 接回来，
        # 让下面 _download_and_submit 的「音频还在就直接重转」那条分支真正用得上。
        prev_by_vid, prev_by_url = {}, {}
        for pv in (prev_videos or []):
            if not pv.get('task_id'):
                continue
            if pv.get('video_id'):
                prev_by_vid.setdefault(pv['video_id'], pv['task_id'])
            if pv.get('video_url'):
                prev_by_url.setdefault(pv['video_url'], pv['task_id'])
        resumed = 0
        for v in videos:
            if v.get('task_id'):
                continue
            old = prev_by_vid.get(v.get('video_id')) or prev_by_url.get(v.get('video_url'))
            if not old:
                continue
            row = taskdb.get(old) or {}
            up = row.get('upload_path') or ''
            if row.get('status') in ('pending', 'running') or (up and os.path.isfile(up)):
                v['task_id'] = old
                resumed += 1
        if resumed:
            print(f'[chain {chain_id[:8]}] Continue：{resumed} 期沿用上次留下的音频，不重下载')

        state['videos'] = videos
        state['download_total'] = len(targets)
        state['download_done'] = reused
        _save_chain(state)

        # ---- 2. 边下边转：并发下载（全局限流），每个下完立刻提交转写 ----
        state['stage'] = 'transcribing'

        def _download_and_submit(i, target):
            """一期的下载 + 提交转写。**绝不让异常冒出去**：这些调用跑在
            _download_executor 里，下面 f.result() 会把异常重新抛到链条主线程，
            一期磁盘写失败就能把整条链判 failed、已下好的几期全作废、
            剩下的期永久停在 downloading。单期出事就单期标失败。"""
            try:
                return _download_and_submit_one(i, target)
            except Exception as e:  # noqa: BLE001
                videos[i]['status'] = 'download_failed'
                videos[i]['error'] = str(e)[:300]
                save()
                return None

        sync_new = set(state.get('sync_new') or []) if syncing else None

        def _download_and_submit_one(i, target):
            v = videos[i]
            if v.get('status') == 'done':        # 复用的旧结果，跳过
                return
            if sync_new is not None and (v.get('video_id') or v.get('video_url')) not in sync_new:
                return                           # 同步只管新视频；以前没下成的旧期原样放着，不重试
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
                    taskdb.set_engine(old_tid, state['engine'])
                    q2 = queue.Queue()
                    tasks[old_tid] = q2
                    # extra_meta 跟下面正常下载那条路径保持一致。少了 chain_id，
                    # worker 里的 _chain_stopped(None) 永远判"没停"——点停止拦不住
                    # 这批复用音频重投的任务，花费也记不到这条链上。
                    submit_transcription(state['engine'], run_transcription, old_tid, up,
                                    state['engine'], row.get('filename'), q2, None,
                                    fallback_whisper=state.get('fallback_whisper', False),
                                    summarize=state.get('summarize', False),
                                    extra_meta={'source_url': target.get('video_url') or v.get('video_url'),
                                                'video_id': v.get('video_id') or target.get('video_id'),
                                                'creator': state.get('author') or target.get('uploader'),
                                                'chain_id': chain_id})
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
                        from downloader import subtitle_usable
                        cand = parse_srt(sp) or None
                        usable, _why = subtitle_usable(cand, (_sub_meta or {}).get('duration')) if cand else (False, '')
                        sub_segs = cand if usable else None      # 残缺字幕 → 当没有，下载转写
                item = None if sub_segs else download_one(target, dl_dir)
            with lock:
                state['download_done'] = state.get('download_done', 0) + 1

            # 有字幕 → 直接落转写结果，跳过音频转写（省下载/转写/API）
            if sub_segs:
                task_id = str(uuid.uuid4())
                _save_subtitle_task(task_id, target, sub_segs, sub_source,
                                    lang=(_sub_meta or {}).get('sub_lang'),
                                    summarize=state.get('summarize', False),
                                    chain_id=chain_id)
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
            submit_transcription(
                state['engine'], run_transcription, task_id, upload_path, state['engine'],
                display_name, q, None,
                fallback_whisper=state.get('fallback_whisper', False),
                summarize=state.get('summarize', False),
                extra_meta={'source_url': target.get('video_url'),
                            'video_id': item.get('video_id'),
                            'creator': state.get('author') or item.get('uploader'),
                            'chain_id': chain_id},     # 记账：这期转写的钱算到这条链上
            )
            v['task_id'] = task_id
            v['title'] = item['title']
            v['video_id'] = item['video_id']
            if item.get('view_count'):
                v['view_count'] = int(item['view_count'])   # 下载时抓到的播放量
            if item.get('upload_date'):
                v['upload_date'] = item['upload_date']
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

            # 证据卡缓存：按 task_id 建索引，别按"第几期"。
            # 文件名仍是 cards_001.json（人要看的），但博主只要新发了视频，probe 返回的
            # 顺序整体前移，所有索引全错位 → 旧缓存一份都认不出来 → Continue 把每期重抽
            # 一遍（实测库里大迎那条 195 份卡片全部失配，白花约 $5）。
            cards_cache = {}
            fresh = [0]                          # 这一轮真正新抽了几期（订阅用：0 就不重做画像）
            cards_files = _cards_file_index(chain_dir)
            for _cf in _glob.glob(os.path.join(chain_dir, 'cards_*.json')):
                try:
                    with open(_cf, 'r', encoding='utf-8') as _fh:
                        _ep = json.load(_fh)
                    if _ep.get('cards') and _ep.get('task_id'):
                        cards_cache[_ep['task_id']] = _ep
                except Exception:  # noqa: BLE001  坏文件当没有
                    pass

            def _analyze_one(v):
                if v['status'] != 'done' or chain_id in _cancel_chains:
                    return None
                tpath = os.path.join(
                    config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
                if not os.path.isfile(tpath):
                    return None
                try:
                    # Continue 省钱：这期的证据卡之前抽过就直接用缓存，不再花钱。
                    # 认 task_id：这期若被重转过（新 task），旧卡自然认不上，重抽。
                    cached = cards_cache.get(v['task_id'])
                    if cached:
                        return cached
                    if sync_new is not None and (v.get('video_id') or v.get('video_url')) not in sync_new:
                        return None                  # 同步：旧期没卡就算了，不再花钱重抽
                    # 废稿（纯音乐/噪音标注、静音幻听、解码死循环）就地判不可用：
                    # 不花体检和抽卡的钱，也**留痕**让合成层的护栏算得上这一期
                    bad = _unusable_transcript(v['task_id'])
                    if bad:
                        from analyze import unusable_episode
                        return unusable_episode(v['title'], bad)
                    # 白嫖：分析读转写时顺手做一次模型体检（模型标、代码删）
                    segs = _review_episode_transcript(
                        v['task_id'], preset=state.get('analysis_preset'))
                    if not segs:
                        return None
                    text = '\n'.join(
                        f"[{s.get('timestamp', '')}] {s.get('text', '')}"
                        for s in segs
                    )
                    fresh[0] += 1
                    with _chain_analysis_sem:    # 全局分析闸
                        ep = analyze_episode(v['title'], text, state['author'],
                                             verify=state.get('verify', False),
                                             preset=state.get('analysis_preset'),
                                             segments=segs,
                                             duration=_task_duration(v['task_id']))
                    fname = f"分析_{v['index'] + 1:03d}_{_safe_doc_name(v['title'])}.md"
                    with open(os.path.join(chain_dir, fname),
                              'w', encoding='utf-8') as fh:
                        fh.write(ep['markdown'])
                    ep['task_id'] = v['task_id']       # 缓存键：重转过就作废
                    if v.get('upload_date'):
                        ep['upload_date'] = v['upload_date']
                    with lock:
                        cpath2 = _cards_path_for(chain_dir, v['index'], v['task_id'], cards_files)
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
                results = list(pool.map(usage.bound(_analyze_one), submitted))
            episodes = [r for r in results if r]
            # 只数真正拿到卡片的期：废稿现在会留痕返回，不该算成"分析成功"
            state['analyzed_ok'] = sum(1 for e in episodes if e.get('cards'))

            # ---- 5. 总合成（只吃证据卡，不吃全文）----
            # 合成是最贵的一步：用户已经按了停止就别再花这笔钱
            # （_reanalyze_chain_inner 一直有这个判断，这里以前漏了）
            skip_synth = chain_id in _sub_runs and not fresh[0] and state.get('final_doc')
            if episodes and chain_id not in _cancel_chains and not skip_synth:
                state['stage'] = 'synthesizing'
                save()
                _annotate_speakers(chain_dir, episodes)
                total_md = None
                old_path = os.path.join(chain_dir, '总分析.md')
                new_eps = [e for e in episodes if e.get('cards') and e.get('task_id') not in cards_cache]
                good_n = sum(1 for e in episodes if e.get('cards'))
                if syncing and os.path.isfile(old_path) and new_eps \
                        and len(new_eps) <= max(3, good_n // 4):
                    # 定期同步、新增不多：在原画像上并入新内容（原结论不动，除非被新证据推翻）
                    try:
                        from analyze import update_portrait
                        with open(old_path, 'r', encoding='utf-8') as fh:
                            old_md = fh.read()
                        total_md = update_portrait(old_md, new_eps, state['author'], good_n,
                                                   preset=state.get('analysis_preset'),
                                                   lang=state.get('lang', 'auto'))
                    except Exception as e:  # noqa: BLE001  增量失败就整份重写
                        print(f'[chain {chain_id[:8]}] 增量更新画像失败，改整份重写：{e}')
                if not total_md:
                    total_md = synthesize(episodes, state['author'],
                                          critique_level=state.get('critique_level', 'analytical'),
                                          preset=state.get('analysis_preset'),
                                          self_verify=state.get('self_verify', False),
                                          lang=state.get('lang', 'auto'),
                                          attempted=len(submitted))
                if os.path.isfile(old_path):     # 旧画像留底（history/ 不出现在文档列表里）
                    os.makedirs(os.path.join(chain_dir, 'history'), exist_ok=True)
                    shutil.copy2(old_path, os.path.join(
                        chain_dir, 'history', f"总分析_{datetime.now().strftime('%Y%m%d_%H%M')}.md"))
                with open(old_path, 'w', encoding='utf-8') as fh:
                    fh.write(total_md)
                state['final_doc'] = '总分析.md'

            if state.get('analyze') and chain_id not in _cancel_chains:
                _auto_tag(chain_dir)
                if state.get('auto_predict') and not syncing:   # 建的时候勾了「分析完自动核对预测」
                    try:
                        import ask
                        ask.check_predictions(chain_dir)
                    except Exception as e:  # noqa: BLE001
                        print(f'[chain {chain_id[:8]}] auto predictions: {e}')
        state.pop('sync_new', None)

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
    with usage.scope(ref=state['id'], chain=state['id']):
        return _reanalyze_chain_inner(state)


def _reanalyze_chain_inner(state):
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

        cards_files = _cards_file_index(chain_dir)

        def _analyze_one(v):
            if state['id'] in _cancel_chains:
                return None
            tpath = os.path.join(
                config.RESULTS_FOLDER, v['task_id'], 'transcript.json')
            if not os.path.isfile(tpath):
                return None
            try:
                bad = _unusable_transcript(v['task_id'])
                if bad:
                    from analyze import unusable_episode
                    return unusable_episode(v['title'], bad)
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
                                         preset=state.get('analysis_preset'),
                                         segments=segs,
                                         duration=_task_duration(v['task_id']))
                fname = f"分析_{v['index'] + 1:03d}_{_safe_doc_name(v['title'])}.md"
                with open(os.path.join(chain_dir, fname),
                          'w', encoding='utf-8') as fh:
                    fh.write(ep['markdown'])
                # Re-analyze 是显式重做：无视旧缓存、写入新证据卡（供以后 Continue 复用）
                ep['task_id'] = v['task_id']
                if v.get('upload_date'):
                    ep['upload_date'] = v['upload_date']
                with lock:
                    cpath2 = _cards_path_for(chain_dir, v['index'], v['task_id'], cards_files)
                    with open(cpath2, 'w', encoding='utf-8') as fh:
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
            results = list(pool.map(usage.bound(_analyze_one), submitted))
        episodes = [r for r in results if r]
        state['analyzed_ok'] = sum(1 for e in episodes if e.get('cards'))

        if episodes and state['id'] not in _cancel_chains:
            state['stage'] = 'synthesizing'
            save()
            _annotate_speakers(chain_dir, episodes)
            total_md = synthesize(episodes, state['author'],
                                  critique_level=state.get('critique_level', 'analytical'),
                                  preset=state.get('analysis_preset'),
                                  self_verify=state.get('self_verify', False),
                                  lang=state.get('lang', 'auto'),
                                  attempted=len(submitted))
            with open(os.path.join(chain_dir, '总分析.md'),
                      'w', encoding='utf-8') as fh:
                fh.write(total_md)
            state['final_doc'] = '总分析.md'
        if state['id'] not in _cancel_chains:
            _auto_tag(chain_dir)

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
    # 在项目里加频道：项目还没有博主、这个频道别处也没跑过 → 就用这个项目（跟以前一样）；
    # 项目里已经有博主了 → 另起一条博主链条，按引用加进项目（owner_project 记着是谁建的，首页不单独列）；
    # 这个频道别处已经有链条 → 直接引用那条，不重跑
    host = str(data.get('project_id') or '')
    host_state = None
    owner = None
    if _CHAIN_ID_RE.match(host) and os.path.isfile(os.path.join(_chain_dir(host), 'chain.json')):
        hs = _read_chain(host)
        existing = _find_chain_by_url(url, skip=host)
        if hs.get('url') and _norm_chain_url(hs['url']) == _norm_chain_url(url):
            return jsonify({'error': 'This channel is already in this project'}), 400
        if existing:
            try:
                _project_add_channels(host, [existing])
            except ValueError as e:
                return jsonify({'error': str(e)}), 400
            return jsonify({'chain_id': existing, 'project_id': host, 'existing': True})
        if hs.get('url') or _reg(host)['channels']:
            owner = host
        else:
            host_state = hs
            chain_id = host
    os.makedirs(_chain_dir(chain_id), exist_ok=True)
    state = {
        'id': chain_id,
        'url': url,
        'engine': data.get('engine') or 'gemini35',
        'max_videos': max_videos,
        'analyze': bool(data.get('analyze', True)),
        'prefer_subs': bool(data.get('prefer_subs', False)),
        'sub_lang': _clean_sub_mode(data.get('sub_lang')),   # 字幕语言：auto / zh / en …
        'fallback_whisper': bool(data.get('fallback_whisper', False)),
        # 每期摘要默认关：分析只用证据卡，摘要只是给人翻单期看的，按期计费
        'summarize': bool(data.get('summarize', False)),
        'verify': bool(data.get('verify', False)),
        'self_verify': bool(data.get('self_verify', False)),
        'lang': (data.get('lang') or 'auto'),
        'critique_level': (data.get('critique_level') or 'analytical'),
        'analysis_preset': (data.get('analysis_preset') or 'gemini'),
        'author': (data.get('author') or '').strip() or '该博主',
        'require_speakers': bool(data.get('require_speakers', False)),
        'auto_predict': bool(data.get('auto_predict', False)),
        'avatar': str(data.get('avatar') or '')[:500],
        'followers': int(data.get('followers') or 0) if str(data.get('followers') or '0').isdigit() else 0,
        'stage': 'starting',
        'created_at': __import__('datetime').datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S'),
    }
    if host_state is not None:
        # 名字：用户自己起过名就留着；还是「未命名」就让频道名顶上（run_chain 只在 author 是占位时替换）
        if host_state.get('renamed'):
            state['author'] = host_state.get('author') or state['author']
        state['created_at'] = host_state.get('created_at') or state['created_at']
        for k in ('analysis_preset', 'lang'):
            if host_state.get(k) and not data.get(k):
                state[k] = host_state[k]
        # 项目里原来的录音挪进登记表：chain.json 的视频表从现在起归频道管，下面 run_chain 会整份重写
        old = [v.get('task_id') for v in host_state.get('videos') or [] if v.get('task_id')]
        if old:
            with _chain_write_lock:
                reg = _reg(chain_id)
                have = {r.get('task_id') for r in reg['recordings']}
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                reg['recordings'] += [{'task_id': t, 'added_at': now} for t in old if t not in have]
                _reg_save(chain_id, reg)
    # 建之前在预览里挑好的那些期：就处理这些，不再按「最新 N 期」去探测
    picked = data.get('targets')
    if isinstance(picked, list) and picked:
        clean = []
        for t in picked[:_MAX_CHAIN_VIDEOS]:
            if not isinstance(t, dict) or not str(t.get('video_url') or '').startswith(('http://', 'https://')):
                continue
            clean.append({k: t.get(k) for k in ('video_url', 'title', 'video_id', 'thumbnail',
                                                 'view_count', 'upload_date', 'duration')})
        if clean:
            state['fixed_targets'] = clean
            state['max_videos'] = len(clean)
    if owner:
        state['owner_project'] = owner
    _save_chain(state)
    if owner:
        try:
            _project_add_channels(owner, [chain_id])
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    iv = data.get('sync_interval_h')
    if iv:                                        # 建的时候就开了定期同步
        try:
            iv = int(iv)
        except (TypeError, ValueError):
            iv = 0
        if iv in SUB_INTERVALS:
            _write_sub(chain_id, {'on': True, 'interval_h': iv, 'last_run': time.time(),
                                  'since': datetime.now().strftime('%Y-%m-%d %H:%M:%S')})
    threading.Thread(target=run_chain, args=(state,), daemon=True).start()
    return jsonify({'chain_id': chain_id, 'project_id': owner or host_state and chain_id or None})


# ---- 建博主之前的预览：是谁、一共多少期、每期多长、哪些已经转写过、之前分析过没有 ----
_preview_cache = {}           # 规范化 URL → (时间, 结果)；30 分钟内同一个链接不重复探测（B站风控）


def _norm_chain_url(url):
    from downloader import _normalize_url
    u = _normalize_url((url or '').strip())
    u = re.sub(r'[?&](spm_id_from|vd_source|share_source|share_medium|si|feature|buvid|from_spmid)=[^&#]*', '', u)
    return u.rstrip('/?&').lower()


@app.route('/api/chain/preview', methods=['POST'])
def api_chain_preview():
    body = request.get_json(silent=True) or {}
    url = str(body.get('url') or '').strip()
    if not url.startswith(('http://', 'https://')):
        return jsonify({'error': 'Paste a full http(s) link'}), 400
    key = _norm_chain_url(url)
    hit = _preview_cache.get(key)
    if hit and time.time() - hit[0] < 1800 and not body.get('refresh'):
        data = dict(hit[1])
    else:
        from downloader import probe
        try:
            targets, channel = probe(url, None)
        except Exception as e:  # noqa: BLE001
            return jsonify({'error': str(e)[:300]}), 502
        data = {'channel': {'name': channel.get('name') or '', 'avatar': channel.get('avatar') or '',
                            'followers': channel.get('followers') or 0},
                'videos': [{k: t.get(k) for k in ('video_url', 'title', 'video_id', 'thumbnail',
                                                   'view_count', 'upload_date', 'duration')}
                           for t in targets[:3000]],
                'total': len(targets)}
        _preview_cache[key] = (time.time(), data)
    # 已经转写过的期（跨博主去重）：这些不花转写费
    idx = _video_id_index()
    for v in data['videos']:
        v['transcribed'] = bool(v.get('video_id') and idx.get(v['video_id']))
    # 之前分析过这个博主吗
    existing = None
    for name in os.listdir(CHAINS_DIR) if os.path.isdir(CHAINS_DIR) else []:
        if not _CHAIN_ID_RE.match(name):
            continue
        try:
            with open(os.path.join(CHAINS_DIR, name, 'chain.json'), 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if st.get('kind') in ('collection', 'project') or not st.get('url') or _norm_chain_url(st['url']) != key:
            continue
        done = sum(1 for v in st.get('videos') or [] if v.get('status') == 'done')
        # 同一个频道跑过好几次：优先「分析过的」，再比期数
        if not existing or (bool(st.get('final_doc')), done) > (existing['analyzed'], existing['episodes']):
            existing = {'chain_id': name, 'episodes': done, 'stage': st.get('stage'),
                        'analyzed': bool(st.get('final_doc')), 'sub_on': bool(_read_sub(name).get('on'))}
    data['existing'] = existing
    host = re.sub(r'^www\.', '', (re.match(r'https?://([^/]+)', url) or [None, ''])[1])
    data['platform'] = 'bilibili' if 'bilibili' in host else 'youtube' if 'youtu' in host else host
    # 估价用的单价（美元）：转写按音频小时，分析按期 + 按小时，画像一次
    data['prices'] = {'transcribe_per_hour': {'gemini35': 0.35, 'gemini': 0.19, 'qwenasr': 0.015, 'whisper': 0},
                      'analysis_per_episode': 0.03, 'analysis_per_hour': 0.07, 'portrait': 0.12}
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')


# ===== 合集：任意一批转写（访谈 / 课程 / 会议 / 几个博主混着）当成一个「博主」来问、看立场 =====
# 存法和博主链条完全一样（results/_chains/<id>/chain.json，kind=collection，没有 url），
# 所以提问 / 立场 / 预测 / 原话 / 导出全都直接能用。建的时候：某期在别的博主那里抽过卡的直接复用（不花钱），
# 没抽过的才抽；然后写综述、打标签、算向量、认说话人。

COLLECTION_KINDS = ('interview', 'course', 'meeting', 'podcast', 'mixed')
_MAX_COLLECTION_ITEMS = 300


def _cards_by_task():
    """所有链条里已经抽过的证据卡：task_id → 文件路径（挑卡片最多的那份）。"""
    import glob as _glob
    best = {}
    for f in _glob.glob(os.path.join(CHAINS_DIR, '*', 'cards_*.json')):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                d = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        tid, n = d.get('task_id'), len(d.get('cards') or [])
        if tid and n and n > best.get(tid, ('', 0))[1]:
            best[tid] = (f, n)
    return {k: v[0] for k, v in best.items()}


def _collection_video(task_id, i):
    meta = {}
    try:
        with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'), 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return {'index': i, 'title': meta.get('ai_title') or meta.get('filename') or task_id,
            'task_id': task_id, 'status': 'done', 'source': 'collection',
            'video_url': meta.get('source_url') or '', 'video_id': meta.get('video_id') or '',
            'upload_date': (meta.get('upload_date') or '').replace('-', '')[:8],
            'creator': meta.get('creator') or ''}


def _build_collection(state, rewrite_overview=True):
    with usage.scope(ref=state['id'], chain=state['id']):
        return _build_collection_inner(state, rewrite_overview)


def _overview_written_this_month(cdir):
    """项目综述（总分析.md）是这个月写的吗。综述是整个项目的大总结，一次约 $0.1：
    2026-10-02 用户说「不要每次我传一个就更新，每个月更新一次」。所以加录音时，这个月写过就先不重写，
    下个月再有新录音时一起并进去；用户自己点「生成证据卡 / 综述」照样马上重写。"""
    try:
        t = os.path.getmtime(os.path.join(cdir, '总分析.md'))
    except OSError:
        return False
    return datetime.fromtimestamp(t).strftime('%Y-%m') == datetime.now().strftime('%Y-%m')


def _build_collection_inner(state, rewrite_overview=True):
    import ask
    from analyze import analyze_episode, synthesize_collection
    cid = state['id']
    cdir = _chain_dir(cid)
    lock = threading.Lock()
    try:
        state['stage'] = 'analyzing'
        state['analyzed_done'] = 0
        state.pop('error', None)
        _save_chain(state)
        have = _cards_file_index(cdir)          # 这个合集自己已经有的
        elsewhere = _cards_by_task()            # 别的博主 / 合集里抽过的
        vids = [v for v in state['videos'] if v.get('task_id')]
        fresh = [0]

        def one(v):
            try:
                tid = v['task_id']
                if tid in have and _card_file_done(have[tid]):
                    with open(have[tid], 'r', encoding='utf-8') as f:
                        return json.load(f)
                src = elsewhere.get(tid)
                if src:                          # 复用：原样拷一份过来，不花钱
                    with open(src, 'r', encoding='utf-8') as f:
                        ep = json.load(f)
                else:
                    bad = _unusable_transcript(tid)
                    if bad:
                        from analyze import unusable_episode
                        return unusable_episode(v['title'], bad)
                    segs = _review_episode_transcript(tid, preset=state.get('analysis_preset'))
                    if not segs:
                        return None
                    text = '\n'.join(f"[{s.get('timestamp', '')}] {s.get('text', '')}" for s in segs)
                    fresh[0] += 1
                    with _chain_analysis_sem:
                        ep = analyze_episode(v['title'], text, v.get('creator') or state['author'],
                                             preset=state.get('analysis_preset'), segments=segs,
                                             duration=_task_duration(tid))
                    ep['task_id'] = tid
                with lock:
                    path = _cards_path_for(cdir, v['index'], tid, have)
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(ep, f, ensure_ascii=False)
                return ep
            except Exception as e:  # noqa: BLE001  单条失败不拖垮整个合集
                print(f'[collection {cid[:8]}] {v.get("task_id")}: {e}')
                return None
            finally:
                with lock:
                    state['analyzed_done'] = state.get('analyzed_done', 0) + 1
                    _save_chain(state)

        with ThreadPoolExecutor(max_workers=config.CHAIN_ANALYSIS_CONCURRENCY) as pool:
            episodes = [e for e in pool.map(usage.bound(one), vids) if e]
        state['analyzed_ok'] = sum(1 for e in episodes if e.get('cards'))
        state['reused'] = len(vids) - fresh[0]
        if episodes and state.get('analyzed_ok'):
            state['stage'] = 'synthesizing'
            _save_chain(state)
            _annotate_speakers(cdir, episodes)
            if rewrite_overview or not _overview_written_this_month(cdir):
                md = synthesize_collection(episodes, state['author'], kind=state.get('collection_kind', 'mixed'),
                                           preset=state.get('analysis_preset'), lang=state.get('lang', 'auto'))
                with open(os.path.join(cdir, '总分析.md'), 'w', encoding='utf-8') as f:
                    f.write(md)
                state['final_doc'] = '总分析.md'
                state.pop('overview_behind', None)
            else:
                state['overview_behind'] = True          # 综述还没并进这个月新加的录音（下个月自动补）
            _auto_tag(cdir)
        state['stage'] = 'done'
    except Exception as e:  # noqa: BLE001
        state['stage'] = 'failed'
        state['error'] = str(e)
    finally:
        state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _save_chain(state)


def _collection_members(body):
    """请求里的成员：task_ids + 整个博主（chain_ids，展开成它已转写的每一期）。去重保序。"""
    tids = [t for t in (body.get('task_ids') or []) if isinstance(t, str)]
    for ch in body.get('chain_ids') or []:
        if not _CHAIN_ID_RE.match(str(ch)):
            continue
        try:
            with open(os.path.join(_chain_dir(ch), 'chain.json'), 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        tids += [v['task_id'] for v in st.get('videos') or [] if v.get('task_id') and v.get('status') == 'done']
    seen, out = set(), []
    for t in tids:
        if t not in seen and os.path.isfile(os.path.join(config.RESULTS_FOLDER, t, 'transcript.json')):
            seen.add(t)
            out.append(t)
    return out


@app.route('/api/collections', methods=['POST'])
def api_collection_create():
    """body: {name, kind, task_ids?, chain_ids?}"""
    body = request.get_json(silent=True) or {}
    name = str(body.get('name') or '').strip()[:60]
    kind = body.get('kind') if body.get('kind') in COLLECTION_KINDS else 'mixed'
    if not name:
        return jsonify({'error': 'Name the collection'}), 400
    tids = _collection_members(body)
    if not tids:
        return jsonify({'error': 'Pick at least one transcript'}), 400
    if len(tids) > _MAX_COLLECTION_ITEMS:
        return jsonify({'error': f'At most {_MAX_COLLECTION_ITEMS} items per collection'}), 400
    cid = uuid.uuid4().hex
    os.makedirs(_chain_dir(cid), exist_ok=True)
    state = {'id': cid, 'kind': 'collection', 'collection_kind': kind, 'url': '', 'author': name,
             'engine': '', 'analyze': True, 'analysis_preset': body.get('analysis_preset') or 'gemini',
             'lang': body.get('lang') or 'auto', 'stage': 'analyzing',
             'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             'videos': [_collection_video(t, i) for i, t in enumerate(tids)],
             'download_total': len(tids), 'download_done': len(tids)}
    _save_chain(state)
    threading.Thread(target=_build_collection, args=(state,), daemon=True).start()
    return jsonify({'ok': True, 'id': cid, 'items': len(tids)})


@app.route('/api/collections/<cid>/add', methods=['POST'])
def api_collection_add(cid):
    """往合集里加内容（再跑一遍：已有的卡直接复用，只抽新加的）。"""
    if not _chain_ok(cid):
        return jsonify({'error': 'Not found'}), 404
    with open(os.path.join(_chain_dir(cid), 'chain.json'), 'r', encoding='utf-8') as f:
        state = json.load(f)
    if state.get('kind') != 'collection':
        return jsonify({'error': 'Not a collection'}), 400
    if state.get('stage') not in ('done', 'failed', 'cancelled'):
        return jsonify({'error': 'Still building'}), 409
    have = {v['task_id'] for v in state['videos']}
    new = [t for t in _collection_members(request.get_json(silent=True) or {}) if t not in have]
    if len(have) + len(new) > _MAX_COLLECTION_ITEMS:
        return jsonify({'error': f'At most {_MAX_COLLECTION_ITEMS} items per collection'}), 400
    state['videos'] += [_collection_video(t, len(state['videos']) + i) for i, t in enumerate(new)]
    state['download_total'] = state['download_done'] = len(state['videos'])
    state['stage'] = 'analyzing'
    _save_chain(state)
    threading.Thread(target=_build_collection, args=(state,), daemon=True).start()
    return jsonify({'ok': True, 'added': len(new)})


@app.route('/api/transcripts/pick')
def api_transcripts_pick():
    """建合集时挑转写：标题 / 博主 / 时长 / 日期 / 有没有说话人，按新到旧，支持 ?q= 搜索。"""
    import glob as _glob
    q = (request.args.get('q') or '').strip().lower()
    rows = []
    for mp in _glob.glob(os.path.join(config.RESULTS_FOLDER, '*', 'meta.json')):
        tid = os.path.basename(os.path.dirname(mp))
        try:
            with open(mp, 'r', encoding='utf-8') as f:
                m = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        title = m.get('ai_title') or m.get('filename') or tid
        creator = m.get('creator') or ''
        if q and q not in (title + ' ' + creator + ' ' + (m.get('filename') or '')).lower():
            continue
        rows.append({'task_id': tid, 'title': title, 'creator': creator, 'date': (m.get('date') or '')[:10],
                     'minutes': round((m.get('duration_seconds') or 0) / 60), 'engine': m.get('engine') or ''})
    rows.sort(key=lambda r: r['date'], reverse=True)
    return Response(json.dumps({'items': rows[:400], 'total': len(rows)}, ensure_ascii=False),
                    mimetype='application/json')


# ===== 项目：任意来源（频道 / 录音 / 文档 / 粘贴的文字）放在一起问 =====
# 存法跟博主链条、合集一样（results/_chains/<id>/chain.json），kind='project'，没有 url。
# template 只决定默认设置：study / blank 不抽证据卡、转写全文入索引；topic 抽卡并写综述（跟合集一样）。
# 文档全局存在 results/_docs/<doc_id>/（sources.py），项目里只记 docs: [{doc_id, added_at}]。
# 提问时文档 / 转写的原文段落跟证据卡一起检索（ask.load(passages=True)），出处能点回原文那一段。

_MAX_PROJECT_DOCS = 300
_project_jobs = {}            # cid -> 'running' | 'dirty'（跑的时候又有新来源进来：跑完再来一轮）
_project_jobs_lock = threading.Lock()


def _read_chain(cid):
    with open(os.path.join(_chain_dir(cid), 'chain.json'), 'r', encoding='utf-8') as f:
        return json.load(f)


def _project_ok(cid):
    return _chain_ok(cid)


# ---- 来源登记表 sources.json：用户自己加的文档、（有频道的项目里）额外加的录音 ----
# 不放 chain.json：频道项目的 chain.json 由 run_chain 整份重写（同步时视频表会重排），
# 文档 / 额外录音放那里会在并发时被覆盖。没有频道的项目，录音照旧放 chain.json 的 videos
# （跟合集一样，抽卡 / 综述都现成能用）。
def _reg_path(cid):
    return os.path.join(_chain_dir(cid), 'sources.json')


def _reg(cid):
    try:
        with open(_reg_path(cid), 'r', encoding='utf-8') as f:
            reg = json.load(f)
    except Exception:  # noqa: BLE001
        reg = {}
    reg.setdefault('docs', [])
    reg.setdefault('recordings', [])
    reg.setdefault('channels', [])
    return reg


def _reg_save(cid, reg):
    tmp = _reg_path(cid) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _reg_path(cid))


def _reg_doc_ids(cid):
    return [d.get('doc_id') for d in _reg(cid)['docs'] if d.get('doc_id')]


# ---- 一个项目里可以有好几个博主：除了项目自己的频道（有 url 的那个），别的博主按引用放在登记表的 channels 里 ----
# 博主的转写、证据卡、画像、镜头、立场、预测都在他自己的链条里算一次，几个项目共用，不重抽、不重复花钱。
# 每个引用有个固定字母（B、C……）：项目里提问时他的卡片 id 写成 B:3-12，聊天记录里的出处靠它对上人，
# 所以拿掉的字母不再发给别人（retired_tags），免得旧回答的出处指到另一个人身上。
_REF_TAGS = 'BCDEFGHIJKLMNOPQRSTUVWXYZ'


def _ref_rows(cid, reg=None):
    reg = reg or _reg(cid)
    out = []
    for r in reg['channels']:
        rid = r.get('chain_id')
        if not _chain_ok(rid):
            continue
        try:
            st = _read_chain(rid)
        except Exception:  # noqa: BLE001
            continue
        out.append({'chain_id': rid, 'tag': r.get('tag') or '', 'added_at': r.get('added_at'), 'state': st})
    return out


def _project_add_channels(cid, chain_ids):
    """把已有的博主链条加进项目（引用，不拷贝）。返回真加进去的。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new = []
    with _chain_write_lock:
        reg = _reg(cid)
        have = {r.get('chain_id') for r in reg['channels']}
        used = {r.get('tag') for r in reg['channels']} | set(reg.get('retired_tags') or [])
        for rid in chain_ids:
            if rid == cid or rid in have or not _chain_ok(rid):
                continue
            tag = next((t for t in _REF_TAGS if t not in used), None)
            if not tag:
                raise ValueError('Too many creators in one project')
            used.add(tag)
            have.add(rid)
            reg['channels'].append({'chain_id': rid, 'tag': tag, 'added_at': now})
            new.append(rid)
        if new:
            _reg_save(cid, reg)
    if new:
        import ask
        for rid in new:                       # 他的卡多半早有向量了；没有的话后台补（项目里按意思检索要用）
            ask.ensure_embeddings_async(_chain_dir(rid))
    return new


def _find_chain_by_url(url, skip=None):
    """同一个频道已经有链条了吗（几个项目共用一个博主）。有好几条取卡最多的。"""
    import glob as _glob
    want = _norm_chain_url(url)
    best = None
    for d in _glob.glob(os.path.join(CHAINS_DIR, '*')):
        name = os.path.basename(d)
        if not _CHAIN_ID_RE.match(name) or name == skip:
            continue
        st = _read_json_safe(os.path.join(d, 'chain.json'))
        if not st.get('url') or st.get('merged_into') or _norm_chain_url(st['url']) != want:
            continue
        n = len(_glob.glob(os.path.join(d, 'cards_*.json')))
        if best is None or n > best[1]:
            best = (name, n)
    return best[0] if best else None


def _person_row(cid, st, tag='', is_self=False):
    cdir = _chain_dir(cid)
    try:
        names = os.listdir(cdir)
    except OSError:
        names = []
    vids = st.get('videos') or []
    return {'chain_id': cid, 'tag': tag, 'self': is_self, 'name': st.get('author') or '',
            'avatar': st.get('avatar') or '', 'url': st.get('url') or '', 'stage': st.get('stage') or '',
            'kind': st.get('kind') or 'creator', 'emoji': (_chain_prefs().get('emoji') or {}).get(cid) or '',
            'episodes': sum(1 for v in vids if v.get('task_id') and v.get('status') == 'done'),
            'has_cards': any(f.startswith('cards_') and _cards_file_has_cards(os.path.join(cdir, f)) for f in names),
            'portrait': bool(st.get('final_doc')) and st.get('final_doc') in names, 'final_doc': st.get('final_doc') or '',
            'lenses': sorted(n[3:-3] for n in names if n.startswith('镜头_') and n.endswith('.md'))}


def _project_people(cid):
    """项目里的「人」：项目自己的频道（或抽过卡的零散录音）+ 引用的博主。画像 / 立场 / 预测 / 原话按人看。"""
    st = _read_chain(cid)
    out = []
    own = _person_row(cid, st, is_self=True)
    if st.get('url') or own['has_cards']:
        out.append(own)
    out += [_person_row(r['chain_id'], r['state'], tag=r['tag']) for r in _ref_rows(cid)]
    return out


@app.route('/api/chain/<chain_id>/people')
def api_project_people(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    return Response(json.dumps({'people': _project_people(chain_id)}, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/chain/<chain_id>/sources/channels', methods=['POST'])
def api_project_add_channels(chain_id):
    """把资料库里已有的博主加进项目：{chain_ids: [...]}（共用他的分析，不重抽）。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    ids = [c for c in ((request.get_json(silent=True) or {}).get('chain_ids') or []) if isinstance(c, str)]
    try:
        new = _project_add_channels(chain_id, ids)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'added': len(new)})


@app.route('/api/projects', methods=['POST'])
def api_project_create():
    """新建项目：马上建一个空的（名字前端给「未命名项目」），进去再加来源。频道也是一种来源。"""
    body = request.get_json(silent=True) or {}
    name = str(body.get('name') or '').strip()[:60] or 'Untitled project'
    cid = uuid.uuid4().hex
    os.makedirs(_chain_dir(cid), exist_ok=True)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    state = {'id': cid, 'kind': 'project', 'template': 'blank', 'url': '', 'author': name,
             'engine': '', 'analyze': False, 'collection_kind': 'mixed', 'index_transcripts': True,
             'analysis_preset': body.get('analysis_preset') or 'gemini', 'lang': body.get('lang') or 'auto',
             'stage': 'done', 'created_at': now, 'finished_at': now,
             'videos': [], 'download_total': 0, 'download_done': 0}
    _save_chain(state)
    _reg_save(cid, {'docs': [], 'recordings': []})
    return jsonify({'ok': True, 'id': cid})


@app.route('/api/chain/<chain_id>/rename', methods=['POST'])
def api_project_rename(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    name = str((request.get_json(silent=True) or {}).get('name') or '').strip()[:60]
    if not name:
        return jsonify({'error': 'Empty name'}), 400
    with _chain_write_lock:
        state = _read_chain(chain_id)
        state['author'] = name
        state['author_checked'] = True        # 改过名就别再拿频道名覆盖
        state['renamed'] = True
        _save_chain(state)
    return jsonify({'ok': True, 'name': name})


def _doc_rows(cid):
    import sources
    out = []
    for ref in _reg(cid)['docs']:
        m = sources.doc_meta(ref.get('doc_id')) or {}
        out.append({'doc_id': ref.get('doc_id'), 'added_at': ref.get('added_at'),
                    'title': m.get('title') or '(missing)', 'ext': m.get('ext') or '',
                    'status': m.get('status') or 'missing', 'error': m.get('error') or '',
                    'pages': m.get('pages'), 'chars': m.get('chars') or 0, 'converter': m.get('converter') or '',
                    'url': m.get('url') or ''})
    return out


def _rec_rows(cid):
    out = []
    for r in _reg(cid)['recordings']:
        tid = r.get('task_id')
        if not tid:
            continue
        done = os.path.isfile(os.path.join(config.RESULTS_FOLDER, tid, 'transcript.json'))
        out.append({'task_id': tid, 'title': _transcript_title(tid) if done else (r.get('title') or tid),
                    'status': 'done' if done else 'transcribing', 'added_at': r.get('added_at')})
    return out


@app.route('/api/chain/<chain_id>/sources')
def api_project_sources(chain_id):
    """左栏「来源」：频道（有的话）、频道里的每期 / 项目里的录音、额外录音、文档。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import sources
    state = _read_chain(chain_id)
    vids = [{k: v.get(k) for k in ('task_id', 'title', 'status', 'source', 'video_url', 'creator', 'index',
                                   'upload_date', 'thumbnail')}
            for v in state.get('videos') or []]
    have_cards = _cards_file_index(_chain_dir(chain_id))
    channel = None
    if state.get('url'):
        channel = {'url': state['url'], 'name': state.get('author') or '', 'avatar': state.get('avatar') or '',
                   'followers': state.get('followers') or 0, 'stage': state.get('stage')}
    missing = [v for v in vids if v.get('task_id') and v.get('status') == 'done'
               and not (v['task_id'] in have_cards and _card_file_done(have_cards[v['task_id']]))]
    # 引用的博主：每个一组，期列在下面（勾选 = 提问范围，跟自己的期一样）
    channels = []
    for r in _ref_rows(chain_id):
        st = r['state']
        channels.append({'chain_id': r['chain_id'], 'tag': r['tag'], 'url': st.get('url') or '',
                         'name': st.get('author') or '', 'avatar': st.get('avatar') or '',
                         'stage': st.get('stage'), 'kind': st.get('kind') or 'creator',
                         'videos': [{k: v.get(k) for k in ('task_id', 'title', 'status', 'video_url', 'index',
                                                            'upload_date')}
                                    for v in st.get('videos') or [] if v.get('task_id')]})
    return Response(json.dumps({
        'kind': state.get('kind') or 'creator', 'channel': channel, 'channels': channels,
        'index_transcripts': bool(state.get('index_transcripts')), 'analyze': bool(state.get('analyze')),
        'videos': vids, 'recordings': _rec_rows(chain_id), 'docs': _doc_rows(chain_id),
        'indexing': chain_id in _project_jobs, 'moye': sources.moye_alive(timeout=1),
        'has_cards': bool(have_cards), 'cards_missing': len(missing) if not state.get('url') else 0,
    }, ensure_ascii=False), mimetype='application/json')


def _project_add_docs(cid, doc_ids):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _chain_write_lock:
        reg = _reg(cid)
        have = {d.get('doc_id') for d in reg['docs']}
        new = [d for d in doc_ids if d not in have]
        if len(have) + len(new) > _MAX_PROJECT_DOCS:
            raise ValueError(f'At most {_MAX_PROJECT_DOCS} documents per project')
        reg['docs'] += [{'doc_id': d, 'added_at': now} for d in new]
        _reg_save(cid, reg)
    return new


def _project_add_recordings(cid, task_ids):
    """有频道的项目：记进登记表（chain.json 的视频表归频道管）；没频道的：进 chain.json 的 videos。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with _chain_write_lock:
        state = _read_chain(cid)
        if state.get('url'):
            reg = _reg(cid)
            have = {r.get('task_id') for r in reg['recordings']} | \
                   {v.get('task_id') for v in state.get('videos') or []}
            new = [t for t in task_ids if t not in have]
            reg['recordings'] += [{'task_id': t, 'added_at': now} for t in new]
            _reg_save(cid, reg)
            return new
        vids = state.get('videos') or []
        have = {v.get('task_id') for v in vids}
        new = [t for t in task_ids if t not in have]
        if len(have) + len(new) > _MAX_COLLECTION_ITEMS:
            raise ValueError(f'At most {_MAX_COLLECTION_ITEMS} recordings per project')
        state['videos'] = vids + [_collection_video(t, len(vids) + i) for i, t in enumerate(new)]
        state['download_total'] = state['download_done'] = len(state['videos'])
        _save_chain(state)
        return new


@app.route('/api/chain/<chain_id>/sources/docs', methods=['POST'])
def api_project_add_docs(chain_id):
    """上传文档（multipart: files）或粘贴文字（JSON: {text, title}）。转换在后台，转完自动建索引。"""
    if not _project_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import sources
    made, errors = [], []
    if request.files:
        for f in request.files.getlist('files'):
            try:
                made.append(sources.create_doc(filename=f.filename, data=f.read()))
            except ValueError as e:
                errors.append({'filename': f.filename, 'error': str(e)})
    else:
        body = request.get_json(silent=True) or {}
        try:
            made.append(sources.create_doc(text=body.get('text'), title=body.get('title')))
        except ValueError as e:
            errors.append({'filename': '', 'error': str(e)})
    if not made:
        return jsonify({'error': (errors[0]['error'] if errors else 'Nothing to add'), 'errors': errors}), 400
    try:
        _project_add_docs(chain_id, [m['id'] for m in made])
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    _project_refresh(chain_id)
    return jsonify({'ok': True, 'docs': [{'doc_id': m['id'], 'title': m['title'], 'status': m['status']}
                                         for m in made], 'errors': errors})


@app.route('/api/chain/<chain_id>/sources/web', methods=['POST'])
def api_project_add_web(chain_id):
    """网页 / 文件链接当来源：{urls: [...]}。后台抓取，抓完自动建索引。"""
    if not _project_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import sources
    body = request.get_json(silent=True) or {}
    urls = body.get('urls') or []
    if isinstance(urls, str):
        urls = urls.split()
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()][:30]
    have = {(sources.doc_meta(d) or {}).get('url') for d in _reg_doc_ids(chain_id)}
    made, errors = [], []
    for u in urls:
        if u in have:
            continue
        try:
            made.append(sources.create_web_doc(u))
            have.add(u)
        except ValueError as e:
            errors.append({'url': u, 'error': str(e)})
    if not made:
        return jsonify({'error': (errors[0]['error'] if errors else 'Already in this project'), 'errors': errors}), 400
    _project_add_docs(chain_id, [m['id'] for m in made])
    _project_refresh(chain_id)
    return jsonify({'ok': True, 'added': len(made), 'errors': errors})


@app.route('/api/chain/<chain_id>/discover', methods=['POST'])
def api_project_discover(chain_id):
    """找来源：{query, kind: web|youtube|bilibili} → 候选列表（不加进项目），已经在项目里的标 have。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import discover
    import sources
    body = request.get_json(silent=True) or {}
    kind = body.get('kind') if body.get('kind') in ('web', 'youtube', 'bilibili') else 'web'
    ref = f'discover:{uuid.uuid4().hex[:12]}'
    try:
        with usage.scope(ref=ref, chain=chain_id):
            items = discover.search(body.get('query'), kind)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)[:300]}), 502
    state = _read_chain(chain_id)
    have = {(sources.doc_meta(d) or {}).get('url') for d in _reg_doc_ids(chain_id)}
    vid_ids = set()
    for v in state.get('videos') or []:
        vid_ids.add(_video_key(v.get('video_url')))
    for r in _reg(chain_id)['recordings']:
        m = _read_json_safe(os.path.join(config.RESULTS_FOLDER, r.get('task_id') or '-', 'meta.json'))
        vid_ids.add(_video_key(m.get('url') or m.get('source_url') or m.get('video_url')))
    vid_ids.discard('')
    for it in items:
        it['have'] = it['url'] in have or (it['type'] == 'video' and _video_key(it['url']) in vid_ids)
    return Response(json.dumps({'items': items, 'cost_usd': (usage.cost_for(ref=ref) or {}).get('cost_usd', 0)},
                               ensure_ascii=False), mimetype='application/json')


def _video_key(url):
    """视频链接 → 平台 + 视频号（同一个视频的不同写法认成一个）。"""
    u = str(url or '')
    m = re.search(r'(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})', u)
    if m:
        return 'yt:' + m.group(1)
    m = re.search(r'(BV[0-9A-Za-z]{10})', u)
    if m:
        return 'bili:' + m.group(1)
    return ''


def _read_json_safe(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


@app.route('/api/chain/<chain_id>/sources/transcripts', methods=['POST'])
def api_project_add_transcripts(chain_id):
    """从资料库挑已经转写好的（task_ids）或整个博主（chain_ids）。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    body = dict(request.get_json(silent=True) or {})
    # 整个博主（有频道的链条）：作为引用加进来，共用他的分析；合集 / 项目还是展开成一条条录音
    creators, others = [], []
    for ch in body.get('chain_ids') or []:
        st = _read_json_safe(os.path.join(_chain_dir(ch), 'chain.json')) if _CHAIN_ID_RE.match(str(ch)) else {}
        (creators if st.get('url') and st.get('kind') not in ('collection', 'project') else others).append(ch)
    body['chain_ids'] = others
    try:
        refs = _project_add_channels(chain_id, creators)
        new = _project_add_recordings(chain_id, _collection_members(body)) if body.get('task_ids') or others else []
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if new:
        _project_refresh(chain_id)
    return jsonify({'ok': True, 'added': len(new) + len(refs)})


@app.route('/api/chain/<chain_id>/sources/upload', methods=['POST'])
def api_project_upload(chain_id):
    """往项目里直接传录音 / 视频：照常转写，转完自己加进这个项目。multipart: audios, engine"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    engine = request.form.get('engine', 'gemini35')
    results = []
    for f in request.files.getlist('audios'):
        tid, err = _enqueue_task(f, engine, _parse_speaker_count(request.form.get('speaker_count')),
                                 extra_meta={'project_id': chain_id})
        results.append({'filename': f.filename, 'task_id': tid, 'error': err})
    if not any(r['task_id'] for r in results):
        return jsonify({'error': 'Nothing to transcribe', 'tasks': results}), 400
    return jsonify({'ok': True, 'tasks': results})


def _project_on_transcribed(cid, task_id):
    """项目里直接传 / 贴链接的录音转完了：加进这个项目（只加一次），再建索引。"""
    if not _CHAIN_ID_RE.match(cid or '') or not os.path.isfile(os.path.join(_chain_dir(cid), 'chain.json')):
        return
    try:
        new = _project_add_recordings(cid, [task_id])
    except ValueError as e:
        print(f'[project {cid[:8]}] {e}')
        return
    if new:
        _project_refresh(cid)


# ================= 声纹：项目里认说话人（voices.py；本机算，音频不出电脑）=================

_VOICE_TID_RE = re.compile(r'^[0-9a-f-]{32,36}$')


def _voices_refresh(cid):
    """项目的录音（声纹）变了：重新认说话人 → 卡片的「谁说的」跟着更新 → 没名字的问一次有没有被点名。"""
    import ask
    import voices
    cdir = _chain_dir(cid)
    try:
        st = voices.identify(cdir)
        if not st.get('speakers'):
            return
        ask.build_speakers(cdir)
        with usage.scope(ref=f'voices:{cid[:8]}', chain=cid):
            voices.suggest_names(cdir)
    except Exception as e:  # noqa: BLE001  认不出说话人不影响项目
        print(f'[voices {cid[:8]}] {e}')


def _voices_projects_of(task_id):
    """这条录音在哪些项目里（声纹做完后这些项目都要重新认人）。"""
    out = []
    for cid in os.listdir(CHAINS_DIR):
        st = _read_json_safe(os.path.join(CHAINS_DIR, cid, 'chain.json')) if _CHAIN_ID_RE.match(cid) else {}
        if st.get('kind') == 'project' and any(v.get('task_id') == task_id for v in st.get('videos') or []):
            out.append(cid)
    return out


def _voices_after(task_id):
    for cid in _voices_projects_of(task_id):
        _voices_refresh(cid)


def _voices_keep(task_id, path, offset_sec=0):
    """转写成功、源文件删掉之前：留一份试听音频，声纹排进后台（一次一条，一小时音频约 2–3 分钟 CPU）。
    VOICES_AUTO=off 可关；没装声纹模型时什么都不做。"""
    import voices
    if offset_sec or (os.environ.get('VOICES_AUTO') or 'on').lower() == 'off' or not voices.available():
        return
    try:
        out = voices.keep_audio(task_id, path)
        if out:
            voices.queue(task_id, out, then=_voices_after)
    except Exception as e:  # noqa: BLE001
        print(f'[voices] keep {task_id[:8]}: {e}')


@app.route('/api/chain/<chain_id>/voices', methods=['GET', 'POST'])
def api_chain_voices(chain_id):
    """GET：说话人名单。POST {action: rename|role|merge|detach|not_same|accept_hint, speaker, …}：用户的决定。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import voices
    cdir = _chain_dir(chain_id)
    if request.method == 'GET':
        out = voices.roster(cdir)
        if out['lessons_with_voices'] and not voices._state(cdir).get('built_at'):   # 声纹是在别处做的、这个项目还没认过
            voices.identify(cdir)
            out = voices.roster(cdir)
        return jsonify(out)
    body = request.get_json(silent=True) or {}
    try:
        out = voices.act(cdir, body.get('action'), speaker=body.get('speaker'), name=body.get('name'),
                         role=body.get('role'), into=body.get('into'), group=body.get('group'), other=body.get('other'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    def relabel():
        import ask
        try:
            ask.build_speakers(cdir)
        except Exception as e:  # noqa: BLE001
            print(f'[voices {chain_id[:8]}] relabel cards: {e}')
    threading.Thread(target=relabel, daemon=True).start()
    return jsonify(out)


@app.route('/api/chain/<chain_id>/voices/fingerprint', methods=['POST'])
def api_chain_voices_fingerprint(chain_id):
    """以前转写、没留音频的录音：按原文件名在本机找原音频（时长对得上才用），找到的排进后台做声纹。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import voices
    if not voices.available():
        return jsonify({'error': 'Voice models are not installed'}), 400
    want = set((request.get_json(silent=True) or {}).get('task_ids') or [])
    queued, not_found = [], []
    for m in voices.roster(_chain_dir(chain_id))['missing']:
        tid = m['task_id']
        if (want and tid not in want) or (m.get('job') or {}).get('state') in ('queued', 'running'):
            continue
        src = voices.audio_path(tid) if voices.has_audio(tid) else voices.find_original(tid)
        if src:
            voices.queue(tid, src, then=lambda t, cid=chain_id: _voices_refresh(cid))
            queued.append(tid)
        else:
            not_found.append({'task_id': tid, 'title': m.get('title'),
                              'filename': (_read_json_safe(os.path.join(config.RESULTS_FOLDER, tid, 'meta.json'))
                                           or {}).get('filename', '')})
    return jsonify({'queued': queued, 'not_found': not_found})


@app.route('/api/chain/<chain_id>/voices/<task_id>', methods=['GET'])
def api_chain_voices_labels(chain_id, task_id):
    """这条录音每句转写是谁说的（转写阅读器用）。"""
    if not _chain_ok(chain_id) or not _VOICE_TID_RE.match(task_id):
        return jsonify({'error': 'Not found'}), 404
    import voices
    segs = voices.speaker_segments(_chain_dir(chain_id), task_id)
    return jsonify({'labels': [{'i': i, 'name': n, 'speaker': sid} for i, (_, n, sid, _) in enumerate(segs)],
                    'audio': voices.has_audio(task_id)})


@app.route('/api/voices/<task_id>/audio')
def api_voice_audio(task_id):
    """试听用的那份压缩音频（支持 Range，可以直接跳到某一秒）。"""
    import voices
    if not _VOICE_TID_RE.match(task_id) or not voices.has_audio(task_id):
        return jsonify({'error': 'Not found'}), 404
    return send_file(voices.audio_path(task_id), mimetype='audio/mp4', conditional=True)


@app.route('/api/voices/<task_id>/fingerprint', methods=['POST'])
def api_voice_fingerprint_upload(task_id):
    """找不到原音频时，用户自己选文件：时长和转写对得上才收，排进后台做声纹，做完删掉这份上传。"""
    import voices
    if not _VOICE_TID_RE.match(task_id) or not os.path.isdir(os.path.join(config.RESULTS_FOLDER, task_id)):
        return jsonify({'error': 'Not found'}), 404
    if not voices.available():
        return jsonify({'error': 'Voice models are not installed'}), 400
    f = request.files.get('audio')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400
    ext = os.path.splitext(f.filename)[1].lower()[:8] or '.bin'
    path = os.path.join(config.UPLOAD_FOLDER, f'voice_{task_id}{ext}')
    f.save(path)
    want = (_read_json_safe(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json')) or {}).get('duration_seconds')
    got = voices._probe_duration(path)
    if want and not voices._close(got, want):
        os.remove(path)
        return jsonify({'error': 'duration_mismatch', 'file_seconds': got, 'transcript_seconds': want}), 400
    voices.queue(task_id, path, cleanup=True, then=_voices_after)
    return jsonify({'queued': True})


@app.route('/api/chain/<chain_id>/sources/<source_id>', methods=['DELETE'])
def api_project_remove_source(chain_id, source_id):
    """从项目里拿掉一个来源（文档 doc_id 或录音 task_id）。全局的文档 / 转写不删——别的项目可能还在用。
    频道里的期不在这里删（那是频道的一部分，删了下次同步又会回来）。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    with _chain_write_lock:
        reg = _reg(chain_id)
        n0 = len(reg['docs']) + len(reg['recordings']) + len(reg['channels'])
        reg['docs'] = [d for d in reg['docs'] if d.get('doc_id') != source_id]
        reg['recordings'] = [r for r in reg['recordings'] if r.get('task_id') != source_id]
        # 引用的博主：只是从这个项目拿掉，他自己的链条（别的项目可能在用）不动；字母作废不再发
        gone = [r for r in reg['channels'] if r.get('chain_id') == source_id]
        if gone:
            reg['retired_tags'] = sorted(set(reg.get('retired_tags') or []) | {r.get('tag') for r in gone if r.get('tag')})
        reg['channels'] = [r for r in reg['channels'] if r.get('chain_id') != source_id]
        removed = len(reg['docs']) + len(reg['recordings']) + len(reg['channels']) != n0
        if removed:
            _reg_save(chain_id, reg)
        state = _read_chain(chain_id)
        if not removed and not state.get('url'):
            vids = [v for v in state.get('videos') or [] if v.get('task_id') != source_id]
            removed = len(vids) != len(state.get('videos') or [])
            if removed:
                for i, v in enumerate(vids):
                    v['index'] = i
                state['videos'] = vids
                state['download_total'] = state['download_done'] = len(vids)
                _save_chain(state)
                # 这期的卡片文件也拿掉（项目自己的那份拷贝；别处的不动）
                import glob as _glob
                for f in _glob.glob(os.path.join(_chain_dir(chain_id), 'cards_*.json')):
                    try:
                        with open(f, 'r', encoding='utf-8') as fh:
                            if json.load(fh).get('task_id') == source_id:
                                os.remove(f)
                    except Exception:  # noqa: BLE001
                        pass
    if not removed:
        return jsonify({'error': 'Not in this project'}), 404
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>/sources/docs/<doc_id>/retry', methods=['POST'])
def api_project_retry_doc(chain_id, doc_id):
    import sources
    if not _chain_ok(chain_id) or not sources.valid_doc_id(doc_id) or not sources.doc_meta(doc_id):
        return jsonify({'error': 'Not found'}), 404
    sources.start_convert(doc_id)
    _project_refresh(chain_id)
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>/cards/build', methods=['POST'])
def api_project_build_cards(chain_id):
    """没有频道的项目：按需给录音抽证据卡（立场 / 预测 / 画像要用），并写一份综述。已经抽过的复用。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    with _chain_write_lock:
        state = _read_chain(chain_id)
        if state.get('url'):
            return jsonify({'error': 'Channel projects extract cards as part of the channel run'}), 400
        if not any(v.get('task_id') for v in state.get('videos') or []):
            return jsonify({'error': 'Add some recordings first'}), 400
        state['analyze'] = True
        _save_chain(state)
    _project_refresh(chain_id, force_build=True)
    return jsonify({'ok': True})


@app.route('/api/docs/<doc_id>')
def api_doc_get(doc_id):
    """阅读器用：元信息 + 整份 Markdown + 切好的段落（定位高亮）。"""
    import sources
    meta = sources.doc_meta(doc_id) if sources.valid_doc_id(doc_id) else None
    if not meta:
        return jsonify({'error': 'Not found'}), 404
    return Response(json.dumps({'meta': meta, 'markdown': sources.doc_markdown(doc_id),
                                'passages': sources.doc_passages(doc_id)}, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/docs/<doc_id>/original')
def api_doc_original(doc_id):
    import sources
    meta = sources.doc_meta(doc_id) if sources.valid_doc_id(doc_id) else None
    if not meta or not meta.get('filename'):
        return jsonify({'error': 'Not found'}), 404
    path = os.path.join(sources.doc_dir(doc_id), f"original.{meta['ext']}")
    if not os.path.isfile(path):
        return jsonify({'error': 'Not found'}), 404
    return send_file(path, download_name=meta['filename'], as_attachment=False)


def _project_refresh(cid, force_build=False):
    """来源变了：后台等文档转完 → （开了抽卡的、没频道的项目）给新录音抽卡、更新综述 → 补向量。
    同一个项目不并发跑；跑的时候又有新来源进来，就标个 dirty，跑完再来一轮。"""
    with _project_jobs_lock:
        if cid in _project_jobs:
            _project_jobs[cid] = 'dirty'
            return
        _project_jobs[cid] = 'running'

    def run():
        import ask
        import sources
        build = force_build            # 第二轮起不再强制（只给新来的录音抽卡）
        try:
            while True:
                t0 = time.time()
                while time.time() - t0 < 2400:             # 等这个项目里的文档都转完（OCR 慢的最多等 40 分钟）
                    if not any((sources.doc_meta(d) or {}).get('status') == 'converting' for d in _reg_doc_ids(cid)):
                        break
                    time.sleep(3)
                state = _read_chain(cid)
                if state.get('analyze') and not state.get('url'):
                    have = _cards_file_index(_chain_dir(cid))
                    if build or any(v.get('task_id') and not (v['task_id'] in have and _card_file_done(have[v['task_id']]))
                                    for v in state.get('videos') or []):
                        _build_collection(state, rewrite_overview=build)   # 已有的卡复用，只抽新的；综述每月最多自动重写一次
                with usage.scope(ref=f'index:{cid[:8]}', chain=cid):
                    ask.embed_chain(_chain_dir(cid))
                _voices_refresh(cid)                       # 录音变了：重新认说话人（没做过声纹的项目什么都不做）
                with _project_jobs_lock:
                    if _project_jobs.get(cid) == 'dirty':
                        _project_jobs[cid] = 'running'
                        build = False
                        continue
                    _project_jobs.pop(cid, None)
                    return
        except Exception as e:  # noqa: BLE001
            print(f'[project {cid[:8]}] refresh failed: {e}')
            with _project_jobs_lock:
                _project_jobs.pop(cid, None)

    threading.Thread(target=run, daemon=True).start()


def _analysed_chain_dirs(include_hidden=False):
    """有证据卡的博主 / 合集目录（按 URL 去重：同一个频道跑过几次只取卡最多的那条）。"""
    import glob as _glob
    hidden = set(_chain_prefs().get('hidden') or [])
    best = {}
    for d in _glob.glob(os.path.join(CHAINS_DIR, '*')):
        name = os.path.basename(d)
        if not _CHAIN_ID_RE.match(name) or (name in hidden and not include_hidden):
            continue
        n = len(_glob.glob(os.path.join(d, 'cards_*.json')))
        if not n:
            continue
        try:
            with open(os.path.join(d, 'chain.json'), 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if st.get('kind') in ('collection', 'project'):  # 合集 / 项目的卡是从博主那借来的，放进来会重复计数
            continue
        key = st.get('url') or name
        if key not in best or n > best[key][1]:
            best[key] = (d, n)
    return [v[0] for v in best.values()]


@app.route('/api/radar', methods=['POST'])
def api_radar():
    """话题雷达：一个话题，所有分析过的博主 / 合集谁谈得多、看好还是看空、前后变没变。"""
    q = str((request.get_json(silent=True) or {}).get('query') or '').strip()
    if not q:
        return jsonify({'error': 'Empty topic'}), 400
    import ask
    try:
        with usage.scope(ref='radar'):
            r = ask.radar(_analysed_chain_dirs(), q)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)[:300]}), 502
    return Response(json.dumps(r, ensure_ascii=False), mimetype='application/json')


@app.route('/api/leaderboard')
def api_leaderboard():
    import ask
    return Response(json.dumps(ask.leaderboard(_analysed_chain_dirs()), ensure_ascii=False),
                    mimetype='application/json')


# ===== 原话的音频片段：本地还留着音频就直接切；没有就只下载原视频的那一小段 =====
_CLIP_DIR = os.path.join(config.RESULTS_FOLDER, '_clips')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_clip_locks = {}


def _local_audio(task_id):
    import glob as _glob
    row = taskdb.get(task_id) or {}
    up = row.get('upload_path') or ''
    if up and os.path.isfile(up):
        return up
    hits = _glob.glob(os.path.join(config.UPLOAD_FOLDER, task_id + '.*'))
    return hits[0] if hits else ''


def _make_clip(task_id, start, dur):
    os.makedirs(_CLIP_DIR, exist_ok=True)
    out = os.path.join(_CLIP_DIR, f'{task_id}_{start}_{dur}.m4a')
    if os.path.isfile(out) and os.path.getsize(out) > 1000:
        return out
    src = _local_audio(task_id)
    tmpdir = None
    if not src:
        meta = {}
        try:
            with open(os.path.join(config.RESULTS_FOLDER, task_id, 'meta.json'), 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:  # noqa: BLE001
            pass
        url = meta.get('source_url')
        if not url:
            raise RuntimeError('No audio kept for this transcript and no source link to fetch it from')
        from downloader import download_one
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix='clip_', dir=_CLIP_DIR)
        got = download_one({'video_url': url, 'title': task_id, 'video_id': meta.get('video_id', '')},
                           tmpdir, section=f'*{start}-{start + dur}')
        if not got:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise RuntimeError('Could not download that part of the video')
        src, offset = got['path'], 0
    else:
        offset = start
    try:
        cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-ss', str(offset), '-i', src, '-t', str(dur),
               '-vn', '-ac', '1', '-c:a', 'aac', '-b:a', '96k', out]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError('ffmpeg failed: ' + (r.stderr or '')[-200:])
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return out


@app.route('/api/clip')
def api_clip():
    """一句原话的音频片段。?task_id&start=秒&dur=秒[&dl=1 下载]。结果缓存在 results/_clips/。"""
    tid = request.args.get('task_id') or ''
    if not _UUID_RE.match(tid):
        return jsonify({'error': 'Invalid task id'}), 400
    try:
        start = max(0, int(float(request.args.get('start') or 0)))
        dur = min(90, max(3, int(float(request.args.get('dur') or 20))))
    except ValueError:
        return jsonify({'error': 'Invalid time'}), 400
    key = (tid, start, dur)
    lock = _clip_locks.setdefault(key, threading.Lock())
    try:
        with lock:
            path = _make_clip(tid, start, dur)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)[:300]}), 502
    name = f"{_transcript_title(tid)[:40]}_{start // 60:02d}m{start % 60:02d}s.m4a"
    return send_file(path, mimetype='audio/mp4', as_attachment=request.args.get('dl') == '1',
                     download_name=re.sub(r'[\\/:*?"<>|]', '_', name), conditional=True)


@app.route('/api/export/docx', methods=['POST'])
def api_export_docx():
    """把带出处的 Markdown（回答 / 对比 / 立场 / 预测）转成 Word。"""
    body = request.get_json(silent=True) or {}
    md = str(body.get('markdown') or '')
    if not md.strip():
        return jsonify({'error': 'Nothing to export'}), 400
    from exporter import markdown_to_docx
    title = str(body.get('title') or 'Verbatim')[:80]
    data = markdown_to_docx(md, title)
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    headers={'Content-Disposition': "attachment; filename*=UTF-8''" +
                             __import__('urllib.parse').parse.quote(re.sub(r'[\\/:*?"<>|]', '_', title) + '.docx')})


@app.route('/api/export/pdf', methods=['POST'])
def api_export_pdf():
    """带出处的 Markdown → PDF：交给墨页（/api/md2pdf，无头 Chrome 打印，不进它的队列和资料库）。"""
    body = request.get_json(silent=True) or {}
    md = str(body.get('markdown') or '')
    if not md.strip():
        return jsonify({'error': 'Nothing to export'}), 400
    import requests
    import sources
    title = str(body.get('title') or 'Verbatim')[:80]
    try:
        r = requests.post(sources.MOYE_URL + '/api/md2pdf', json={'markdown': md, 'title': title},
                          timeout=180, proxies={'http': None, 'https': None})
    except requests.RequestException:
        return jsonify({'error': 'Moye is not running, so PDF export is unavailable. Word and Markdown still work.'}), 503
    if r.status_code != 200 or not r.content.startswith(b'%PDF'):
        try:
            err = (r.json() or {}).get('error')
        except ValueError:
            err = None
        return jsonify({'error': err or f'Moye could not make the PDF ({r.status_code})'}), 502
    return Response(r.content, mimetype='application/pdf',
                    headers={'Content-Disposition': "attachment; filename*=UTF-8''" +
                             __import__('urllib.parse').parse.quote(re.sub(r'[\\/:*?"<>|]', '_', title) + '.pdf')})


# ================= 项目工作台：复习工具（study.py）=================

@app.route('/api/chain/<chain_id>/studio')
def api_studio_list(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import study
    items = study.list_outputs(_chain_dir(chain_id))
    for o in items:              # 立场 / 预测要先打标签：那个人的标签还在打，这条就显示「在做」
        if o.get('kind') == 'view' and o.get('view') in ('topics', 'predictions') and \
                (_job_view(o.get('chain'), 'tag') or {}).get('status') == 'running':
            o['status'] = 'running'
    return Response(json.dumps({'items': items}, ensure_ascii=False), mimetype='application/json')


def _project_person_ids(chain_id):
    return {p['chain_id']: p for p in _project_people(chain_id)} or \
        {chain_id: _person_row(chain_id, _read_chain(chain_id), is_self=True)}


@app.route('/api/chain/<chain_id>/studio/view', methods=['POST'])
def api_studio_view(chain_id):
    """格子「立场 / 预测 / 原话」做一份：{view, chain（看谁）, generate（没打标签就顺手开始打）}。
    同一个人的同一样已经有了就返回那条。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    import study
    body = request.get_json(silent=True) or {}
    who = str(body.get('chain') or chain_id)
    people = _project_person_ids(chain_id)
    if who not in people:
        return jsonify({'error': 'Not in this project'}), 400
    p = people[who]
    try:
        item, new = study.add_view(_chain_dir(chain_id), body.get('view'), who,
                                   person=p['name'] if p.get('url') else '')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    started = False
    if body.get('generate') and item['view'] in ('topics', 'predictions') and not DEMO_MODE:
        cdir = _chain_dir(who)
        if not ask.topics(cdir).get('tagged'):
            started = _start_card_job(who, 'tag', lambda prog: ask.tag_chain(cdir, progress=prog))
    return jsonify({'ok': True, 'item': item, 'new': new, 'started': started})


@app.route('/api/chain/<chain_id>/studio/compare', methods=['POST'])
def api_studio_compare(chain_id):
    """格子「对比」做一份：{chains: [项目里的 2–4 个人], question}，后台生成，好了出现在列表里。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import study
    body = request.get_json(silent=True) or {}
    people = _project_person_ids(chain_id)
    ids = [c for c in dict.fromkeys(body.get('chains') or []) if c in people]
    try:
        o = study.start_compare(_chain_dir(chain_id), chain_id, [(_chain_dir(c), c) for c in ids],
                                body.get('question'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'item': study._summary(o)})


@app.route('/api/chain/<chain_id>/studio', methods=['POST'])
def api_studio_create(chain_id):
    """生成一份：body {kind: report|flashcards|quiz|coverage, format?（报告：briefing|guide|faq|timeline|custom）,
    prompt?（自定义报告的要求）, focus?, n?, scope?, outline?（对照检查：哪个来源是清单）}"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import study
    body = request.get_json(silent=True) or {}
    kind = body.get('kind')
    if kind not in study.KINDS:
        return jsonify({'error': 'Unknown tool'}), 400
    if kind == 'coverage' and not body.get('outline'):
        return jsonify({'error': 'Pick the source that is the list to check'}), 400
    if not config.gemini_key():
        return jsonify({'error': 'Add a Gemini API key in Settings first'}), 400
    try:
        o = study.start(_chain_dir(chain_id), chain_id, kind, focus=body.get('focus'), n=body.get('n'),
                        scope=body.get('scope') if isinstance(body.get('scope'), dict) else None,
                        outline=str(body.get('outline') or '') or None, lang=body.get('lang'),
                        fmt=body.get('format'), prompt=body.get('prompt'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'item': study._summary(o)})


@app.route('/api/chain/<chain_id>/studio/note', methods=['POST'])
def api_studio_note(chain_id):
    """把问答里的一条回答存进工作台。body {at, head}：那条回答的时间和开头几个字，
    用来在聊天记录里找到它——内容和出处都从记录里取，不信前端传的。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    import study
    cdir = _chain_dir(chain_id)
    msgs = ask.load_history(cdir)
    body = request.get_json(silent=True) or {}
    at, head = str(body.get('at') or ''), str(body.get('head') or '')[:80]
    i = next((k for k in range(len(msgs) - 1, -1, -1)
              if msgs[k].get('role') == 'assistant' and msgs[k].get('content') and at
              and msgs[k].get('at') == at and msgs[k]['content'].startswith(head)), None)
    if i is None:
        return jsonify({'error': 'Answer not found'}), 404
    m = msgs[i]
    q = next((x.get('content') for x in reversed(msgs[:i]) if x.get('role') == 'user'), '')
    o = study.save_note(cdir, chain_id, q, m['content'], m.get('citations'))
    return jsonify({'ok': True, 'item': study._summary(o)})


@app.route('/api/chain/<chain_id>/studio/<oid>')
def api_studio_get(chain_id, oid):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import study
    o = study.get_output(_chain_dir(chain_id), oid)
    if not o:
        return jsonify({'error': 'Not found'}), 404
    return Response(json.dumps(o, ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/studio/<oid>', methods=['DELETE'])
def api_studio_delete(chain_id, oid):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import study
    return jsonify({'ok': study.delete_output(_chain_dir(chain_id), oid)})


_CHAIN_PREFS = os.path.join(CHAINS_DIR, '_prefs.json')


def _chain_prefs():
    try:
        with open(_CHAIN_PREFS, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


@app.route('/api/chains/hidden', methods=['POST'])
def api_chains_hidden():
    """博主列表里「隐藏」一条（测试跑的、不想看见的）。只记名单，数据一个字不动，随时可恢复。"""
    body = request.get_json(silent=True) or {}
    cid = body.get('id') or ''
    if not _CHAIN_ID_RE.match(cid):
        return jsonify({'error': 'Invalid chain id'}), 400
    prefs = _chain_prefs()
    hidden = set(prefs.get('hidden') or [])
    (hidden.add if body.get('hidden', True) else hidden.discard)(cid)
    prefs['hidden'] = sorted(hidden)
    _save_chain_prefs(prefs)
    return jsonify({'ok': True})


_prefs_lock = threading.Lock()


def _save_chain_prefs(prefs):
    tmp = _CHAIN_PREFS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(prefs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _CHAIN_PREFS)


# ---- 项目首页的个人偏好（跟 Gemini Notebook 一样）：置顶、自选表情、分组。都只记在 _prefs.json，
#      不写 chain.json——频道项目的 chain.json 同步时会整份重写，写进去会丢 ----
_EMOJI_MAX = 16


@app.route('/api/chains/prefs', methods=['POST'])
def api_chains_prefs():
    """{id, pinned?: bool, emoji?: "📚" | ""（空 = 恢复默认）}"""
    body = request.get_json(silent=True) or {}
    cid = body.get('id') or ''
    if not _CHAIN_ID_RE.match(cid):
        return jsonify({'error': 'Invalid chain id'}), 400
    with _prefs_lock:
        prefs = _chain_prefs()
        if 'pinned' in body:
            pinned = [x for x in (prefs.get('pinned') or []) if x != cid]
            if body['pinned']:
                pinned.insert(0, cid)              # 最近置顶的排最前
            prefs['pinned'] = pinned
        if 'emoji' in body:
            em = str(body.get('emoji') or '').strip()
            emojis = prefs.get('emoji') or {}
            if em and len(em) <= _EMOJI_MAX and not re.search(r'[<>&"\'\s]', em):
                emojis[cid] = em
            else:
                emojis.pop(cid, None)
            prefs['emoji'] = emojis
        _save_chain_prefs(prefs)
    return jsonify({'ok': True})


def _collections(prefs=None):
    """项目分组（首页筛选用；跟 kind=collection 的「合集」项目是两回事，接口叫 /api/groups 以免撞名）。"""
    return (prefs or _chain_prefs()).get('collections') or []


@app.route('/api/groups')
def api_groups():
    return jsonify({'items': [{'id': c['id'], 'name': c['name'], 'count': len(c.get('items') or [])}
                              for c in _collections()]})


@app.route('/api/groups', methods=['POST'])
def api_group_create():
    """新建分组：{name, add?: 项目 id}"""
    body = request.get_json(silent=True) or {}
    name = str(body.get('name') or '').strip()[:60]
    if not name:
        return jsonify({'error': 'Name the collection'}), 400
    add = body.get('add') if _CHAIN_ID_RE.match(str(body.get('add') or '')) else None
    with _prefs_lock:
        prefs = _chain_prefs()
        cols = _collections(prefs)
        col = {'id': uuid.uuid4().hex[:10], 'name': name, 'items': [add] if add else []}
        cols.append(col)
        prefs['collections'] = cols
        _save_chain_prefs(prefs)
    return jsonify({'ok': True, 'id': col['id']})


@app.route('/api/groups/<col_id>', methods=['POST'])
def api_group_update(col_id):
    """{id, add: bool}：把项目放进 / 拿出分组；{name}：改名"""
    body = request.get_json(silent=True) or {}
    with _prefs_lock:
        prefs = _chain_prefs()
        col = next((c for c in _collections(prefs) if c['id'] == col_id), None)
        if not col:
            return jsonify({'error': 'Not found'}), 404
        if body.get('name'):
            col['name'] = str(body['name']).strip()[:60] or col['name']
        cid = str(body.get('id') or '')
        if _CHAIN_ID_RE.match(cid):
            items = [x for x in (col.get('items') or []) if x != cid]
            if body.get('add', True):
                items.append(cid)
            col['items'] = items
        _save_chain_prefs(prefs)
    return jsonify({'ok': True})


@app.route('/api/groups/<col_id>', methods=['DELETE'])
def api_group_delete(col_id):
    """删分组只删这个分组，里面的项目不动。"""
    with _prefs_lock:
        prefs = _chain_prefs()
        prefs['collections'] = [c for c in _collections(prefs) if c['id'] != col_id]
        _save_chain_prefs(prefs)
    return jsonify({'ok': True})


_has_cards_cache = {}


def _cards_file_has_cards(path):
    """这份卡片文件里真有卡吗（抽卡失败的期也会留一个空壳文件）。按修改时间缓存。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return False
    hit = _has_cards_cache.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, 'r', encoding='utf-8') as f:
            ok = bool((json.load(f) or {}).get('cards'))
    except Exception:  # noqa: BLE001
        ok = False
    _has_cards_cache[path] = (mt, ok)
    return ok


@app.route('/api/chains')
def api_chains():
    prefs = _chain_prefs()
    hidden = set(prefs.get('hidden') or [])
    pinned = {cid: i for i, cid in enumerate(prefs.get('pinned') or [])}
    emojis = prefs.get('emoji') or {}
    in_cols = {}
    for col in _collections(prefs):
        for cid in col.get('items') or []:
            in_cols.setdefault(cid, []).append(col['id'])
    entries = []
    if os.path.isdir(CHAINS_DIR):
        for name in os.listdir(CHAINS_DIR):
            cpath = os.path.join(CHAINS_DIR, name, 'chain.json')
            if _CHAIN_ID_RE.match(name) and os.path.isfile(cpath):
                try:
                    with open(cpath, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    if not DEMO_MODE:
                        _ensure_raw_doc(state)      # 老链条按需补『合并原文.md』
                    cdir = os.path.join(CHAINS_DIR, name)
                    # 有没有证据卡（跨博主对比只列有卡的）+ 订阅状态（卡片上的「有更新」小点）
                    names = os.listdir(cdir)
                    # 有卡 = 至少一份卡片文件里真有卡（抽卡失败的期也会留一个空文件）
                    state['has_cards'] = any(f.startswith('cards_') and _cards_file_has_cards(os.path.join(cdir, f))
                                             for f in names)
                    state['has_tags'] = 'tags.json' in names
                    state['hidden'] = name in hidden
                    state['pin'] = pinned.get(name)            # None = 没置顶；数字越小越靠前
                    state['emoji'] = emojis.get(name) or ''
                    state['collections'] = in_cols.get(name) or []
                    # 在项目里加的第二个、第三个博主：首页不单独列（在项目里看）；那个项目删了才露出来
                    own = state.get('owner_project')
                    state['ref_only'] = bool(own) and _CHAIN_ID_RE.match(own or '') is not None and \
                        os.path.isfile(os.path.join(CHAINS_DIR, own, 'chain.json'))
                    _attach_sources(state, name, names)
                    sub = _read_sub(name)
                    state['sub_on'] = bool(sub.get('on'))
                    state['sub_new'] = bool((sub.get('digest') or {}).get('new_videos')
                                            and not (sub.get('digest') or {}).get('seen'))
                    entries.append(state)
                except Exception:
                    pass
    entries.sort(key=lambda e: e.get('created_at', ''), reverse=True)
    return jsonify(entries)


_channel_backfilling = set()   # 正在补频道信息的链条，防重复重探


def _dl_is_real_avatar(url):
    """downloader.is_real_avatar 的懒加载包装（downloader 在本文件一律按需 import）。"""
    try:
        from downloader import is_real_avatar
        return is_real_avatar(url)
    except Exception:  # noqa: BLE001  取不到就当"不是真头像"，最多多探一次
        return False


def _backfill_channel(chain_id, url):
    """老链条重探一次频道元信息（名字/订阅数/头像），只取频道级、不列全部视频。"""
    try:
        from downloader import probe, channel_followers, is_real_avatar
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
                # 头像：空的要补，被当头像用的视频封面也要换掉（老数据里一大片）
                new_av = (channel or {}).get('avatar') or ''
                if new_av and (not st.get('avatar')
                               or (is_real_avatar(new_av) and not is_real_avatar(st['avatar']))):
                    st['avatar'] = new_av
                # 名字也补：旧链创建时没存频道名，卡片只能显示裸 URL
                if (channel or {}).get('name') and st.get('author') in (None, '', '该博主'):
                    st['author'] = channel['name']
                st['followers_checked'] = True   # 探过就记住，别每次轮询都重探
                st['author_checked'] = True      # 名字也探过（取不到就是取不到，别反复探）
                st['avatar_checked'] = True      # 头像同理：番剧等真没有的，别反复探
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
    data['emoji'] = (_chain_prefs().get('emoji') or {}).get(chain_id) or ''   # 只给前端看，不落盘
    # 打开详情时顺手校对：transcribing 的视频按 taskdb 真实状态回写并落盘
    # （任务级 recover 转完后，链条循环已死不会更新——靠这里自愈）
    healed = False
    for v in data.get('videos', []):
        if v.get('status') in ('transcribing', 'downloading') \
                and _sync_video_with_taskdb(v):
            healed = True
    # 只在链已终态时落盘：还在跑的链由 run_chain 用内存里的 state 写，这里无锁读-改-写
    # 会拿旧快照覆盖它刚写进去的字段。演示实例只读，不写
    if healed and not DEMO_MODE and data.get('stage') in ('done', 'failed', 'cancelled'):
        try:
            _save_chain({k: v for k, v in data.items() if k != 'emoji'})
        except Exception:  # noqa: BLE001
            pass

    # 老链条补频道信息（名字/订阅数/头像）：只对已终态的链、且没探过的重探一次
    # （*_checked 标记防每次轮询重复打网络；running 中的链不碰，交给 run_chain）
    needs_followers = not data.get('followers') and not data.get('followers_checked')
    needs_author = data.get('author') in (None, '', '该博主') \
        and not data.get('author_checked')
    # 头像：空的、或存的其实是视频封面（老数据默认行为），都值得重探一次
    needs_avatar = not data.get('avatar_checked') \
        and not _dl_is_real_avatar(data.get('avatar'))
    if (needs_followers or needs_author or needs_avatar) and not DEMO_MODE \
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
    # 标题只是视频号（BV1xxxx，老链条抓列表时拿不到标题）的，换成那期转写的 AI 标题
    for v in data.get('videos', []):
        if v.get('task_id') and v.get('status') == 'done' \
                and re.fullmatch(r'(BV[0-9A-Za-z]{10}|[A-Za-z0-9_-]{11})', v.get('title') or ''):
            t = _transcript_title(v['task_id'])
            if t and t != v['task_id']:
                v['title'] = t
    data['cost'] = usage.cost_for(chain=chain_id)
    _attach_sources(data, chain_id)
    return jsonify(data)


def _attach_sources(state, cid, names=None):
    """列表 / 详情里带上：文档、额外录音、来源总数（频道的每期 + 录音 + 文档）、最近动过的时间。"""
    reg = _reg(cid) if names is None or 'sources.json' in names else {'docs': [], 'recordings': []}
    state['docs'] = reg['docs']
    state['recordings'] = reg['recordings']
    n_vid = sum(1 for v in state.get('videos') or [] if v.get('task_id') and v.get('status') == 'done')
    state['n_sources'] = n_vid + len(reg['recordings']) + len(reg['docs'])
    try:
        mt = max(os.path.getmtime(os.path.join(_chain_dir(cid), f)) for f in ('chain.json', 'sources.json')
                 if os.path.isfile(os.path.join(_chain_dir(cid), f)))
        state['updated_at'] = datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        state['updated_at'] = state.get('finished_at') or state.get('created_at') or ''


@app.route('/api/chain/<chain_id>/reanalyze', methods=['POST'])
def api_chain_reanalyze(chain_id):
    """只重跑分析+合成（复用已有转写）。body: {verify: bool}。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    # 检查和占位在同一把锁里做：以前连点两下能起两个 _reanalyze_chain（还漏了 starting）
    with _chain_write_lock:
        with open(cpath, 'r', encoding='utf-8') as f:
            state = json.load(f)
        if state.get('stage') not in ('done', 'failed', 'cancelled'):
            return jsonify({'ok': False, 'error': 'This pipeline is still running — wait for it to finish before re-analyzing'}), 409
        state['stage'] = 'starting'
        _save_chain(state)

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


def _auto_tag(chain_dir):
    """分析跑完顺手给卡片打话题标签——只在便宜的时候：新抽的卡自带原始话题（只需映射），
    或者没标过的老卡不超过一千张（约 7 美分）。更大的老链由用户在博主页手动点。"""
    try:
        import ask
        raw = ask._raw_cards(chain_dir)
        tags = ask._read_json(os.path.join(chain_dir, 'tags.json'), {}) or {}
        done = tags.get('cards') or {}
        old_untagged = sum(1 for k, c in raw.items() if not c.get('topic') and k not in done)
        if old_untagged <= 1000:
            ask.tag_chain(chain_dir)
        ask.embed_chain(chain_dir)          # 新卡补向量（按意思检索用），每千张约 2 美分
        ask.build_speakers(chain_dir)       # 带说话人的转写：每张卡标上是谁说的（多人的期让便宜模型认角色）
    except Exception as e:  # noqa: BLE001  标签是锦上添花，失败不影响链条
        print(f'[chain] auto-tag skipped: {e}')


def _annotate_speakers(chain_dir, episodes):
    """写画像 / 综述之前先认说话人，并把「谁说的」挂到卡片上——不然画像会把连麦嘉宾的话算到博主头上。"""
    try:
        import ask
        ask.build_speakers(chain_dir)
        corpus = ask.load(chain_dir)
        who = {}
        for c in corpus['cards']:
            if c.get('speaker'):
                who[(corpus['episodes'][c['ep']]['task_id'], c['quote'])] = c['speaker']
        if not who:
            return
        for ep in episodes:
            for c in ep.get('cards') or []:
                sp = who.get((ep.get('task_id'), str(c.get('quote') or '').strip()))
                if sp:
                    c['speaker'] = sp
    except Exception as e:  # noqa: BLE001  认不出来就按没有说话人处理
        print(f'[speakers] {chain_dir}: {e}')


def _card_file_done(path):
    """这期的卡算不算抽完了：有卡，或者转写被判「不可用」（重抽也没用、白花钱）。
    抽卡时网络断了 / 模型报错留下的空文件（extract_failed、没有 unusable）不算——下次「继续」要重抽。
    博主那条路径一直是这么认的（只缓存有卡的）；合集 / 项目这条以前把空文件也当抽完了，断一次网就永远补不上。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            d = json.load(f) or {}
    except Exception:  # noqa: BLE001  坏文件当没抽
        return False
    return bool(d.get('cards')) or bool(d.get('unusable'))


def _cards_file_index(chain_dir):
    """task_id → 这期证据卡所在的文件。一轮分析开始时建一次，写新文件后调用方自己补进去。"""
    import glob as _glob
    out = {}
    for f in _glob.glob(os.path.join(chain_dir, 'cards_*.json')):
        try:
            with open(f, 'r', encoding='utf-8') as fh:
                tid = (json.load(fh) or {}).get('task_id')
        except Exception:  # noqa: BLE001
            continue
        if tid:
            out.setdefault(tid, f)
    return out


def _cards_path_for(chain_dir, index, task_id, file_index):
    """这一期证据卡该写到哪个文件。

    这期以前写过 → 还写回原文件（卡片 id = 文件编号-下标，聊天记录里的引用靠它）。
    没写过 → 默认 cards_<期号>.json；但博主发了新视频后频道列表整体后移，同一个期号
    可能已经是**另一期**的卡——直接覆盖就把那期的卡弄丢了、下次还得花钱重抽，
    而且挨个往后连锁覆盖。这时改用一个没占用的新编号。
    file_index：_cards_file_index() 的结果，本函数会把新分配的文件登记进去。
    """
    if task_id in file_index:
        return file_index[task_id]
    taken = set(file_index.values())
    want = os.path.join(chain_dir, f"cards_{index + 1:03d}.json")
    if want in taken:
        nums = [int(m.group(1)) for f in taken
                for m in [re.match(r'cards_(\d+)\.json$', os.path.basename(f))] if m]
        want = os.path.join(chain_dir, f"cards_{max(nums + [index + 1]) + 1:03d}.json")
    file_index[task_id] = want
    return want


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


@app.route('/api/chain/<chain_id>/cards')
def api_chain_cards(chain_id):
    """博主页的「证据卡」墙：把逐期抽过的卡片摊平成一个列表，每张带上出处
    （哪一期、task_id、时间戳），前端点卡片能跳回那期转写的那一秒。只读现成的
    cards_*.json，不调模型。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    if not os.path.isfile(os.path.join(_chain_dir(chain_id), 'chain.json')):
        return jsonify({'error': 'Not found'}), 404
    # 期信息单列一份，卡片只记期的下标：一条 160 期的链有上万张卡，每张都带一遍
    # 期名的话光重复标题就几 MB；中文也按 UTF-8 原样输出（jsonify 默认转 \uXXXX，体积×2）
    import ask
    corpus = ask.load(_chain_dir(chain_id))
    episodes = [{'title': e['title'], 'task_id': e['task_id'], 'date': e['date'],
                 'video_url': e['video_url']} for e in corpus['episodes']]
    cards = [{'id': c['id'], 'obs': c['obs'], 'quote': c['quote'], 'timestamp': c['ts'],
              'layer': c['layer'], 'ep': c['ep'], 'topic': c['topic'], 'stance': c['stance'],
              'pred': c['pred'], 'speaker': c.get('speaker', '')} for c in corpus['cards']]
    return Response(json.dumps({'episodes': episodes, 'cards': cards, 'author': corpus['author'],
                                'rhetoric': ask.rhetoric(corpus)}, ensure_ascii=False),
                    mimetype='application/json')


_lens_jobs = {}  # (chain_id, lens) -> 'running' | 'done' | 'error:...'


@app.route('/api/chain/<chain_id>/lens', methods=['POST'])
def api_chain_lens(chain_id):
    """换个角度看这个博主：拿现成证据卡跑一个镜头（roast/craft/fun/...）。
    后台生成 → 存 镜头_<lens>.md；前端轮询 GET 取。"""
    if not _CHAIN_ID_RE.match(chain_id or ''):
        return jsonify({'error': 'Invalid chain id'}), 400
    from analyze import LENSES
    body = request.get_json(silent=True) or {}
    lens = body.get('lens')
    force = bool(body.get('force'))          # 重新生成：旧的挪进 history/，再跑一遍
    if lens not in LENSES:
        return jsonify({'error': '未知镜头'}), 400
    if force and DEMO_MODE:
        return jsonify({'error': 'This is a read-only demo.'}), 403
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    if not os.path.isfile(cpath):
        return jsonify({'error': 'Not found'}), 404
    fpath = os.path.join(_chain_dir(chain_id), f'镜头_{lens}.md')
    key = (chain_id, lens)
    if _lens_jobs.get(key) == 'running':
        return jsonify({'ok': True, 'ready': False, 'status': 'running'})
    if os.path.isfile(fpath) and not force:   # 已生成过，直接给
        with open(fpath, 'r', encoding='utf-8') as f:
            return jsonify({'ok': True, 'ready': True, 'markdown': f.read()})
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    eps = _load_chain_cards(chain_id)
    if not eps:
        return jsonify({'error': 'No evidence cards yet — run analysis once first'}), 400

    def _run():
        try:
            from analyze import render_lens
            with usage.scope(ref=chain_id, chain=chain_id):
                md = render_lens(eps, lens, author=state.get('author', '该博主'),
                                 preset=state.get('analysis_preset'),
                                 lang=state.get('lang', 'auto'))
            if md and os.path.isfile(fpath):        # 重新生成：旧版本留底，不覆盖掉
                hdir = os.path.join(_chain_dir(chain_id), 'history')
                os.makedirs(hdir, exist_ok=True)
                shutil.copy2(fpath, os.path.join(hdir, f'镜头_{lens}_{datetime.now().strftime("%Y%m%d-%H%M%S")}.md'))
            if md:
                with open(fpath, 'w', encoding='utf-8') as fh:
                    fh.write(md)
                _lens_jobs[key] = 'done'
            else:
                _lens_jobs[key] = 'error:empty result'
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
    st = _lens_jobs.get((chain_id, lens), '')
    if st == 'running':                       # 重新生成时旧文件还在：别把旧的当成新结果
        return jsonify({'ready': False, 'status': 'running'})
    if os.path.isfile(fpath):
        with open(fpath, 'r', encoding='utf-8') as f:
            return jsonify({'ready': True, 'markdown': f.read()})
    if st.startswith('error:'):
        return jsonify({'ready': False, 'error': st[6:]})
    return jsonify({'ready': False, 'status': st or 'idle'})


# ===== 问证据卡 / 话题时间线 / 预测记账 / 跨博主对比 / 订阅（逻辑在 ask.py）=====
# 模型调用都记到这条链上（usage.scope chain=…），博主页的 Cost 会一起算。

_card_jobs = {}   # (chain_id, kind) -> {'status': running|done|error, 'done', 'total', 'error', 'result'}


def _chain_ok(chain_id):
    return bool(_CHAIN_ID_RE.match(chain_id or '')) and \
        os.path.isfile(os.path.join(_chain_dir(chain_id), 'chain.json'))


def _start_card_job(chain_id, kind, fn):
    """后台跑一个卡片相关的活（打标签 / 核对预测 / 补日期），同一条链同一种只跑一个。"""
    key = (chain_id, kind)
    if (_card_jobs.get(key) or {}).get('status') == 'running':
        return False
    job = {'status': 'running', 'done': 0, 'total': 0, 'error': '', 'started': time.time()}
    _card_jobs[key] = job

    def progress(done, total):
        job['done'], job['total'] = done, total

    def run():
        try:
            with usage.scope(ref=chain_id, chain=chain_id):
                job['result'] = fn(progress)
            job['status'] = 'done'
        except Exception as e:  # noqa: BLE001
            job['status'] = 'error'
            job['error'] = str(e)[:300]
    threading.Thread(target=run, daemon=True).start()
    return True


def _job_view(chain_id, kind):
    j = _card_jobs.get((chain_id, kind))
    if not j:
        return None
    return {k: j.get(k) for k in ('status', 'done', 'total', 'error', 'result')}


@app.route('/api/chain/<chain_id>/ask', methods=['GET'])
def api_chain_ask_history(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    return Response(json.dumps({'messages': ask.load_history(_chain_dir(chain_id))},
                               ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/ask', methods=['POST'])
def api_chain_ask(chain_id):
    """问一个问题：只凭证据卡回答、每句带出处。body: {question, mode: about|as, topic?}"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    body = request.get_json(silent=True) or {}
    q = str(body.get('question') or '').strip()
    if not q:
        return jsonify({'error': 'Empty question'}), 400
    if len(q) > 2000:
        return jsonify({'error': 'Question too long (max 2000 characters)'}), 400
    mode = body.get('mode') if body.get('mode') in ('about', 'as') else 'about'
    topic = str(body.get('topic') or '').strip() or None
    scope = body.get('scope') if isinstance(body.get('scope'), dict) else None
    cdir = _chain_dir(chain_id)
    history = [] if body.get('ephemeral') else ask.load_history(cdir)
    try:
        ref = f'ask:{uuid.uuid4().hex[:12]}'      # 单独一个 ref 才数得出这一问花了多少
        with usage.scope(ref=ref, chain=chain_id):
            r = ask.answer(cdir, q, mode=mode, history=history, topic=topic,
                           ui_lang=body.get('ui_lang'), scope=scope, persona=body.get('persona'))
        r['cost_usd'] = (usage.cost_for(ref=ref) or {}).get('cost_usd', 0)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)[:300]}), 502
    at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if body.get('ephemeral'):                  # MCP / 脚本来问：不写进网页上的聊天记录
        r['cost_usd'] = r.get('cost_usd')
        return Response(json.dumps({'ok': True, 'message': {'role': 'assistant', 'content': r['answer'],
                                    'citations': r['citations'], 'coverage': r['coverage'],
                                    'cost_usd': r['cost_usd']}}, ensure_ascii=False),
                        mimetype='application/json')
    user_msg = {'role': 'user', 'content': q, 'mode': mode, 'topic': topic, 'at': at}
    bot_msg = {'role': 'assistant', 'content': r['answer'], 'mode': mode, 'topic': topic,
               'citations': r['citations'], 'coverage': r['coverage'],
               'dropped_citations': r['dropped_citations'], 'dropped_ids': r.get('dropped_ids'),
               'cost_usd': r['cost_usd'], 'at': at}
    ask.append_history(cdir, user_msg, bot_msg)
    return Response(json.dumps({'ok': True, 'message': bot_msg}, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/chain/<chain_id>/ask/stream', methods=['POST'])
def api_chain_ask_stream(chain_id):
    """流式提问：一行一个 JSON 事件（NDJSON）。stage(search/write/rewrite) → delta… → done。
    浏览器中途断开（点了停止）：已写出的半截照样校验出处、标「已停止」存进聊天记录。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    body = request.get_json(silent=True) or {}
    q = str(body.get('question') or '').strip()
    if not q:
        return jsonify({'error': 'Empty question'}), 400
    if len(q) > 2000:
        return jsonify({'error': 'Question too long (max 2000 characters)'}), 400
    mode = body.get('mode') if body.get('mode') in ('about', 'as') else 'about'
    topic = str(body.get('topic') or '').strip() or None
    scope = body.get('scope') if isinstance(body.get('scope'), dict) else None
    ui_lang = body.get('ui_lang')
    cdir = _chain_dir(chain_id)
    history = ask.load_history(cdir)
    at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    user_msg = {'role': 'user', 'content': q, 'mode': mode, 'topic': topic, 'at': at}

    def line(obj):
        return json.dumps(obj, ensure_ascii=False) + '\n'

    # 项目里有好几个博主时「用他的口吻回答」要说清楚是谁（只在模拟时有用）
    persona = str(body.get('persona') or '') if mode == 'as' else ''

    def bot_msg(r, ref, **extra):
        return {'role': 'assistant', 'content': r['answer'], 'mode': mode, 'topic': topic,
                **({'persona': r.get('persona')} if r.get('persona') else {}),
                'citations': r['citations'], 'coverage': r['coverage'],
                'dropped_citations': r['dropped_citations'], 'dropped_ids': r.get('dropped_ids'),
                'cost_usd': (usage.cost_for(ref=ref) or {}).get('cost_usd', 0), 'at': at, **extra}

    def gen():
        ref = f'ask:{uuid.uuid4().hex[:12]}'      # 单独一个 ref 才数得出这一问花了多少
        ctx, parts, finished = None, [], False
        with usage.scope(ref=ref, chain=chain_id):
            stream = ask.answer_stream(cdir, q, mode=mode, history=history, topic=topic, ui_lang=ui_lang,
                                       scope=scope, persona=persona)
            try:
                for ev in stream:
                    if ev['type'] == 'ctx':
                        ctx = ev['ctx']
                        continue
                    if ev['type'] == 'delta':
                        parts.append(ev['text'])
                    if ev['type'] == 'result':
                        r = {k: v for k, v in ev.items() if k != 'type'}
                        msg = bot_msg(r, ref)
                        ask.append_history(cdir, user_msg, msg)
                        finished = True
                        yield line({'type': 'done', 'message': msg})
                        continue
                    yield line(ev)
            except GeneratorExit:
                # 浏览器断开了：先关掉模型那头（它会按已收到的字记一笔账），再存半截回答
                stream.close()
                if not finished:
                    try:
                        if ctx is not None and parts:
                            msg = bot_msg(ask.finish_partial(''.join(parts), ctx), ref, stopped=True)
                        else:                  # 还在挑卡就停了：留一条空的，记录仍是一问一答成对
                            msg = {'role': 'assistant', 'content': '', 'mode': mode, 'topic': topic,
                                   'citations': {}, 'stopped': True, 'at': at,
                                   'cost_usd': (usage.cost_for(ref=ref) or {}).get('cost_usd', 0)}
                        ask.append_history(cdir, user_msg, msg)
                    except Exception:  # noqa: BLE001
                        pass
                raise
            except Exception as e:  # noqa: BLE001
                yield line({'type': 'error', 'error': str(e)[:300]})

    return Response(gen(), mimetype='application/x-ndjson',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/chain/<chain_id>/ask', methods=['DELETE'])
def api_chain_ask_clear(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    ask.clear_history(_chain_dir(chain_id))
    return jsonify({'ok': True})


@app.route('/api/chain/<chain_id>/ask/starters')
def api_chain_ask_starters(chain_id):
    """开场问题（画像里出，中英一次生成并缓存）。演示实例只读缓存，不现生成。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    lang = 'zh' if request.args.get('lang') == 'zh' else 'en'
    cdir = _chain_dir(chain_id)
    if DEMO_MODE:
        cache = ask._read_json(os.path.join(cdir, 'chat_starters.json'), {}) or {}
        return jsonify({'starters': cache.get(lang) or []})
    with usage.scope(ref=chain_id, chain=chain_id):
        return jsonify({'starters': ask.starters(cdir, lang)})


@app.route('/api/chain/<chain_id>/topics')
def api_chain_topics(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    cdir = _chain_dir(chain_id)
    out = ask.topics(cdir, topic=request.args.get('topic'))
    out['job'] = _job_view(chain_id, 'tag')
    out['dates_job'] = _job_view(chain_id, 'dates')
    if not out['tagged']:
        out['status'] = ask.tag_status(cdir)
    return Response(json.dumps(out, ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/tag', methods=['POST'])
def api_chain_tag(chain_id):
    """给卡片补话题/立场/预测（后台，每千张卡约 7 美分）。body: {rebuild: bool}"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    rebuild = bool((request.get_json(silent=True) or {}).get('rebuild'))
    cdir = _chain_dir(chain_id)
    started = _start_card_job(chain_id, 'tag',
                              lambda p: ask.tag_chain(cdir, progress=p, rebuild=rebuild))
    return jsonify({'ok': True, 'started': started})


_bili_api_lock = threading.Lock()


def _backfill_dates(chain_id, progress):
    """老链条补上架日期。B 站走投稿列表接口一次拿全；YouTube 逐个问 yt-dlp（不下载），每个之间停 1.5 秒。"""
    from downloader import fetch_upload_date
    cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
    with open(cpath, 'r', encoding='utf-8') as f:
        state = json.load(f)
    todo = [v for v in state.get('videos') or []
            if v.get('task_id') and v.get('video_url') and not v.get('upload_date')]
    got = {}
    # B站 UP 主空间：签名投稿列表接口一页 30 条、自带发布时间，几页就拿全——
    # 比逐个视频问 yt-dlp（几百次请求，容易触发 412 被封 IP）温和得多
    from downloader import _bili_mid, bili_space_dates
    mid = _bili_mid(state.get('url') or '', None) if 'bilibili.com' in (state.get('url') or '') else ''
    if mid and todo:
        progress(0, len(todo))
        # 同一时间只让一条链打 B 站接口：几条一起打会被风控，直接回一张 HTML 拦截页
        with _bili_api_lock:
            by_id = bili_space_dates(mid)
        for v in todo:
            if by_id.get(v.get('video_id')):
                got[v['video_url']] = by_id[v['video_id']]
        todo = [v for v in todo if v['video_url'] not in got]
        if len(todo) > 20:          # 接口拿不到的（被删/隐藏的视频）只补少量，别逐个敲 B 站
            todo = []

    def flush():
        # 重读再写：跑的这几分钟里链条可能被别的操作改过
        with open(cpath, 'r', encoding='utf-8') as f:
            cur = json.load(f)
        if cur.get('stage') in ('downloading', 'transcribing', 'analyzing', 'synthesizing'):
            raise RuntimeError('Pipeline started running — try again after it finishes')
        for v in cur.get('videos') or []:
            if v.get('video_url') in got and not v.get('upload_date'):
                v['upload_date'] = got[v['video_url']]
        _save_chain(cur)

    fails = 0
    for i, v in enumerate(todo):
        d = fetch_upload_date(v['video_url'])
        if d:
            got[v['video_url']] = d
            fails = 0
        else:
            fails += 1
            if fails >= 5:              # 连着 5 个都拿不到：多半被风控了，停手
                break
        progress(i + 1, len(todo))
        if (i + 1) % 20 == 0 and got:   # 边补边存：中途停了也不白跑
            flush()
        time.sleep(1.5)
    if got:
        flush()
    return {'filled': len(got), 'asked': len(todo)}


@app.route('/api/chain/<chain_id>/dates', methods=['POST'])
def api_chain_dates(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    with open(os.path.join(_chain_dir(chain_id), 'chain.json'), 'r', encoding='utf-8') as f:
        if json.load(f).get('stage') in ('downloading', 'transcribing', 'analyzing', 'synthesizing'):
            return jsonify({'error': 'This pipeline is still running'}), 409
    started = _start_card_job(chain_id, 'dates', lambda p: _backfill_dates(chain_id, p))
    return jsonify({'ok': True, 'started': started})


@app.route('/api/chain/<chain_id>/predictions')
def api_chain_predictions(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    out = ask.predictions(_chain_dir(chain_id))
    out['job'] = _job_view(chain_id, 'predict')
    return Response(json.dumps(out, ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/predictions/check', methods=['POST'])
def api_chain_predictions_check(chain_id):
    """联网核对他的预测（Google 搜索 grounding）。默认只核没核过的和上次还没到期的。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    body = request.get_json(silent=True) or {}
    ids = body.get('ids') if isinstance(body.get('ids'), list) else None
    recheck = bool(body.get('recheck'))
    cdir = _chain_dir(chain_id)
    started = _start_card_job(chain_id, 'predict', lambda p: ask.check_predictions(
        cdir, ids=set(map(str, ids)) if ids else None, recheck=recheck, progress=p))
    return jsonify({'ok': True, 'started': started})


@app.route('/api/chain/<chain_id>/beliefs')
def api_chain_beliefs(chain_id):
    """核心信念：同一个主张在 ≥3 期里反复出现（每条带全部原话出处）。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    out = ask.beliefs(_chain_dir(chain_id))
    out['job'] = _job_view(chain_id, 'beliefs')
    return Response(json.dumps(out, ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/beliefs/find', methods=['POST'])
def api_chain_beliefs_find(chain_id):
    """后台找核心信念（flash-lite 按话题分批，代码复核跨期数）。没打话题标签的先补标。"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    import ask
    cdir = _chain_dir(chain_id)
    started = _start_card_job(chain_id, 'beliefs', lambda p: ask.find_beliefs(cdir, progress=p))
    return jsonify({'ok': True, 'started': started})


@app.route('/api/compare', methods=['POST'])
def api_compare():
    """跨博主：同一个问题，2~4 个博主的立场并排放，各自带原话。body: {chains: [id], question}"""
    body = request.get_json(silent=True) or {}
    ids = [c for c in (body.get('chains') or []) if isinstance(c, str)]
    ids = list(dict.fromkeys(ids))
    q = str(body.get('question') or '').strip()
    if not q:
        return jsonify({'error': 'Empty question'}), 400
    if not 2 <= len(ids) <= 4 or not all(_chain_ok(c) for c in ids):
        return jsonify({'error': 'Pick 2–4 creators'}), 400
    import ask
    try:
        with usage.scope(ref='compare'):
            r = ask.compare([_chain_dir(c) for c in ids], q)
    except Exception as e:  # noqa: BLE001
        return jsonify({'error': str(e)[:300]}), 502
    for cr, cid in zip(r['creators'], ids):
        cr['chain_id'] = cid
    return Response(json.dumps(r, ensure_ascii=False), mimetype='application/json')


# ---- 订阅：定时「Continue」，有新视频就增量抽卡，出一份「这次他说了什么新东西」----
# 设置单独放 subscription.json：链条线程手里拿着整份 state，跑完会把 chain.json 整个写回，
# 设置要是写在 chain.json 里，会被正在跑的链条悄悄冲掉。
SUB_INTERVAL_H = float(os.environ.get('SUBSCRIPTION_INTERVAL_HOURS') or 168)   # 默认每周
SUB_INTERVALS = (24, 168, 336, 720)          # 每天 / 每周 / 每两周 / 每月
SYNC_PROBE_N = int(os.environ.get('SYNC_PROBE_N') or 30)   # 同步时只看频道最新这么多期
_sub_runs = set()           # 由订阅触发的这一轮：没新视频就不重做画像（省下合成的钱）


def _sub_path(chain_id):
    return os.path.join(_chain_dir(chain_id), 'subscription.json')


def _read_sub(chain_id):
    try:
        with open(_sub_path(chain_id), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _write_sub(chain_id, sub):
    tmp = _sub_path(chain_id) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(sub, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _sub_path(chain_id))


@app.route('/api/chain/<chain_id>/subscription', methods=['GET'])
def api_chain_sub_get(chain_id):
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    sub = _read_sub(chain_id)
    sub['interval_h'] = int(sub.get('interval_h') or SUB_INTERVAL_H)
    sub['running'] = chain_id in _sub_runs
    if sub.get('on'):
        nxt = float(sub.get('last_run') or 0) + sub['interval_h'] * 3600
        sub['next_run_at'] = datetime.fromtimestamp(max(nxt, time.time())).strftime('%Y-%m-%d %H:%M')
    return Response(json.dumps(sub, ensure_ascii=False), mimetype='application/json')


@app.route('/api/chain/<chain_id>/subscription', methods=['POST'])
def api_chain_sub_set(chain_id):
    """body: {on?: bool, keywords?: [str], seen?: true, run_now?: true}"""
    if not _chain_ok(chain_id):
        return jsonify({'error': 'Not found'}), 404
    body = request.get_json(silent=True) or {}
    sub = _read_sub(chain_id)
    if 'on' in body:
        sub['on'] = bool(body['on'])
        sub.setdefault('since', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    if body.get('interval_h') is not None:
        try:
            iv = int(body['interval_h'])
        except (TypeError, ValueError):
            iv = 0
        if iv in SUB_INTERVALS:
            sub['interval_h'] = iv
    if isinstance(body.get('keywords'), list):
        sub['keywords'] = [str(k).strip()[:40] for k in body['keywords'] if str(k).strip()][:20]
    if body.get('seen') and sub.get('digest'):
        sub['digest']['seen'] = True
    _write_sub(chain_id, sub)
    if body.get('run_now'):
        if chain_id in _sub_runs:
            return jsonify({'ok': True, 'started': False})
        threading.Thread(target=_subscription_run, args=(chain_id,), daemon=True).start()
        return jsonify({'ok': True, 'started': True})
    return jsonify({'ok': True})


DIGEST_PROMPT = """The creator "{author}" published {n} new video(s) since the last check. Below are the evidence cards from them.
Write a short update for a subscriber, in {lang}: what's new in what they said — main points first, anything that looks like a new position or a prediction, and anything touching the subscriber's keywords ({keywords}; say plainly if none came up).
After every sentence, cite the supporting card ids exactly like [#3-12] — one id per bracket, several as [#3-12][#3-13]. Use only these cards. Report claims as their claims. 3–6 bullet points, no heading.

Episodes:
{episodes}

Cards:
{cards}"""


def _subscription_run(chain_id):
    """订阅的一轮：Continue（增量）→ 新视频的卡 → 一份更新摘要。"""
    import ask
    if chain_id in _sub_runs:
        return
    _sub_runs.add(chain_id)
    sub = _read_sub(chain_id)
    try:
        cpath = os.path.join(_chain_dir(chain_id), 'chain.json')
        with open(cpath, 'r', encoding='utf-8') as f:
            state = json.load(f)
        if state.get('stage') in ('downloading', 'transcribing', 'analyzing', 'synthesizing') \
                or state.get('kind') in ('collection', 'project'):
            return
        before = {v.get('task_id') for v in state.get('videos') or [] if v.get('task_id')}
        _cancel_chains.discard(chain_id)
        run_chain(state)            # 同步跑完：重新探测 → 只下/转/抽新的
        with open(cpath, 'r', encoding='utf-8') as f:
            state = json.load(f)
        new_tids = [v['task_id'] for v in state.get('videos') or []
                    if v.get('task_id') and v['task_id'] not in before and v.get('status') == 'done']
        sub = _read_sub(chain_id)
        sub['last_run'] = time.time()
        sub['last_run_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sub['last_error'] = state.get('error') if state.get('stage') == 'failed' else ''
        sub['last_new'] = len(new_tids)
        if new_tids:
            cdir = _chain_dir(chain_id)
            sub['digest'] = _subscription_digest(chain_id, new_tids, sub.get('keywords') or [])
            if os.path.isfile(os.path.join(cdir, 'predictions.json')):
                try:
                    corpus = ask.load(cdir)
                    ids = {c['id'] for c in corpus['cards']
                           if c.get('pred') and corpus['episodes'][c['ep']]['task_id'] in set(new_tids)}
                    if ids:
                        with usage.scope(ref=chain_id, chain=chain_id):
                            ask.check_predictions(cdir, ids=ids)
                        sub['digest']['predictions_checked'] = len(ids)
                except Exception as e:  # noqa: BLE001
                    print(f'[subscription] predictions: {e}')
        _write_sub(chain_id, sub)
    except Exception as e:  # noqa: BLE001
        sub = _read_sub(chain_id)
        sub['last_run'] = time.time()
        sub['last_error'] = str(e)[:300]
        _write_sub(chain_id, sub)
    finally:
        _sub_runs.discard(chain_id)


def _subscription_digest(chain_id, new_tids, keywords):
    import ask
    cdir = _chain_dir(chain_id)
    corpus = ask.load(cdir)
    eps = {i for i, e in enumerate(corpus['episodes']) if e['task_id'] in set(new_tids)}
    cards = [c for c in corpus['cards'] if c['ep'] in eps]
    kw = [k.lower() for k in keywords]
    hits = [c['id'] for c in cards
            if kw and any(k in (c['quote'] + ' ' + c['obs']).lower() for k in kw)]
    out = {'at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'seen': False,
           'episodes': [{'title': corpus['episodes'][i]['title'],
                         'task_id': corpus['episodes'][i]['task_id'],
                         'date': corpus['episodes'][i]['date']} for i in sorted(eps)],
           'new_videos': len(new_tids), 'cards': len(cards), 'keyword_hits': hits[:50],
           'markdown': '', 'citations': {}}
    if not cards:
        return out
    lang = 'Chinese' if ask._content_lang(corpus) == 'Chinese' else 'English'
    try:
        with usage.scope(ref=chain_id, chain=chain_id):
            raw = ask._llm(DIGEST_PROMPT.format(
                author=corpus['author'] or 'this creator', n=len(eps), lang=lang,
                keywords=', '.join(keywords) or 'none',
                episodes=ask._episode_lines(corpus, eps),
                cards='\n'.join(ask._card_line(c, corpus) for c in cards[:400])), purpose='digest')
        import citations
        cit = citations.Citations().add(corpus, cards)
        out['markdown'] = cit.clean(raw)
        out['citations'] = cit.used
    except Exception as e:  # noqa: BLE001
        out['error'] = str(e)[:200]
    return out


def _subscription_loop():
    """每 15 分钟看一眼：到点的订阅挨个跑（一次一条，别一口气同时探测一堆频道）。"""
    time.sleep(120)
    while True:
        try:
            for d in sorted(os.listdir(CHAINS_DIR)):
                if not _CHAIN_ID_RE.match(d):
                    continue
                sub = _read_sub(d)
                if not sub.get('on'):
                    continue
                if time.time() - float(sub.get('last_run') or 0) < float(sub.get('interval_h') or SUB_INTERVAL_H) * 3600:
                    continue
                _subscription_run(d)
        except Exception as e:  # noqa: BLE001
            print(f'[subscription] {e}')
        time.sleep(900)


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
    # 模式默认沿用这条链原来的（只转写就还是只转写，别替用户决定花分析的钱）；
    # 只有前端明确传了 analyze 才改——「接着上次那条跑」时会把表单里的当前模式传过来。
    if 'analyze' in body:
        state['analyze'] = bool(body['analyze'])
    if 'require_speakers' in body:
        state['require_speakers'] = bool(body['require_speakers'])
    try:                                      # 想多拿几期：接着跑时可以把上限调大
        more = int(body.get('max_videos') or 0)
    except (TypeError, ValueError):
        more = 0
    if more > 0:
        state['max_videos'] = min(max(more, state.get('max_videos') or 0), _MAX_CHAIN_VIDEOS)
    # 新表单里对一个分析过的博主又挑了几期：并进原来的列表（原来的一期不少），只处理新挑的
    picked = body.get('targets')
    if isinstance(picked, list) and picked and state.get('kind') != 'collection':
        have = {v.get('video_url') for v in state.get('videos') or []}
        base = state.get('fixed_targets') or [
            {k: v.get(k) for k in ('video_url', 'title', 'video_id', 'thumbnail', 'view_count', 'upload_date', 'duration')}
            for v in state.get('videos') or [] if v.get('video_url')]
        add = [{k: t.get(k) for k in ('video_url', 'title', 'video_id', 'thumbnail', 'view_count', 'upload_date', 'duration')}
               for t in picked if isinstance(t, dict) and str(t.get('video_url') or '').startswith('http')
               and t.get('video_url') not in have]
        state['fixed_targets'] = (add + base)[:_MAX_CHAIN_VIDEOS]
        state['max_videos'] = len(state['fixed_targets'])
    for k in ('require_speakers', 'auto_predict'):
        if k in body:
            state[k] = bool(body[k])
    _cancel_chains.discard(chain_id)          # 清掉可能残留的取消标记
    if state.get('kind') == 'project':        # 项目没有链接：重新建索引（topic 模板顺带给新录音抽卡）
        _project_refresh(chain_id)
        return jsonify({'ok': True})
    if state.get('kind') == 'collection':     # 合集没有链接可探测：直接重建（已有的卡复用）
        threading.Thread(target=_build_collection, args=(state,), daemon=True).start()
        return jsonify({'ok': True})
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


def _chain_summarize(chain_id):
    """这条链有没有勾「每期摘要」（老链没这个字段 = 没勾）。"""
    try:
        with open(os.path.join(_chain_dir(chain_id), 'chain.json'), 'r', encoding='utf-8') as f:
            return bool(json.load(f).get('summarize', False))
    except Exception:  # noqa: BLE001
        return False


def _retranscribe_video(chain_id, index, target, engine):
    """只对一个视频：重下音频 → 用指定引擎转写 → 回写这张卡的状态。后台线程跑。"""
    from downloader import download_one
    # 每个视频单独一个下载目录：finally 里会整个删掉，共用目录的话同时重转几期
    # 会把别人下到一半的文件删了
    dl_dir = os.path.join(_chain_dir(chain_id), 'downloads', f'retranscribe-{index}')
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
        _manual_tasks.add(task_id)      # 链可能已停止（cancelled）：用户手点的这一期照样转
        fallback = False
        try:
            with open(os.path.join(_chain_dir(chain_id), 'chain.json'), 'r', encoding='utf-8') as f:
                fallback = bool(json.load(f).get('fallback_whisper'))
        except Exception:  # noqa: BLE001
            pass
        submit_transcription(engine, run_transcription, task_id, upload_path, engine,
                        display_name, q, None,
                        fallback_whisper=fallback,
                        summarize=_chain_summarize(chain_id),
                        extra_meta={'source_url': target.get('video_url'),
                                    'video_id': item.get('video_id'),
                                    'creator': item.get('uploader'),
                                    'chain_id': chain_id})
        _update_video(chain_id, index, {
            'task_id': task_id, 'title': item['title'],
            'video_id': item['video_id'], 'status': 'transcribing',
            'source': f'retranscribe:{engine}', 'error': None,
        })

        # 轮询到落定，回写状态。链被删了 / 任务行没了就不等了；排队再久也有个头（2 天）
        deadline = time.time() + 2 * 86400
        while time.time() < deadline:
            time.sleep(5)
            if not os.path.isdir(_chain_dir(chain_id)):
                return
            row = taskdb.get(task_id)
            if not row:
                _update_video(chain_id, index, {'status': 'failed'})
                return
            if row['status'] in ('done', 'failed'):
                fields = {'status': row['status']}
                if row['status'] == 'done':
                    fields['engine_used'] = _engine_used(task_id)
                _update_video(chain_id, index, fields)
                return
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
    engine = body.get('engine') or 'gemini35'
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
    # 正在跑的先停：链条线程看 _cancel_chains 收手；已排队的转写看到目录没了
    # 也会自己放弃（_chain_stopped），不会删了链还接着转写计费
    _cancel_chains.add(chain_id)
    try:
        doc_ids = _reg_doc_ids(chain_id)
    except Exception:  # noqa: BLE001
        doc_ids = []
    # 只删链条目录（分析产物）；转写结果仍留在历史里
    shutil.rmtree(cdir, ignore_errors=True)
    _drop_orphan_docs(doc_ids)
    return jsonify({'ok': True})


def _drop_orphan_docs(doc_ids):
    """项目删了：它的文档如果别的项目都没在用，一起删掉（文档不像转写那样在资料库里单独可见，留着就是垃圾）。"""
    import glob as _glob
    import sources
    doc_ids = [d for d in doc_ids if sources.valid_doc_id(d)]
    if not doc_ids:
        return
    used = set()
    for rp in _glob.glob(os.path.join(CHAINS_DIR, '*', 'sources.json')):
        try:
            with open(rp, 'r', encoding='utf-8') as f:
                used |= {d.get('doc_id') for d in (json.load(f).get('docs') or [])}
        except Exception:  # noqa: BLE001
            used |= set(doc_ids)          # 读不了某个项目：保守起见一份都不删
    for d in doc_ids:
        if d not in used:
            shutil.rmtree(sources.doc_dir(d), ignore_errors=True)


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
    if request.args.get('detail'):           # 工作台列表要显示生成时间；默认格式（纯文件名）MCP 在用，不动
        return jsonify([{'name': f, 'mtime': datetime.fromtimestamp(os.path.getmtime(os.path.join(cdir, f)))
                         .strftime('%Y-%m-%d %H:%M')} for f in files])
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
                              os.path.join(results_dir, '_tagmap.json'),
                              offline=DEMO_MODE)     # 演示实例只用缓存，不调模型
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
    # 演示实例只读不花钱：GET 也不许触发模型（只挡了非 GET 的守卫管不到这里）
    return jsonify(reflect.build(config.RESULTS_FOLDER, rng, lang, refresh and not DEMO_MODE,
                                 generate=not DEMO_MODE))


# ========== Settings API ==========

def _mask_key(env_name):
    """返回 (是否已设置, 末4位提示)，绝不回传完整 key。"""
    v = (os.environ.get(env_name) or '').strip()
    if not v:
        return False, ''
    if DEMO_MODE:
        return True, '••••'      # 演示实例对外展示：连末 4 位也不露
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


@app.route('/api/costs')
def api_costs():
    """Settings → Costs：模型调用费用汇总（本月 / 全部，按服务商 / 用途 / 模型）。"""
    out = usage.summary()
    if DEMO_MODE:
        out.pop('prices_path', None)    # 本机路径，演示实例不外露
    return jsonify(out)


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
    up_bytes, up_count = 0, 0
    for path in _stale_uploads():
        try:
            up_bytes += os.path.getsize(path)
        except OSError:
            continue
        up_count += 1
    return jsonify({'audio_bytes': total, 'audio_count': count,
                    'compressed_count': compressed,
                    'upload_bytes': up_bytes, 'upload_count': up_count,
                    'keep_audio': _keep_audio()})


def _stale_uploads():
    """uploads/ 里可以删的文件：所属任务已完成、或任务库里根本没这条（孤儿）。
    排队中 / 运行中 / 失败的任务不动——失败的留着给 Continue 直接重转。"""
    up = config.UPLOAD_FOLDER
    if not os.path.isdir(up):
        return
    for name in os.listdir(up):
        path = os.path.join(up, name)
        if not (os.path.isfile(path) or os.path.islink(path)):
            continue
        try:
            if time.time() - os.lstat(path).st_mtime < _ORPHAN_MIN_AGE_S:
                continue     # 可能正在上传 / 移入，taskdb 行还没建
        except OSError:
            continue
        tid = name.split('.')[0].replace('_audio', '')
        row = taskdb.get(tid) if _is_valid_task_id(tid) else None
        if row and row.get('status') in ('pending', 'running', 'failed'):
            continue
        yield path


_purge_state = {'running': False, 'done': 0, 'total': 0, 'freed': 0, 'errors': 0}
_purge_lock = threading.Lock()


def _run_purge_audio():
    """删掉所有已转写任务的音频副本 + 残留上传。转写稿、摘要、元数据一个字节不动。"""
    audio = [p for p, _ in _audio_files()]
    stale = list(_stale_uploads())
    with _purge_lock:
        _purge_state.update(running=True, done=0, total=len(audio) + len(stale),
                            freed=0, errors=0)
    emptied = set()      # 真删掉了音频的任务目录
    for path in audio + stale:
        try:
            size = os.path.getsize(path) if not os.path.islink(path) else 0
            os.remove(path)          # 软链只删链接本身，原文件不受影响
            with _purge_lock:
                _purge_state['freed'] += size
            if path in audio:
                emptied.add(os.path.dirname(path))
        except OSError:
            with _purge_lock:
                _purge_state['errors'] += 1
        with _purge_lock:
            _purge_state['done'] += 1
    # 只改真删掉了音频、且目录里已没有别的音频的记录：删失败的、清理期间新存了音频的
    # 都不该被标成「没有音频」；其余 meta 不重写，免得 mtime 变了下次备份全量再拷一遍
    for td in emptied:
        mp = os.path.join(td, 'meta.json')
        try:
            left = any(n.startswith('audio.') and not n.endswith('.tmp') for n in os.listdir(td))
        except OSError:
            continue
        if not left and os.path.isfile(mp):
            _update_meta(mp, {'audio_ext': ''})
    with _purge_lock:
        _purge_state['running'] = False


@app.route('/api/audio/purge', methods=['POST'])
def api_audio_purge():
    with _purge_lock:
        if _purge_state['running']:
            return jsonify({'ok': False, 'error': 'already running'}), 409
    threading.Thread(target=_run_purge_audio, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/backup/status')
def api_backup_status():
    import backup
    st = backup.status(config.RESULTS_FOLDER)
    if DEMO_MODE:
        st['dest'] = ''     # Drive 路径里带账号邮箱和用户名，演示实例不外露
    return jsonify(st)


@app.route('/api/backup/run', methods=['POST'])
def api_backup_run():
    import backup
    if backup.status(config.RESULTS_FOLDER)['running']:
        return jsonify({'ok': False, 'error': 'already running'}), 409
    threading.Thread(target=backup.run, args=(config.RESULTS_FOLDER,), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/audio/purge_status')
def api_audio_purge_status():
    with _purge_lock:
        return jsonify(dict(_purge_state))


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

        with usage.scope(ref='xhs'):
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


def start_background():
    """服务起来前要跑的恢复与后台线程。直接 `python app.py` 和打包版的
    packaging/launcher.py 都调这一个——以前 launcher 只抄了两个 recover，
    打包版里定期同步、回顾、备份的调度线程都没起。"""
    recover_unfinished_tasks()
    recover_unfinished_chains()
    threading.Thread(target=_backfill_source_links, daemon=True).start()
    threading.Thread(target=_subscription_loop, daemon=True).start()   # 订阅的博主定时增量更新
    if DEMO_MODE:
        return   # 演示库只读不花钱：不预算回顾（调模型），也不备份（task_id 与真实库相同，会覆盖真实备份）
    try:
        import reflect
        reflect.start_scheduler(config.RESULTS_FOLDER)   # 回顾提前算好，打开不用等
    except Exception:  # noqa: BLE001
        pass
    try:
        import backup
        backup.start_scheduler(config.RESULTS_FOLDER)    # 内置备份：启动后 90 秒跑一次，之后每 6 小时
    except Exception:  # noqa: BLE001
        pass


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
        start_background()
    # HOST 默认 127.0.0.1（本地只对自己开）；Docker 里设 HOST=0.0.0.0 对外暴露。
    app.run(debug=debug, threaded=True, host=_host,
            port=int(os.environ.get('PORT', 5001)))
