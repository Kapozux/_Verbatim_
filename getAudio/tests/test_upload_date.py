"""单个链接转写、再建合集：上架日期不能丢。
2026-10-02 All-In 预测对账踩过：合集里那期没有 upload_date，预测按错的年份核对，「说中」全被降级，只能手动补日期。
原因：贴链接转写时，字幕路径和下载路径都拿到了上架日期，却都没写进转写的 meta；建合集只从 meta 里取。
跑生产代码：/api/transcribe_urls → _download_then_transcribe（字幕 / 下载两条路）→ 落盘的 meta；
/api/collections 建合集、/add 加内容 → _build_collection（缺日期的先补）。
桩（只在网络 / 模型边界）：downloader.fetch_subtitle / parse_srt / subtitle_usable / download_one / fetch_upload_date，
app.run_transcription（记下 extra_meta，不真转写）、enrich.enrich_task、analyze.analyze_episode / synthesize_collection、
ask._embed_texts；_auto_tag / _annotate_speakers / _review_episode_transcript 置空。"""
import json
import os
import time

from _support import Checks, fake_embed, isolate, make_transcript

isolate('uploaddate')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import downloader  # noqa: E402
import enrich  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
asked, transcribed = [], {}


def fake_fetch_subtitle(target, dl_dir, lang='auto'):
    if 'hassubs' not in target['video_url']:
        return None, None, None
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, 'x.srt')
    open(path, 'w').write('1\n00:00:01,000 --> 00:00:02,000\nhello\n')
    return path, 'manual', {'title': 'E1', 'duration': 2, 'sub_lang': 'en', 'video_id': 'subs00000001',
                            'upload_date': '20250115', 'uploader': 'All-In Podcast'}


def fake_download_one(item, dl_dir, section=None):
    os.makedirs(dl_dir, exist_ok=True)
    path = os.path.join(dl_dir, 'a.mp3')
    open(path, 'wb').write(b'\0')
    return {'path': path, 'title': 'E2', 'video_id': 'nosubs000001', 'uploader': 'All-In Podcast',
            'upload_date': '20250220'}


def fake_run_transcription(task_id, audio_path, engine, title, q, *a, **k):
    transcribed[task_id] = k.get('extra_meta') or {}
    A.taskdb.set_status(task_id, 'done')
    A.tasks.pop(task_id, None)


def fake_fetch_upload_date(url):
    asked.append(url)
    return '' if 'private' in url else '20250301'


downloader.fetch_subtitle = fake_fetch_subtitle
downloader.parse_srt = lambda p: [{'start': 1.0, 'end': 2.0, 'text': 'hello'}]
downloader.subtitle_usable = lambda segs, dur: (True, '')
downloader.download_one = fake_download_one
downloader.fetch_upload_date = fake_fetch_upload_date
A.run_transcription = fake_run_transcription
enrich.enrich_task = lambda *a, **k: None
analyze.analyze_episode = lambda title, text, author, **k: {
    'title': title, 'cards': [{'obs': 'x', 'quote': text[:20], 'timestamp': '00:00:01', 'layer': '他的主张'}], 'metrics': {}}
analyze.synthesize_collection = lambda eps, name, **k: '# 综述\n'
ask._embed_texts = fake_embed
A._auto_tag = lambda cdir: None
A._annotate_speakers = lambda cdir, eps: None
A._review_episode_transcript = lambda tid, preset=None: json.load(
    open(os.path.join(R, tid, 'transcript.json'), encoding='utf-8'))
c = A.app.test_client()


def wait_for(cond, limit=10.0):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def meta(tid):
    with open(os.path.join(R, tid, 'meta.json'), encoding='utf-8') as f:
        return json.load(f)


def chain(cid):
    with open(os.path.join(A._chain_dir(cid), 'chain.json'), encoding='utf-8') as f:
        return json.load(f)


# 1 字幕路径：查字幕那一次就拿到了上架日期和频道名，要写进 meta
r = c.post('/api/transcribe_urls', json={'urls': 'https://www.youtube.com/watch?v=hassubs1', 'engine': 'gemini35'})
t1 = r.get_json()['tasks'][0]['task_id']
done1 = wait_for(lambda: (A.taskdb.get(t1) or {}).get('status') == 'done' and os.path.isfile(os.path.join(R, t1, 'meta.json')))
m1 = meta(t1) if done1 else {}
t.check('字幕路径：上架日期、频道名写进转写的 meta', m1.get('upload_date') == '20250115'
        and m1.get('creator') == 'All-In Podcast', json.dumps(m1, ensure_ascii=False)[:300])

# 2 下载路径：下载结果里就有上架日期，要跟着交给转写写进 meta
r = c.post('/api/transcribe_urls', json={'urls': 'https://www.youtube.com/watch?v=nosubs1', 'engine': 'gemini35'})
t2 = r.get_json()['tasks'][0]['task_id']
wait_for(lambda: t2 in transcribed)
t.check('下载路径：上架日期交给转写落盘', transcribed.get(t2, {}).get('upload_date') == '20250220',
        f'{transcribed.get(t2)}')

# 3 以前转的、meta 里没日期的单条：建合集时先补上（问一次，不下载），合集和那条转写都记下
old = make_transcript(R, [('00:00:01', '我预测明年利率会降到三以下。')], title='All-In E3',
                      url='https://www.youtube.com/watch?v=old0000001')
cid = c.post('/api/collections', json={'name': 'All-In 2025', 'task_ids': [old, t1]}).get_json()['id']
built = wait_for(lambda: chain(cid).get('stage') == 'done')
vs = {v['task_id']: v for v in chain(cid)['videos']}
t.check('建合集：缺日期的那条补上了，有日期的不再问', built and vs[old].get('upload_date') == '20250301'
        and vs[t1].get('upload_date') == '20250115' and asked == ['https://www.youtube.com/watch?v=old0000001'],
        f'{[(k[:6], v.get("upload_date")) for k, v in vs.items()]} asked={asked}')
t.check('补到的日期写回那条转写，下次建合集直接用', meta(old).get('upload_date') == '20250301')
t.check('问答 / 预测核对拿到的是这一天', any(e.get('date') == '2025-03-01' for e in ask.load(A._chain_dir(cid))['episodes']),
        f'{[e.get("date") for e in ask.load(A._chain_dir(cid))["episodes"]]}')

# 4 问不到日期的（私密 / 删了）只问一次：项目每次刷新都重问会卡住建卡（yt-dlp 一次最多一分钟）
priv = make_transcript(R, [('00:00:01', '第二期。')], title='Private', url='https://www.youtube.com/watch?v=private001')
c.post(f'/api/collections/{cid}/add', json={'task_ids': [priv]})
wait_for(lambda: chain(cid).get('stage') == 'done' and any(v['task_id'] == priv for v in chain(cid)['videos']))
n = len(asked)
newer = make_transcript(R, [('00:00:01', '第三期。')], title='E5', url='https://www.youtube.com/watch?v=new0000005')
c.post(f'/api/collections/{cid}/add', json={'task_ids': [newer]})
wait_for(lambda: chain(cid).get('stage') == 'done' and any(v['task_id'] == newer for v in chain(cid)['videos']))
t.check('问不到的只问一次，下次加内容不再问它', asked[n:] == ['https://www.youtube.com/watch?v=new0000005']
        and asked.count('https://www.youtube.com/watch?v=private001') == 1, f'asked={asked}')
t.finish()
