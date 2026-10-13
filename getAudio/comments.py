"""B 站评论区（2026-10-13，用户：「这个评论能帮我抓吗」「对一些很有意思的保留一些原话，更多用概述，总数有限制」）。

评论是一期的附属（跟说话人、读书笔记一样属于来源），存在 `results/<task_id>/comments.json`；
整理成文是工作台的产出，见 `build_prompt()` / `parse_digest()`。见 ADR 0018。

    bvid_of(url)                      → 'BV…' / ''
    sessdata()                        → 能用的登录态；失效了自己从浏览器取新的（用户：「我啥都不动，它自己就行了」）
    fetch(bvid, progress=None)        → {'bvid','aid','title','fetched_at','logged_in','reported','items':[…]}
    load(tid) / save(tid, data)       → 读写 comments.json
    threads(data)                     → 按楼组织：[{top, replies:[…]}]，楼按赞排
    build_prompt(data_list, …)        → 给模型的整理提示（评论带 c 编号，原话要标编号）
    parse_digest(text, data_list)     → 把正文里的 [c12] 换成出处列表

不登录时 B 站只给前几条（10-13 实测 3,034 条的视频只拿到 3 条），所以抓之前先确认登录态，
拿不到就直说，不默默只抓 3 条。
"""
import asyncio
import json
import os
import random
import re
import subprocess
import tempfile
import time

import config

FILE = 'comments.json'
LOGIN_FILE = os.path.join(config.DATA_DIR, 'bili_login.local.json')   # 自己续到的登录态，gitignore
_BV = re.compile(r'(BV[0-9A-Za-z]{10})')
_EMOTE = re.compile(r'\[[^\[\]]{1,15}\]')
_check = {'value': '', 'ok': None, 'at': 0.0}       # 登录态检查结果缓存（一小时）

# 测试用：use_api(fake)，fake 有 nav(sess) / view(bvid) / main(aid, offset, sess) / sub(aid, root, pn, sess) / browser()
_api = None


def use_api(fake):
    global _api
    _api = fake


def bvid_of(url):
    m = _BV.search(url or '')
    return m.group(1) if m else ''


# ---------- 登录态 ----------

def _saved():
    try:
        with open(LOGIN_FILE, encoding='utf-8') as f:
            return (json.load(f).get('sessdata') or '').strip()
    except Exception:
        return ''


def _save(value):
    tmp = LOGIN_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'sessdata': value, 'saved_at': time.strftime('%Y-%m-%d %H:%M:%S')}, f)
    os.replace(tmp, LOGIN_FILE)


