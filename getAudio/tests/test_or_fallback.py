"""官方 Gemini 转不了（月度花费上限 / 调用失败）→ 先改走 OpenRouter 上的 Gemini，再不行才落本地 Whisper。
10-28：「CLL 伤逝」撞上 spending cap，落到 Whisper 又因为笔记本用电池原地等插电，界面一直「转写中」。
用户：「如果 google 官方的不行就用 OR（加一个这个 fallback）」。
桩：transcribe_gemini / transcribe_gemini35 / transcribe_whisper 的 transcribe_audio，
transcribe_openrouter 的 HTTP（requests.post）——不连网、不花钱。"""
import base64
import os
import queue
import uuid

from _support import Checks, isolate

TMP = isolate('or_fallback')
os.environ['VOICES_AUTO'] = 'off'
import app as A  # noqa: E402
import taskdb  # noqa: E402
import transcribe_gemini  # noqa: E402
import transcribe_gemini35  # noqa: E402
import transcribe_openrouter  # noqa: E402
import transcribe_whisper  # noqa: E402
import usage  # noqa: E402

t = Checks()
CAP = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Your project has exceeded its monthly "
       "spending cap.', 'status': 'RESOURCE_EXHAUSTED'}}")
calls = {'google': 0, 'or': 0, 'whisper': 0}
or_behaviour = {'fail': False}


def google_cap(path, progress_callback=None, speaker_count=None):
    calls['google'] += 1
    raise RuntimeError(CAP)


def google_500(path, progress_callback=None, speaker_count=None):
    calls['google'] += 1
    raise RuntimeError('Gemini 3.5 Transcribe 调用失败: HTTP 500: internal')


def or_tx(path, progress_callback=None):
    calls['or'] += 1
    if or_behaviour['fail']:
        raise RuntimeError('OpenRouter 转写失败: HTTP 402 insufficient credits')
    segs = [{'timestamp': '00:00:01', 'text': '说话人1：你好'}]
    return segs, '[00:00:01] 说话人1：你好'


def whisper_tx(path, progress_callback=None):
    calls['whisper'] += 1
    return [{'start': 1, 'end': 2, 'text': '你好'}]


transcribe_gemini.transcribe_audio = google_cap
transcribe_gemini35.transcribe_audio = google_cap
transcribe_whisper.transcribe_audio = whisper_tx
A._on_battery = lambda: False
A._check_transcript = lambda *a, **k: None      # 桩出来的一句话会被当成「太短」的白卷


def run(engine, name, fresh=True):
    for k in calls:
        calls[k] = 0
    if fresh:
        A._quota_out.clear()
    tid = str(uuid.uuid4())
    src = os.path.join(TMP, name)
    open(src, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, name, engine, None, src)
    A.run_transcription(tid, src, engine, name, queue.Queue(), fallback_whisper=False, summarize=False)
    return taskdb.get(tid) or {}


def engine_of(tid_row):
    import json
    p = os.path.join(A.config.RESULTS_FOLDER, tid_row['id'], 'meta.json')
    return json.load(open(p, encoding='utf-8')).get('engine') if os.path.isfile(p) else None


# 1. 没配 OpenRouter key：跟以前一样直接落 Whisper
os.environ['OPENROUTER_API_KEY'] = ''
transcribe_openrouter.transcribe_audio = or_tx
r = run('gemini35', 'a.m4a')
t.check('没 OR key：落 Whisper、不碰 OR', r.get('status') == 'done' and calls['or'] == 0 and calls['whisper'] == 1, str(calls))

# 2. 有 key：spending cap → OpenRouter 转出来，不再落 Whisper
os.environ['OPENROUTER_API_KEY'] = 'test-or-key'
r = run('gemini35', 'b.m4a')
t.check('gemini35 撞上限 → 走 OR 成功', r.get('status') == 'done' and calls['or'] == 1 and calls['whisper'] == 0,
        str(calls) + ' ' + str(r.get('error'))[:200])
t.check('结果记成 openrouter 引擎', engine_of(r) == 'openrouter', str(engine_of(r)))

r = run('gemini', 'c.m4a')
t.check('gemini 引擎同样走 OR', r.get('status') == 'done' and calls['or'] == 1 and calls['whisper'] == 0, str(calls))

# 3. 不是额度、是普通调用失败：也先试 OR（用户：「官方的不行就用 OR」）
transcribe_gemini35.transcribe_audio = google_500
r = run('gemini35', 'd.m4a')
t.check('官方 HTTP 500 → 也走 OR', r.get('status') == 'done' and calls['or'] == 1 and calls['whisper'] == 0, str(calls))
transcribe_gemini35.transcribe_audio = google_cap

# 4. OR 也失败：额度用完照旧落 Whisper；两边原因都留着
or_behaviour['fail'] = True
r = run('gemini35', 'e.m4a')
t.check('OR 也挂 + 额度用完 → 还是落 Whisper', r.get('status') == 'done' and calls['or'] == 1 and calls['whisper'] == 1, str(calls))
_meta = os.path.join(A.config.RESULTS_FOLDER, r['id'], 'meta.json')
_tm = __import__('json').load(open(_meta, encoding='utf-8')).get('timing') or {} if os.path.isfile(_meta) else {}
t.check('Whisper 接住了也留下 OR 的失败原因', 'HTTP 402' in str(_tm.get('openrouter_error')), str(_tm)[:300])
transcribe_gemini35.transcribe_audio = google_500
r = run('gemini35', 'f.m4a')
t.check('OR 也挂 + 普通失败（没勾兜底）→ 判失败', r.get('status') == 'failed', str(r.get('status')))
t.check('失败原因里两边都有', 'HTTP 500' in (r.get('error') or '') and 'OpenRouter' in (r.get('error') or ''), str(r.get('error'))[:300])
transcribe_gemini35.transcribe_audio = google_cap
or_behaviour['fail'] = False

