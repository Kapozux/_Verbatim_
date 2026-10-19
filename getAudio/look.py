"""资料库「问」的查资料命令（2026-10-19，照 Ephemeris 的 look.py）：只读，Claude Code 只被允许跑这一个命令（libask）。人也能在终端直接用。

python look.py search <词> [词…] [--any] [--since D] [--until D] [--creator 名] [--n 30]   哪些转写提到（几个词默认都要出现；标题 / 标签 / 一句话 / 正文）
python look.py hits   <转写> <词> [--n 20]                                                 一份转写里这个词出现的每一句，带时间
python look.py read   <转写> [--at 词 | --from 时:分:秒] [--n 40]                         读一段原文（默认从头；--at 跳到词第一次出现的地方前几句）
python look.py info   <转写>                                                               标题、一句话、标签、来源、时长、摘要和分节
python look.py list   [--since D] [--until D] [--creator 名] [--engine E] [--min 分钟] [--max 分钟] [--n 50]   按时间列转写（最新的在前；--old 最早的在前）
python look.py stats                                                                       一共多少条、多少小时、按月、按引擎、按博主
python look.py schema                                                                      sql 能查的表和列
python look.py sql "<SELECT …>" [--n 30]                                                   自己写只读 SQL（改不了库）

<转写> 是 8 位短号（输出里 [[t:c8a04c4e]] 里那串），也可以是完整 id。
出处记号：[[t:短号]] 是一份转写，[[t:短号@时:分:秒]] 是那份里的某一句。回答时照抄，Verbatim 会认回原文、点了跳到那一秒。
日期 D 写 YYYY-MM-DD；date 是转写的时间（录音、视频是哪天的不一定知道；有 upload_date 的是视频发布日）。
"""
import argparse
import contextlib
import io
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

DB = os.path.join(config.DATA_DIR, 'library.db')
RESULTS = config.RESULTS_FOLDER
SEP = '\x1e'                 # 跟 library.SEP 一样：正文里句与句之间
MAX_SQL_ROWS = 500


def _db():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _one_line(text, n):
    t = ' '.join(str(text or '').split())
    return t if len(t) <= n else t[:n] + '…'


def short(tid):
    return str(tid)[:8]


