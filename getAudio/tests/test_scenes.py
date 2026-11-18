"""画面描述（scenes.py + app 里的接线 + /api/scenes 接口）。

跑生产代码：scenes.scene_times / build / as_segments / queue（后台线程照常起）、frames 的解码和取帧（真 ffmpeg）、
/upload（没声音的视频不再拒收、勾「同时描述画面」的标记）、run_transcription 的没声音分支（真落盘、真 library）、
_keep_video_for_scenes、/api/scenes/<tid> 读 / 补做 / 取缩略图。
桩（只在边界）：frames._call（模型：按提示词里真实出现的编号造回答）、frames.download_video（返回本地合成视频）、
app.submit_transcription（上传那几条只记下提交）。
数据：ffmpeg 现做的无声视频：红 25 秒、蓝 5 秒、绿闪 0.5 秒、黄 6 秒；另一段带声音的。
"""
import io
import json
import os
import re
import subprocess
import time

from _support import Checks, isolate, make_transcript

TMP = isolate('scenes')
os.environ['SCENES_DEFAULT'] = '1'      # 本机 .env 里关掉了也照默认开来测；下面单独测 0
import app as A  # noqa: E402
import frames  # noqa: E402
import scenes  # noqa: E402

t = Checks()
if not os.path.exists(A.config.FFMPEG_BIN):
    t.skip('全部', '没装 ffmpeg')
    t.finish()
FF = A.config.FFMPEG_BIN

SILENT = os.path.join(TMP, 'silent.mp4')
subprocess.run([FF, '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=10:d=25',
                '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=10:d=5',
                '-f', 'lavfi', '-i', 'color=c=green:s=320x180:r=10:d=0.5',
                '-f', 'lavfi', '-i', 'color=c=yellow:s=320x180:r=10:d=6',
                '-filter_complex', '[0:v][1:v][2:v][3:v]concat=n=4:v=1[v]', '-map', '[v]',
                '-pix_fmt', 'yuv420p', SILENT], check=True)
LOUD = os.path.join(TMP, 'loud.mp4')
subprocess.run([FF, '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=160x90:r=10:d=4',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-shortest',
                '-pix_fmt', 'yuv420p', LOUD], check=True)

calls = []


SCREEN_TEXT = [False]     # 假模型这次是否「看到」画面上的字（字幕视频）


def fake_call(prompt, images):
    """按提示词里「Tile k = frame i」（第 k 张图）回答。画面上有字 → 相邻两格同一句字幕（要被并成一条）。"""
    tiles = [(int(k), int(i)) for k, i in re.findall(r'^Tile (\d+) = frame (\d+),', prompt, re.M)]
    ids = [i for _, i in tiles]
    lang = re.search(r'complete description in (.+?) of what', prompt)
    from PIL import Image
    calls.append({'ids': ids, 'n_img': len(images), 'lang': lang.group(1), 'cap': SCREEN_TEXT[0],
                  'grid_w': Image.open(images[0]).width if images else 0,
                  'speech': 'Red part' in prompt or 'Speech' in prompt, 'prev': 'just before this batch' in prompt,
                  'verbatim': 'Do not correct, translate, reword' in prompt})
    if SCREEN_TEXT[0]:
        return json.dumps({'scenes': [{'n': k, 'caption': f'字幕 {i // 2}', 'text': f'Pic {i}'} for k, i in tiles]})
    out = [{'n': k, 'caption': '', 'text': f'Scene {i}'} for k, i in tiles]
    out.append({'n': 99, 'text': 'made up'})          # 不在这张拼图里的格子要丢掉
    return json.dumps({'scenes': out})


frames._call = fake_call
frames.download_video = lambda url, dest: SILENT

# 1 挑帧：红 25 秒超过 20 秒补成两帧；绿闪 0.5 秒并进蓝；黄一帧 → 4 帧，时间连续
dur, fps = frames.probe(SILENT)
picks = scenes.scene_times(frames.sample_gray(SILENT, 2.0), 2.0)
print('1 挑帧：', round(dur, 1), [(p['start'], p['end'], p['t']) for p in picks])
t.check('挑帧：长镜头补帧、闪一下不算一段', len(picks) == 4 and picks[0]['end'] <= 13 and 25 <= picks[2]['start'] <= 25.5
        and 29 <= picks[2]['end'] <= 31 and picks[3]['end'] >= 35, str(picks))
