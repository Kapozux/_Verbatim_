"""转写列表和搜索走 SQLite 索引（library.db），结果跟原来逐个读文件一样。
2026-10-03 用户：「你就不能缓存吗」「如果 SQLite 已经有时间复杂度快的算法就直接用」。
原来每次列转写都读 3410 份 meta.json（0.1 秒），搜正文逐个读 140 MB 的 transcript.json（0.6–1.2 秒）。
跑生产代码：GET /api/history、/api/search；写入点 _save_results、_update_meta、_save_subtitle_task_inner、
_review_episode_transcript、enrich.enrich_task、DELETE /api/history/<id>、DELETE /api/history；对账 library.sync()。
桩（只在模型边界）：enrich.generate_card_meta、analyze.review_transcript、taskdb 不桩（临时库）。
对照：old_search / old_history 是原来逐个读文件的写法原样搬过来，新结果逐条跟它比。"""
import builtins
import json
import os
import time
import uuid

from _support import Checks, isolate, make_transcript, write_json

isolate('library')
import analyze  # noqa: E402
import app as A  # noqa: E402
import enrich  # noqa: E402
import library  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
c = A.app.test_client()
enrich.generate_card_meta = lambda filename, content: {'title': '补出来的标题', 'one_line': '一句话', 'tags': ['新标签'],
                                                       'filename_meaningful': False}
A.probe_audio_duration_seconds = lambda p: 9.0
A._keep_audio = lambda: False
SAVED, SUBT = str(uuid.uuid4()), str(uuid.uuid4())
AUDIO = os.path.join(os.path.dirname(R), 'x.m4a')
open(AUDIO, 'wb').write(b'\0')


def transcript(segs, title, date, **meta):
    tid = make_transcript(R, segs, title=title, tid=str(uuid.uuid4()))   # 真实格式：删除接口只认 UUID
    p = os.path.join(R, tid, 'meta.json')
    m = json.load(open(p, encoding='utf-8'))
    m.update(date=date, **meta)
    write_json(p, m)
    return tid


# ---- 原来的写法（对照）----
def old_history():
    out = []
    for name in os.listdir(R):
        mp = os.path.join(R, name, 'meta.json')
        if os.path.isfile(mp):
            try:
                out.append(json.load(open(mp, encoding='utf-8')))
            except Exception:  # noqa: BLE001
                continue
    out.sort(key=lambda e: e.get('date', ''), reverse=True)
    return out


def old_search(query):
    query = query.lower()
    hits = {}
    for name in os.listdir(R):
        mp = os.path.join(R, name, 'meta.json')
        if not os.path.isfile(mp):
            continue
        try:
            meta = json.load(open(mp, encoding='utf-8'))
        except Exception:  # noqa: BLE001
            continue
        snippet = ''
        hay = [meta.get('filename', ''), meta.get('ai_title', ''), meta.get('ai_one_line', ''),
               ' '.join(meta.get('ai_tags', []) or []), meta.get('video_id', '') or '', meta.get('source_url', '') or '']
        matched = any(query in h.lower() for h in hay if h)
        if not matched:
            tp = os.path.join(R, name, 'transcript.json')
            if os.path.isfile(tp):
                try:
                    for s in json.load(open(tp, encoding='utf-8')):
                        text = s.get('text', '')
                        idx = text.lower().find(query)
                        if idx != -1:
                            start = max(0, idx - 20)
                            snippet = f"[{s.get('timestamp', '')}] ...{text[start:idx + len(query) + 40]}..."
                            matched = True
                            break
                except Exception:  # noqa: BLE001
                    pass
        if matched:
            hits[name] = snippet
    return hits


def new_search(q):
    return {e['id']: e['snippet'] for e in c.get('/api/search', query_string={'q': q}).get_json()}


QUERIES = ['利率', '美联储加息', 'NVIDIA', 'nvidia', '50%', 'a_b', '标题里才有', '00:01', '第二句',
           '不存在的词组', 'x', '长句里的关键词', '跨\n行', 'tag-only', 'é']


def same_as_files(label):
    bad = []
    for q in QUERIES:
        old, new = old_search(q), new_search(q)
        if old != new:
            bad.append(f'{q!r}: 原来 {sorted(old.items())[:3]} 现在 {sorted(new.items())[:3]}')
    t.check(label, not bad, '；'.join(bad)[:600])


# ---- 资料库 ----
a = transcript([('00:00:01', '今年美联储加息之后，利率会怎么走？'), ('00:00:05', '第二句：NVIDIA 的财报。')],
               '利率节目', '2026-09-01 10:00:00')
b = transcript([('00:01:00', '回撤 50% 也不卖'), ('00:01:30', '变量名 a_b 不是通配符')], 'Episode B', '2026-09-02 10:00:00')
d = transcript([('00:00:02', '这里什么都没有')], '只在标题里才有', '2026-09-03 10:00:00', ai_tags=['tag-only'])
e = transcript([('00:00:03', '换行的' + '\n' + '一句话 Nvidia')], 'E', '2026-09-04 10:00:00')
f = transcript([('00:00:04', 'Café résumé É')], 'F', '2026-09-05 10:00:00')
long = transcript([('00:02:00', '前面' * 20 + '长句里的关键词' + '后面的话' * 20)], 'Long', '2026-09-05 11:00:00')
broken = os.path.join(R, 'notjson0000000000000000000000000')
os.makedirs(broken)
open(os.path.join(broken, 'meta.json'), 'w').write('{坏的')

