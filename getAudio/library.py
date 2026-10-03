"""转写索引：results/<tid>/meta.json 和 transcript.json 的 SQLite 索引（数据目录下的 library.db）。

磁盘上的文件仍是唯一的原始数据，这里只为了快（2026-10-03，3410 条转写、正文 4900 万字）：
  · 列转写 / 最近 N 条：按 date 建了索引，取 12 条 0.1 ms（原来每次读 3410 份 meta.json，100 ms）；
  · 搜正文：FTS5 trigram 分词，中文子串也能查。3 个字以上走索引几毫秒；1–2 个字索引用不上，
    在库里整表扫约 70 ms（原来逐个读 140 MB 的 transcript.json，0.6–1.2 秒）。detail=none：库 190 MB，
    detail=full 要 365 MB，搜出来的一样。
搜索结果、命中片段跟原来逐个读文件的写法一字不差（tests/test_library.py 逐条对照）。

怎么保持跟磁盘一致：
  · 写文件的地方写完调 changed(tid)，马上生效；
  · 每次读之前列一遍 results/（约 1 ms），新出现、消失的目录当场补上 / 删掉；
  · 每 RESYNC_S 秒在后台按修改时间对一次账，兜住脚本、手工直接改文件；
  · 正文还没进索引的（第一次建索引的那十来秒、刚出现还没建的）搜索时退回直接读文件，结果不会少；
  · SQLite 出任何错都退回逐个读文件，索引坏了不影响功能。
备份只同步 results/，这个库在它外面，不会被带进云备份（它随时能从文件重建）。"""
import json
import os
import sqlite3
import threading
import time

import config

DB_PATH = os.path.join(config.DATA_DIR, 'library.db')
RESULTS = config.RESULTS_FOLDER
RESYNC_S = 60
SEP = '\x1e'               # 正文里句与句之间的分隔符：查询里不会有它，命中不会跨句
_VERSION = 1

_lock = threading.RLock()  # 写库串行：对账、写入点通知
_bg_lock = threading.Lock()
_state = {'synced_at': 0.0, 'names': None, 'bg': None}


# ---------- 库 ----------

def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute('PRAGMA journal_mode=WAL')
    if c.execute('PRAGMA user_version').fetchone()[0] != _VERSION:
        with _lock:
            if c.execute('PRAGMA user_version').fetchone()[0] != _VERSION:
                _schema(c)
    return c


def _schema(c):
    c.executescript('''
        DROP TABLE IF EXISTS docs;
        DROP TABLE IF EXISTS body;
        CREATE TABLE docs (
            rid INTEGER PRIMARY KEY,
            id TEXT UNIQUE NOT NULL,
            date TEXT NOT NULL,
            meta_mtime INTEGER NOT NULL,
            text_mtime INTEGER,
            text_state INTEGER NOT NULL,   -- 0 没有正文 / 1 已进索引 / 2 有正文还没进
            meta TEXT NOT NULL,
            hay TEXT NOT NULL               -- 搜索要看的 meta 字段，小写，\\0 隔开
        );
        CREATE INDEX docs_date ON docs(date DESC);''')
    try:
        c.execute("CREATE VIRTUAL TABLE body USING fts5(text, ts UNINDEXED, tokenize='trigram', detail=none)")
    except sqlite3.OperationalError:   # SQLite 太老没有 trigram：普通表，LIKE 整表扫，仍比读文件快
        c.execute('CREATE TABLE body (rowid INTEGER PRIMARY KEY, text TEXT, ts TEXT)')
    c.execute(f'PRAGMA user_version = {_VERSION}')
    c.commit()


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _hay(meta):
    tags = meta.get('ai_tags') or []
    fields = [meta.get('filename'), meta.get('ai_title'), meta.get('ai_one_line'),
              ' '.join(str(x) for x in tags) if isinstance(tags, list) else str(tags),
              meta.get('video_id'), meta.get('source_url')]
    return '\0'.join(str(h).lower() for h in fields if h)


def _segments(name):
    segs = _read_json(os.path.join(RESULTS, name, 'transcript.json'))
    if not isinstance(segs, list):
        return None
    return [(str(s.get('timestamp', '')), str(s.get('text') or '')) for s in segs if isinstance(s, dict)]


def _drop(c, rid):
    c.execute('DELETE FROM docs WHERE rid = ?', (rid,))
    c.execute('DELETE FROM body WHERE rowid = ?', (rid,))