t.check('挑帧：每帧落在自己那段里', all(p['start'] <= p['t'] <= p['end'] for p in picks))
old_max = scenes.MAX_SCENES
scenes.MAX_SCENES = 2
few = scenes.scene_times(frames.sample_gray(SILENT, 2.0), 2.0)
scenes.MAX_SCENES = old_max
t.check('挑帧：超上限均匀抽、时间范围合并', len(few) == 2 and few[0]['start'] == 0 and few[1]['end'] >= 35, str(few))

# 2 build（没有转写 = 没声音）：一批描述 4 帧、提示词说没声音、图缩略存好、视频不留；第二次不再调模型
TID0 = make_transcript(A.config.RESULTS_FOLDER, [], title='x')
os.remove(os.path.join(A.config.RESULTS_FOLDER, TID0, 'transcript.json'))
d = scenes.build(A.config.RESULTS_FOLDER, TID0, video_path=SILENT)
vdir = frames.visual_dir(A.config.RESULTS_FOLDER, TID0)
files = sorted(os.listdir(vdir))
n = len(calls)
again = scenes.build(A.config.RESULTS_FOLDER, TID0)
print('2 build：', [(s['start'], s['text'], s['img']) for s in d['scenes']], files, calls)
t.check('build：每帧一句、丢掉不在批里的编号', [s['text'] for s in d['scenes']] == [f'Scene {i}' for i in range(4)])
t.check('build：没声音默认写中文、单图读（一帧一张、768 宽）', calls[0]['lang'] == 'Chinese (Simplified)'
        and calls[0]['n_img'] == len(calls[0]['ids']) == 4 and scenes.READ_W == 768
        and calls[0]['grid_w'] == 320)     # 合成视频只有 320 宽：不放大
t.check('提示词：画面上的字照抄、不许改', calls[0]['verbatim'])
t.check('build：缩略图在、视频不留', len([f for f in files if f.startswith('s_')]) == 4 and 'scenes.json' in files
        and not any(f.endswith('.mp4') for f in files))
t.check('build：做过直接用', again == d and len(calls) == n)
segs = scenes.as_segments(d)
t.check('as_segments：时间戳 + 描述', segs[0]['timestamp'] == '00:00' and segs[2]['text'] == '［画面］Scene 2'
        and segs[2]['timestamp'] == '00:25', str(segs[:3]))
img = A.app.test_client().get(f"/api/scenes/{TID0}/{d['scenes'][0]['img']}")
bad = A.app.test_client().get(f'/api/scenes/{TID0}/scenes.json').status_code
t.check('缩略图接口：能取、乱名字 404', img.status_code == 200 and img.mimetype == 'image/jpeg' and bad == 404)

# 3 一批 8 帧：几批同时读、各读各的；有转写也不把说的话塞进去（会带偏），语言跟转写
calls.clear()
TID1 = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', '说话人1：Red part.'), ('00:00:26', 'Blue part.')],
                       title='talk', url='https://www.youtube.com/watch?v=abcdefghijk')
old_gap = scenes.MAX_GAP_S
scenes.MAX_GAP_S = 3.0
d1 = scenes.build(A.config.RESULTS_FOLDER, TID1, url='https://www.youtube.com/watch?v=abcdefghijk')
scenes.MAX_GAP_S = old_gap
print('3 两批：', [(c['ids'], c['prev'], c['speech']) for c in calls])
t.check('分批：几批各读各的（进共用读图池同时读，不再带上一批的最后一句）', len(calls) >= 2
        and not any(c['prev'] for c in calls) and all(len(c['ids']) <= scenes.BATCH for c in calls))
t.check('有转写：不带说的话、语言跟转写', not any(c['speech'] for c in calls) and calls[0]['lang'] == 'English')

