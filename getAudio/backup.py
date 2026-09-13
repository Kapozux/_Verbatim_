"""
Verbatim 内置备份：把转写库的文本部分只增不删地同步到 Google Drive 的本地同步目录。

之前的做法是应用外面挂一个 launchd 定时脚本，坏了没人知道（2026-07 起 macOS 不让
launchd 起的 bash 读 Documents 下的脚本，连续失败两个月，界面上毫无痕迹）。现在备份由
应用自己跑、状态在 Settings → Storage 里看得见。

只备文本：meta / transcript / transcript_raw / summary 和博主分析的 chain.json + md 文档。
音频默认已不再保存，就算有也不备（体积大、回放又没人用）。只增不删：目标里只会多不会少，
在应用里误删一条，备份里还在。

节奏：启动 90 秒后跑一次，之后每 6 小时；每有转写完成，10 分钟防抖后再跑一次。
"""

import glob
import json
import os
import shutil
import threading
import time
from datetime import datetime

STATE_NAME = '_backup_state.json'
INTERVAL_S = 6 * 3600
FIRST_DELAY_S = 90
DEBOUNCE_S = 600
TEXT_EXT = ('.json', '.md', '.txt', '.srt')

_lock = threading.Lock()
_running = False
_debounce = None


def default_dest():
    """备份目录：Settings 里填了就用它；否则找本机 Google Drive 的 My Drive。
    老目录 getAudio_备份 若存在就沿用（里面有 7 月的快照，只增不删接着写）。"""
    env = (os.environ.get('BACKUP_DIR') or '').strip()
    if env:
        return os.path.expanduser(env)
    for base in sorted(glob.glob(os.path.expanduser('~/Library/CloudStorage/GoogleDrive-*/My Drive'))):
        old = os.path.join(base, 'getAudio_备份')
        return old if os.path.isdir(old) else os.path.join(base, 'Verbatim_备份')
    return ''


def _state_path(results_dir):
    return os.path.join(results_dir, STATE_NAME)


def load_state(results_dir):
    try:
        with open(_state_path(results_dir), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(results_dir, st):
    p = _state_path(results_dir)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _count_records(d):
    if not d or not os.path.isdir(d):
        return 0
    n = 0
    for name in os.listdir(d):
        if not name.startswith('_') and os.path.isfile(os.path.join(d, name, 'meta.json')):
            n += 1
    return n


def status(results_dir):
    st = load_state(results_dir)
    dest = default_dest()
    parent = os.path.dirname(dest) if dest else ''
    return {
        'dest': dest,
        'dest_available': bool(parent) and os.path.isdir(parent),
        'running': _running,
        'last_run': st.get('last_run'),
        'last_ok': st.get('last_ok'),
        'copied_last_run': st.get('copied', 0),
        'error': st.get('error', ''),
        'local_count': _count_records(results_dir),
        'backed_count': _count_records(dest),
    }


def _sync_dir(src, dst):
    """把 src 下的文本文件复制到 dst：目标缺失或源更新才复制，从不删除。返回复制的文件数。"""
    copied = 0
    try:
        names = os.listdir(src)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(TEXT_EXT) or name.endswith('.tmp'):
            continue
        s = os.path.join(src, name)
        if not os.path.isfile(s):
            continue
        d = os.path.join(dst, name)
        try:
            if os.path.isfile(d) and os.path.getmtime(d) + 1 >= os.path.getmtime(s):
                continue
            os.makedirs(dst, exist_ok=True)
            shutil.copy2(s, d)
            copied += 1
        except OSError:
            continue
    return copied


def run(results_dir):
    """同步一遍。已在跑就直接返回当前状态。"""
    global _running
    with _lock:
        if _running:
            return status(results_dir)
        _running = True
    st = load_state(results_dir)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    st['last_run'] = now
    try:
        dest = default_dest()
        if not dest or not os.path.isdir(os.path.dirname(dest)):
            raise RuntimeError('Google Drive folder not found — is Google Drive running and signed in?')
        os.makedirs(dest, exist_ok=True)
        copied = 0
        for name in os.listdir(results_dir):
            src = os.path.join(results_dir, name)
            if not os.path.isdir(src):
                continue
            if name == '_chains':
                for cid in os.listdir(src):
                    copied += _sync_dir(os.path.join(src, cid), os.path.join(dest, '_chains', cid))
            elif not name.startswith('_'):
                copied += _sync_dir(src, os.path.join(dest, name))
        st.update(last_ok=now, copied=copied, error='')
    except Exception as e:  # noqa: BLE001
        st['error'] = str(e)[:300]
    finally:
        try:
            _save_state(results_dir, st)
        except OSError:
            pass
        _running = False
    return status(results_dir)


def touch(results_dir):
    """有新转写：10 分钟防抖后跑一次（一批下载 100 条只触发一次）。"""
    global _debounce
    with _lock:
        if _debounce:
            _debounce.cancel()
        _debounce = threading.Timer(DEBOUNCE_S, run, args=(results_dir,))
        _debounce.daemon = True
        _debounce.start()


def start_scheduler(results_dir):
    def loop():
        time.sleep(FIRST_DELAY_S)
        while True:
            run(results_dir)
            time.sleep(INTERVAL_S)
    threading.Thread(target=loop, daemon=True).start()
