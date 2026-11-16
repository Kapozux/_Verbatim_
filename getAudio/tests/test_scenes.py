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


def fake_call(prompt, images):
    ids = [int(x) for x in re.findall(r'^Frame (\d+) ', prompt, re.M)]
    calls.append({'ids': ids, 'n_img': len(images), 'lang': re.search(r'Write in ([^.]+)\.', prompt).group(1),
                  'speech': 'Red part' in prompt or 'Speech' in prompt, 'prev': 'just before this batch' in prompt})
    out = [{'i': i, 'text': f'Scene {i}'} for i in ids]
    out.append({'i': 999, 'text': 'made up'})          # 不在这批里的编号要丢掉
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
t.check('build：没声音默认写中文、一批 4 张图', calls[0]['lang'] == 'Chinese (Simplified)' and calls[0]['n_img'] == 4)
t.check('build：缩略图在、视频不留', len([f for f in files if f.startswith('s_')]) == 4 and 'scenes.json' in files
        and not any(f.endswith('.mp4') for f in files))
t.check('build：做过直接用', again == d and len(calls) == n)
segs = scenes.as_segments(d)
t.check('as_segments：时间戳 + 描述', segs[0]['timestamp'] == '00:00' and segs[2]['text'] == 'Scene 2'
        and segs[2]['timestamp'] == '00:25', str(segs[:3]))
img = A.app.test_client().get(f"/api/scenes/{TID0}/{d['scenes'][0]['img']}")
bad = A.app.test_client().get(f'/api/scenes/{TID0}/scenes.json').status_code
t.check('缩略图接口：能取、乱名字 404', img.status_code == 200 and img.mimetype == 'image/jpeg' and bad == 404)

# 3 一批 8 帧：第二批带上一批最后一句；有转写也不把说的话塞进去（会带偏），语言跟转写
calls.clear()
TID1 = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', '说话人1：Red part.'), ('00:00:26', 'Blue part.')],
                       title='talk', url='https://www.youtube.com/watch?v=abcdefghijk')
old_gap = scenes.MAX_GAP_S
scenes.MAX_GAP_S = 3.0
d1 = scenes.build(A.config.RESULTS_FOLDER, TID1, url='https://www.youtube.com/watch?v=abcdefghijk')
scenes.MAX_GAP_S = old_gap
print('3 两批：', [(c['ids'], c['prev'], c['speech']) for c in calls])
t.check('分批：第二批带上一批的最后一句', len(calls) >= 2 and not calls[0]['prev'] and calls[1]['prev']
        and all(len(c['ids']) <= scenes.BATCH for c in calls))
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
l_code, l_meta = upload(LOUD, 'talk.mp4', scenes='1')
p_code, p_meta = upload(LOUD, 'talk.mp4')
print('4 上传：', (s_code, s_meta), (l_code, l_meta), (p_code, p_meta))
t.check('没声音的视频收下、只描述画面', s_code == 200 and s_meta == {'scenes_only': True})
t.check('勾了同时描述画面', l_code == 200 and l_meta == {'want_scenes': True})
t.check('不勾不加标记', p_code == 200 and not p_meta)
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
q = _q.Queue()
A.tasks[TID2] = q
A.run_transcription(TID2, src, 'gemini35', 'screen.mp4', q, None, False, True,
                    extra_meta={'scenes_only': True}, summarize=False)
events = []
while not q.empty():
    events.append(json.loads(q.get()))
meta = json.load(open(os.path.join(A.config.RESULTS_FOLDER, TID2, 'meta.json')))
tr = json.load(open(os.path.join(A.config.RESULTS_FOLDER, TID2, 'transcript.json')))
print('5 整条：', taskdb.get(TID2)['status'], meta.get('engine'), meta.get('audio_ext'), len(tr), [e['type'] for e in events][-3:])
t.check('没声音：转完、引擎 scenes、正文是画面描述', taskdb.get(TID2)['status'] == 'done' and meta['engine'] == 'scenes'
        and [s['text'] for s in tr] == [f'Scene {i}' for i in range(4)] and events[-1]['type'] == 'done')
t.check('没声音：不存一份当音频、源文件删了', not meta.get('audio_ext') and not os.path.exists(src)
        and not [f for f in os.listdir(os.path.join(A.config.RESULTS_FOLDER, TID2)) if f.startswith('audio')])
t.check('没声音：scenes.json 也在', scenes.load(A.config.RESULTS_FOLDER, TID2) is not None)

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

t.finish()
