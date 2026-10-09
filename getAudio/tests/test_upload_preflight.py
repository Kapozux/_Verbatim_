"""上传前预检（借 Open Notebook 的 _assert_file_supported）：后缀对、内容不是音视频的文件
（坏文件、改了后缀的文档、空文件、没有音轨的录屏）当场拒收，不进引擎池占线程、不建任务。

跑生产代码：/upload、/api/transcribe_local 路由 → _enqueue_task / _enqueue_local_task / preflight_media（真 ffprobe）。
桩：app.submit_transcription（记下提交，不起转写线程）。
数据：python 写的真 WAV、改成 .mp3 的文本、空文件、ffmpeg 现做的无音轨 mp4（没装 ffmpeg 时跳过这一条）。
"""
import io
import os
import shutil
import subprocess
import wave

from _support import Checks, isolate

TMP = isolate('preflight')
import app as A  # noqa: E402
import taskdb  # noqa: E402

t = Checks()
submitted = []
A.submit_transcription = lambda engine, fn, *a, **k: submitted.append(a[0])
client = A.app.test_client()


def wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b'\0\0' * 16000)
    return buf.getvalue()


def upload(name, data):
    submitted.clear()
    r = client.post('/upload', data={'audio': (io.BytesIO(data), name), 'engine': 'gemini35'},
                    content_type='multipart/form-data')
    return r.status_code, r.get_json() or {}


def leftovers():
    return [n for n in os.listdir(A.config.UPLOAD_FOLDER) if not n.startswith('_')]


if not os.path.exists(A.config.FFPROBE_BIN):
    t.skip('全部', '没装 ffprobe')
    t.finish()

code, body = upload('talk.wav', wav_bytes())
t.check('真音频照常收下', code == 200 and len(submitted) == 1, str(body))
before = set(leftovers())

code, body = upload('notes.mp3', b'This is a text file, not audio.\n' * 50)
t.check('改了后缀的文本被拒', code == 400 and not submitted, str(body))
t.check('拒收理由说清楚', 'audio or video' in (body.get('error') or ''), str(body))
t.check('拒收后不留文件', set(leftovers()) == before, str(set(leftovers()) - before))

code, body = upload('empty.m4a', b'')
t.check('空文件被拒', code == 400 and not submitted and 'empty' in (body.get('error') or ''), str(body))
t.check('被拒的不建任务', not [r for r in taskdb.unfinished() if r['filename'] in ('notes.mp3', 'empty.m4a')])

ffmpeg = A.config.FFMPEG_BIN
if os.path.exists(ffmpeg):
    silent = os.path.join(TMP, 'screen.mp4')
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=64x64:rate=5', '-t', '1',
                    '-pix_fmt', 'yuv420p', silent], check=True)
    code, body = upload('screen.mp4', open(silent, 'rb').read())
    t.check('没有音轨的视频被拒', code == 400 and not submitted and 'no audio' in (body.get('error') or ''),
            str(body))
    submitted.clear()
    r = client.post('/api/transcribe_local', json={'path': silent})
    t.check('本机路径也预检', r.status_code == 400 and not submitted, str(r.get_json()))
else:
    t.skip('没有音轨的视频', '没装 ffmpeg')

bad = os.path.join(TMP, 'fake.mp4')
open(bad, 'wb').write(b'not a video' * 100)
submitted.clear()
r = client.post('/api/transcribe_local', json={'path': bad})
t.check('本机路径：坏文件被拒、原件不动', r.status_code == 400 and not submitted and os.path.exists(bad),
        str(r.get_json()))

t.finish()
