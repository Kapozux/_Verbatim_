"""本机几个应用（Verbatim / Ephemeris / 墨页）共用一份 Claude 订阅额度时的分配器（2026-10-13）。

没有常驻服务：大家读写同一个 SQLite（CLAUDE_GATE 指向它，例 ~/.claude-gate/gate.db；没设就整个不起作用，行为跟以前一样）。
  · 总并发：三边加起来同时最多 CAP 个 claude 进程（CLAUDE_GATE_CAP，默认 24）；留 RESERVE 个（默认 4）只给交互的活
    （prio='live'：问、问证据卡），批量跑满了你问一句也不用排队。
  · 给人自己留额度：设了 CLAUDE_GATE_LIMIT_5H（5 小时上限折合的等值美元，10-13 实测撞上限时约 $360）就算「最近 5 小时
    三个应用 + 人自己在 Claude Code 里用的」合计，到了 LIMIT - CLAUDE_GATE_KEEP（默认 100，10-13）
    批量和普通的活就等着，交互的照常。人自己用的量从 ~/.claude/projects 的对话记录增量读（一分钟最多读一次）；
    应用自己起的 claude（~/.verbatim-claude 之类的目录名下）不算进去，它们已经记在 calls 里。
  · 给人自己留周额度（10-31：20x 订阅取消了 5 小时上限，只剩 7 天的）：设了 CLAUDE_GATE_WEEK_KEEP（给人留几成，例 0.3）
    就看 Anthropic 那边的真数（claude -p 的 rate_limit_event 里「7 天用了几成」，人在 claude.ai 聊天、自己用 Claude Code 都算）：
    批量和普通的用到 1 - KEEP 就不起 claude（抛 Paused，各自退回原来的模型），交互的到 1 - CLAUDE_GATE_WEEK_KEEP_LIVE（默认 0.1）。
    十分钟最多探一次（Haiku 回一个字、不留会话），三边共用 meta 里那份。
  · 一起停：谁撞了上限就写进 pause，三边都停到重置时间（各自退回原来的模型），不用各撞一遍。
  · 记账：每次调用一行（应用、用途、模型、token、等值美元、秒数）。`python3 claude_gate.py` 看现在谁占着、5 小时 / 7 天各用了多少。
同一份文件在 Verbatim（getAudio/claude_gate.py）和 Ephemeris（ephemeris/claude_gate.py）各放一份（项目之间不互相 import），
墨页是 Node 版 server/claude-gate.mjs；表结构三份一样，改一处要三处一起改。

11-03 加了 GPT（codex exec，用户的 ChatGPT 订阅）：同一个闸按「哪一家」（provider：claude / codex）分开算——
并发上限各算各的（codex 用 CLAUDE_GATE_CAP_CODEX / CLAUDE_GATE_RESERVE_CODEX，默认 4 / 1，Plus 额度小），
撞上限也只停那一家（表 pauses）；账记在同一张 calls 里（provider 一列），`python3 claude_gate.py` 一页看两家。
老的 pause 表（只有 Claude 一行）还读，没升级的应用写进去也认。
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
CREATE TABLE IF NOT EXISTS cc_files (path TEXT PRIMARY KEY, offset INTEGER);
CREATE TABLE IF NOT EXISTS cc_calls (key TEXT PRIMARY KEY, ts REAL, model TEXT, usd REAL);
CREATE INDEX IF NOT EXISTS cc_calls_ts ON cc_calls(ts);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v REAL);
CREATE TABLE IF NOT EXISTS pauses (provider TEXT PRIMARY KEY, until REAL, reason TEXT, app TEXT);
"""
PROVIDERS = ('claude', 'codex')
NAMES = {'claude': 'Claude', 'codex': 'GPT（Codex）'}
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
    for t in ('slots', 'calls'):                        # 老库补列：没写 provider 的（没升级的应用）都是 Claude
        if 'provider' not in [r[1] for r in c.execute(f'PRAGMA table_info({t})')]:
            try:
                c.execute(f"ALTER TABLE {t} ADD COLUMN provider TEXT DEFAULT 'claude'")
            except sqlite3.OperationalError:            # 别的进程刚补上
                pass
    return c


