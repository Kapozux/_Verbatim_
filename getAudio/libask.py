"""资料库「问」：用一句话描述要找的转写 → 最像的几份 + 为什么是它 + 跳到哪一秒。不是聊天，不写长回答。（2026-10-19）

两边各借一半：
  · 界面和回答的样子照墨页的「问」（pdf2md-web server/find.mjs）：搜索框里一个「✦ 问」，最多 8 份，每份一句理由；
    模型编出来的、重复的都丢掉。
  · 查法照 Ephemeris 的「问 Claude」（ephemeris/qa.py + look.py）：墨页三百来份，整库目录一次交给模型就行；
    Verbatim 三千多份、正文几千万字，而且人记得的常常是录音里说过的话、不是标题——所以让本机 Claude Code 自己跑
    look.py（只读，只放行这一个命令）搜正文、读前后文，查完再挑。
  · 没有 Claude Code（别人装的 Verbatim）：退回墨页的做法，把最新的转写目录（标题 / 一句话 / 标签）一次交给便宜模型挑，
    只看得到标题不看正文，界面上说清楚。

start(q) 后台问（同一时间只问一个）；state() 给前端轮询：steps 是查找进度，result 是挑好的。
"""
import json
import os
import re
import shlex
import sys
import threading
import time
from datetime import date

import claude_cli
import config
import look

MAX_RESULTS = 8
MODEL = os.environ.get('LIBASK_MODEL') or 'claude-sonnet-5-5'    # 找东西不用 Opus：Sonnet 快一倍，订阅额度记 $0
EFFORT = os.environ.get('LIBASK_EFFORT') or 'high'    # 10-19 medium 时它看了摘要就交、不读原文
CATALOG_BUDGET = 60000          # 没有 Claude 时交给便宜模型的目录，最多这么多字（约 3 万 token，flash-lite 一次不到 $0.01）
HERE = os.path.dirname(os.path.abspath(__file__))
LOOK = f'{sys.executable} {os.path.join(HERE, "look.py")}'

PROMPT = """你在帮一个人从他自己的 Verbatim 资料库里找转写（他录的音、上传的文件、转写过的视频）。今天是 {today}。
资料库一共 {total} 份转写。他要找：「{question}」

用 Bash 运行 `{look} <命令>` 查（别的命令都会被拒）：
  search <词> [词…] [--any] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--creator 名] [--mine] [--n 30]   哪些转写提到（默认几个词都要出现；标题 / 标签 / 一句话 / 正文）
  hits   <短号> <词> [--n 20]                    一份转写里这个词出现的每一句，带时间
  read   <短号> [--at 词 | --from 时:分:秒] [--n 40]   读一段原文
  info   <短号>                                   标题、一句话、标签、来源、时长、摘要和分节
  list   [--since …] [--until …] [--creator 名] [--mine] [--engine E] [--min 分钟] [--max 分钟] [--n 50] [--old]   按时间列
  stats                                            总量、按月、按引擎、按博主
  schema / sql "<SELECT …>"                        自己写只读 SQL

怎么找：
- 他记得的多半是片段：说过的一句话、聊的话题、谁说的、大概哪天、什么语言、是录音还是视频。把这些拆开，换几种说法都搜：
  中英文、简称、近义词（「机器学习 / 模型 / SHAP」「考试 / 期末 / exam」）；正文是转写出来的，专有名词常被听错，多试几种写法。
- 说了时间（「上周」「十月」「昨天晚上」）就按今天推算加 --since / --until；说了「我录的」「我自己的」「不是博主的」
  就加 --mine（只看他自己上传的录音 / 文件）；说了博主就加 --creator。
- 搜不到字面的词，多半是他的说法跟录音里的说法不一样（口语、简称、转写听错）：换说法再搜；还不行就用 list --mine（加时间范围）
  按时间翻、用 info 看摘要挑候选。
- **要列进结果的每一份，都必须先用 hits 或 read 读到跟他的描述对得上的原话**，at 填那句话的时间。只看了标题 / 一句话 / 摘要的
  不算找到，不许列——系统会把没读过原文的结果直接丢掉。读了发现对不上就换下一个候选。
- 每次只跑一条完整的命令（照上面的写法原样写全路径）：不要用 shell 变量、不要用 ; && | 把几条连起来——那样会被拒。
- 读原文核实过、把握大了就可以停；把握不大就接着找，宁可多读几份。真找不到就给空数组，在 note 里说查了哪些说法、读了哪几份。

最后只输出 JSON，不要别的文字：
{{"results": [{{"t": "短号", "at": "时:分:秒 或空字符串", "why": "一句话说为什么是它"}}], "note": "可空：一句话补充（比如没找到时说查了什么）"}}
- 最多 {n} 份，最像的排前面；一份都不像就给空数组，不要硬凑。
- 短号只能用查到的（输出里 [[t:短号]] 那串）；at 写他要找的那段话开始的时间（工具输出里 @ 后面那个），说不准就留空。
- why 不超过 40 个字，用他提问的语言，说出跟他描述对得上的地方（「10 月 18 日宿舍录音，7:41 起讲 SHAP」）。
- why 和 note 里提到哪份转写都用标题，不要写短号（他看不懂那串字）。
"""

