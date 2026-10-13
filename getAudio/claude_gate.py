"""本机几个应用（Verbatim / Ephemeris / 墨页）共用一份 Claude 订阅额度时的分配器（2026-10-13，用户：「有一个监控所有 usage 的分配器」）。

没有常驻服务：大家读写同一个 SQLite（CLAUDE_GATE 指向它，例 ~/.claude-gate/gate.db；没设就整个不起作用，行为跟以前一样）。
  · 总并发：三边加起来同时最多 CAP 个 claude 进程（CLAUDE_GATE_CAP，默认 24）；留 RESERVE 个（默认 4）只给交互的活
    （prio='live'：问、问证据卡），批量跑满了你问一句也不用排队。
  · 批量预算：设了 CLAUDE_GATE_BATCH_5H（最近 5 小时三边合计的等值美元）就在超过后让 prio='batch' 的活等着，交互的照常。
  · 一起停：谁撞了上限就写进 pause，三边都停到重置时间（各自退回原来的模型），不用各撞一遍。
  · 记账：每次调用一行（应用、用途、模型、token、等值美元、秒数）。`python3 claude_gate.py` 看现在谁占着、5 小时 / 7 天各用了多少。
同一份文件在 Verbatim（getAudio/claude_gate.py）和 Ephemeris（ephemeris/claude_gate.py）各放一份（项目之间不互相 import），
墨页是 Node 版 server/claude-gate.mjs；表结构三份一样，改一处要三处一起改。
"""
import os
import sqlite3
import sys
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (id INTEGER PRIMARY KEY, app TEXT, purpose TEXT, prio TEXT, pid INTEGER, started REAL);
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, ts REAL, app TEXT, purpose TEXT, prio TEXT, model TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER, usd REAL, secs REAL, ok INTEGER);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE TABLE IF NOT EXISTS pause (k INTEGER PRIMARY KEY CHECK (k = 1), until REAL, reason TEXT, app TEXT);
"""
STALE_S = 2 * 3600          # 占了两个小时还没还的位子当它死了（进程崩了没走到 finally）


class Paused(RuntimeError):
    """三边一起停着（有人撞了上限）：调用方当 Claude 用不了，走原来的模型。"""


def path():
    p = (os.environ.get('CLAUDE_GATE') or '').strip()
    return os.path.expanduser(p) if p else ''


def enabled():
    return bool(path())


def _num(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _db():
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=30, isolation_level=None)
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript(SCHEMA)
    return c


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def paused_until():
    if not enabled():
        return 0.0
    try:
        c = _db()
        row = c.execute('SELECT until FROM pause WHERE k = 1').fetchone()
        c.close()
        return float(row[0]) if row and row[0] and row[0] > time.time() else 0.0
    except sqlite3.Error:
        return 0.0


def pause(until, reason='', app=''):
    if not enabled():
        return
    c = _db()
    c.execute('INSERT INTO pause (k, until, reason, app) VALUES (1, ?, ?, ?) '
              'ON CONFLICT(k) DO UPDATE SET until = MAX(until, excluded.until), reason = excluded.reason, app = excluded.app',
              (float(until), str(reason)[:300], app))
    c.close()


def _used_5h(c):
    return c.execute('SELECT COALESCE(SUM(usd), 0) FROM calls WHERE ts > ?', (time.time() - 5 * 3600,)).fetchone()[0]


def _may(c, prio):
    cap, reserve = int(_num('CLAUDE_GATE_CAP', 24)), int(_num('CLAUDE_GATE_RESERVE', 4))
    n = c.execute('SELECT COUNT(*) FROM slots').fetchone()[0]
    if prio == 'live':
        return n < cap
    if n >= cap - reserve:
        return False
    budget = _num('CLAUDE_GATE_BATCH_5H', 0)
    return not (prio == 'batch' and budget and _used_5h(c) >= budget)


@contextmanager
def slot(app, purpose='', prio='normal', wait=1800):
    """占一个位子再起 claude 进程。prio：live（交互）/ normal / batch（批量）。停着就抛 Paused；等了 wait 秒还没轮到也抛。"""
    if not enabled():
        yield
        return
    c = _db()
    deadline, sid = time.time() + wait, None
    try:
        while sid is None:
            c.execute('BEGIN IMMEDIATE')
            try:
                now = time.time()
                for i, pid, started in c.execute('SELECT id, pid, started FROM slots').fetchall():
                    if started < now - STALE_S or not _alive(pid):
                        c.execute('DELETE FROM slots WHERE id = ?', (i,))
                row = c.execute('SELECT until, reason FROM pause WHERE k = 1').fetchone()
                if row and row[0] and row[0] > now:
                    raise Paused(f'Claude usage limit (shared pause until {time.strftime("%H:%M", time.localtime(row[0]))}): {row[1]}')
                if _may(c, prio):
                    sid = c.execute('INSERT INTO slots (app, purpose, prio, pid, started) VALUES (?, ?, ?, ?, ?)',
                                    (app, purpose, prio, os.getpid(), now)).lastrowid
            finally:
                c.execute('COMMIT')
            if sid is None:
                if time.time() > deadline:
                    raise Paused(f'Claude gate busy for {wait}s')
                time.sleep(0.5 if prio == 'live' else 1.5)
        yield
    finally:
        if sid is not None:
            c.execute('DELETE FROM slots WHERE id = ?', (sid,))
        c.close()


def record(app, purpose, model, d, secs=0.0, prio='normal'):
    """d：claude -p 的 result（--output-format json 的整个对象，或 stream-json 的最后一个 result 事件）。"""
    if not enabled():
        return
    u = (d or {}).get('usage') or {}
    try:
        c = _db()
        c.execute('INSERT INTO calls (ts, app, purpose, prio, model, input_tokens, output_tokens, cache_read, cache_write, usd, secs, ok) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                  (time.time(), app, purpose, prio, model, u.get('input_tokens') or 0, u.get('output_tokens') or 0,
                   u.get('cache_read_input_tokens') or 0, u.get('cache_creation_input_tokens') or 0,
                   float((d or {}).get('total_cost_usd') or 0), round(secs, 1), 0 if (d or {}).get('is_error') else 1))
        c.close()
    except sqlite3.Error:
        pass


def status():
    c = _db()
    now = time.time()
    out = {'slots': c.execute('SELECT app, purpose, prio, ROUND(? - started) FROM slots ORDER BY started', (now,)).fetchall(),
           'pause': c.execute('SELECT until, reason, app FROM pause WHERE k = 1 AND until > ?', (now,)).fetchone()}
    for name, span in (('5h', 5 * 3600), ('7d', 7 * 86400)):
        out[name] = c.execute('SELECT app, model, COUNT(*), ROUND(SUM(usd), 3), SUM(ok = 0) FROM calls WHERE ts > ? '
                              'GROUP BY app, model ORDER BY SUM(usd) DESC', (now - span,)).fetchall()
    c.close()
    return out


if __name__ == '__main__':
    if not enabled():
        os.environ['CLAUDE_GATE'] = '~/.claude-gate/gate.db'
    s = status()
    print(f'总并发上限 {int(_num("CLAUDE_GATE_CAP", 24))}（留 {int(_num("CLAUDE_GATE_RESERVE", 4))} 个给交互）；现在占着 {len(s["slots"])} 个')
    for app, purpose, prio, secs in s['slots']:
        print(f'  {app:10} {purpose:14} {prio:6} {int(secs)}s')
    if s['pause']:
        print(f'停着：到 {time.strftime("%m-%d %H:%M", time.localtime(s["pause"][0]))}（{s["pause"][2]}：{s["pause"][1][:80]}）')
    for name in ('5h', '7d'):
        total = sum(r[3] or 0 for r in s[name])
        print(f'最近 {name}：等值 ${total:.2f}')
        for app, model, n, usd, bad in s[name]:
            print(f'  {app:10} {model:24} {n:5} 次  ${usd or 0:7.2f}' + (f'  失败 {bad}' if bad else ''))
    sys.exit(0)