def _index_text(c, rid, name):
    tm = _mtime(os.path.join(RESULTS, name, 'transcript.json'))
    c.execute('DELETE FROM body WHERE rowid = ?', (rid,))
    segs = _segments(name) if tm is not None else None
    if segs is None:   # 没有正文 / 读不了：记下这个修改时间，文件不变就不再重读
        c.execute('UPDATE docs SET text_state = 0, text_mtime = ? WHERE rid = ?', (tm, rid))
        return
    c.execute('INSERT INTO body (rowid, text, ts) VALUES (?, ?, ?)',
              (rid, SEP.join(x for _, x in segs), SEP.join(t for t, _ in segs)))
    c.execute('UPDATE docs SET text_state = 1, text_mtime = ? WHERE rid = ?', (tm, rid))


def _refresh(c, name, row, text):
    """按磁盘更新一条。row = (rid, meta_mtime, text_mtime, text_state) 或 None。
    返回需要补正文的 rid（text=False 时），否则 None。"""
    mm = _mtime(os.path.join(RESULTS, name, 'meta.json'))
    if mm is None:
        if row:
            _drop(c, row[0])
        return None
    if row is None or row[1] != mm:
        meta = _read_json(os.path.join(RESULTS, name, 'meta.json'))
        if not isinstance(meta, dict):     # 坏的 meta：原来的列表里也没有它
            if row:
                _drop(c, row[0])
            return None
        args = (str(meta.get('date') or ''), mm, json.dumps(meta, ensure_ascii=False), _hay(meta))
        if row:
            c.execute('UPDATE docs SET date = ?, meta_mtime = ?, meta = ?, hay = ? WHERE rid = ?', (*args, row[0]))
        else:
            cur = c.execute('INSERT INTO docs (date, meta_mtime, meta, hay, id, text_state) VALUES (?, ?, ?, ?, ?, 2)',
                            (*args, name))
            row = (cur.lastrowid, mm, None, 2)
    tm = _mtime(os.path.join(RESULTS, name, 'transcript.json'))
    if tm is None:                         # 没有正文
        if row[3] != 0 or row[2] is not None:
            c.execute('DELETE FROM body WHERE rowid = ?', (row[0],))
            c.execute('UPDATE docs SET text_state = 0, text_mtime = NULL WHERE rid = ?', (row[0],))
        return None
    if tm == row[2] and row[3] != 2:
        return None
    if text:
        _index_text(c, row[0], name)
        return None
    c.execute('UPDATE docs SET text_state = 2 WHERE rid = ?', (row[0],))
    return row[0]


def _rows(c, names=None):
    q = 'SELECT id, rid, meta_mtime, text_mtime, text_state FROM docs'
    if names is None:
        return {r[0]: r[1:] for r in c.execute(q)}
    return {r[0]: r[1:] for n in names for r in c.execute(q + ' WHERE id = ?', (n,))}


def _listdir():
    try:
        return set(os.listdir(RESULTS))
    except OSError:
        return set()


# ---------- 对外 ----------

def sync(text=True):
    """跟磁盘全量对账（只重读修改时间变了的文件）。text=False 只对 meta，正文标成待建。"""
    try:
        with _lock:
            c = _conn()
            try:
                names = _listdir()
                known = _rows(c)
                for gone in set(known) - names:
                    _drop(c, known[gone][0])
                for name in names:
                    _refresh(c, name, known.get(name), text=False)
                c.commit()
                pending = c.execute('SELECT rid, id FROM docs WHERE text_state = 2').fetchall()
            finally:
                c.close()
            _state.update(synced_at=time.time(), names=names)
        if text:   # 正文一批 20 条，批与批之间放开锁，写入点通知、别的请求不用等整轮建完
            for i in range(0, len(pending), 20):
                with _lock:
                    c = _conn()
                    try:
                        for rid, name in pending[i:i + 20]:
                            if c.execute('SELECT text_state FROM docs WHERE rid = ?', (rid,)).fetchone() == (2,):
                                _index_text(c, rid, name)
                        c.commit()
                    finally:
                        c.close()
    except sqlite3.OperationalError:   # 锁住、磁盘问题：不是库坏了，别删
        raise
    except sqlite3.DatabaseError as e:  # 库文件坏了：删掉，下次从文件重建
        print(f'[library] 索引库坏了，删掉重建：{e}')
        for suf in ('', '-wal', '-shm'):
            try:
                os.remove(DB_PATH + suf)
            except OSError:
                pass
        raise


def changed(tid):
    """results/<tid>/ 里的文件写完 / 删完以后调：这一条马上按磁盘更新。出错不抛（定时对账会补）。"""
    if not tid or os.sep in tid or tid.startswith('.'):
        return
    try:
        with _lock:
            c = _conn()
            try:
                _refresh(c, tid, _rows(c, [tid]).get(tid), text=True)
                c.commit()
            finally:
                c.close()
    except Exception as e:  # noqa: BLE001
        print(f'[library] 更新 {tid} 失败：{e}')


