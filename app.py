"""
Flask application for MP3-to-text transcription.
Supports Whisper (local) and Gemini (cloud) engines with SSE progress streaming.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import uuid

from flask import Flask, Response, jsonify, render_template, request

import config

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = config.MAX_CONTENT_LENGTH

# Ensure upload directory exists
os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)

# Active tasks: task_id -> queue.Queue
tasks = {}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and \
        filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS


def format_seconds(s):
    """Convert float seconds to MM:SS format."""
    total = int(s)
    minutes = total // 60
    seconds = total % 60
    return f"{minutes:02d}:{seconds:02d}"


def is_video_file(filepath):
    """Check if file path has a known video extension."""
    ext = os.path.splitext(filepath)[1].lower().lstrip('.')
    return ext in config.VIDEO_EXTENSIONS


def resolve_ffmpeg_binary():
    """Resolve ffmpeg executable from PATH or common Homebrew path."""
    return shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'


def extract_audio_from_video(video_path, output_path):
    """Extract mono 16k WAV audio from a video file using ffmpeg."""
    ffmpeg_bin = resolve_ffmpeg_binary()
    if not os.path.exists(ffmpeg_bin):
        raise RuntimeError('未检测到 ffmpeg，无法从视频中提取音频')

    cmd = [
        ffmpeg_bin,
        '-y',
        '-v',
        'error',
        '-i',
        video_path,
        '-vn',
        '-ac',
        '1',
        '-ar',
        '16000',
        '-c:a',
        'pcm_s16le',
        output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return output_path


def run_transcription(task_id, filepath, engine, q):
    """Background worker: runs transcription, pushes events to queue."""

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

        if engine == 'whisper':
            from transcribe_whisper import transcribe_audio

            q.put(json.dumps({
                'type': 'progress',
                'percent': 0,
                'message': '正在加载 Whisper 模型（首次可能需要下载）...',
            }))

            segments = transcribe_audio(input_path, progress_callback=progress_cb)

            # Normalize Whisper segments and send each one
            normalized = []
            for seg in segments:
                item = {
                    'timestamp': format_seconds(seg['start']),
                    'end': format_seconds(seg['end']),
                    'text': seg['text'].strip(),
                }
                normalized.append(item)
                q.put(json.dumps({'type': 'segment', **item}))

            q.put(json.dumps({
                'type': 'done',
                'segments': normalized,
            }))

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

            q.put(json.dumps({
                'type': 'done',
                'segments': segments,
            }))

        else:
            q.put(json.dumps({
                'type': 'error',
                'message': f'Unknown engine: {engine}',
            }))

    except Exception as e:
        q.put(json.dumps({
            'type': 'error',
            'message': str(e),
        }))

    finally:
        # Clean up uploaded/transient files
        for path in cleanup_paths:
            try:
                os.remove(path)
            except OSError:
                pass


@app.route('/')
def index():
    """Serve the main page."""
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    """Handle file upload and start transcription task."""
    file = request.files.get('audio')
    engine = request.form.get('engine', 'whisper')

    if not file or file.filename == '':
        return jsonify({'error': '请选择一个音频或视频文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({
            'error': f'不支持的文件格式。支持: {", ".join(config.ALLOWED_EXTENSIONS)}'
        }), 400

    # Save uploaded file
    task_id = str(uuid.uuid4())
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp3'
    filename = f"{task_id}.{ext}"
    filepath = os.path.join(config.UPLOAD_FOLDER, filename)
    file.save(filepath)

    # Create task queue and start background thread
    q = queue.Queue()
    tasks[task_id] = q

    t = threading.Thread(
        target=run_transcription,
        args=(task_id, filepath, engine, q),
        daemon=True,
    )
    t.start()

    return jsonify({'task_id': task_id})


@app.route('/stream/<task_id>')
def stream(task_id):
    """SSE endpoint: streams progress and results for a task."""

    def event_stream():
        q = tasks.get(task_id)
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Task not found'})}\n\n"
            return

        while True:
            try:
                msg = q.get(timeout=300)  # 5-minute timeout
                yield f"data: {msg}\n\n"
                data = json.loads(msg)
                if data['type'] in ('done', 'error'):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'error', 'message': '转写超时'})}\n\n"
                break

        # Cleanup task
        tasks.pop(task_id, None)

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


if __name__ == '__main__':
    app.run(debug=True, threaded=True, port=5001)
