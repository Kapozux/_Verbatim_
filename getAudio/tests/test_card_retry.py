"""抽卡失败（断网、模型报错）留下的空卡片文件，下次「继续」要重抽；有卡的、被判「转写不可用」的不重抽。
2026-10-02 用户的语文项目卡在「已分析 6/11」就是这个：合集 / 项目这条路把空文件当成抽完了，断一次网永远补不上。
跑生产代码：/api/projects、/sources/transcripts、/sources、/retry、_project_refresh → _build_collection。
桩（只在模型边界）：analyze.analyze_episode / synthesize_collection、ask._embed_texts；_auto_tag / _annotate_speakers 置空。"""
import json
import os
import time

from _support import Checks, fake_embed, isolate, make_transcript

isolate('cardretry')
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402

t = Checks()
calls = []


def fake_extract(title, text, author, **k):
    calls.append(title)
    return {'title': title, 'cards': [{'obs': 'x', 'quote': text.split('] ', 1)[-1][:40], 'timestamp': '00:00:01',
                                       'layer': '他的主张'}], 'metrics': {}}


analyze.analyze_episode = fake_extract
analyze.synthesize_collection = lambda eps, name, **k: '# 综述\n\nstub'
ask._embed_texts = fake_embed
A._auto_tag = lambda cdir: None
A._annotate_speakers = lambda cdir, eps: None
A._review_episode_transcript = lambda tid, preset=None: json.load(
    open(os.path.join(A.config.RESULTS_FOLDER, tid, 'transcript.json'), encoding='utf-8'))
c = A.app.test_client()


def wait_idle(cid, limit=60):
    end = time.time() + limit
    while time.time() < end and cid in A._project_jobs:
        time.sleep(0.1)


R = A.config.RESULTS_FOLDER
tids = [make_transcript(R, [('00:00:01', f'第{n}课：老师讲鲁迅的小说。'), ('00:00:09', '同学们讨论。')], title=f'第{n}课')
        for n in range(1, 4)]
pid = c.post('/api/projects', json={'name': '语文'}).get_json()['id']
c.post(f'/api/chain/{pid}/sources/transcripts', json={'task_ids': tids})
c.post(f'/api/chain/{pid}/cards/build')
wait_idle(pid)
first = len(calls)

# 模拟上一轮：第 1 课抽成功（已有卡），第 2 课断网失败（空文件），第 3 课转写不可用（空文件 + unusable）
d = A._chain_dir(pid)
files = {json.load(open(os.path.join(d, f), encoding='utf-8'))['task_id']: os.path.join(d, f)
         for f in os.listdir(d) if f.startswith('cards_')}
with open(files[tids[1]], 'w', encoding='utf-8') as f:
    json.dump({'task_id': tids[1], 'title': '第2课', 'cards': [], 'extract_failed': True}, f, ensure_ascii=False)
with open(files[tids[2]], 'w', encoding='utf-8') as f:
    json.dump({'task_id': tids[2], 'title': '第3课', 'cards': [], 'extract_failed': True, 'unusable': 'empty'},
              f, ensure_ascii=False)

missing = c.get(f'/api/chain/{pid}/sources').get_json()['cards_missing']
t.check('来源栏把断网失败的那期算作缺卡（不可用的不算）', missing == 1, f'cards_missing={missing}')

calls.clear()
c.post(f'/api/chain/{pid}/retry', json={})
wait_idle(pid)
again = json.load(open(files[tids[1]], encoding='utf-8'))
t.check('「继续」只重抽断网失败的那期', calls == ['第2课'], f'抽了 {calls}（第一轮抽了 {first} 期）')
t.check('重抽后那期有卡、不可用的那期没动', bool(again['cards']) and not again.get('extract_failed')
        and json.load(open(files[tids[2]], encoding='utf-8')).get('unusable') == 'empty')
t.check('补齐后不再显示缺卡', c.get(f'/api/chain/{pid}/sources').get_json()['cards_missing'] == 0)
t.finish()
