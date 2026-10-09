"""下载失败分几类、给能照着做的提示（借 Open Notebook / content-core 的类型化提取错误）：
以前不管什么原因都只说「下载失败——检查链接」，yt-dlp 的原话被丢掉；视频被删、要登录、地区限制
这类重试也没用的错，照样重试三遍、等十几秒。

跑生产代码：downloader.classify_download_error / download_one / last_download_error、
app._download_then_transcribe（单条链接）。
桩：downloader.subprocess.run（假的 yt-dlp：按设定吐 stderr、记下被调了几次）、downloader.time.sleep（不真等）、
downloader._bili_festival_url（不连网）。
"""
import json
import queue
import subprocess
import uuid

from _support import Checks, isolate

TMP = isolate('dl_errors')
import app as A  # noqa: E402
import downloader  # noqa: E402
import taskdb  # noqa: E402

t = Checks()

samples = {
    'not_found': ['ERROR: [youtube] abc: Video unavailable. This video is private',
                  'ERROR: [youtube] abc: This video has been removed by the uploader',
                  'ERROR: [BiliBili] BV1xx: HTTP Error 404: Not Found'],
    'login': ['ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.',
              'ERROR: [youtube] abc: Join this channel to get access to members-only content like this video'],
    'blocked': ["ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser",
                'ERROR: [BiliBili] BV1xx: HTTP Error 412: Precondition Failed',
                'ERROR: unable to download video data: HTTP Error 429: Too Many Requests'],
    'geo': ['ERROR: [youtube] abc: The uploader has not made this video available in your country'],
    'unsupported': ['ERROR: Unsupported URL: https://example.com/page'],
    'network': ['ERROR: [youtube] abc: Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided>',
                'ERROR: Connection reset by peer'],
}
for kind, lines in samples.items():
    for line in lines:
        got = downloader.classify_download_error(line)
        t.check(f'{kind}: {line[:50]}', got == kind, got)
t.check('超时算网络', downloader.classify_download_error('', timed_out=True) == 'network')
t.check('认不出的算 unknown', downloader.classify_download_error('ERROR: something odd') == 'unknown')
t.check('每一类都有给用户的提示', all(downloader.DOWNLOAD_ERRORS.get(k) for k in list(samples) + ['unknown']))

calls = []
stderr_now = ['']


def fake_run(cmd, **kw):
    calls.append(cmd)
    return subprocess.CompletedProcess(cmd, 1, stdout='', stderr=stderr_now[0])


downloader.subprocess.run = fake_run
downloader.time.sleep = lambda s: None
downloader._bili_festival_url = lambda u: None

# ---- 重试没用的错：试一次就停 ----
stderr_now[0] = samples['not_found'][0]
calls.clear()
t.check('视频没了：返回 None', downloader.download_one({'video_url': 'https://youtu.be/abc'}, TMP) is None)
t.check('视频没了：只试一次', len(calls) == 1, str(len(calls)))
t.check('视频没了：记下类别', downloader.last_download_error() == 'not_found')

# ---- 可能过一会就好的错：照旧重试 ----
stderr_now[0] = samples['blocked'][1]
calls.clear()
downloader.download_one({'video_url': 'https://www.bilibili.com/video/BV1xx'}, TMP)
t.check('被限流：照旧重试', len(calls) == downloader._DOWNLOAD_ATTEMPTS, str(len(calls)))
t.check('被限流：记下类别', downloader.last_download_error() == 'blocked')

# ---- 单条链接：任务的错误写的是这一类的提示，不是 yt-dlp 的原话 ----
stderr_now[0] = samples['login'][0]
tid = str(uuid.uuid4())
taskdb.create(tid, 'https://youtu.be/abc', 'gemini35', None, '')
q = queue.Queue()
A._download_then_transcribe(tid, 'https://youtu.be/abc', 'gemini35', q, None, 0, 'off', None, stage='download')
row = taskdb.get(tid) or {}
t.check('单条链接：失败原因是这一类的提示', row.get('error') == downloader.DOWNLOAD_ERRORS['login'], row.get('error'))
msgs = []
while not q.empty():
    msgs.append(json.loads(q.get()))
err = [m for m in msgs if m.get('type') == 'error']
t.check('单条链接：推给界面的也是这句', err and err[-1]['message'] == downloader.DOWNLOAD_ERRORS['login'], str(err))
t.check('单条链接：不把 yt-dlp 原话推给界面', not any('Sign in to confirm your age' in m.get('message', '') for m in err))

t.finish()
