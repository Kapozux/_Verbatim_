"""
任务持久化（SQLite）。

只记录任务的生命周期状态（pending/running/done/failed），
转写结果本身仍然存 results/<task_id>/ 文件目录，职责不变。

价值：服务重启后能从 DB 找回没跑完的任务并重新入队，
不再像纯内存 tasks 字典那样凭空消失。
"""

import os
import sqlite3
import threading
from datetime import datetime

import config

# GETAUDIO_DB 允许把库挪到持久卷里（Docker 把它指到 results/ 下，
# 否则镜像重建就丢任务状态）；默认落在 config.DATA_DIR——源码直跑时是
# getAudio/tasks.db（不变），打包成 .app 跑时是 Application Support 目录。
DB_PATH = (os.environ.get('GETAUDIO_DB')
           or os.path.join(config.DATA_DIR, 'tasks.db'))

# sqlite3 连接不跨线程共享；每次操作开新连接（量小，开销可忽略），
# 写操作用锁串行化，避免 WAL 下偶发的 database is locked。
_write_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _write_lock, _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id            TEXT PRIMARY KEY,
                filename      TEXT,
                engine        TEXT,
                speaker_count INTEGER,
                upload_path   TEXT,
                status        TEXT,
                error         TEXT,
                created_at    TEXT,
                updated_at    TEXT
            )
        ''')


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def create(task_id, filename, engine, speaker_count, upload_path):
    with _write_lock, _conn() as c:
        c.execute(
            'INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?,?)',
            (task_id, filename, engine, speaker_count, upload_path,
             'pending', None, _now(), _now()),
        )


def set_status(task_id, status, error=None):
    with _write_lock, _conn() as c:
        c.execute(
            'UPDATE tasks SET status=?, error=?, updated_at=? WHERE id=?',
            (status, error, _now(), task_id),
        )


def unfinished():
    """服务启动时调用：返回所有没跑完的任务行。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status IN ('pending', 'running')"
        ).fetchall()
    return [dict(r) for r in rows]


def get(task_id):
    """按 id 取单个任务行，不存在返回 None。"""
    with _conn() as c:
        r = c.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    return dict(r) if r else None