# 4 上传：没声音的视频收下、标成只描述画面；带声音的勾了「同时描述画面」标 want_scenes；不勾什么都不加
submitted = []
real_submit = A.submit_transcription
A.submit_transcription = lambda engine, fn, *a, **k: submitted.append((a, k))
c = A.app.test_client()


def upload(path, name, **form):
    submitted.clear()
    r = c.post('/upload', data={'audio': (io.BytesIO(open(path, 'rb').read()), name), 'engine': 'gemini35', **form},
               content_type='multipart/form-data')
    return r.status_code, (submitted[0][1].get('extra_meta') if submitted else 'NOT SUBMITTED')


s_code, s_meta = upload(SILENT, 'screen.mp4')
l_code, l_meta = upload(LOUD, 'talk.mp4')                 # 11-16 起默认就做（没传 = 要）
p_code, p_meta = upload(LOUD, 'talk.mp4', scenes='0')
wav = os.path.join(TMP, 'a.wav')
subprocess.run([FF, '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', wav], check=True)
a_code, a_meta = upload(wav, 'a.wav')
print('4 上传：', (s_code, s_meta), (l_code, l_meta), (p_code, p_meta))
t.check('没声音的视频收下、只描述画面', s_code == 200 and s_meta == {'scenes_only': True})
t.check('视频默认就描述画面', l_code == 200 and l_meta == {'want_scenes': True})
t.check('明说不要（scenes=0）不加标记', p_code == 200 and not p_meta)
t.check('音频文件不加标记', a_code == 200 and not a_meta)
os.environ['SCENES_DEFAULT'] = '0'
_, off_meta = upload(LOUD, 'talk.mp4')
os.environ['SCENES_DEFAULT'] = '1'
t.check('SCENES_DEFAULT=0 时没传就不做', not off_meta)
r = c.post('/api/transcribe_local', json={'path': SILENT})
t.check('本机路径：没声音的视频也收下', r.status_code == 200 and submitted and submitted[-1][1]['extra_meta'] == {'scenes_only': True})
A.submit_transcription = real_submit

# 5 没声音的视频整条走一遍 run_transcription：正文 = 画面描述，引擎记 scenes，不存音频、不体检
import queue as _q  # noqa: E402
import taskdb  # noqa: E402
TID2 = '11111111-2222-3333-4444-555555555555'
src = os.path.join(A.config.UPLOAD_FOLDER, f'{TID2}.mp4')
with open(SILENT, 'rb') as fi, open(src, 'wb') as fo:
    fo.write(fi.read())
taskdb.create(TID2, 'screen.mp4', 'gemini35', None, src)
SCREEN_TEXT[0] = True          # 画面上有字：字幕 + 画面
q = _q.Queue()
A.tasks[TID2] = q
A.run_transcription(TID2, src, 'gemini35', 'screen.mp4', q, None, False, True,
                    extra_meta={'scenes_only': True}, summarize=False)
events = []
while not q.empty():
    events.append(json.loads(q.get()))
meta = json.load(open(os.path.join(A.config.RESULTS_FOLDER, TID2, 'meta.json')))
tr = json.load(open(os.path.join(A.config.RESULTS_FOLDER, TID2, 'transcript.json')))
print('5 整条：', taskdb.get(TID2)['status'], meta.get('engine'), meta.get('audio_ext'), [s['text'] for s in tr], [e['type'] for e in events][-3:])
caps = [s['text'].split('　')[0] for s in tr]
t.check('没声音：转完、引擎 scenes、正文是字幕 + 画面（字幕模式）', taskdb.get(TID2)['status'] == 'done' and meta['engine'] == 'scenes'
        and tr and all(re.match(r'字幕 \d+　［画面］Pic \d+$', s['text']) for s in tr) and events[-1]['type'] == 'done')
t.check('没声音：同一句字幕连着的并成一条', len(caps) == len(set(caps)) and len(tr) >= 3)
t.check('没声音：不存一份当音频、源文件删了', not meta.get('audio_ext') and not os.path.exists(src)
        and not [f for f in os.listdir(os.path.join(A.config.RESULTS_FOLDER, TID2)) if f.startswith('audio')])
