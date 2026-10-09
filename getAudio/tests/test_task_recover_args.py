"""任务把完整入参存进 tasks.db（借 Open Notebook：command 表存整份 args），重启恢复时原样带上。
以前只存了位置参数那几列：贴链接 / 加进笔记本的转写一重启，source_url、video_id、project_id、
upload_date 全丢，恢复出来的那期在笔记本里找不到、建合集时也没有年份。

跑生产代码：app.run_transcription（落入参）、taskdb、app.recover_unfinished_tasks。
桩：app._run_transcription（真正转写那一段，不跑引擎）、app.submit_transcription（记下重投的参数，不起线程）。
数据：一个假的音频文件放在 uploads/。
"""
import os

from _support import Checks, isolate

TMP = isolate('task_recover')
import app as A  # noqa: E402
import taskdb  # noqa: E402

t = Checks()

ran = []
A._run_transcription = lambda *a, **k: ran.append(k)

tid = 'a' * 32
up = os.path.join(A.config.UPLOAD_FOLDER, f'{tid}.m4a')
os.makedirs(A.config.UPLOAD_FOLDER, exist_ok=True)
with open(up, 'wb') as f:
    f.write(b'\0' * 64)
meta = {'source_url': 'https://www.youtube.com/watch?v=abcdefghijk', 'video_id': 'abcdefghijk',
        'creator': 'Ann', 'upload_date': '20260901', 'project_id': 'p' * 32}

taskdb.create(tid, 'Ep 1', 'gemini35', 2, up)
A.run_transcription(tid, up, 'gemini35', 'Ep 1', None, 2, False, True,
                    offset_sec=0, extra_meta=meta, summarize=False)
t.check('桩跑过一次', len(ran) == 1)

# 服务在转写中途挂了
taskdb.set_status(tid, 'running')
resubmitted = []
A.submit_transcription = lambda engine, fn, *a, **k: resubmitted.append((engine, fn, a, k))
A.recover_unfinished_tasks()

t.check('恢复时重投了这一个任务', len(resubmitted) == 1, str(resubmitted))
eng, fn, args, kw = resubmitted[0] if resubmitted else (None, None, (), {})
em = kw.get('extra_meta') or {}
t.check('恢复后带着 source_url / video_id', em.get('source_url') == meta['source_url']
        and em.get('video_id') == 'abcdefghijk', str(kw))
t.check('恢复后带着 project_id（还进原来的笔记本）', em.get('project_id') == meta['project_id'])
t.check('恢复后带着 upload_date', em.get('upload_date') == '20260901')
t.check('恢复后 summarize 仍是 False', kw.get('summarize') is False)
t.check('恢复后 model_review 仍是 True', kw.get('model_review') is True)
t.check('说话人数还在位置参数里', args[-1] == 2 and 'speaker_count' not in kw, str(args))

# 老库：没有入参那一列的行（升级前写的）照旧按旧办法恢复，不报错
taskdb.set_status(tid, 'running')
with taskdb._conn() as c:
    c.execute('UPDATE tasks SET kwargs=NULL WHERE id=?', (tid,))
resubmitted.clear()
A.recover_unfinished_tasks()
t.check('没存入参的老行按默认恢复', len(resubmitted) == 1
        and resubmitted[0][3] == {'model_review': True}, str(resubmitted))

t.finish()