def _cap(provider):
    if provider == 'claude':
        return int(_num('CLAUDE_GATE_CAP', 24)), int(_num('CLAUDE_GATE_RESERVE', 4))
    return int(_num('CLAUDE_GATE_CAP_CODEX', 4)), int(_num('CLAUDE_GATE_RESERVE_CODEX', 1))


def _alive(pid):
    if sys.platform == 'win32':                         # os.kill(pid, 0) 在 Windows 上会给那个进程发 Ctrl+C
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))    # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                        # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


# 等值美元（每百万 token 输入 / 输出；缓存读 0.1 倍输入、1 小时缓存写 2 倍、5 分钟缓存写 1.25 倍）。10-13 用 claude -p 的
# total_cost_usd 反推：Haiku 5.5 0.10 / 0.50、Sonnet 5.5 2 / 10、Opus 5.5 4 / 20
_PRICE = {'haiku': (0.10, 0.50), 'sonnet': (2.00, 10.00), 'opus': (4.00, 20.00)}
_APP_DIRS = ('verbatim-claude', 'ephemeris-claude', 'moye-claude')


def _usd(model, u):
    fam = next((k for k in _PRICE if k in str(model)), 'opus')
    pi, po = _PRICE[fam]
    cc = u.get('cache_creation') or {}
    w1 = cc.get('ephemeral_1h_input_tokens') or 0
    w5 = (cc.get('ephemeral_5m_input_tokens') or 0) if cc else (u.get('cache_creation_input_tokens') or 0)
    return ((u.get('input_tokens') or 0) * pi + (u.get('output_tokens') or 0) * po
            + (u.get('cache_read_input_tokens') or 0) * pi * 0.1 + w1 * pi * 2 + w5 * pi * 1.25) / 1e6