library.sync()
new, old = c.get('/api/history').get_json(), old_history()
t.check('列表跟逐个读文件一样（内容、从新到旧）', [{k: v for k, v in x.items() if k != 'source'} for x in new] == old,
        f'{[x["id"][:6] for x in new]} vs {[x["id"][:6] for x in old]}')
same_as_files('搜索结果、命中片段跟逐个读文件一样')

# ---- 建好索引以后：列表和搜索不再读 meta.json / transcript.json ----
opened = []
real_open = builtins.open


def spy_open(p, *a, **k):
    if str(p).startswith(R) and str(p).endswith(('meta.json', 'transcript.json')):
        opened.append(p)
    return real_open(p, *a, **k)


builtins.open = spy_open
try:
    c.get('/api/history?limit=12')
    c.get('/api/history')
    c.get('/api/search', query_string={'q': '美联储加息'})
    c.get('/api/search', query_string={'q': '利率'})
finally:
    builtins.open = real_open
t.check('列表和搜索都走索引，不读转写文件', not opened, f'读了 {len(opened)} 次：{opened[:3]}')

# ---- 写入点：写完马上反映，不用等对账 ----
library.RESYNC_S = 1e9                                   # 关掉定时对账，只看写入点有没有通知

A._update_meta(os.path.join(R, a, 'meta.json'), {'ai_title': '改过的标题'})
t.check('改标题（_update_meta）马上生效', any(x['id'] == a and x.get('ai_title') == '改过的标题'
                                       for x in c.get('/api/history').get_json()))

A._save_results(SAVED, '新录音.m4a', 'whisper', AUDIO,
                [{'timestamp': '00:00:09', 'text': '新转写里有一句独角兽公司'}], None)
t.check('新转写（_save_results）马上能列出、能搜到', new_search('独角兽公司').get(SAVED, '')
        .startswith('[00:00:09]'), f'{new_search("独角兽公司")}')

A._save_subtitle_task_inner(SUBT, {'video_url': 'https://youtu.be/x', 'title': 'Sub'},
                            [{'timestamp': '00:00:01', 'text': '字幕里说到量子退火'}], 'manual', 'zh', None, False)
t.check('字幕直取（_save_subtitle_task_inner）马上能搜到', SUBT in new_search('量子退火'))

analyze.review_transcript = lambda segs, preset=None: {1}
A._review_episode_transcript(b)
t.check('体检删掉的句子（_review_episode_transcript）搜不到了', b not in new_search('不是通配符')
        and b in new_search('也不卖'))

m = json.load(open(os.path.join(R, d, 'meta.json'), encoding='utf-8'))
m.pop('ai_title', None)
write_json(os.path.join(R, d, 'meta.json'), m)
enrich.enrich_task(os.path.join(R, d))
t.check('补标题标签（enrich_task）马上能按新标签搜到', d in new_search('新标签'))

c.delete(f'/api/history/{e}')
t.check('删一条（DELETE /api/history/<id>）马上从列表和搜索里消失',
        e not in [x['id'] for x in c.get('/api/history').get_json()] and e not in new_search('一句话 nvidia'))
same_as_files('一连串写入之后，搜索仍跟逐个读文件一样')

# ---- 绕过写入点直接改文件（脚本、手工）：对账以后跟上 ----
g = transcript([('00:00:01', '脚本直接写进来的转写提到稀土')], '脚本写的', '2026-09-06 10:00:00')
time.sleep(0.01)
m = json.load(open(os.path.join(R, f, 'meta.json'), encoding='utf-8'))
m['ai_title'] = '手工改的标题'
write_json(os.path.join(R, f, 'meta.json'), m)
os.utime(os.path.join(R, f, 'meta.json'), (time.time() + 5, time.time() + 5))
import shutil  # noqa: E402
shutil.rmtree(os.path.join(R, d))
library.sync()
ids = [x['id'] for x in c.get('/api/history').get_json()]
t.check('对账：直接写进来的、手工改的、手工删的都跟上',
        g in ids and d not in ids and any(x['id'] == f and x.get('ai_title') == '手工改的标题'
                                          for x in c.get('/api/history').get_json()) and g in new_search('稀土'))
same_as_files('对账之后，搜索仍跟逐个读文件一样')

# ---- 定时对账：超过间隔再打开列表时在后台跑，不让这次请求等 ----
h = transcript([('00:00:01', '没通知过的一条')], '没通知', '2026-09-07 10:00:00')
library.RESYNC_S = 0
c.get('/api/history')                                   # 触发后台对账
ok = False
for _ in range(100):
    if h in [x['id'] for x in c.get('/api/history').get_json()]:
        ok = True
        break
    time.sleep(0.05)
library.RESYNC_S = 1e9
t.check('超过对账间隔：后台对账把没通知过的新转写补进来', ok)

# ---- 正文还没进索引（第一次建索引期间）：搜索退回读文件，结果不少 ----
for suf in ('', '-wal', '-shm'):                        # 当成第一次装上：索引库还不存在
    if os.path.exists(library.DB_PATH + suf):
        os.remove(library.DB_PATH + suf)
library.sync(text=False)
same_as_files('正文索引还没建好时，搜索结果也跟逐个读文件一样')
library.sync()

# ---- 清空 ----
c.delete('/api/history')
t.check('全部删除（DELETE /api/history）后列表为空', c.get('/api/history').get_json() == [])
t.finish()
