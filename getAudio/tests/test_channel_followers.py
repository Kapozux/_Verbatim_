"""channel_followers()：yt-dlp 吐出字面量 "null" 时（GitHub #3）别靠 except 吞掉 AttributeError
悄悄回 0，要跟另外两处一样按「空结果」处理，并且打一行日志，好跟「真的 0 订阅」区分开。
桩：downloader.subprocess.run（假 yt-dlp）、downloader._resolve_ytdlp。"""
import subprocess

from _support import Checks, isolate

TMP = isolate('channel_followers')
import downloader  # noqa: E402

t = Checks()
out = ['']


def fake_run(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, stdout=out[0], stderr='')


downloader.subprocess.run = fake_run
downloader._resolve_ytdlp = lambda: 'yt-dlp'

logged = []
downloader.print = lambda *a, **k: logged.append(' '.join(map(str, a)))   # downloader 的日志就是 print（进 server.log）

out[0] = '{"channel_follower_count": 1234}'
t.check('正常结果照取', downloader.channel_followers('https://www.youtube.com/@x') == 1234)

out[0] = 'null'
logged.clear()
t.check('yt-dlp 吐 null 不抛错、回 0', downloader.channel_followers('https://www.youtube.com/@x') == 0)
t.check('吐 null 时记一行日志（跟真的 0 订阅区分）', any('channel_followers' in m for m in logged), logged)

out[0] = '{"channel_follower_count": null}'
logged.clear()
t.check('字段为空就是 0，不当成解析失败', downloader.channel_followers('https://www.youtube.com/@x') == 0 and not logged, logged)

t.finish()
