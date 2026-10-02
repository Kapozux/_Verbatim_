"""做声纹时网页不能卡住。sherpa-onnx 的切段整段攥着 GIL：在服务进程的线程里跑，一小时的课会让所有接口没反应约 2 分钟。
现在真引擎放到子进程里跑。这里用真模型做两段音频，量主线程（= 处理网页请求的线程）最长被卡了多久：
一段是 macOS 自带 `say` 念出来的两人对话（有真的说话声，切段要算一阵子）；一段是纯噪声（一句话都切不出来，以前会崩）。
跑生产代码：voices.queue → 子进程 → voices.fingerprint（真 sherpa-onnx + 真模型；本机没装模型 / 没有 say 就跳过）。
不连网、不花钱。"""
import os
import shutil
import subprocess
import threading
import time
import wave

import numpy as np

from _support import SRC, Checks, isolate, make_transcript

isolate('voicesiso')
MODELS = os.path.join(SRC, 'models', 'voices')
os.environ['VERBATIM_VOICE_MODELS'] = MODELS
import config  # noqa: E402
import voices  # noqa: E402

t = Checks()
if not voices.available():
    t.skip('做声纹不卡网页', '本机没装声纹模型（models/voices）')
    t.finish()



def watch(tid):
    """等这条声纹做完，返回 (最终状态, 主线程最长卡了几秒)。"""
    worst, last = 0.0, time.monotonic()
    deadline = time.time() + 300
    while time.time() < deadline and (voices.job(tid) or {}).get('state') in ('queued', 'running'):
        time.sleep(0.005)
        now = time.monotonic()
        worst = max(worst, now - last)
        last = now
    return voices.job(tid) or {}, worst


os.makedirs(config.UPLOAD_FOLDER, exist_ok=True)
t.check('真引擎走子进程', voices._isolated())

# 1 两个人轮流说话（say 的两个声音），约两分钟
say = shutil.which('say')
if say:
    lines = ['今天我们讨论鲁迅的药，人血馒头到底象征什么。', 'I think the bread stands for the crowd that just watches.',
             '那华老栓为什么一定要买这个馒头呢？', 'Because he believes it will cure his son, he trusts the rumour.'] * 6
    parts = []
    for i, text in enumerate(lines):
        aiff = os.path.join(config.UPLOAD_FOLDER, f'l{i}.aiff')
        subprocess.run([say, '-v', 'Tingting' if i % 2 == 0 else 'Daniel', '-o', aiff, text], check=True,
                       capture_output=True)
        parts.append(aiff)
    lst = os.path.join(config.UPLOAD_FOLDER, 'list.txt')
    open(lst, 'w').write(''.join(f"file '{p}'\n" for p in parts))
    talk = os.path.join(config.UPLOAD_FOLDER, 'talk.wav')
    subprocess.run([config.FFMPEG_BIN, '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst,
                    '-ac', '1', '-ar', '16000', talk], check=True)
    tid = make_transcript(config.RESULTS_FOLDER, [('00:00:01', '对话')], title='对话')
    voices.queue(tid, talk)
    state, worst = watch(tid)
    rec = voices.recording(tid)
    t.check('有说话声的录音：声纹做完、切出了好几段', state.get('state') == 'done' and rec is not None and len(rec.start) >= 4,
            f'{state} turns={len(rec.start) if rec else None}')
    t.check('做声纹的这段时间主线程最多卡 0.5 秒（在本进程里跑会卡住整段）', worst < 0.5, f'最长卡了 {worst:.2f} 秒')
else:
    t.skip('有说话声的录音不卡网页', '本机没有 say（不是 macOS）')

# 2 纯噪声：一句话都切不出来——以前在 reshape 上崩，这期就永远没有声纹、每次都重做
rng = np.random.default_rng(1)
pcm = (rng.normal(scale=0.2, size=16000 * 30) * 32767 * 0.3).astype('<i2')
noise = os.path.join(config.UPLOAD_FOLDER, 'noise.wav')
with wave.open(noise, 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(pcm.tobytes())
tid2 = make_transcript(config.RESULTS_FOLDER, [('00:00:01', '噪声')], title='噪声')
voices.queue(tid2, noise)
state, _ = watch(tid2)
rec2 = voices.recording(tid2)
t.check('没有说话声的录音：存一份空声纹，不崩', state.get('state') == 'done' and rec2 is not None and len(rec2.start) == 0,
        f'{state}')
t.finish()