t.check('没声音：scenes.json 也在', scenes.load(A.config.RESULTS_FOLDER, TID2) is not None)
SCREEN_TEXT[0] = False

# 6 勾了的视频：留一份（硬链接）→ 后台做 → 做完删掉留的那份
TID3 = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', 'Hello.')], title='upload')
up = os.path.join(A.config.UPLOAD_FOLDER, f'{TID3}.mp4')
with open(SILENT, 'rb') as fi, open(up, 'wb') as fo:
    fo.write(fi.read())
kept = A._keep_video_for_scenes(TID3, up)
os.remove(up)                                       # 转写那边照常删源文件
t.check('留视频：硬链接、源文件删了也还在', kept and os.path.exists(kept) and kept.endswith('_scenes.mp4'))
t.check('音频文件不留', A._keep_video_for_scenes(TID3, os.path.join(TMP, 'a.mp3')) is None)
queued = A._queue_scenes(TID3, video_path=kept)
dup = A._queue_scenes(TID3, video_path=None, url='https://example.com/v')
for _ in range(200):
    if scenes.status(A.config.RESULTS_FOLDER, TID3).get('status') in ('done', 'failed'):
        break
    time.sleep(0.05)
print('6 后台：', queued, dup, scenes.status(A.config.RESULTS_FOLDER, TID3))
t.check('后台做完、留的那份删掉、不重复排', queued and not dup and scenes.status(A.config.RESULTS_FOLDER, TID3)['status'] == 'done'
        and not os.path.exists(kept))
