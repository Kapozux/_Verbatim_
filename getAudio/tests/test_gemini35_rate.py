"""Gemini 3.5 Transcribe 被限流（429）时按服务端说的时间等，不算失败（GitHub #7）。
以前：429 也只按 2 / 5 / 10 秒重试三次，三次都落在同一个每分钟窗口里，整期判失败（600 期里失败 12 期）。
现在：按报错里的 "retry in Ns"（没有就看 Retry-After 头，再没有等 60 秒）等，等待不占重试次数，
所有线程共用一个闸（配额是全项目的）；5xx 还是照旧 3 次。

跑生产代码：transcribe_gemini35._call_with_retry / _wait_rate_gate / _push_rate_gate / _rate_wait_seconds。
桩：requests.post（按剧本回 429 / 500 / 200）、模块里的 time（假时钟，sleep 只拨表不真等）、
_parse_response / _record_usage（回答怎么解析不在这里测）、Files API 客户端（文件直接 ACTIVE）。
"""
from _support import Checks, isolate

TMP = isolate('gemini35_rate')
import transcribe_gemini35 as G  # noqa: E402

t = Checks()


class Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


class Resp:
    def __init__(self, status, text='', headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def json(self):
        return {'ok': True}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class Files:
    def get(self, name):
        return type('F', (), {'state': 'ACTIVE'})()


class Uploaded:
    name, uri, mime_type = 'files/x', 'https://files/x', 'audio/mpeg'


client = type('C', (), {'files': Files()})()
G._parse_response = lambda data, off, secs: ['parsed']
G._record_usage = lambda data, path: None
LIMIT = ('Rate limit exceeded for model gemini-3.5-transcribe (limit: 100000 input tokens per minute on Tier 2). '
         'Please retry in %s or upgrade your tier at https://ai.dev/rate-limit.')


def run(script):
    """按剧本回响应；返回（结果或异常, 假时钟, 每次请求发出的时刻）。"""
    clock = Clock()
    G.time = clock
    G._rate_until = 0.0
    sent = []
    script = list(script)

    def post(*a, **k):
        sent.append(clock.now)
        return script.pop(0)

    G.requests.post = post
    try:
        out = G._call_with_retry(client, Uploaded(), '/tmp/x.mp3', 0, None)
    except Exception as e:  # noqa: BLE001
        out = e
    return out, clock, sent


# 1. 连着 5 次 429（比 3 次重试上限多）再成功：不判失败，每次都等够服务端说的 45 秒
out, clock, sent = run([Resp(429, LIMIT % '45s')] * 5 + [Resp(200)])
gaps = [b - a for a, b in zip(sent, sent[1:])]
t.check('5 次 429 之后成功（限流等待不占重试次数）', out == ['parsed'], out)
t.check('每次都等够 45 秒才重发', len(gaps) == 5 and all(g >= 45 for g in gaps), gaps)
t.check('没用 2 / 5 / 10 秒那套', all(g < 60 for g in gaps) and 2 not in clock.sleeps[:1], clock.sleeps)

# 2. 等多久怎么定
t.check('"retry in 45s" → 45', G._rate_wait_seconds(LIMIT % '45s') == 45)
t.check('"retry in 44.86s" → 44.86', abs(G._rate_wait_seconds(LIMIT % '44.86s') - 44.86) < 1e-6)
t.check('"retry in 1m5s" → 65', G._rate_wait_seconds(LIMIT % '1m5s') == 65, G._rate_wait_seconds(LIMIT % '1m5s'))
t.check('正文没说、有 Retry-After 头 → 用头里的', G._rate_wait_seconds('quota exceeded', '50') == 50)
t.check('正文说了就以正文为准', G._rate_wait_seconds(LIMIT % '45s', '5') == 45)
t.check('都没有 → 等一个完整的每分钟窗口（60 秒）', G._rate_wait_seconds('quota exceeded') == 60)
t.check('Retry-After: 0 → 现在就能再试', G._rate_wait_seconds('quota exceeded', '0') == 0)
t.check('Retry-After: inf / 负数 → 60', G._rate_wait_seconds('', 'inf') == 60 and G._rate_wait_seconds('', '-5') == 60)
t.check('Retry-After 不是数字 → 也是 60', G._rate_wait_seconds('', 'Wed, 21 Oct 2026 07:28:00 GMT') == 60)

out, clock, sent = run([Resp(429, 'quota exceeded', {'Retry-After': '50'}), Resp(200)])
t.check('Retry-After 头：等够 50 秒', out == ['parsed'] and sent[1] - sent[0] >= 50, sent)
out, clock, sent = run([Resp(429, 'quota exceeded'), Resp(200)])
t.check('没给时间：等够 60 秒', out == ['parsed'] and sent[1] - sent[0] >= 60, sent)

# 3. 闸是共用的：一个请求被限流，别的线程发请求前也先等
clock = Clock()
G.time = clock
G._rate_until = 0.0
G._push_rate_gate(LIMIT % '45s')
before = clock.now
G._wait_rate_gate()
t.check('别的请求也等到闸放开', clock.now - before >= 45, clock.now - before)

# 4. 5xx 照旧：3 次，间隔 2 / 5 秒，然后判失败
out, clock, sent = run([Resp(500, 'boom')] * 3)
t.check('5xx 试 3 次后失败', isinstance(out, Exception) and len(sent) == 3, (out, len(sent)))
t.check('5xx 的间隔是 2、5 秒', [round(b - a) for a, b in zip(sent, sent[1:])] == [2, 5], sent)

# 服务端让等的时间比耐心上限（一小时）还长：马上判失败，不让所有线程干等两小时
out, clock, sent = run([Resp(429, 'quota exceeded', {'Retry-After': '7200'}), Resp(200)])
t.check('要等两小时：判失败', isinstance(out, Exception) and '429' in str(out), out)
t.check('没有真去等', clock.now - sent[0] < 60 and len(sent) == 1, (clock.now - sent[0], len(sent)))

# 5. 一直 429：有耐心上限，不会永远等
G._RATE_LIMIT_PATIENCE_SECONDS = 200
out, clock, sent = run([Resp(429, LIMIT % '45s')] * 50)
t.check('超过耐心上限后失败', isinstance(out, Exception) and '429' in str(out), out)
t.check('没把 50 次都试完', len(sent) < 12, len(sent))

t.finish()
