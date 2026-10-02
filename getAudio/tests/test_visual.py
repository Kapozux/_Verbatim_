"""画面证据卡（frames.py + study.start_visual + /visual、/studio/visual 接口）。

跑生产代码：frames.keyframes / content_mask / dhash / contact_sheets / said_during / build / run_batch（真 ffmpeg 解码）、
study.start_visual（后台线程照常起）、/api/chain/<id>/visual/episodes、/studio/visual、/api/visual/<tid>/<图>。
桩（只在边界）：frames._call（模型：按提示词里真实出现的编号造回答）、frames.download_video（返回本地合成视频）。
数据：ffmpeg 现做一段 16 秒视频——三页「幻灯片」A、B、C 再回到 A，右下角一个一直在动的方块当讲的人；一期合成转写。
"""
import os
import re
import subprocess
import time

from _support import Checks, isolate, make_transcript

TMP = isolate('visual')
import app as A  # noqa: E402
import frames  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ok = {}

# ---------- 合成视频：A(0-4) B(4-8) C(8-12) A(12-16)，方块在右下角来回跑 ----------
VID = os.path.join(TMP, 'lecture.mp4')
slides = []
for name, lines in (('A', 6), ('B', 3), ('C', 9), ('A', 6)):
    p = os.path.join(TMP, f'slide_{len(slides)}.png')
    img = Image.new('RGB', (640, 360), 'white')
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 620, 70], fill={'A': (30, 60, 160), 'B': (160, 40, 40), 'C': (20, 120, 60)}[name])
    for j in range(lines):
        d.rectangle([40, 90 + j * 28, 40 + 60 * ((j * 7 + ord(name)) % 9 + 1), 104 + j * 28], fill=(20, 20, 20))
    img.save(p)
    slides.append(p)
concat = os.path.join(TMP, 'list.txt')
with open(concat, 'w') as f:
    for p in slides:
        f.write(f"file '{p}'\nduration 4\n")
    f.write(f"file '{slides[-1]}'\n")
subprocess.run(['ffmpeg', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', concat,
                '-f', 'lavfi', '-i', 'color=c=orange:s=70x70:r=30',
                '-filter_complex', "[0:v]fps=30,format=yuv420p[bg];[bg][1:v]overlay=x='500+60*sin(t*5)':y=270:shortest=1",
                '-t', '16', '-pix_fmt', 'yuv420p', VID], check=True)

# 1 数值过一遍：三张不同的关键帧，A 记两段时间；动的方块不算内容、不额外起关键帧
dur, native = frames.probe(VID)
g = frames.sample_gray(VID, 2.0)
mask = frames.content_mask(g)
keys = frames.keyframes(g, 2.0, mask)
print('1 关键帧：', round(dur, 1), native, g.shape, '| 内容区域', round(float(mask.mean()), 2), '|', keys)
corner_masked = not mask[int(frames.GH * 0.8), int(frames.GW * 0.85)]
ok['keyframes'] = (len(keys) == 3 and len(keys[0]['shown']) == 2 and keys[0]['shown'][1][0] >= 11.5
                   and 3 <= keys[0]['t'] < 4 and corner_masked and abs(dur - 16) < 0.5)

# 2 跟转写对上：只拿画面显示那段（往前多 15 秒）说的话
segs = [(0.0, 'intro'), (20.0, 'later'), (40.0, 'much later')]
ok['said'] = frames.said_during(segs, [[30, 35]]) == 'later' and frames.said_during(segs, [[2, 4]]) == 'intro'

# ---------- 一期 + 项目 ----------
TID = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', '说话人1：Slide A is the setup.'),
                                                ('00:00:04', 'Slide B shows the result.'),
                                                ('00:00:08', 'Slide C is the method.'),
                                                ('00:00:12', 'Back to A to wrap up.')],
                      title='Lecture', url='https://www.youtube.com/watch?v=abcdefghijk')
NOURL = make_transcript(A.config.RESULTS_FOLDER, [('00:00:00', 'Just audio.')], title='Upload')
calls = []