def _nav_ok(value):
    if not value:
        return False
    if _api:
        return bool(_api.nav(value))
    import requests
    try:
        r = requests.get('https://api.bilibili.com/x/web-interface/nav', cookies={'SESSDATA': value},
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        return bool((r.json().get('data') or {}).get('isLogin'))
    except Exception:
        return False


def _from_browser():
    """借 yt-dlp 读浏览器里存的 B 站 cookie（只读 cookie 文件，不启动浏览器）。跟下载用的是同一个浏览器设置。"""
    if _api:
        return _api.browser()
    browser = config.YTDLP_COOKIES_FROM_BROWSER
    if not browser or not config.YTDLP_BIN:
        return ''
    with tempfile.TemporaryDirectory() as d:
        jar = os.path.join(d, 'c.txt')
        try:
            subprocess.run([config.YTDLP_BIN, '--cookies-from-browser', browser, '--cookies', jar,
                            '--skip-download', '-q', '--no-warnings', 'https://www.bilibili.com/'],
                           capture_output=True, timeout=60)
            with open(jar, encoding='utf-8') as f:
                for line in f:
                    p = line.rstrip('\n').split('\t')
                    if len(p) == 7 and p[0].endswith('bilibili.com') and p[5] == 'SESSDATA':
                        return p[6]
        except Exception:
            pass
    return ''


def sessdata(force=False):
    """先用存着的（自己续的 > .env / 设置里的），不行就从浏览器取一个新的存下。都不行返回 ''。"""
    now = time.time()
    if not force and _check['ok'] is not None and now - _check['at'] < 3600:
        return _check['value'] if _check['ok'] else ''
    for value in dict.fromkeys(v for v in (_saved(), os.environ.get('BILI_SESSDATA', '').strip()) if v):
        if _nav_ok(value):
            _check.update(value=value, ok=True, at=now)
            return value
    fresh = _from_browser()
    if fresh and _nav_ok(fresh):
        _save(fresh)
        _check.update(value=fresh, ok=True, at=now)
        return fresh
    _check.update(value='', ok=False, at=now)
    return ''


# ---------- 抓 ----------

def _row(x):
    return {'id': str(x['rpid']), 'root': str(x.get('root') or 0), 'parent': str(x.get('parent') or 0),
            'user': (x.get('member') or {}).get('uname', ''), 'like': int(x.get('like') or 0),
            'ctime': int(x.get('ctime') or 0), 'rcount': int(x.get('rcount') or 0),
            'text': ((x.get('content') or {}).get('message') or '').strip()}


class _RealApi:
    def __init__(self):
        from bilibili_api import Credential
        self._cred = Credential

    def view(self, bvid):
        import requests
        r = requests.get('https://api.bilibili.com/x/web-interface/view', params={'bvid': bvid},
                         headers={'User-Agent': 'Mozilla/5.0'}, timeout=15).json()
        d = r.get('data') or {}
        return {'aid': d.get('aid'), 'title': d.get('title', ''), 'reply': (d.get('stat') or {}).get('reply', 0)}

    def main(self, aid, offset, sess):
        from bilibili_api import comment
        from bilibili_api.comment import CommentResourceType, OrderType
        return asyncio.run(comment.get_comments_lazy(aid, CommentResourceType.VIDEO, offset=offset,
                                                     order=OrderType.TIME, credential=self._cred(sessdata=sess)))

    def sub(self, aid, root, pn, sess):
        from bilibili_api import comment
        from bilibili_api.comment import CommentResourceType
        c = comment.Comment(aid, CommentResourceType.VIDEO, int(root), credential=self._cred(sessdata=sess))
        return asyncio.run(c.get_sub_comments(page_index=pn, page_size=20))


def _sleep(a, b):
    if not _api:
        time.sleep(random.uniform(a, b))


def fetch(bvid, progress=None, sess=None):
    """一级评论按时间翻完，再把每楼的回复翻全。progress(n_have, n_reported) 随时回报。没登录态抛 RuntimeError。"""
    api = _api or _RealApi()
    sess = sess if sess is not None else sessdata()
    if not sess:
        raise RuntimeError('bili_login')
    info = api.view(bvid)
    aid, reported = info['aid'], int(info.get('reply') or 0)
    rows, seen, offset = [], set(), ''

    def add(x):
        if str(x['rpid']) not in seen:
            seen.add(str(x['rpid']))
            rows.append(_row(x))

    for page in range(1, 2000):
        r = api.main(aid, offset, sess)
        for x in (r.get('top_replies') or []) if page == 1 else []:
            add(x)
        for x in r.get('replies') or []:
            add(x)
            for y in x.get('replies') or []:
                add(y)
        if progress:
            progress(len(rows), reported)
        cur = r.get('cursor') or {}
        offset = ((cur.get('pagination_reply') or {}).get('next_offset')) or ''
        if cur.get('is_end') or not offset:
            break
        _sleep(0.8, 1.5)

    for top in [r for r in rows if r['root'] == '0' and r['rcount'] > 0]:
        have = sum(1 for r in rows if r['root'] == top['id'])
        if have >= top['rcount']:
            continue
        for pn in range(1, 500):
            d = api.sub(aid, top['id'], pn, sess)
            reps = d.get('replies') or []
            for x in reps:
                add(x)
            if len(reps) < 20:
                break
            _sleep(0.5, 1.0)
        if progress:
            progress(len(rows), reported)
        _sleep(0.4, 0.8)

    return {'bvid': bvid, 'aid': aid, 'title': info.get('title', ''), 'reported': reported,
            'fetched_at': time.strftime('%Y-%m-%d %H:%M'), 'logged_in': True, 'items': rows}


# ---------- 存 ----------

def _path(tid):
    return os.path.join(config.RESULTS_FOLDER, tid, FILE)


def load(tid):
    try:
        with open(_path(tid), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save(tid, data):
    p = _path(tid)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def summary(data):
    """列表 / 来源行用的一行数字。"""
    if not data:
        return None
    items = data.get('items') or []
    return {'count': len(items), 'top': sum(1 for r in items if r['root'] == '0'),
            'reported': data.get('reported', 0), 'fetched_at': data.get('fetched_at', '')}


def threads(data):
    items = (data or {}).get('items') or []
    by_root = {}
    for r in items:
        if r['root'] != '0':
            by_root.setdefault(r['root'], []).append(r)
    tops = sorted((r for r in items if r['root'] == '0'), key=lambda r: (-r['like'], r['ctime']))
    return [{'top': t, 'replies': sorted(by_root.get(t['id'], []), key=lambda r: r['ctime'])} for t in tops]


# ---------- 整理 ----------

LENGTHS = {'short': 800, 'medium': 2000, 'long': 4000}
_MAX_INPUT = 400_000           # 送进模型的评论正文最多这么多字（约 1 万条短评）；再多按赞截掉


def _clip(s, n):
    s = re.sub(r'\s+', ' ', s)
    return s if len(s) <= n else s[:n] + '…'


def _meaningless(text):
    return not re.sub(r'\s+', '', _EMOTE.sub('', text or ''))


def corpus(data_list):
    """把几期的评论排成给模型读的文本。编号 c1、c2…… 全局唯一；返回 (文本, {编号: (tid, 评论)})。"""
    lines, refs, n, used = [], {}, 0, 0
    for tid, data in data_list:
        lines.append(f'### 视频：{data.get("title") or tid}（{len(data.get("items") or [])} 条评论）')
        for th in threads(data):
            group = [th['top']] + sorted(th['replies'], key=lambda r: -r['like'])
            for i, r in enumerate(group):
                if _meaningless(r['text']):
                    continue
                n += 1
                cid = f'c{n}'
                refs[cid] = (tid, r)
                line = (f'[{cid}] {r["like"]}赞'
                        + (f' {th["top"]["rcount"]}回' if i == 0 else '') + ' ' + _clip(r['text'], 400))
                line = line if i == 0 else '  └' + line
                used += len(line)
                if used > _MAX_INPUT:
                    lines.append('（后面赞更少的评论放不下，略）')
                    return '\n'.join(lines), refs
                lines.append(line)
    return '\n'.join(lines), refs


def build_prompt(data_list, length='medium', focus='', lang='zh'):
    text, refs = corpus(data_list)
    limit = LENGTHS.get(length, LENGTHS['medium'])
    total = sum(len(d.get('items') or []) for _, d in data_list)
    out_lang = '中文' if lang == 'zh' else 'English'
    prompt = f"""下面是 B 站视频评论区的全部评论（共 {total} 条），一级评论按赞排，└ 是这一楼下面的回复，按赞排。
每条前面的 [c编号] 是它的编号。

请把评论区整理成一份 {limit} 字左右（不超过 {int(limit * 1.2)} 字）的 Markdown，用{out_lang}写：

## 主要讨论方向
按热度分 4–7 类。每类一个 **加粗小标题** + 两三句概述（大家怎么说、主流和少数派各是什么），
再挑 1–3 条最有代表性、最有意思的原话，用 > 引用，一字不改，后面紧跟它的编号，例如：
> 以为自己包了个私家车，结果私家车晚上还偷偷背着自己出去跑滴滴 [c12]

## 值得单独看的几条
3–5 条：有梗、有信息量、或者引发大量争论的楼。一句话说清它是什么，原话照上面的规矩标编号。

## 整体氛围
三四条要点：评论区站哪边、少数派说什么、刷屏的梗、广告 / @ 好友这类噪音占多少。

规矩：
- 原话只能从下面复制，一字不改；每条原话后面都要有编号，编号必须是下面真有的。
- 概述里提到的说法如果来自某条评论，也可以在句末标编号。
- 不要编造评论里没有的事实；评论区网友对当事人背景的说法，写成「评论区说」，不要当成事实。
- 只输出整理正文，不要开场白。
{('- 用户的侧重：' + focus.strip()) if focus.strip() else ''}

评论：
{text}"""
    return prompt, refs


_CITE = re.compile(r'\[(c\d+(?:\s*[,，]\s*c\d+)*)\]')


def parse_digest(text, refs):
    """[c12] → 出处。返回 (正文, 出处列表)；正文里的编号换成 〔1〕〔2〕……（出处列表的下标 + 1），编错的编号直接删掉。"""
    order, cites = {}, []

    def repl(m):
        out = []
        for cid in re.split(r'\s*[,，]\s*', m.group(1)):
            if cid not in refs:
                continue
            if cid not in order:
                tid, r = refs[cid]
                order[cid] = len(cites) + 1
                cites.append({'n': order[cid], 'tid': tid, 'id': r['id'], 'root': r['root'], 'user': r['user'],
                              'like': r['like'], 'ctime': r['ctime'], 'text': r['text']})
            out.append(f'〔{order[cid]}〕')
        return ''.join(out)

    return _CITE.sub(repl, text).strip(), cites


# ---------- 一期的 B 站链接 ----------

def bvid_for_task(tid):
    try:
        with open(os.path.join(config.RESULTS_FOLDER, tid, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:
        return ''
    vid = str(meta.get('video_id') or '')
    return vid if _BV.fullmatch(vid) else bvid_of(meta.get('source_url') or '')


# ---------- 后台抓（来源那一行 ⋯「抓评论区」）----------

import threading                                   # noqa: E402

_jobs = {}                  # tid -> {'have', 'reported', 'error'}；只在本进程里，重启就没了（再点一次就行）
_jobs_lock = threading.Lock()


def job_state(tid):
    with _jobs_lock:
        j = _jobs.get(tid)
        return dict(j) if j else None


def _run_fetch(tid, bvid):
    def prog(have, reported):
        with _jobs_lock:
            _jobs[tid].update(have=have, reported=reported)
    try:
        save(tid, fetch(bvid, progress=prog))
        with _jobs_lock:
            _jobs.pop(tid, None)
    except Exception as e:  # noqa: BLE001
        with _jobs_lock:
            _jobs[tid].update(error=str(e)[:200], running=False)


def start_fetch(tid, run=None):
    """开始抓一期；已经在抓就不重复。返回 (ok, error)。"""
    bvid = bvid_for_task(tid)
    if not bvid:
        return False, 'not_bili'
    with _jobs_lock:
        j = _jobs.get(tid)
        if j and j.get('running'):
            return True, None
        _jobs[tid] = {'have': 0, 'reported': 0, 'error': None, 'running': True}
    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(lambda: _run_fetch(tid, bvid))
    return True, None


# ---------- 工作台「评论区整理」 ----------

def estimate(tids):
    """(美元, 是否走 Claude 订阅)。没抓过的期按 2,000 条、每条 40 字估。"""
    import ask
    import claude_cli
    chars = 0
    for tid in tids:
        d = load(tid)
        chars += sum(len(r['text']) + 12 for r in d['items']) if d else 2000 * 52
    if claude_cli.text_engine():
        return 0.0, True
    import usage
    tokens = ask.est_tokens('评' * min(chars, _MAX_INPUT)) + 1500
    return round(usage.estimate_cost(ask.ASK_MODEL, tokens, 3500) or 0, 4), False


def start_digest(cdir, chain_id, tids, length='medium', focus='', lang='zh', run=None, titles=None):
    """后台：没抓过评论的期先抓，再整理成一份。结果存成工作台的一条 kind=comments。"""
    import ask
    import study
    import usage
    import uuid
    tids = [t for t in dict.fromkeys(tids or []) if bvid_for_task(t)]
    if not tids:
        raise ValueError('no_bili')
    length = length if length in LENGTHS else 'medium'
    oid = uuid.uuid4().hex[:12]
    out = {'id': oid, 'kind': 'comments', 'status': 'running', 'created_at': study._now(), 'chain': chain_id,
           'tids': tids, 'length': length, 'focus': str(focus or '')[:300],
           'titles': [str((titles or {}).get(t) or '') for t in tids]}
    todo = [t for t in tids if not load(t)]
    if todo:
        out['progress'] = {'stage': 'fetch', 'done': 0, 'total': len(todo)}
    study._save(cdir, out)
    with study._lock:
        study._running.add(oid)

    def job():
        try:
            for i, t in enumerate(todo):
                out['progress'] = {'stage': 'fetch', 'done': i, 'total': len(todo)}
                study._save(cdir, out)
                save(t, fetch(bvid_for_task(t)))
            out['progress'] = {'stage': 'write', 'done': 0, 'total': 1}
            study._save(cdir, out)
            data = [(t, load(t)) for t in tids]
            prompt, refs = build_prompt(data, length, focus, lang)
            with usage.scope(ref='study:' + oid, chain=chain_id):
                text = ask._llm(prompt, purpose='digest')
            md, cites = parse_digest(text or '', refs)
            if not md:
                raise RuntimeError('empty')
            out.update(status='done', result={'md': md}, citations=cites,
                       n=sum(len(d.get('items') or []) for _, d in data),
                       title=' / '.join(d.get('title') or '' for _, d in data)[:120])
            out.pop('progress', None)
        except Exception as e:  # noqa: BLE001
            out.update(status='failed', error=str(e)[:300])
        finally:
            out['cost_usd'] = (usage.cost_for(ref='study:' + oid) or {}).get('cost_usd', 0)
            study._save(cdir, out)
            with study._lock:
                study._running.discard(oid)
    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(job)
    return out
