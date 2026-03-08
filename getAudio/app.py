"""
Flask application for audio/video transcription.
Supports Whisper (local) and Gemini (cloud) engines with SSE progress streaming.
Persists results (audio + transcript + summary) to disk for history playback.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import uuid

from flask import Flask, Response, jsonify, render_template, request, send_file

import config

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH

os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
os.makedirs(config.RESULTS_FOLDER, exist_ok=True)

tasks = {}


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

    meta = {
        'id': task_id,
        'filename': original_filename,
        'engine': engine,
        'date': __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'audio_ext': ext,
        'segment_count': len(segments),
    }
    with open(os.path.join(task_dir, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(task_dir, 'transcript.json'), 'w', encoding='utf-8') as f:
        json.dump(segments, f, ensure_ascii=False, indent=2)

    if summary:
        with open(os.path.join(task_dir, 'summary.json'), 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)


def run_transcription(task_id, filepath, engine, original_filename, q):
    """Background worker: runs transcription, saves results, pushes events."""

    def progress_cb(percent):
        q.put(json.dumps({
            'type': 'progress',
            'percent': percent,
            'message': f'转写中... {percent}%',
        }))

    cleanup_paths = [filepath]
    input_path = filepath

    try:
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

        else:
            q.put(json.dumps({
                'type': 'error',
                'message': f'Unknown engine: {engine}',
            }))
            return

        _save_results(task_id, original_filename, engine, input_path,
                      segments, summary_data)

        q.put(json.dumps({
            'type': 'done',
            'task_id': task_id,
            'segments': segments,
            'summary': summary_data,
        }))

    except Exception as e:
        q.put(json.dumps({
            'type': 'error',
            'message': str(e),
        }))

    finally:
        for path in cleanup_paths:
            try:
                os.remove(path)
            except OSError:
                pass


# ========== Pages ==========

@app.route('/')
def index():
    return render_template('index.html')


# ========== Upload & Stream ==========

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('audio')
    engine = request.form.get('engine', 'whisper')

    if not file or file.filename == '':
        return jsonify({'error': '请选择一个音频或视频文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({
            'error': f'不支持的文件格式。支持: {", ".join(config.ALLOWED_EXTENSIONS)}'
        }), 400

    task_id = str(uuid.uuid4())
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp3'
    filename = f"{task_id}.{ext}"
    filepath = os.path.join(config.UPLOAD_FOLDER, filename)
    file.save(filepath)

    q = queue.Queue()
    tasks[task_id] = q

    t = threading.Thread(
        target=run_transcription,
        args=(task_id, filepath, engine, file.filename, q),
        daemon=True,
    )
    t.start()

    return jsonify({'task_id': task_id})


@app.route('/stream/<task_id>')
def stream(task_id):
    def event_stream():
        q = tasks.get(task_id)
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Task not found'})}\n\n"
            return

        while True:
            try:
                msg = q.get(timeout=300)
                yield f"data: {msg}\n\n"
                data = json.loads(msg)
                if data['type'] in ('done', 'error'):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': '转写超时'})}\n\n"
                break

        tasks.pop(task_id, None)

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
    return send_file(audio_path, mimetype=mime_map.get(audio_ext, 'audio/wav'))


@app.route('/api/history/<task_id>', methods=['DELETE'])
def api_history_delete(task_id):
    """Delete a saved transcription and its files."""
    task_dir = os.path.join(config.RESULTS_FOLDER, task_id)
    if os.path.isdir(task_dir):
        shutil.rmtree(task_dir, ignore_errors=True)
        return jsonify({'ok': True})
    return jsonify({'error': 'Not found'}), 404


if __name__ == '__main__':
    app.run(debug=True, threaded=True, port=5001)