def fake_call(prompt, images):
    calls.append((prompt[:40], len(images)))
    if 'Pick the frames' in prompt:
        ids = [int(x) for x in re.findall(r'#(\d+)', prompt.split('Frames on this sheet:')[1])]
        return '{"keep": [%s]}' % ', '.join(str(i) for i in ids if i != 1)      # #1（B 页）当成没信息的
    frames_in = [int(x) for x in re.findall(r'^Frame (\d+) ', prompt, re.M)]
    said = re.findall(r'^Speaker at that time: (.*)$', prompt, re.M)
    cards = [{'i': i, 'kind': 'slide', 'title': f'Title {i}', 'text': f'TEXT {i}', 'desc': 'd', 'obs': s[:30]}
             for i, s in zip(frames_in, said)]
    cards.append({'i': 99, 'kind': 'slide', 'title': 'made up'})                 # 不在这批里的编号要丢掉
    import json
    return json.dumps({'cards': cards})


frames._call = fake_call
frames.download_video = lambda url, dest: VID

# 3 build：两遍模型（缩略图一张 + 读图一批），留下 2 张卡、图存在 visual/、视频不留；第二次直接用、不再调模型
d = frames.build(A.config.RESULTS_FOLDER, TID, url='https://www.youtube.com/watch?v=abcdefghijk')
vdir = frames.visual_dir(A.config.RESULTS_FOLDER, TID)
files = sorted(os.listdir(vdir))
n_calls = len(calls)
again = frames.build(A.config.RESULTS_FOLDER, TID)
print('3 build：', [(c['id'], c['title'], c['at'], c['img'], c['obs']) for c in d['cards']], '| 文件', files,
      '| 调用', calls, '| 第二次多调', len(calls) - n_calls)
ok['build'] = (len(d['cards']) == 2 and [c['title'] for c in d['cards']] == ['Title 0', 'Title 2']
               and d['keyframes'] == 3 and d['triaged'] == 2 and n_calls == 2 and calls[0][1] == 1 and calls[1][1] == 2
               and 'Slide A' in d['cards'][0]['said'] and '说话人' not in d['cards'][0]['said']
               and len([f for f in files if f.endswith('.jpg')]) == 2 and 'cards.json' in files
               and not any(f.endswith('.mp4') for f in files) and again == d and len(calls) == n_calls)

# 4 接口：清单（有链接的能做、没链接的标出来、做过的估价 0）→ 做一份 → 列表里一条 visual，带卡和费用字段；图能取、乱名字 404
c = A.app.test_client()
PID = c.post('/api/projects', json={'name': 'Visual'}).get_json()['id']
c.post(f'/api/chain/{PID}/sources/transcripts', json={'task_ids': [TID, NOURL]})
eps = c.get(f'/api/chain/{PID}/visual/episodes').get_json()['episodes']
by = {e['task_id']: e for e in eps}
print('4 清单：', [(e['title'], e['has_video'], e['done'], e['n'], e['est_usd']) for e in eps])
r = c.post(f'/api/chain/{PID}/studio/visual', json={'task_ids': [TID, NOURL, 'not-in-project']}).get_json()
empty = c.post(f'/api/chain/{PID}/studio/visual', json={'task_ids': [NOURL]}).status_code
o = {}
for _ in range(100):
    o = c.get(f"/api/chain/{PID}/studio/{r['item']['id']}").get_json()
    if o['status'] != 'running':
        break
    time.sleep(0.05)
items = c.get(f'/api/chain/{PID}/studio').get_json()['items']
img = c.get(f"/api/visual/{TID}/{d['cards'][0]['img']}")
bad = [c.get(f'/api/visual/{TID}/../meta.json').status_code, c.get(f'/api/visual/{TID}/cards.json').status_code]
print('   一份：', o['status'], o.get('n'), o.get('tasks'), [(e['title'], e.get('cached'), len(e['cards'])) for e in o['result']['episodes']],
      '| 费用', o.get('cost_usd'), '| 只选没链接的', empty, '| 图', img.status_code, img.mimetype, '| 乱名字', bad)
ok['api'] = (by[TID]['has_video'] and by[TID]['done'] and by[TID]['est_usd'] == 0 and not by[NOURL]['has_video']
             and o['status'] == 'done' and o['tasks'] == [TID] and o['n'] == 2 and o['result']['episodes'][0]['cached']
             and o.get('cost_usd') == 0 and 'progress' not in o and empty == 400
             and any(i['id'] == o['id'] and i['kind'] == 'visual' for i in items)
             and img.status_code == 200 and img.mimetype == 'image/jpeg' and bad[1] == 404 and bad[0] == 404)

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