CATALOG_PROMPT = """你在帮一个人从他自己的转写资料库里找转写。下面是最新的 {shown} 份（一共 {total} 份）的目录，每行：编号 | 转写日期 | 标题 | 一句话简介 | 标签 | 博主或「上传的文件」| 时长。

{catalog}

他要找：「{question}」

从目录里挑出最可能是他要找的，最多 {n} 份，最像的排前面。一份都不像就返回空数组，不要硬凑。
编号只能用目录里出现过的。"why" 用一句话说为什么是它（不超过 30 个字，用他提问的语言）。
只输出 JSON，不要别的文字：{{"results":[{{"n":编号,"why":"…"}}]}}"""

_lock = threading.Lock()
_job = {'running': False, 'q': '', 'total': 0, 'steps': [], 'result': None, 'error': '', 'started': 0.0, 'ended': 0.0, 'mode': ''}
_stop = threading.Event()


def mode():
    """'claude'：本机 Claude Code 自己查正文；'catalog'：只看目录（标题 / 一句话 / 标签）。"""
    return 'claude' if claude_cli.available() else 'catalog'


def state():
    with _lock:
        return {**_job, 'steps': list(_job['steps']), 'now': time.time()}


def stop():
    if not _job['running']:
        return False
    _stop.set()
    return True


def start(question):
    q = ' '.join(str(question or '').split())[:300]
    if not q:
        raise ValueError('empty question')
    with _lock:
        if _job['running']:
            raise RuntimeError('busy')
        _stop.clear()
        _job.update(running=True, q=q, total=_total(), steps=[], result=None, error='', started=time.time(), ended=0.0, mode=mode())
    threading.Thread(target=_run, args=(q,), daemon=True).start()


def _run(q):
    try:
        result = find(q, progress=_step) if _job['mode'] == 'claude' else find_catalog(q)
        with _lock:
            _job.update(result=None if _stop.is_set() else result, error='stopped' if _stop.is_set() else '')
    except Exception as e:                                       # noqa: BLE001
        print(f'[libask] {type(e).__name__}: {e}')
        with _lock:
            _job['error'] = str(e)[:300] or type(e).__name__
    finally:
        with _lock:
            _job.update(running=False, ended=time.time())


def _step(step):
    with _lock:
        _job['steps'].append(step)


def step_of(command):
    """它跑的那条命令 → {'k': 'search', 'd': 'SHAP 俄语'}（前端按 k 翻译成「搜」「读」……）。认不出返回 None。"""
    try:
        parts = shlex.split(command or '')
    except ValueError:
        parts = (command or '').split()
    while parts and parts[0] not in look.CMDS:
        parts.pop(0)
    if not parts:
        return None
    try:
        a = look.parser().parse_args(parts)
    except SystemExit:
        return None
    k = a.cmd
    if k in ('read', 'info', 'hits'):
        tid = look.resolve(a.tid)
        m = look._meta(tid) if tid else {}
        d = m.get('ai_title') or m.get('filename') or a.tid
        return {'k': k, 'd': _clip(f'{d} · {a.word}' if k == 'hits' else d, 80), **({'tid': tid} if tid else {})}
    return {'k': k, 'd': _clip(' '.join(getattr(a, 'words', None) or []), 80)}


def _total():
    try:
        with look._db() as c:
            return c.execute('SELECT COUNT(*) FROM docs').fetchone()[0]
    except Exception:                                            # noqa: BLE001
        return 0


def _json(raw):
    text = str(raw or '')
    a, b = text.find('{'), text.rfind('}')
    if a < 0 or b <= a:
        raise ValueError(f'no JSON in answer: {text[:120]}')
    return json.loads(text[a:b + 1])


def _clip(s, n):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n] + '…'


def _snap(tid, at):
    """模型给的时间 → 那份转写里真有的一句（at 之前最近的一句）；对不上返回 ('', '')。"""
    at = str(at or '').strip()
    if not re.fullmatch(r'\d{1,2}(:[0-5]\d){1,2}', at):
        return '', ''
    t0, best, segs = look._secs(at), None, look._segments(tid)
    if not segs or t0 > look._secs(segs[-1][0]) + 120:          # 比最后一句还晚两分钟：编的
        return '', ''
    for ts, text in segs:
        if look._secs(ts) <= t0 + 0.5:
            best = (ts, text)
        else:
            break
    return best or ('', '')


