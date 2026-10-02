"""链接转写的「查字幕」快速通道：有现成字幕的视频不该排在本地 Whisper 后面。
2026-10-02 用户批量贴了 20 期节目：大半有 YouTube 字幕，本来几秒就能完成，却因为整条
「查字幕 → 下载 → 转写」都在 Whisper 的 4 线程池里跑，排在要下载+转写的视频后面等了半小时。
跑生产代码：/api/transcribe_urls → _download_then_transcribe（probe / download 两段）→ submit_transcription。
桩（只在网络 / 模型边界）：downloader.fetch_subtitle / parse_srt / subtitle_usable / download_one，
app.run_transcription（用一个闸门卡住，模拟一条很慢的 Whisper），app._save_subtitle_task。"""
import os
import threading
import time

from _support import Checks, isolate

isolate('sublane')
os.environ['WHISPER_CONCURRENCY'] = '1'      # Whisper 池只有一个线程：被占住时，别的任务只能干等
import app as A  # noqa: E402
import downloader  # noqa: E402

t = Checks()
gate = threading.Event()                     # 不放行，「慢 Whisper」就一直占着池子
probed, transcribed, saved = [], [], []


def fake_fetch_subtitle(target, dl_dir, lang='auto'):
    url = target['video_url']
    probed.append(url)
    if 'hassubs' not in url:
        return None, None, None
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, 'x.srt')
    open(path, 'w').write('1\n00:00:01,000 --> 00:00:02,000\nhello\n')
    return path, 'manual', {'title': 'With subs', 'duration': 2, 'sub_lang': 'en', 'video_id': 'v1'}


def fake_download_one(item, dl_dir, section=None):
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, 'a.mp3')
    open(path, 'wb').write(b'\0')
    return {'path': path, 'title': 'No subs', 'video_id': 'v2'}


def fake_run_transcription(task_id, audio_path, engine, title, q, *a, **k):
    transcribed.append((task_id, engine))
    gate.wait(20)
    A.taskdb.set_status(task_id, 'done')
    A.tasks.pop(task_id, None)


def fake_save_subtitle_task(task_id, target, segments, source, lang=None, timing=None, **k):
    saved.append(task_id)
    A.taskdb.set_status(task_id, 'done')
    return segments


downloader.fetch_subtitle = fake_fetch_subtitle
downloader.parse_srt = lambda p: [{'start': 1.0, 'end': 2.0, 'text': 'hello'}]
downloader.subtitle_usable = lambda segs, dur: (True, '')
downloader.download_one = fake_download_one
A.run_transcription = fake_run_transcription
A._save_subtitle_task = fake_save_subtitle_task
c = A.app.test_client()


def status(tid):
    return (A.taskdb.get(tid) or {}).get('status')


def wait_for(cond, limit=5.0):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# 1. 先贴一条没字幕的：查完字幕没有 → 交给 Whisper 池下载 + 转写，然后被闸门卡住，占满这个池子
r = c.post('/api/transcribe_urls', json={'urls': 'https://www.youtube.com/watch?v=nosubs', 'engine': 'whisper'})
slow = r.get_json()['tasks'][0]['task_id']
t.check('没字幕的视频交给了 Whisper 池去下载 + 转写',
        wait_for(lambda: any(tid == slow and eng == 'whisper' for tid, eng in transcribed)),
        f'transcribed={transcribed}')

# 2. Whisper 池被占着时再贴一条有字幕的：应该几秒内直接完成，不排在后面
r = c.post('/api/transcribe_urls', json={'urls': 'https://www.youtube.com/watch?v=hassubs', 'engine': 'whisper'})
fast = r.get_json()['tasks'][0]['task_id']
t.check('Whisper 池被占满时，有字幕的视频照样几秒内完成', wait_for(lambda: status(fast) == 'done'),
        f'status={status(fast)}')
t.check('有字幕的视频没下载、没进转写', fast in saved and all(tid != fast for tid, _ in transcribed))
t.check('字幕命中后任务表清理干净（不会一直显示「进行中」）', wait_for(lambda: fast not in A.tasks))
t.check('慢任务这时还在转写中（快通道没有等它）', status(slow) != 'done', f'status={status(slow)}')

# 3. 选了「不用字幕」：根本不去查字幕，直接排转写
n_probed = len(probed)
r = c.post('/api/transcribe_urls', json={'urls': 'https://www.youtube.com/watch?v=hassubs2', 'engine': 'whisper',
                                         'subs': 'off'})
off = r.get_json()['tasks'][0]['task_id']
gate.set()                                   # 放行：慢任务和这条都能转完
t.check('选了「不用字幕」就不查字幕', wait_for(lambda: status(off) == 'done') and len(probed) == n_probed,
        f'probed={probed[n_probed:]} status={status(off)}')
t.check('放行后慢任务正常完成', wait_for(lambda: status(slow) == 'done'), f'status={status(slow)}')
t.check('每条只转写一次（交接没有重复提交）', sorted(tid for tid, _ in transcribed) == sorted([slow, off]),
        f'transcribed={transcribed}')
t.finish()
