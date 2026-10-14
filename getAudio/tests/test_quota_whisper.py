"""云引擎额度用完（Gemini 月度花费上限）→ 不看「失败兜底 Whisper」开关，直接落本地 Whisper；
撞上限后一小时内同引擎的下一期不再先碰云端。10-14：30 期合集两分钟内全挂在 spending cap。
桩：transcribe_gemini.transcribe_audio（抛额度错）、transcribe_whisper.transcribe_audio（假转写）。"""
import os
import queue
import time
import uuid

from _support import Checks, isolate

TMP = isolate('quota_whisper')
os.environ['VOICES_AUTO'] = 'off'      # 转写成功会把声纹排进后台（真模型、非守护线程）：测试进程会等它跑完才退
import app as A  # noqa: E402
import taskdb  # noqa: E402
import transcribe_gemini  # noqa: E402
import transcribe_whisper  # noqa: E402

t = Checks()
CAP = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Your project has exceeded its monthly "
       "spending cap. Please go to AI Studio at https://ai.studio/spend', 'status': 'RESOURCE_EXHAUSTED'}}")
cloud = []


def gemini_cap(path, progress_callback=None):
    cloud.append(path)
    raise RuntimeError(CAP)


transcribe_gemini.transcribe_audio = gemini_cap
A._on_battery = lambda: False          # 笔记本用电池时本地 Whisper 会原地等插电（测试会一直卡着）
transcribe_whisper.transcribe_audio = lambda path, progress_callback=None: [{'start': 1, 'end': 2, 'text': '你好'}]

t.check('认得出月度花费上限', A._is_quota_out(RuntimeError(CAP)))
t.check('普通每分钟限流不算额度用完', not A._is_quota_out(RuntimeError('429 RESOURCE_EXHAUSTED rate limit, retry in 30s')))
t.check('超时不算', not A._is_quota_out(RuntimeError('deadline exceeded')))


def run(name):
    tid = str(uuid.uuid4())
    src = os.path.join(TMP, name)
    open(src, 'wb').write(b'\0' * 4096)
    taskdb.create(tid, name, 'gemini', None, src)
    A.run_transcription(tid, src, 'gemini', name, queue.Queue(), fallback_whisper=False, summarize=False)
    return taskdb.get(tid) or {}


r1 = run('a.mp3')
t.check('开关没勾也落 Whisper：任务成功', r1.get('status') == 'done', str(r1.get('status')) + ' ' + str(r1.get('error'))[:200])
t.check('记下 gemini 撞上限', A._quota_recent('gemini'))
cloud.clear()
r2 = run('b.mp3')
t.check('下一期直接 Whisper、不再碰云端', r2.get('status') == 'done' and not cloud, str(cloud))
A._quota_out['gemini'] = time.time() - A._QUOTA_HOLD_S - 1
cloud.clear()
r3 = run('c.mp3')
t.check('过了一小时再探一次云端（还是上限 → 又落 Whisper）', cloud and r3.get('status') == 'done', str(cloud))

t.finish()