def card(tid, why, at=''):
    """一份结果：前端照资料库的卡片画，多一句理由、一个跳到那一秒的链接、那一句的原话。"""
    m = look._meta(tid)
    ts, quote = _snap(tid, at) if at else ('', '')
    return {'id': tid, 'why': _clip(why, 80), 'at': ts, 'sec': round(look._secs(ts)) if ts else None,
            'quote': _clip(quote, 160), **{k: m.get(k) for k in ('filename', 'ai_title', 'ai_one_line', 'ai_tags', 'date',
                                                               'engine', 'duration_seconds', 'segment_count', 'source_url',
                                                               'creator')}}


def pick(raw, by_ref, read=None):
    """模型的回答 → 结果卡片；认不出的、重复的丢掉。by_ref(ref) → 完整 id 或 None。
    read：它这次真用 hits / read 读过原文的转写（Claude 自己查时）；不在里面的丢掉——只看了摘要就列上来的不算（10-19 用户：「为什么不看原文」）。"""
    data = _json(raw)
    seen, out = set(), []
    for item in data.get('results') or []:
        if not isinstance(item, dict):
            continue
        tid = by_ref(item.get('t') if 't' in item else item.get('n'))
        if not tid or tid in seen or (read is not None and tid not in read):
            continue
        seen.add(tid)
        out.append(card(tid, item.get('why'), item.get('at') or ''))
        if len(out) >= MAX_RESULTS:
            break
    return out, _clip(data.get('note'), 200)


def find(question, progress=None):
    """本机 Claude Code 自己查（几十秒）。返回 {'results', 'note', 'considered', 'mode'}。"""
    progress = progress or (lambda s: None)
    total = _total()
    prompt = PROMPT.format(today=date.today().isoformat(), total=total, question=question, look=LOOK, n=MAX_RESULTS)

    read = set()

    def seen(kind, text):
        if kind == 'tool':
            s = step_of(text)
            if s:
                progress(s)
                if s['k'] in ('read', 'hits') and s.get('tid'):
                    read.add(s['tid'])
    raw, _ = claude_cli.agent(prompt, LOOK, on_event=seen, model=MODEL, effort=EFFORT, purpose='find', stop=_stop,
                              env={'GETAUDIO_DATA_DIR': config.DATA_DIR})
    if _stop.is_set():
        return None
    results, note = pick(raw, look.resolve, read)
    return {'results': results, 'note': note, 'considered': total, 'mode': 'claude'}


def catalog(budget=CATALOG_BUDGET):
    """最新的转写 → 目录行（编号从 1 开始）。放不下就从旧的那头截掉。返回 (文字, {编号: id}, 一共几份)。"""
    with look._db() as c:
        rows = [(r['id'], json.loads(r['meta'])) for r in c.execute('SELECT id, meta FROM docs ORDER BY date DESC')]
    lines, by_n, used = [], {}, 0
    for i, (tid, m) in enumerate(rows, 1):
        line = ' | '.join([str(i), str(m.get('date') or '')[:10], _clip(m.get('ai_title') or m.get('filename'), 40),
                           _clip(m.get('ai_one_line'), 70), _clip('、'.join(map(str, m.get('ai_tags') or [])), 40),
                           str(m.get('creator') or ('' if m.get('source_url') else '上传的文件')),
                           look._dur(m.get('duration_seconds'))])
        if used + len(line) > budget:
            break
        lines.append(line)
        by_n[i] = tid
        used += len(line) + 1
    return '\n'.join(lines), by_n, len(rows)


def _cheap(prompt):
    import llmroute
    text = llmroute.text(prompt, 'find')
    if text:
        return text
    import ask
    return ask._llm(prompt, purpose='find')


def find_catalog(question, ask=None):
    """没有 Claude Code：目录一次交给便宜模型挑（只看得到标题、一句话、标签）。"""
    text, by_n, total = catalog()
    if not by_n:
        return {'results': [], 'note': '', 'considered': 0, 'mode': 'catalog'}
    raw = (ask or _cheap)(CATALOG_PROMPT.format(shown=len(by_n), total=total, catalog=text, question=question, n=MAX_RESULTS))
    results, note = pick(raw, lambda n: by_n.get(int(n)) if str(n).isdigit() else None)
    return {'results': results, 'note': note, 'considered': len(by_n), 'mode': 'catalog'}
