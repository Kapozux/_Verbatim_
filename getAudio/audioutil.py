"""转写完成后把存档音频压成 Opus 24k 单声道，省磁盘。

音频此时只剩“回放对照”用途，语音在 24k opus 单声道下依然清晰。
安全红线：只处理已经有 transcript.json 的任务；压不出更小就保留原文件。
"""

import json
import os
import shutil
import subprocess

# 目标码率可用环境变量覆盖；默认 24k（语音够清晰，体积最省）
OPUS_BITRATE = os.environ.get('AUDIO_OPUS_BITRATE', '24k')
_TARGET_EXT = '.ogg'          # opus 放 ogg 容器，浏览器 <audio> 直接能放


def _ffmpeg():
    if os.path.exists('/opt/homebrew/bin/ffmpeg'):
        return '/opt/homebrew/bin/ffmpeg'
    return shutil.which('ffmpeg') or 'ffmpeg'


def _find_audio(task_dir, meta):
    """定位当前音频文件：优先按 meta 的 audio_ext，兜底 glob audio.*。"""
    ext = meta.get('audio_ext') if meta else None
    if ext:
        p = os.path.join(task_dir, f'audio{ext}')
        if os.path.isfile(p):
            return p
    try:
        for name in os.listdir(task_dir):
            if name.startswith('audio.') and not name.endswith('.tmp'):
                return os.path.join(task_dir, name)
    except OSError:
        pass
    return None


def compress_task(task_dir):
    """把某任务的音频压成 opus。返回 (省下的字节数, 状态)。

    状态：ok / skip:no-transcript / skip:no-audio / skip:already /
          skip:not-smaller / error:...
    """
    if not os.path.isfile(os.path.join(task_dir, 'transcript.json')):
        return 0, 'skip:no-transcript'          # 没转写成功的音频绝不动

    meta_path = os.path.join(task_dir, 'meta.json')
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
        except Exception:
            meta = {}

    src = _find_audio(task_dir, meta)
    if not src:
        return 0, 'skip:no-audio'
    if src.endswith(_TARGET_EXT) or meta.get('audio_compressed'):
        return 0, 'skip:already'

    before = os.path.getsize(src)
    dst = os.path.join(task_dir, 'audio' + _TARGET_EXT)
    tmp = dst + '.tmp'
    try:
        subprocess.run(
            [_ffmpeg(), '-y', '-v', 'error', '-i', src,
             '-ac', '1', '-c:a', 'libopus', '-b:a', OPUS_BITRATE,
             '-f', 'ogg', tmp],          # 显式指定容器：tmp 后缀是 .tmp，ffmpeg 猜不出格式
            check=True, timeout=900,
        )
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return 0, f'error:{str(e)[:80]}'

    after = os.path.getsize(tmp)
    if after >= before:                          # 没压小（原本就低码率）→ 保留原文件
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0, 'skip:not-smaller'

    os.replace(tmp, dst)
    if os.path.abspath(src) != os.path.abspath(dst):
        try:
            os.remove(src)
        except OSError:
            pass

    meta['audio_ext'] = _TARGET_EXT
    meta['audio_compressed'] = True
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return max(0, before - after), 'ok'