def _dur(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ''
    return f'{sec // 3600}小时{sec % 3600 // 60}分' if sec >= 3600 else f'{sec // 60}分{sec % 60}秒'


def resolve(ref):
    """短号 / 完整 id → 完整 id；认不出返回 None。"""
    ref = str(ref or '').strip().strip('[]').removeprefix('t:').split('@')[0]
    if not re.fullmatch(r'[0-9a-fA-F-]{6,36}', ref):
        return None
    if os.path.isfile(os.path.join(RESULTS, ref, 'meta.json')):
        return ref
    with _db() as c:
        rows = c.execute('SELECT id FROM docs WHERE id LIKE ? LIMIT 2', (ref.lower() + '%',)).fetchall()
    return rows[0]['id'] if len(rows) == 1 else None


def _meta(tid):
    try:
        with open(os.path.join(RESULTS, tid, 'meta.json'), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _segments(tid):
    try:
        with open(os.path.join(RESULTS, tid, 'transcript.json'), encoding='utf-8') as f:
            segs = json.load(f)
    except (OSError, ValueError):
        return []
    return [(str(s.get('timestamp') or ''), str(s.get('text') or '')) for s in segs if isinstance(s, dict)] \
        if isinstance(segs, list) else []


def _head(tid, m):
    """一份转写的一行：记号、日期、标题、时长、博主、来源。"""
    bits = [f"[[t:{short(tid)}]]", str(m.get('date') or '')[:10], _one_line(m.get('ai_title') or m.get('filename'), 50)]
    if m.get('duration_seconds'):
        bits.append(_dur(m['duration_seconds']))
    if m.get('creator'):
        bits.append('博主 ' + str(m['creator']))
    if m.get('upload_date'):
        bits.append('发布 ' + str(m['upload_date']))
    bits.append('链接' if m.get('source_url') else '上传的文件')
    return ' | '.join(bits)


def _range(args, col='d.date'):
    sql, vals = '', []
    if getattr(args, 'since', None):
        sql += f' AND {col} >= ?'
        vals.append(args.since)
    if getattr(args, 'until', None):
        sql += f' AND {col} < ?'
        vals.append(args.until + '~')            # 当天整天都算（"2026-10-18~" 排在那天所有时间之后）
    return sql, vals


def _creator_ok(m, name):
    return not name or name.lower() in str(m.get('creator') or '').lower()


def _first_hit(text, ts, word, width=40):
    for t, s in zip(ts.split(SEP), text.split(SEP)):
        i = s.lower().find(word.lower())
        if i >= 0:
            a = max(0, i - width)
            return t, ('…' if a else '') + s[a:i + len(word) + width] + ('…' if i + len(word) + width < len(s) else '')
    return None


def cmd_search(args):
    words = [w for w in args.words if w.strip()]
    if not words:
        return '要给至少一个词。'
    rng, vals = _range(args)
    with _db() as c:
        sets = []
        for w in words:
            lw = w.lower()
            ids = {r['id'] for r in c.execute('SELECT id FROM docs d WHERE instr(hay, ?) > 0' + rng, (lw, *vals))}
            pat = '%' + lw.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            ids |= {r['id'] for r in c.execute(
                "SELECT d.id FROM body b CROSS JOIN docs d ON d.rid = b.rowid WHERE b.text LIKE ? ESCAPE '\\'" + rng,
                (pat, *vals))}
            sets.append(ids)
        found = set.union(*sets) if args.any else set.intersection(*sets)
        if not found:
            return f"没有转写{'提到任一个' if args.any else '同时提到'}：{' / '.join(words)}"
        rows = c.execute(f"SELECT d.id, d.meta, b.text, b.ts FROM docs d LEFT JOIN body b ON b.rowid = d.rid "
                         f"WHERE d.id IN ({','.join('?' * len(found))}) ORDER BY d.date {'ASC' if args.old else 'DESC'}",
                         tuple(found)).fetchall()
    out, n = [], 0
    for r in rows:
        m = json.loads(r['meta'])
        if not _creator_ok(m, args.creator):
            continue
        n += 1
        if n > args.n:
            continue
        lines = [_head(r['id'], m)]
        if m.get('ai_one_line'):
            lines.append('    一句话：' + _one_line(m['ai_one_line'], 90))
        for w in words:
            hit = _first_hit(r['text'] or '', r['ts'] or '', w)
            if hit:
                cnt = (r['text'] or '').lower().count(w.lower())
                lines.append(f"    「{w}」{cnt} 处，头一处 [[t:{short(r['id'])}@{hit[0]}]] {hit[1]}")
        out.append('\n'.join(lines))
    more = f'\n（一共 {n} 份，只列了 {args.n} 份；加 --n 或缩小时间范围看更多）' if n > args.n else f'\n（一共 {n} 份）'
    return '\n'.join(out) + more if out else '没有符合条件的转写。'


def cmd_hits(args):
    tid = resolve(args.tid)
    if not tid:
        return f'认不出转写：{args.tid}'
    w = args.word.lower()
    segs = [(t, s) for t, s in _segments(tid) if w in s.lower()]
    if not segs:
        return f'{_head(tid, _meta(tid))}\n里面没有「{args.word}」。'
    lines = [_head(tid, _meta(tid)), f'「{args.word}」出现在 {len(segs)} 句：']
    lines += [f'[[t:{short(tid)}@{t}]] {_one_line(s, 160)}' for t, s in segs[:args.n]]
    if len(segs) > args.n:
        lines.append(f'（还有 {len(segs) - args.n} 句，加 --n 看）')
    return '\n'.join(lines)


def _secs(ts):
    try:
        parts = [float(x) for x in str(ts).split(':')]
    except ValueError:
        return 0.0
    v = 0.0
    for p in parts:
        v = v * 60 + p
    return v


def cmd_read(args):
    tid = resolve(args.tid)
    if not tid:
        return f'认不出转写：{args.tid}'
    segs = _segments(tid)
    if not segs:
        return f'{_head(tid, _meta(tid))}\n没有正文。'
    start = 0
    if args.at:
        i = next((k for k, (_, s) in enumerate(segs) if args.at.lower() in s.lower()), None)
        if i is None:
            return f'里面没有「{args.at}」。'
        start = max(0, i - 3)
    elif args.start:
        t0 = _secs(args.start)
        start = next((k for k, (t, _) in enumerate(segs) if _secs(t) >= t0), len(segs) - 1)
    part = segs[start:start + args.n]
    lines = [_head(tid, _meta(tid)), f'第 {start + 1}–{start + len(part)} 句（共 {len(segs)} 句）：']
    lines += [f'[[t:{short(tid)}@{t}]] {s}' for t, s in part]
    if start + len(part) < len(segs):
        lines.append(f'（后面还有，接着读用 --from {segs[start + len(part)][0]}）')
    return '\n'.join(lines)


def cmd_info(args):
    tid = resolve(args.tid)
    if not tid:
        return f'认不出转写：{args.tid}'
    m = _meta(tid)
    lines = [_head(tid, m), f"文件名：{m.get('filename') or ''}", f"转写引擎：{m.get('engine') or ''}，{m.get('segment_count') or 0} 句"]
    if m.get('source_url'):
        lines.append('来源：' + m['source_url'])
    if m.get('ai_one_line'):
        lines.append('一句话：' + m['ai_one_line'])
    if m.get('ai_tags'):
        lines.append('标签：' + '、'.join(map(str, m['ai_tags'])))
    try:
        with open(os.path.join(RESULTS, tid, 'summary.json'), encoding='utf-8') as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = None
    if isinstance(s, dict):
        if s.get('overview'):
            lines.append('摘要：' + _one_line(s['overview'], 600))
        for sec in (s.get('sections') or [])[:20]:
            start = str(sec.get('time_range') or '').split('-')[0].strip()
            mark = f'[[t:{short(tid)}@{start}]]' if start else ''
            lines.append(f"  · {mark} {sec.get('title') or ''}：{_one_line(sec.get('summary'), 160)}")
    return '\n'.join(lines)


def cmd_list(args):
    rng, vals = _range(args)
    with _db() as c:
        rows = c.execute(f"SELECT id, meta FROM docs d WHERE 1=1{rng} ORDER BY date {'ASC' if args.old else 'DESC'}",
                         vals).fetchall()
    out, n = [], 0
    for r in rows:
        m = json.loads(r['meta'])
        mins = float(m.get('duration_seconds') or 0) / 60
        if not _creator_ok(m, args.creator) or (args.engine and m.get('engine') != args.engine) \
                or (args.min is not None and mins < args.min) or (args.max is not None and mins > args.max):
            continue
        n += 1
        if n <= args.n:
            out.append(_head(r['id'], m) + (f"\n    {_one_line(m.get('ai_one_line'), 90)}" if m.get('ai_one_line') else ''))
    tail = f'\n（一共 {n} 份，只列了 {args.n} 份）' if n > args.n else f'\n（一共 {n} 份）'
    return '\n'.join(out) + tail if out else '这个范围里没有转写。'


def cmd_stats(args):
    with _db() as c:
        metas = [json.loads(r['meta']) for r in c.execute('SELECT meta FROM docs')]
    if not metas:
        return '资料库是空的。'
    from collections import Counter
    hours = sum(float(m.get('duration_seconds') or 0) for m in metas) / 3600
    month = Counter(str(m.get('date') or '')[:7] for m in metas)
    eng = Counter(m.get('engine') or '?' for m in metas)
    who = Counter(m.get('creator') for m in metas if m.get('creator'))
    linked = sum(1 for m in metas if m.get('source_url'))
    lines = [f'一共 {len(metas)} 份转写，约 {hours:.0f} 小时；来自链接 {linked} 份，上传的文件 {len(metas) - linked} 份。',
             '按月：' + '，'.join(f'{k} {v}' for k, v in sorted(month.items())),
             '按引擎：' + '，'.join(f'{k} {v}' for k, v in eng.most_common()),
             '博主（前 20）：' + ('，'.join(f'{k} {v}' for k, v in who.most_common(20)) or '无')]
    return '\n'.join(lines)


SCHEMA = """表 docs：一份转写一行。
  id（完整 id；短号是前 8 位）、date（转写的时间，'YYYY-MM-DD HH:MM:SS'）、meta（JSON 文本，用 json_extract 取）
  meta 里常用的：$.ai_title 标题、$.ai_one_line 一句话、$.ai_tags 标签（数组）、$.filename、$.engine、$.duration_seconds、
  $.segment_count、$.char_count、$.source_url、$.video_id、$.creator 博主、$.upload_date 视频发布日（多数没有）
  text_state：1 = 正文进了索引
表 body：正文，rowid 对 docs.rid；text 是整份正文（句与句之间用 char(30) 隔开），ts 是对应的时间。正文搜词用 search / hits 更快。
例：SELECT substr(date,1,7) m, COUNT(*), ROUND(SUM(json_extract(meta,'$.duration_seconds'))/3600,1) FROM docs GROUP BY m"""


def cmd_sql(args):
    q = args.query.strip().rstrip(';')
    if not re.match(r'(?is)^\s*(select|with)\b', q) or ';' in q:
        return '只能写一条 SELECT 或 WITH。'
    try:
        with _db() as c:
            cur = c.execute(q)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(min(args.n, MAX_SQL_ROWS) + 1)
    except sqlite3.Error as e:
        return f'SQL 出错：{e}'
    lines = [' | '.join(cols)] + [' | '.join(_one_line(v, 120) for v in r) for r in rows[:args.n]]
    if len(rows) > args.n:
        lines.append(f'（只列了 {args.n} 行，加 --n 看更多）')
    return '\n'.join(lines)


def parser():
    p = argparse.ArgumentParser(prog='look', description='查 Verbatim 资料库（只读）')
    sub = p.add_subparsers(dest='cmd', required=True)

    def rng(s):
        s.add_argument('--since')
        s.add_argument('--until')
        s.add_argument('--creator')
        s.add_argument('--old', action='store_true')

    s = sub.add_parser('search')
    s.add_argument('words', nargs='+')
    s.add_argument('--any', action='store_true')
    s.add_argument('--n', type=int, default=30)
    rng(s)
    s = sub.add_parser('hits')
    s.add_argument('tid')
    s.add_argument('word')
    s.add_argument('--n', type=int, default=20)
    s = sub.add_parser('read')
    s.add_argument('tid')
    s.add_argument('--at')
    s.add_argument('--from', dest='start')
    s.add_argument('--n', type=int, default=40)
    s = sub.add_parser('info')
    s.add_argument('tid')
    s = sub.add_parser('list')
    rng(s)
    s.add_argument('--engine')
    s.add_argument('--min', type=float)
    s.add_argument('--max', type=float)
    s.add_argument('--n', type=int, default=50)
    sub.add_parser('stats')
    sub.add_parser('schema')
    s = sub.add_parser('sql')
    s.add_argument('query')
    s.add_argument('--n', type=int, default=30)
    return p


CMDS = {'search': cmd_search, 'hits': cmd_hits, 'read': cmd_read, 'info': cmd_info, 'list': cmd_list,
        'stats': cmd_stats, 'schema': lambda a: SCHEMA, 'sql': cmd_sql}


def run(argv):
    """跑一条命令，返回它要打的字（测试里直接调）。"""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            args = parser().parse_args(argv)
    except SystemExit:
        return '命令写错了。' + __doc__.split('\n\n')[1]
    if not os.path.isfile(DB):
        return '资料库索引还没建好（library.db 不在），等 Verbatim 开着跑一分钟再查。'
    return CMDS[args.cmd](args)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    print(run(sys.argv[1:]))
