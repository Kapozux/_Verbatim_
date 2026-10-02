"""大总结每月最多自动重写一次（2026-10-02 用户：「月度、总数这种大的，不要每次我传一个就更新」）。
跑生产代码：reflect.build / refresh_stale（回顾面板四个时段的叙事）、项目加录音 → _project_refresh → 综述。
桩（只在模型边界）：reflect 的后台重算排队（记下排了谁）、analyze.analyze_episode / synthesize_collection、向量。
另外核对摘要 / 标题标签用的是便宜模型。"""
import json
import os
import time

from _support import Checks, fake_embed, isolate, make_transcript, write_json

TMP = isolate('monthly')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import config  # noqa: E402
import reflect  # noqa: E402

t = Checks()
R = config.RESULTS_FOLDER

# ---- 回顾面板 ----
def recent(tid):
    """转写日期改成今天（make_transcript 默认是 9 月 1 日，不在「近一个月」里）。"""
    p = os.path.join(R, tid, 'meta.json')
    meta = json.load(open(p, encoding='utf-8'))
    meta['date'] = time.strftime('%Y-%m-%d %H:%M:%S')
    write_json(p, meta)
    return tid


recent(make_transcript(R, [('00:00:01', '第一条')], title='一'))
queued = []
reflect.schedule_regenerate = lambda rd, rk: queued.append(rk) or True
data = reflect.compute(R, '1m')
fp_old = reflect._fingerprint(data['_items'])
text = {'headline': '旧标题', 'narrative': '旧叙事', 'topics': {}, 'generated': True}


def cache_at(at, fp):
    write_json(reflect._cache_path(R), {f'{rk}:{l}': {'fp': fp, 'text': text, 'at': at}
                                        for rk in reflect.RANGES for l in ('zh', 'en')})


this_month = time.strftime('%Y-%m-%d %H:%M:%S')
cache_at(this_month, fp_old)
recent(make_transcript(R, [('00:00:01', '又一条')], title='二'))    # 这个月又转了一条：条目变了
d = reflect.build(R, '1m', 'zh')
t.check('这个月写过：新转写不触发重写，先用这个月的', queued == [] and d['headline'] == '旧标题' and not d['stale'],
        f'{queued} {d["headline"]}')
t.check('并告诉前端这段文字写于哪天', d['as_of'] == this_month[:10], d['as_of'])
reflect.refresh_stale(R)
t.check('后台定时检查也不重写', queued == [], f'{queued}')

cache_at('2000-01-15 10:00:00', fp_old)                         # 上个月（或更早）写的
d = reflect.build(R, '1m', 'zh')
t.check('上个月写的、条目又变了：排一次重写', queued == ['1m'] and d['stale'], f'{queued}')
queued.clear()
reflect.refresh_stale(R)
t.check('定时检查把四个时段都排上', sorted(queued) == sorted(reflect.RANGES), f'{queued}')
queued.clear()
cache_at('2000-01-15 10:00:00', reflect._fingerprint(reflect.compute(R, '1m')['_items']))
reflect.build(R, '1m', 'zh')
t.check('上个月写的但条目没变：不用重写', queued == [], f'{queued}')

# ---- 项目综述 ----
synth = []
analyze.analyze_episode = lambda title, text, author, **k: {
    'title': title, 'cards': [{'obs': 'x', 'quote': text[:20], 'timestamp': '00:00:01', 'layer': '他的主张'}], 'metrics': {}}
analyze.synthesize_collection = lambda eps, name, **k: synth.append(len(eps)) or f'# 综述（{len(eps)} 期）'
ask._embed_texts = fake_embed
A._auto_tag = lambda cdir: None
A._annotate_speakers = lambda cdir, eps: None
A._review_episode_transcript = lambda tid, preset=None: json.load(
    open(os.path.join(R, tid, 'transcript.json'), encoding='utf-8'))
c = A.app.test_client()


def idle(pid):
    end = time.time() + 30
    while time.time() < end and pid in A._project_jobs:
        time.sleep(0.05)


pid = c.post('/api/projects', json={'name': '课'}).get_json()['id']
cdir = A._chain_dir(pid)
first = make_transcript(R, [('00:00:01', '第一课：老师讲鲁迅。')], title='第一课')
c.post(f'/api/chain/{pid}/sources/transcripts', json={'task_ids': [first]})
idle(pid)
c.post(f'/api/chain/{pid}/cards/build')                          # 用户点「生成」：马上写综述
idle(pid)
t.check('用户点生成：写综述', synth == [1] and os.path.isfile(os.path.join(cdir, '总分析.md')), f'{synth}')

second = make_transcript(R, [('00:00:01', '第二课：老师讲孔乙己。')], title='第二课')
c.post(f'/api/chain/{pid}/sources/transcripts', json={'task_ids': [second]})
idle(pid)
state = A._read_chain(pid)
t.check('这个月写过综述：加录音不重写（只抽新卡）', synth == [1] and state.get('overview_behind') is True, f'{synth} {state.get("overview_behind")}')

old = time.mktime((2000, 1, 15, 10, 0, 0, 0, 0, -1))
os.utime(os.path.join(cdir, '总分析.md'), (old, old))           # 综述是上个月写的
third = make_transcript(R, [('00:00:01', '第三课：老师讲药。')], title='第三课')
c.post(f'/api/chain/{pid}/sources/transcripts', json={'task_ids': [third]})
idle(pid)
state = A._read_chain(pid)
t.check('综述是上个月的：加录音时重写一次，把三课都并进去', synth == [1, 3] and not state.get('overview_behind'),
        f'{synth} {state.get("overview_behind")}')

# ---- 便宜模型 ----
t.check('摘要 / 标题标签用 3.1-flash-lite（不跟转写共用一个模型名）',
        config.GEMINI_SUMMARY_MODEL == 'gemini-3.1-flash-lite' and config.GEMINI_ENRICH_MODEL == 'gemini-3.1-flash-lite'
        and config.GEMINI_MODEL != config.GEMINI_SUMMARY_MODEL)
import usage  # noqa: E402
t.check('它有价格，记账算得出钱', usage.estimate_cost('gemini-3.1-flash-lite', input_tokens=1_000_000, output_tokens=0) == 0.25)
t.finish()