def sync_cc(c=None, every=60):
    """把人自己在 Claude Code 里的用量增量读进 cc_calls（每个文件记读到哪了）。every 秒内读过就不读。"""
    import calendar
    import glob
    import json
    own = c is None
    c = c or _db()
    try:
        last = c.execute("SELECT v FROM meta WHERE k = 'cc_scan'").fetchone()
        now = time.time()
        if last and now - last[0] < every:
            return
        c.execute("INSERT INTO meta (k, v) VALUES ('cc_scan', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (now,))
        root = os.path.expanduser(os.environ.get('CLAUDE_GATE_CC_DIR') or '~/.claude/projects')   # 测试指到空目录
        for f in glob.glob(os.path.join(root, '**', '*.jsonl'), recursive=True):
            if any(a in f for a in _APP_DIRS):
                continue
            try:
                size = os.path.getsize(f)
                if os.path.getmtime(f) < now - 8 * 86400:
                    continue
            except OSError:
                continue
            row = c.execute('SELECT offset FROM cc_files WHERE path = ?', (f,)).fetchone()
            off = row[0] if row and row[0] <= size else 0
            if off == size:
                continue
            rows = []
            with open(f, 'rb') as fh:
                fh.seek(off)
                data = fh.read()
            end = data.rfind(b'\n') + 1
            for line in data[:end].split(b'\n'):
                if b'"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get('message') or {}
                u, model, ts = m.get('usage'), m.get('model') or '', d.get('timestamp')
                if not u or model == '<synthetic>' or not ts:
                    continue
                try:
                    t = calendar.timegm(time.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S'))      # 记录里是 UTC
                except ValueError:
                    continue
                rows.append((f"{m.get('id')}|{d.get('requestId')}", t, model, _usd(model, u)))
            c.execute('BEGIN')
            c.executemany('INSERT OR IGNORE INTO cc_calls (key, ts, model, usd) VALUES (?, ?, ?, ?)', rows)
            c.execute('INSERT INTO cc_files (path, offset) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET offset = excluded.offset',
                      (f, off + end))
            c.execute('COMMIT')
        c.execute('DELETE FROM cc_calls WHERE ts < ?', (now - 8 * 86400,))
    except sqlite3.Error:
        pass
    finally:
        if own:
            c.close()


def used_5h(c):
    """最近 5 小时：(三个应用, 人自己在 Claude Code 里) 的等值美元。"""
    since = time.time() - 5 * 3600
    return (c.execute('SELECT COALESCE(SUM(usd), 0) FROM calls WHERE ts > ?', (since,)).fetchone()[0],
            c.execute('SELECT COALESCE(SUM(usd), 0) FROM cc_calls WHERE ts > ?', (since,)).fetchone()[0])


WEEK_EVERY = 600


def _probe():
    """问一次 Haiku，从 rate_limit_event 读 (7 天用了几成, 重置时间)；读不到 None。"""
    import json
    import shutil
    import subprocess
    exe = shutil.which('claude')
    if not exe:
        return None
    try:
        r = subprocess.run([exe, '-p', '--model', 'haiku', '--system-prompt', 'Reply with one word.', '--tools', '',
                            '--setting-sources', '', '--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence',
                            '--output-format', 'stream-json', '--verbose'], input='ok', capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=90, cwd=os.path.dirname(path()),
                           env={**os.environ, 'DISABLE_PROMPT_CACHING': '1'})
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            w = json.loads(line)['rate_limit_info']['unifiedWindows']['seven_day']
            return float(w['utilization']), float(w['resetsAt'])
        except (ValueError, KeyError, TypeError):
            continue
    return None


def week(c):
    """(7 天用了几成, 重置时间)；探不到、或者存着的那份已经过了重置时间，是 None。"""
    m = dict(c.execute("SELECT k, v FROM meta WHERE k IN ('week_util', 'week_reset', 'week_ts')").fetchall())
    now = time.time()
    have = (m['week_util'], m['week_reset']) if m.get('week_reset', 0) > now and 'week_util' in m else None
    if have and m.get('week_ts', 0) > now - WEEK_EVERY:
        return have
    c.execute("INSERT INTO meta (k, v) VALUES ('week_ts', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (now,))  # 别的进程这十分钟不探
    got = _probe()
    if got:
        c.executemany("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                      [('week_util', got[0]), ('week_reset', got[1])])
    return got or have


def _paused(c, provider, now):
    """(停到几点, 原因, 哪个应用撞的)；没停着是 None。Claude 也看老的 pause 表。"""
    rows = [c.execute('SELECT until, reason, app FROM pauses WHERE provider = ?', (provider,)).fetchone()]
    if provider == 'claude':
        rows.append(c.execute('SELECT until, reason, app FROM pause WHERE k = 1').fetchone())
    rows = [r for r in rows if r and r[0] and r[0] > now]
    return max(rows) if rows else None


def paused_until(provider='claude'):
    if not enabled():
        return 0.0
    try:
        c = _db()
        row = _paused(c, provider, time.time())
        c.close()
        return float(row[0]) if row else 0.0
    except sqlite3.Error:
        return 0.0


def pause(until, reason='', app='', provider='claude'):
    if not enabled():
        return
    c = _db()
    c.execute('INSERT INTO pauses (provider, until, reason, app) VALUES (?, ?, ?, ?) ON CONFLICT(provider) DO UPDATE SET '
              'until = MAX(until, excluded.until), reason = excluded.reason, app = excluded.app',
              (provider, float(until), str(reason)[:300], app))
    c.close()


def _may(c, prio, provider='claude'):
    cap, reserve = _cap(provider)
    n = c.execute("SELECT COUNT(*) FROM slots WHERE COALESCE(provider, 'claude') = ?", (provider,)).fetchone()[0]
    if prio == 'live':
        return n < cap
    if n >= cap - reserve:
        return False
    if provider != 'claude':
        return True
    limit = _num('CLAUDE_GATE_LIMIT_5H', 0)
    return not (limit and sum(used_5h(c)) >= limit - _num('CLAUDE_GATE_KEEP', 100))


@contextmanager
def slot(app, purpose='', prio='normal', wait=1800, provider='claude'):
    """占一个位子再起 claude / codex 进程。prio：live（交互）/ normal / batch（批量）。停着就抛 Paused；等了 wait 秒还没轮到也抛。"""
    if not enabled():
        yield
        return
    c = _db()
    if provider == 'claude' and _num('CLAUDE_GATE_WEEK_KEEP', 0):
        keep = _num('CLAUDE_GATE_WEEK_KEEP_LIVE', 0.1) if prio == 'live' else _num('CLAUDE_GATE_WEEK_KEEP', 0)
        w = week(c)
        if w and w[0] >= 1 - keep:
            c.close()
            raise Paused(f'Claude 7 天额度已用 {w[0]:.0%}，剩下的留给你自己（{time.strftime("%m-%d %H:%M", time.localtime(w[1]))} 重置）')
    if provider == 'claude' and prio != 'live' and _num('CLAUDE_GATE_LIMIT_5H', 0):
        sync_cc(c)
    who = NAMES.get(provider, provider)
    deadline, sid = time.time() + wait, None
    try:
        while sid is None:
            c.execute('BEGIN IMMEDIATE')
            try:
                now = time.time()
                for i, pid, started in c.execute('SELECT id, pid, started FROM slots').fetchall():
                    if started < now - STALE_S or not _alive(pid):
                        c.execute('DELETE FROM slots WHERE id = ?', (i,))
                row = _paused(c, provider, now)
                if row:
                    raise Paused(f'{who} usage limit (shared pause until {time.strftime("%H:%M", time.localtime(row[0]))}): {row[1]}')
                if _may(c, prio, provider):
                    sid = c.execute('INSERT INTO slots (app, purpose, prio, pid, started, provider) VALUES (?, ?, ?, ?, ?, ?)',
                                    (app, purpose, prio, os.getpid(), now, provider)).lastrowid
            finally:
                c.execute('COMMIT')
            if sid is None:
                if provider == 'claude' and prio != 'live' and _num('CLAUDE_GATE_LIMIT_5H', 0):
                    sync_cc(c)          # 等着的时候也一分钟更新一次人自己用了多少
                if time.time() > deadline:
                    raise Paused(f'{who} gate busy for {wait}s')
                time.sleep(0.5 if prio == 'live' else 1.5)
        yield
    finally:
        if sid is not None:
            c.execute('DELETE FROM slots WHERE id = ?', (sid,))
        c.close()


def record(app, purpose, model, d, secs=0.0, prio='normal', provider='claude'):
    """d：claude -p 的 result（--output-format json 的整个对象，或 stream-json 的最后一个 result 事件）；
    codex：{'usage': turn.completed 里的 usage, 'is_error': …}（订阅不按钱算，usd 记 0）。"""
    if not enabled():
        return
    u = (d or {}).get('usage') or {}
    if provider == 'codex':
        u = {'input_tokens': u.get('input_tokens'), 'output_tokens': (u.get('output_tokens') or 0) + (u.get('reasoning_output_tokens') or 0),
             'cache_read_input_tokens': u.get('cached_input_tokens'), 'cache_creation_input_tokens': u.get('cache_write_input_tokens')}
    try:
        c = _db()
        c.execute('INSERT INTO calls (ts, app, purpose, prio, model, input_tokens, output_tokens, cache_read, cache_write, usd, secs, ok, provider) '
                  'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                  (time.time(), app, purpose, prio, model, u.get('input_tokens') or 0, u.get('output_tokens') or 0,
                   u.get('cache_read_input_tokens') or 0, u.get('cache_creation_input_tokens') or 0,
                   float((d or {}).get('total_cost_usd') or 0), round(secs, 1), 0 if (d or {}).get('is_error') else 1, provider))
        c.close()
    except sqlite3.Error:
        pass


def codex_week():
    """GPT（Codex）7 天额度最后一次看到的：(用了几成, 重置时间, 看到的时间)。应用自己调的不留会话记录，
    所以读的是人自己最近用 Codex 留下的会话（只显示，不拿它卡人）。没有是 None。"""
    import glob
    import json
    root = os.path.expanduser(os.environ.get('CLAUDE_GATE_CODEX_DIR') or '~/.codex/sessions')
    files = sorted(glob.glob(os.path.join(root, '**', 'rollout-*.jsonl'), recursive=True), key=os.path.getmtime)
    for f in reversed(files[-20:]):
        got = None
        try:
            with open(f, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if '"rate_limits"' in line:
                        got = line
        except OSError:
            continue
        if got:
            try:
                p = (json.loads(got).get('payload') or {}).get('rate_limits') or {}
                w = p.get('secondary') if (p.get('secondary') or {}).get('window_minutes') == 10080 else p.get('primary')
                return float(w['used_percent']) / 100, float(w['resets_at']), os.path.getmtime(f)
            except (ValueError, KeyError, TypeError):
                continue
    return None


def status():
    c = _db()
    now = time.time()
    sync_cc(c)
    out = {'apps_5h': used_5h(c)[0], 'cc_5h': used_5h(c)[1],
           'cc_7d': c.execute('SELECT COALESCE(SUM(usd), 0) FROM cc_calls WHERE ts > ?', (now - 7 * 86400,)).fetchone()[0],
           'slots': c.execute("SELECT app, purpose, prio, ROUND(? - started), COALESCE(provider, 'claude') FROM slots ORDER BY started",
                              (now,)).fetchall(),
           'pause': {p: _paused(c, p, now) for p in PROVIDERS}}
    for name, span in (('5h', 5 * 3600), ('7d', 7 * 86400)):
        out[name] = c.execute("SELECT app, model, COUNT(*), ROUND(SUM(usd), 3), SUM(ok = 0), COALESCE(provider, 'claude'), "
                              'SUM(input_tokens + output_tokens) FROM calls WHERE ts > ? '
                              'GROUP BY app, model ORDER BY SUM(usd) DESC', (now - span,)).fetchall()
    c.close()
    return out


if __name__ == '__main__':
    if not enabled():
        os.environ['CLAUDE_GATE'] = '~/.claude-gate/gate.db'
    s = status()
    for p in PROVIDERS:
        cap, reserve = _cap(p)
        mine = [r for r in s['slots'] if r[4] == p]
        print(f'{NAMES[p]}：并发上限 {cap}（留 {reserve} 个给交互）；现在占着 {len(mine)} 个')
        for app, purpose, prio, secs, _ in mine:
            print(f'  {app:10} {purpose:14} {prio:6} {int(secs)}s')
        if s['pause'][p]:
            u, why, app = s['pause'][p]
            print(f'  停着：到 {time.strftime("%m-%d %H:%M", time.localtime(u))}（{app}：{(why or "")[:80]}）')
    cw = codex_week()
    if cw:
        seen = time.strftime("%m-%d %H:%M", time.localtime(cw[2]))
        print(f'GPT（Codex）7 天额度：{seen} 那次看到已用 {cw[0]:.0%}，' + (
            f'{time.strftime("%m-%d %H:%M", time.localtime(cw[1]))} 重置' if cw[1] > time.time()
            else '之后已经重置过；现在用了多少，要等你自己下次用 Codex 才看得到'))
    limit, keep = _num('CLAUDE_GATE_LIMIT_5H', 0), _num('CLAUDE_GATE_KEEP', 100)
    print(f'最近 5h 合计 ${s["apps_5h"] + s["cc_5h"]:.0f}（应用 ${s["apps_5h"]:.2f} + 你自己用 Claude Code ${s["cc_5h"]:.0f}）'
          + (f'；上限按 ${limit:.0f} 算，到 ${limit - keep:.0f} 应用就让路' if limit else ''))
    if _num('CLAUDE_GATE_WEEK_KEEP', 0):
        c = _db()
        w = week(c)
        c.close()
        stop, live = 1 - _num('CLAUDE_GATE_WEEK_KEEP', 0), 1 - _num('CLAUDE_GATE_WEEK_KEEP_LIVE', 0.1)
        print(f'7 天额度：' + (f'已用 {w[0]:.0%}（{time.strftime("%m-%d %H:%M", time.localtime(w[1]))} 重置）' if w else '没读到')
              + f'；批量 / 普通到 {stop:.0%}、交互到 {live:.0%} 就让给你')
    print(f'你自己用 Claude Code 最近 7 天：${s["cc_7d"]:.0f}')
    for name in ('5h', '7d'):
        total = sum(r[3] or 0 for r in s[name])
        print(f'应用最近 {name}：等值 ${total:.2f}')
        for app, model, n, usd, bad, prov, tok in s[name]:
            cost = f'${usd or 0:7.2f}' if prov == 'claude' else f'{(tok or 0) / 1e6:5.2f}M token（订阅）'
            print(f'  {app:10} {model:24} {n:5} 次  {cost}' + (f'  失败 {bad}' if bad else ''))
    sys.exit(0)