t.check('没视频也没链接不排', A._queue_scenes(make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', 'x')])) is False)

# 7 接口：有链接没做过 → can_make；补做 → 排队 → done 带描述；做过的 can_make 假；没链接的补做 400
TID4 = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', 'Hi.')], title='link',
                       url='https://www.youtube.com/watch?v=zzzzzzzzzzz')
before = c.get(f'/api/scenes/{TID4}').get_json()
made = c.post(f'/api/scenes/{TID4}').get_json()
for _ in range(200):
    after = c.get(f'/api/scenes/{TID4}').get_json()
    if after['status'] in ('done', 'failed'):
        break
    time.sleep(0.05)
nolink = c.post(f'/api/scenes/{TID3}').status_code
print('7 接口：', before, made, {k: v for k, v in after.items() if k != 'scenes'}, nolink)
t.check('接口：没做过、有链接 → 能补', before['status'] == '' and before['can_make'] and 'scenes' not in before)
t.check('接口：补做完带描述、不能再补', made.get('status') in ('queued', 'running', 'done') and after['status'] == 'done'
        and len(after['scenes']) == 4 and not after['can_make'])
t.check('接口：没链接的补不了', nolink == 400)
t.check('接口：乱 id 404', c.get('/api/scenes/../meta').status_code == 404)

# 8 字幕模式挑帧：背景不动、底下一行字幕每 2.5 秒换一次 → 普通模式只 1 帧，字幕模式一句一帧
from PIL import Image, ImageDraw  # noqa: E402
CAPV = os.path.join(TMP, 'caps.mp4')
lst = os.path.join(TMP, 'caps.txt')
with open(lst, 'w') as f:
    for k in range(6):
        p = os.path.join(TMP, f'cap{k}.png')
        im = Image.new('RGB', (640, 360), (40, 60, 80))
        ImageDraw.Draw(im).rectangle([120 + 20 * k, 310, 300 + 45 * k, 340], fill='white')     # 「字幕」：长短不一的白条
        im.save(p)
        f.write(f"file '{p}'\nduration 2.5\n")
    f.write(f"file '{p}'\n")
subprocess.run([FF, '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst,
                '-f', 'lavfi', '-i', 'sine=frequency=300:duration=15', '-t', '15', '-vf', 'fps=10',
                '-pix_fmt', 'yuv420p', '-shortest', CAPV], check=True)
gcap = frames.sample_gray(CAPV, 2.0)
plain, capped = scenes.scene_times(gcap, 2.0), scenes.scene_times(gcap, 2.0, captions=True)
print('8 字幕挑帧：', [p['t'] for p in plain], [(p['start'], p['end'], p['t']) for p in capped])
t.check('字幕模式：换一行字幕就是新的一帧', len(plain) == 1 and len(capped) == 6
        and all(k * 2.5 <= p['t'] < (k + 1) * 2.5 for k, p in enumerate(capped)), str(capped))
merged = scenes.merge_captions([{'start': 0, 'end': 1, 'caption': '你好，世界', 'text': 'a'},
                                {'start': 1, 'end': 2, 'caption': '你好 世界!', 'text': 'b'},
                                {'start': 2, 'end': 3, 'text': 'c'}, {'start': 3, 'end': 4, 'caption': '再见', 'text': 'd'}])
t.check('字幕去重：标点空格不同也算同一句、时间连起来', [(m['start'], m['end'], m.get('caption')) for m in merged]
        == [(0, 2, '你好，世界'), (2, 3, None), (3, 4, '再见')], str(merged))
t.check('正文一行：字幕在前、画面在后', scenes.scene_line({'caption': 'Hi', 'text': 'x'}) == 'Hi　［画面］x'
        and scenes.scene_line({'text': 'x'}) == '［画面］x')

# 9 话太少：每分钟不到 40 字（[音乐] 这类标注不算）
seg = lambda *xs: [{'text': x} for x in xs]  # noqa: E731
t.check('话太少的判断', scenes.speech_sparse(seg('[音乐]', '（音乐）'), 120) and scenes.speech_sparse([], 60)
        and scenes.speech_sparse(seg('嗯，好。'), 60) and not scenes.speech_sparse(seg('这' * 300), 60)
        and not scenes.speech_sparse(seg(''), 10))

# 10 有声音、转出白卷（只有配乐）→ 不落 Whisper / 别的引擎，改读画面字幕当正文
import transcribe_openrouter  # noqa: E402
import transcribe_gemini  # noqa: E402
other = []
transcribe_gemini.transcribe_audio = lambda *a, **k: other.append('gemini') or ([], '')
A._whisper_transcribe = None


def run(tid, fake_segments):
    transcribe_openrouter.transcribe_audio = lambda *a, **k: (fake_segments, '')
    src = os.path.join(A.config.UPLOAD_FOLDER, f'{tid}.mp4')
    with open(CAPV, 'rb') as fi, open(src, 'wb') as fo:
        fo.write(fi.read())
    taskdb.create(tid, 'music.mp4', 'openrouter', None, src)
    qq = _q.Queue()
    A.tasks[tid] = qq
    A.run_transcription(tid, src, 'openrouter', 'music.mp4', qq, None, False, False, summarize=False)
    m = json.load(open(os.path.join(A.config.RESULTS_FOLDER, tid, 'meta.json')))
    return m, json.load(open(os.path.join(A.config.RESULTS_FOLDER, tid, 'transcript.json')))


calls.clear()
SCREEN_TEXT[0] = True
TID5 = '22222222-2222-3333-4444-555555555555'
m5, tr5 = run(TID5, [])
print('10 白卷：', taskdb.get(TID5)['status'], m5['engine'], [s['text'] for s in tr5], other, (taskdb.get(TID5) or {}).get('error'))
t.check('白卷的视频：改读画面字幕，正文是字幕', taskdb.get(TID5)['status'] == 'done' and m5['engine'] == 'scenes'
        and tr5 and tr5[0]['text'].startswith('字幕 0') and calls and all(c['cap'] for c in calls))
t.check('白卷的视频：不再去试别的引擎', other == [])

# 10b 引擎直接回空文本（11-16 实测：Gemini 对着纯配乐回 finish_reason=STOP 的空文本，报的是普通错误）→ 也改读画面
def raise_empty(*a, **k):
    raise RuntimeError('返回空文本（finish_reason=stop）')


TID7 = '44444444-2222-3333-4444-555555555555'
real_run_tx = transcribe_openrouter.transcribe_audio
src7 = os.path.join(A.config.UPLOAD_FOLDER, f'{TID7}.mp4')
with open(CAPV, 'rb') as fi, open(src7, 'wb') as fo:
    fo.write(fi.read())
transcribe_openrouter.transcribe_audio = raise_empty
taskdb.create(TID7, 'music.mp4', 'openrouter', None, src7)
A.tasks[TID7] = _q.Queue()
A.run_transcription(TID7, src7, 'openrouter', 'music.mp4', A.tasks[TID7], None, False, False, summarize=False)
m7 = json.load(open(os.path.join(A.config.RESULTS_FOLDER, TID7, 'meta.json')))
print('10b 回空：', taskdb.get(TID7)['status'], m7['engine'], other)
t.check('引擎回空文本的视频：也改读画面', taskdb.get(TID7)['status'] == 'done' and m7['engine'] == 'scenes')

# 10c 只转一段（@开始-结束）的链接转出白卷：不拿整个视频的画面顶上（时间也会错位），照常算没转出来
TID8 = '55555555-2222-3333-4444-555555555555'
src8 = os.path.join(A.config.UPLOAD_FOLDER, f'{TID8}.mp4')
with open(CAPV, 'rb') as fi, open(src8, 'wb') as fo:
    fo.write(fi.read())
transcribe_openrouter.transcribe_audio = lambda *a, **k: ([], '')
taskdb.create(TID8, 'clip.mp4', 'openrouter', None, src8)
A.tasks[TID8] = _q.Queue()
n_calls = len(calls)
import transcribe_whisper as _tw  # noqa: E402
whisper_ran = []
real_tw, real_batt = _tw.transcribe_audio, A._on_battery
_tw.transcribe_audio = lambda *a, **k: whisper_ran.append(1) or []     # 白卷照旧落本机 Whisper：这里不真跑
A._on_battery = lambda: False                                          # 不看测试机插没插电
A.run_transcription(TID8, src8, 'openrouter', 'clip.mp4', A.tasks[TID8], None, False, False, offset_sec=600,
                    extra_meta={'source_url': 'https://example.com/v', 'clip': '*600-900'}, summarize=False)
_tw.transcribe_audio, A._on_battery = real_tw, real_batt
print('10c 片段白卷：', taskdb.get(TID8)['status'], len(calls) - n_calls, whisper_ran)
t.check('只转一段的白卷：不读整个视频的画面', len(calls) == n_calls
        and not os.path.exists(scenes.scenes_path(A.config.RESULTS_FOLDER, TID8)))

# 10d 模型回的 JSON 形状不对（根不是对象 / 条目不是对象）：跳过，不让整期失败
real_call = frames._call
one = [{'i': 0, 'start': 0, 'end': 1, 'img': b''}]
for bad in ('[1, 2]', '{"scenes": "nope"}', '{"scenes": [3, null, {"n": 1, "text": "ok"}]}'):
    frames._call = lambda prompt, images, bad=bad: bad
    try:
        got = scenes._read_batch(one, 'zh')
    except Exception as e:  # noqa: BLE001
        got = e
    t.check(f'模型回的形状不对不报错：{bad[:22]}', got == ({0: {'text': 'ok', 'caption': ''}} if '"ok"' in bad else {}), got)
frames._call = real_call

# 11 有声音、只转出一两句（配乐 + 字幕）：正文照存，不勾也自动在后台加画面轨（字幕模式）
calls.clear()
TID6 = '33333333-2222-3333-4444-555555555555'
m6, tr6 = run(TID6, [{'timestamp': '00:00', 'text': '嗯，好。'}])
for _ in range(200):
    if scenes.status(A.config.RESULTS_FOLDER, TID6).get('status') in ('done', 'failed'):
        break
    time.sleep(0.05)
d6 = scenes.load(A.config.RESULTS_FOLDER, TID6) or {}
print('11 话少：', m6['engine'], [s['text'] for s in tr6], scenes.status(A.config.RESULTS_FOLDER, TID6), d6.get('captions'))
t.check('话少：正文照存、自动加画面轨（字幕模式）', m6['engine'] == 'openrouter' and tr6[0]['text'] == '嗯，好。'
        and d6.get('captions') is True and d6.get('scenes'))
t.check('话少：留的视频做完删掉', not [n for n in os.listdir(A.config.UPLOAD_FOLDER) if n.startswith(TID6)])

# 12 大批量默认不读画面（11-16 用户：「大批量也不是会看图，默认去掉读图」）：没明确传 scenes、一次超过 SCENES_BULK 条 → 不读
t.check('一次几条：没传时 ≤10 条读、>10 条不读；明确要 / 不要照办',
        A._want_scenes(None, 10) and not A._want_scenes(None, 11) and A._want_scenes('1', 50) and not A._want_scenes('0', 1)
        and A._want_scenes(None))
seen = []
real_submit, real_probe = A.submit_transcription, A._subtitle_probe_executor.submit
A.submit_transcription = lambda *a, **k: seen.append(k.get('scenes'))
A._subtitle_probe_executor.submit = lambda *a, **k: seen.append(k.get('scenes'))
cl = A.app.test_client()
links = lambda n: '\n'.join(f'https://www.youtube.com/watch?v=abcdefghij{k % 10}' for k in range(n))
cl.post('/api/transcribe_urls', json={'urls': links(3)})
few = list(seen); seen.clear()
cl.post('/api/transcribe_urls', json={'urls': links(12)})
many = list(seen); seen.clear()
cl.post('/api/transcribe_urls', json={'urls': links(12), 'scenes': True})
forced = list(seen); seen.clear()
A.submit_transcription, A._subtitle_probe_executor.submit = real_submit, real_probe
print('12 大批量：', few, many[:3], forced[:3])
t.check('链接：3 条读画面、12 条默认不读、12 条明确要就读', few == [True] * 3 and many == [False] * 12 and forced == [True] * 12)

# 13 转写引擎在纯配乐上编话（11-16 真事：讲 Google 的配乐 + 字幕视频，前 5 分钟空白，最后 2 分 40 秒编出 44 句情话，
#    本机 Whisper 听同一段判「没人说话」）→ 画面上的字多、话只集中在一小段、跟画面字对不上 → 正文换成画面上的字
import summarize as _summ  # noqa: E402
import enrich as _enr  # noqa: E402
_summ.summarize_transcript = lambda text: {'overview': 'OV:' + text[:20], 'sections': []}
_enr.enrich_task = lambda task_dir: True
RD = A.config.RESULTS_FOLDER
SLIDES = ['AlphaFold 能算出蛋白质折成什么形状', '两亿多个结构全世界免费用', '谷歌大脑和 DeepMind 合并', 'Gemini 已有十亿用户',
          '打开摄像头它陪你一起看', '一个人也能拍一部短片', '数据中心遍布四十三个区域', '量子计算五分钟算完']


def scenes_doc(tid, dur):
    step = dur / len(SLIDES)
    d = {'version': scenes.VERSION, 'task_id': tid, 'duration': dur, 'captions': True, 'picked': len(SLIDES),
         'scenes': [{'start': round(k * step, 1), 'end': round((k + 1) * step, 1), 'img': '', 'text': '一张幻灯片',
                     'caption': f'{c} 标题{k} 要点一 要点二 要点三 {c} {c}'}
                    for k, c in enumerate(SLIDES)]}
    os.makedirs(os.path.join(RD, tid, 'visual'), exist_ok=True)
    json.dump(d, open(scenes.scenes_path(RD, tid), 'w'), ensure_ascii=False)
    return d


love = [(f'00:0{5 + k // 20}:{(15 + k * 3) % 60:02d}', l) for k, l in enumerate(
    ['我真的真的好想你', '也真的好想去看看你', '可我也知道', '我再也没有身份去找你', '以前打扰了以后不会了', '我想你却不能去找你'] * 3)]
T13 = make_transcript(RD, love, title='为什么只有Google是最伟大的科技公司', engine='openrouter')
m13 = json.load(open(os.path.join(RD, T13, 'meta.json'))); m13['duration_seconds'] = 476
json.dump(m13, open(os.path.join(RD, T13, 'meta.json'), 'w'))
d13 = scenes_doc(T13, 476)
why = scenes.speech_doubtful(json.load(open(os.path.join(RD, T13, 'transcript.json'))), 476, d13)
done13 = scenes.use_captions_as_body(RD, T13)
tr13 = json.load(open(os.path.join(RD, T13, 'transcript.json')))
mm13 = json.load(open(os.path.join(RD, T13, 'meta.json')))
kept = json.load(open(os.path.join(RD, T13, 'transcript.speech.json')))
sm13 = json.load(open(os.path.join(RD, T13, 'summary.json')))
print('13 编的话：', why, done13, [s['text'][:16] for s in tr13[:2]], mm13.get('engine'), mm13.get('speech_engine'), sm13)
t.check('编的话：判成可疑（画面字多、话只在一小段、跟画面字对不上）', bool(why))
t.check('编的话：正文换成画面上的字、引擎记 scenes、原来的话另存不删', done13 and tr13[0]['text'].startswith(SLIDES[0])
        and mm13['engine'] == 'scenes' and mm13['speech_engine'] == 'openrouter' and mm13.get('speech_dropped')
        and len(kept) == len(love) and mm13['segment_count'] == len(tr13))
t.check('编的话：摘要按新正文重写', sm13['overview'].startswith('OV:[00:00] AlphaFold'))
t.check('编的话：再跑一次不重复换', scenes.use_captions_as_body(RD, T13) is False)

# 13b 摘要重写失败：写着那段编出来的话的旧摘要不能留
T13b = make_transcript(RD, love, title='旧摘要', engine='openrouter')
mb = json.load(open(os.path.join(RD, T13b, 'meta.json'))); mb.update(duration_seconds=476, has_summary=True)
json.dump(mb, open(os.path.join(RD, T13b, 'meta.json'), 'w'))
json.dump({'overview': '一段情话'}, open(os.path.join(RD, T13b, 'summary.json'), 'w'), ensure_ascii=False)
scenes_doc(T13b, 476)


def summ_down(text):
    raise RuntimeError('model down')


_summ.summarize_transcript = summ_down
ok13b = scenes.use_captions_as_body(RD, T13b)
_summ.summarize_transcript = lambda text: {'overview': 'OV:' + text[:20], 'sections': []}
t.check('编的话：摘要重写失败就删掉旧摘要', ok13b and not os.path.exists(os.path.join(RD, T13b, 'summary.json'))
        and json.load(open(os.path.join(RD, T13b, 'meta.json'))).get('has_summary') is False)

# 话跟画面字幕对得上（真在说话、字幕是说的话）→ 不动
T14 = make_transcript(RD, [(f'00:0{5 + k // 2}:{k % 2 * 30:02d}', c) for k, c in enumerate(SLIDES)], engine='openrouter')
d14 = scenes_doc(T14, 476)
t.check('话跟画面字对得上：不动', scenes.speech_doubtful(json.load(open(os.path.join(RD, T14, 'transcript.json'))), 476, d14) is None
        and scenes.use_captions_as_body(RD, T14) is False)
# 从头讲到尾、内容跟幻灯片不一样（讲课）→ 不动
lecture = [(f'00:{k // 2:02d}:{k % 2 * 30:02d}', f'今天我们讲第{k}个问题，大家注意这个推导过程') for k in range(16)]
T15 = make_transcript(RD, lecture, engine='openrouter')
d15 = scenes_doc(T15, 476)
t.check('从头讲到尾（讲课配幻灯片）：不动', scenes.speech_doubtful(json.load(open(os.path.join(RD, T15, 'transcript.json'))), 476, d15) is None)
# 画面上没多少字 → 不动（没东西可换）
T16 = make_transcript(RD, love, engine='openrouter')
d16 = scenes_doc(T16, 476)
for sc in d16['scenes']:
    sc.pop('caption')
t.check('画面上没字：不动', scenes.speech_doubtful(love and json.load(open(os.path.join(RD, T16, 'transcript.json'))), 476, d16) is None)

t.finish()
