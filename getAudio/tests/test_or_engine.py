"""OpenRouter 当主转写引擎（11-09 用户：「就只有模型，默认都是 OR 去做，可以选 API：OR / Gemini / agy」）。
桩：transcribe_openrouter / transcribe_gemini / whisper 的 transcribe_audio——不连网、不花钱。"""
import json
import os
import queue
import uuid

from _support import Checks, isolate

TMP = isolate('or_engine')
os.environ['VOICES_AUTO'] = 'off'
os.environ['OPENROUTER_API_KEY'] = 'sk-or-fake'
os.environ.pop('DEFAULT_ENGINE', None)
import app as A  # noqa: E402
import config  # noqa: E402
import taskdb  # noqa: E402
import transcribe_gemini  # noqa: E402
import transcribe_openrouter  # noqa: E402
import transcribe_whisper  # noqa: E402

t = Checks()

# ---- 默认引擎：有 OR key 就 OR（装没装 agy 都一样）；DEFAULT_ENGINE 仍然优先 ----
config.agy_installed = lambda: True
t.check('有 OR key：默认 openrouter', config.default_engine() == 'openrouter', config.default_engine())
os.environ['DEFAULT_ENGINE'] = 'gemini35'
t.check('DEFAULT_ENGINE 优先', config.default_engine() == 'gemini35')
os.environ.pop('DEFAULT_ENGINE')
os.environ['OPENROUTER_API_KEY'] = ''
t.check('没 OR key、装了 agy：agy_flash', config.default_engine() == 'agy_flash', config.default_engine())
os.environ['OPENROUTER_API_KEY'] = 'sk-or-fake'

# ---- 首页：三条路（OR / Gemini API / 订阅）在外面，OR 默认勾上；别的收进「更多」 ----
c = A.app.test_client()
html = c.get('/').get_data(as_text=True)
t.check('首页有 openrouter 选项且默认勾上', 'value="openrouter" checked' in html, '')
main_part = html.split('id="more-engines"')[0].split('name="engine"', 1)[1] if 'id="more-engines"' in html else ''
t.check('外面只有 OR / Gemini / 订阅 Flash 三个',
        all(f'value="{v}"' in main_part for v in ('openrouter', 'gemini', 'agy_flash'))
        and not any(f'value="{v}"' in main_part for v in ('gemini35', 'whisper', 'agy_argon', 'qwenasr')), '')
t.check('笔记本表单默认选 openrouter', 'value="openrouter" selected' in html)

# ---- app 里跑一条 ----
A._on_battery = lambda: False
A._check_transcript = lambda *a, **k: None
calls = {'or': 0, 'gemini': 0, 'whisper': 0}
or_fail = [False]


def or_tx(path, progress_callback=None):
    calls['or'] += 1
    if or_fail[0]:
        raise RuntimeError('OpenRouter 402: insufficient credits')
    return [{'timestamp': '00:00', 'start': 0, 'end': 2, 'text': '你好'}], '[00:00] 你好'


def gemini_tx(path, progress_callback=None, **kw):
    calls['gemini'] += 1
    return [{'timestamp': '00:00', 'start': 0, 'end': 2, 'text': '你好'}], '[00:00] 你好'


def whisper_tx(path, progress_callback=None):
    calls['whisper'] += 1
    return [{'start': 0, 'end': 2, 'text': '你好'}]


transcribe_openrouter.transcribe_audio = or_tx
transcribe_gemini.transcribe_audio = gemini_tx
transcribe_whisper.transcribe_audio = whisper_tx


def run(engine, name):
    tid = str(uuid.uuid4())
    p = os.path.join(TMP, name)
    open(p, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, name, engine, None, p)
    A.run_transcription(tid, p, engine, name, queue.Queue(), fallback_whisper=False, summarize=False)
    row = taskdb.get(tid) or {}
    mp = os.path.join(config.RESULTS_FOLDER, tid, 'meta.json')
    return row, (json.load(open(mp, encoding='utf-8')).get('engine') if os.path.isfile(mp) else None)


row, eng = run('openrouter', 'a.mp3')
t.check('app：openrouter 转写完成、记成 openrouter',
        row.get('status') == 'done' and eng == 'openrouter' and calls['or'] == 1, f"{row.get('error')} {eng}")

os.environ['GEMINI_API_KEY'] = 'fake-gemini'
or_fail[0] = True
row, eng = run('openrouter', 'b.mp3')
t.check('OR 失败 → 改走 Gemini API（同一个模型），不落 Whisper',
        row.get('status') == 'done' and eng == 'gemini' and calls['gemini'] == 1 and calls['whisper'] == 0,
        f"{row.get('status')} {eng} {row.get('error')} {calls}")

t.check('Gemini API 转写默认用 3.8 Flash（跟 OR 同一个模型）', transcribe_gemini.transcribe_model() == 'gemini-3.8-flash',
        transcribe_gemini.transcribe_model())

t.finish()