def start():
    """服务启动时调：后台对一次账（第一次装上时建全部正文索引，约十来秒）。"""
    _kick()


def _kick():
    with _bg_lock:
        if _state['bg'] and _state['bg'].is_alive():
            return
        th = threading.Thread(target=_bg_sync, daemon=True)
        _state['bg'] = th
        th.start()


def _bg_sync():
    try:
        sync()
    except Exception as e:  # noqa: BLE001
        print(f'[library] 后台对账失败：{e}')


def _ensure():
    if not _state['synced_at']:
        sync(text=False)          # 这个进程第一次读：先把 meta 对齐（之后重启只 stat），正文在后台建
        _kick()
        return
    names = _listdir()
    diff = names ^ (_state['names'] or set())
    if diff:                      # 新出现 / 消失的目录：当场补，不等定时对账
        with _lock:
            c = _conn()
            try:
                known = _rows(c, diff)
                for name in diff:
                    _refresh(c, name, known.get(name), text=True)
                c.commit()
            finally:
                c.close()
            _state['names'] = names
    if time.time() - _state['synced_at'] > RESYNC_S:
        _kick()


def entries(limit=None):
    """[(tid, meta)]，按 date 从新到旧；limit 只要最新的几条。"""
    try:
        _ensure()
        c = _conn()
        try:
            rows = c.execute('SELECT id, meta FROM docs ORDER BY date DESC LIMIT ?',
                             (limit if limit else -1,)).fetchall()
        finally:
            c.close()
        return [(tid, json.loads(m)) for tid, m in rows]
    except sqlite3.Error as e:
        print(f'[library] 索引不可用，改读文件：{e}')
        return _entries_from_files(limit)


def search(query):
    """[(tid, meta, snippet)]，按 date 从新到旧。meta 字段（文件名 / 标题 / 一句话 / 标签 / 视频 id / 链接）
    命中的 snippet 为空；否则在正文里找第一句命中的，片段是「[时间] ...前 20 字 命中 后 40 字...」。"""
    q = (query or '').lower()
    if not q:
        return []
    try:
        _ensure()
        c = _conn()
        try:
            hits = {tid: (json.loads(m), '') for tid, m in
                    c.execute('SELECT id, meta FROM docs WHERE instr(hay, ?) > 0', (q,))}
            esc = any(ch in q for ch in '%_\\')
            pat = '%' + (q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') if esc else q) + '%'
            # CROSS JOIN 固定先查 body：让 LIKE 走 trigram 索引，而不是先扫 docs 再逐条比正文
            sql = ('SELECT d.id, d.meta, b.text, b.ts FROM body b CROSS JOIN docs d ON d.rid = b.rowid '
                   'WHERE b.text LIKE ?' + (" ESCAPE '\\'" if esc else '') + ' AND d.text_state = 1')
            for tid, m, text, ts in c.execute(sql, (pat,)):
                if tid not in hits:
                    s = _snippet(zip(ts.split(SEP), text.split(SEP)), q)
                    if s:
                        hits[tid] = (json.loads(m), s)
            for tid, m in c.execute('SELECT id, meta FROM docs WHERE text_state = 2'):
                if tid not in hits:   # 正文还没进索引：直接读文件
                    s = _snippet(_segments(tid) or [], q)
                    if s:
                        hits[tid] = (json.loads(m), s)
        finally:
            c.close()
        out = [(tid, m, s) for tid, (m, s) in hits.items()]
    except sqlite3.Error as e:
        print(f'[library] 索引不可用，改读文件：{e}')
        out = _search_files(q)
    out.sort(key=lambda x: str(x[1].get('date') or ''), reverse=True)
    return out


def _snippet(segs, q):
    for ts, text in segs:
        idx = text.lower().find(q)
        if idx != -1:
            return f"[{ts}] ...{text[max(0, idx - 20):idx + len(q) + 40]}..."
    return ''


# ---------- 索引不可用时：逐个读文件（原来的写法） ----------

def _metas_from_files():
    for name in _listdir():
        meta = _read_json(os.path.join(RESULTS, name, 'meta.json'))
        if isinstance(meta, dict):
            yield name, meta


def _entries_from_files(limit):
    out = sorted(_metas_from_files(), key=lambda x: str(x[1].get('date') or ''), reverse=True)
    return out[:limit] if limit else out


def _search_files(q):
    out = []
    for name, meta in _metas_from_files():
        if q in _hay(meta):
            out.append((name, meta, ''))
            continue
        s = _snippet(_segments(name) or [], q)
        if s:
            out.append((name, meta, s))
    return out