# 5. 内容拦截：OR 上也是同一个 Gemini，白试一次没意义 → 照旧直接 Whisper
transcribe_gemini35.transcribe_audio = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('finish_reason RECITATION'))
r = run('gemini35', 'g.m4a')
t.check('内容拦截不试 OR', calls['or'] == 0 and calls['whisper'] == 1, str(calls))
transcribe_gemini35.transcribe_audio = google_cap

# 6. 撞上限后一小时内：下一期直接 OR，不再先碰官方
run('gemini35', 'h.m4a')
run('gemini35', 'i.m4a', fresh=False)
t.check('一小时内下一期：不碰官方、直接 OR', calls['google'] == 0 and calls['or'] == 1 and calls['whisper'] == 0, str(calls))

# 7. transcribe_openrouter 本身：请求格式、解析、记账
import importlib  # noqa: E402
txor = importlib.reload(transcribe_openrouter)
sent = []


class _Resp:
    status_code = 200
    text = ''

    def __init__(self, body):
        self._b = body

    def json(self):
        return self._b

    def raise_for_status(self):
        pass


def fake_post(url, headers=None, json=None, timeout=None):
    sent.append({'url': url, 'headers': headers, 'json': json, 'timeout': timeout})
    return _Resp({
        'model': 'google/gemini-3.8-flash',
        'choices': [{'message': {'content': '[00:00:00] 说话人1：大家好\n[00:00:31] 说话人2：你好'}}],
        'usage': {'prompt_tokens': 900, 'completion_tokens': 40, 'cost': 0.0012},
    })


txor.requests.post = fake_post
audio = os.path.join(TMP, 'clip.m4a')
open(audio, 'wb').write(b'fake-m4a-bytes')
recorded = []
real_record = usage.record
usage.record = lambda *a, **k: recorded.append((a, k))
try:
    segs, full = txor.transcribe_audio(audio)
finally:
    usage.record = real_record
req = sent[0] if sent else {}
body = req.get('json') or {}
content = (body.get('messages') or [{}])[0].get('content') or []
aud = next((c for c in content if c.get('type') == 'input_audio'), {})
t.check('打的是 OpenRouter chat/completions', req.get('url', '').endswith('/chat/completions') and 'openrouter' in req.get('url', ''), req.get('url'))
t.check('带 Bearer key', (req.get('headers') or {}).get('Authorization') == 'Bearer test-or-key')
t.check('模型是 OR 上的 google/gemini-*', str(body.get('model', '')).startswith('google/gemini'), body.get('model'))
t.check('音频 base64 原样送、格式按后缀', aud.get('input_audio', {}).get('format') == 'm4a'
        and base64.b64decode(aud.get('input_audio', {}).get('data', '')) == b'fake-m4a-bytes', str(aud)[:200])
t.check('有超时（别再无限挂着）', isinstance(req.get('timeout'), (int, float)) and req.get('timeout') > 0, str(req.get('timeout')))
t.check('解析出两段带时间', len(segs) == 2 and segs[1]['timestamp'] == '00:00:31' and '说话人2' in segs[1]['text'], str(segs))
t.check('记账：openrouter + transcribe + 实价', recorded and recorded[0][0][0] == 'openrouter'
        and recorded[0][0][2] == 'transcribe' and recorded[0][1].get('cost_usd') == 0.0012, str(recorded)[:300])

# 8. 没人说话的一块（10-28「CLL 伤逝」最后 40 秒是静音）：模型正常收尾、内容为空 = 这块没语音，不是失败，别重试
sent.clear()
txor.requests.post = lambda url, headers=None, json=None, timeout=None: (sent.append(1), _Resp({
    'choices': [{'finish_reason': 'stop', 'message': {'content': ''}}],
    'usage': {'prompt_tokens': 1390, 'completion_tokens': 115, 'cost': 0.0015}}))[1]
txor.time.sleep = lambda s: None
try:
    text = txor._transcribe_chunk('test-or-key', audio)
    t.check('静音块：返回空、不报错', text == '', repr(text))
except Exception as e:  # noqa: BLE001
    t.check('静音块：返回空、不报错', False, str(e)[:200])
t.check('静音块只请求一次', len(sent) == 1, str(len(sent)))

# 9. 切块按 5 分钟：15 分钟一块时模型整块只打两三个时间戳（10-28 实测 3 个 / 15 分钟），5 分钟一块每 30 多秒一个
asked = []
txor._can_split_audio = lambda: True
txor.split_audio_file = lambda path, secs: (asked.append(secs), ([(path, 0)], None))[1]
txor.requests.post = fake_post
txor.transcribe_audio(audio)
t.check('OpenRouter 按 5 分钟切块', asked == [300], str(asked))

t.finish()
