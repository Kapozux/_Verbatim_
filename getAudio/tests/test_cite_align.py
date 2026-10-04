"""点出处时现场对齐（ADR 0012）：旧的 Gemini 期没留音频、也没 YouTube 字幕（比如 B 站）——
只截出处前 20 秒到后 70 秒的音频，本机 Whisper 打逐词时间，在这一小段里找原话；结果缓存，下次直接给。
测试里换掉截音频（_make_clip）和跑 Whisper（wordtime.whisper_words）。"""
import os
import uuid

from _support import Checks, isolate, make_transcript

TMP = isolate('cite_align')
import app as A  # noqa: E402
import wordtime  # noqa: E402

t = Checks()
clips, runs = [], []


def fake_clip(tid, start, dur):
    clips.append((start, dur))
    p = os.path.join(TMP, f'clip_{start}.m4a')
    open(p, 'wb').write(b'\0')
    return p


def fake_whisper(path):
    runs.append(path)
    # 这一段音频从第 45 秒开始（出处 1:05 往前 20 秒）：原话在片段里第 33.6 秒 → 原视频 78.6 秒
    return [(30.0, 'pretty'), (31.0, 'shocking'), (31.5, 'stats'), (33.6, 'So'), (34.0, 'one'), (34.2, 'of'),
            (34.4, 'the'), (34.8, 'big'), (35.0, 'ones'), (35.6, 'is'), (36.2, 'the'), (36.5, 'number'), (36.7, 'of')]


A._make_clip = fake_clip
wordtime.whisper_words = fake_whisper
tid = make_transcript(os.path.join(TMP, 'results'), [('00:01:05', 'So, we did a bit of an analysis …')], tid=str(uuid.uuid4()))
c = A.app.test_client()
body = {'task_id': tid, 'sec': 65, 'quote': 'So, one of the big ones is the number of hard tech companies'}
r = c.post('/api/cite/align', json=body).get_json()
t.check('截出处前 20 秒起的 90 秒，Whisper 找到原话：78 秒、exact', r.get('sec') == 78 and r.get('exact') is True
        and clips == [(45, 90)], f'{r} {clips}')
r2 = c.post('/api/cite/align', json=body).get_json()
t.check('同一条再问：直接给缓存，不再截音频、不再跑 Whisper', r2.get('sec') == 78 and len(runs) == 1 and len(clips) == 1, f'{r2} {runs}')
r3 = c.post('/api/cite/align', json={**body, 'quote': 'we are raising a series B next quarter from sequoia'}).get_json()
t.check('这一段里没有原话：sec 为 None（前端保持原样）', r3.get('sec') is None and r3.get('exact') is False, str(r3))
t.check('参数不对：400', c.post('/api/cite/align', json={'task_id': 'x', 'sec': 1, 'quote': 'a b c'}).status_code == 400)
wordtime.save(tid, [(10.0, 'hello'), (79.5, 'So'), (79.8, 'one'), (80.0, 'of'), (80.2, 'the'), (80.4, 'big')], source='youtube')
runs.clear()
r4 = c.post('/api/cite/align', json={**body, 'quote': 'So one of the big ones'}).get_json()
t.check('有逐词时间轴的期：直接查，不截音频', r4.get('sec') == 79 and not runs, str(r4))

t.finish()
