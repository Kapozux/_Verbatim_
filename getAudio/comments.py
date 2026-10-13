"""B 站评论区（2026-10-13，用户：「这个评论能帮我抓吗」「对一些很有意思的保留一些原话，更多用概述，总数有限制」）。

评论是一期的附属（跟说话人、读书笔记一样属于来源），存在 `results/<task_id>/comments.json`；
整理成文是工作台的产出，见 `build_prompt()` / `parse_digest()`。见 ADR 0019。

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
import json
import os
import random
import re
import subprocess
import tempfile
import time

import config

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
        import asyncio                       # 用到才导入：在请求线程里随手 import comments 不连带 asyncio（几个线程同时首次导入会撞循环导入）
        from bilibili_api import comment
        from bilibili_api.comment import CommentResourceType, OrderType
        return asyncio.run(comment.get_comments_lazy(aid, CommentResourceType.VIDEO, offset=offset,
                                                     order=OrderType.LIKE, credential=self._cred(sessdata=sess)))

    def sub(self, aid, root, pn, sess):
        import asyncio
        from bilibili_api import comment
        from bilibili_api.comment import CommentResourceType
        c = comment.Comment(aid, CommentResourceType.VIDEO, int(root), credential=self._cred(sessdata=sess))
        return asyncio.run(c.get_sub_comments(page_index=pn, page_size=20))


def _sleep(a, b):
    if not _api:
        time.sleep(random.uniform(a, b))


def limits():
    """(最多几楼, 每楼最多几条回复)。10-13 用户：「评论设置个上限，我觉得 500 吧」→ 按楼算 500、每楼 50 条回复；设置里能改。"""
    def num(name, default):
        try:
            return max(1, int(os.environ.get(name) or default))
        except ValueError:
            return default
    return num('COMMENTS_MAX_TOP', 500), num('COMMENTS_MAX_REPLIES', 50)


def fetch(bvid, progress=None, sess=None):
    """一级评论按热度翻，最多 limits()[0] 楼；每楼回复最多翻前两倍上限条，按赞留 limits()[1] 条。
    progress(n_have, n_reported) 随时回报。没登录态抛 RuntimeError。"""
    api = _api or _RealApi()
    sess = sess if sess is not None else sessdata()
    if not sess:
        raise RuntimeError('bili_login')
    max_top, max_rep = limits()
    info = api.view(bvid)
    aid, reported = info['aid'], int(info.get('reply') or 0)
    rows, seen, offset = [], set(), ''

    def add(x):
        if str(x['rpid']) not in seen:
            seen.add(str(x['rpid']))
            rows.append(_row(x))

    def n_top():
        return sum(1 for r in rows if r['root'] == '0')

    for page in range(1, 2000):
        r = api.main(aid, offset, sess)
        for x in ((r.get('top_replies') or []) if page == 1 else []) + (r.get('replies') or []):
            if n_top() >= max_top and str(x['rpid']) not in seen:
                break
            add(x)
            for y in x.get('replies') or []:
                add(y)
        if progress:
            progress(len(rows), reported)
        cur = r.get('cursor') or {}
        offset = ((cur.get('pagination_reply') or {}).get('next_offset')) or ''
        if n_top() >= max_top or cur.get('is_end') or not offset:
            break
        _sleep(0.8, 1.5)

    pages = -(-2 * max_rep // 20)                   # 每楼最多翻这么多页（楼中楼接口只能按时间翻）
    for top in [r for r in rows if r['root'] == '0' and r['rcount'] > 0]:
        have = sum(1 for r in rows if r['root'] == top['id'])
        if have >= min(top['rcount'], max_rep):
            continue
        for pn in range(1, pages + 1):
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

    by_root = {}
    for r in rows:
        if r['root'] != '0':
            by_root.setdefault(r['root'], []).append(r)
    drop = set()
    for root, reps in by_root.items():                # 每楼按赞留前 max_rep 条
        drop.update(r['id'] for r in sorted(reps, key=lambda r: -r['like'])[max_rep:])
    rows = [r for r in rows if r['id'] not in drop]
    capped = n_top() >= max_top or bool(drop)        # 真的截掉了才算（B 站报的回复数里常有删掉的）
    return {'bvid': bvid, 'aid': aid, 'title': info.get('title', ''), 'reported': reported,
            'fetched_at': time.strftime('%Y-%m-%d %H:%M'), 'logged_in': True, 'items': rows,
            'limits': {'top': max_top, 'replies': max_rep}, 'capped': capped}


# ---------- 存 ----------
# 按视频存（results/_comments/<BV>.json）：同一个视频在转写页贴链接下的、转写后看的、笔记本里看的是同一份。
# 下面凡是叫 key 的，可以是期的 task_id，也可以直接是 BV 号（转写页贴链接、还没转写时）。

def _dir():
    return os.path.join(config.RESULTS_FOLDER, '_comments')


def _path(key, suffix='.json'):
    bv = bvid_for_task(key)
    return os.path.join(_dir(), bv + suffix) if bv else ''


def load(key):
    p = _path(key)
    if not p:
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save(key, data):
    p = _path(key)
    os.makedirs(_dir(), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def summary(data):
    """列表 / 来源行用的一行数字。"""
    if not data:
        return None
    items = data.get('items') or []
    return {'count': len(items), 'top': sum(1 for r in items if r['root'] == '0'), 'title': data.get('title', ''),
            'capped': bool(data.get('capped')), 'limits': data.get('limits'),
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


def _norm(s):
    """比原话用：去表情、空白、标点差异（全角半角统一）、大小写。B 站评论里 [doge] 之类很多，逐字比会误判。"""
    import unicodedata
    s = unicodedata.normalize('NFKC', _EMOTE.sub('', s or '')).lower()
    return re.sub(r'[\s\W_]+', '', s)


_QUOTE = re.compile(r'^(\s*>\s*)(.*?)\s*((?:〔\d+〕)+)\s*$')


def _check_quote(line, cites):
    """引用行「> 原话 〔n〕」：原话要真在那条评论里（归一化后是子串）。对不上就换成那条评论的原文，不留改过的话。"""
    m = _QUOTE.match(line)
    if not m or not m.group(2):
        return line
    ns = [int(x) for x in re.findall(r'〔(\d+)〕', m.group(3))]
    q = _norm(m.group(2))
    if not q or any(q in _norm(cites[n - 1]['text']) for n in ns if 0 < n <= len(cites)):
        return line
    real = cites[ns[0] - 1]['text'] if 0 < ns[0] <= len(cites) else ''
    return f'{m.group(1)}{_clip(real, 300)} {m.group(3)}' if real else line


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

    out, prev = [], ''
    for line in _CITE.sub(repl, text).strip().split('\n'):
        line = _check_quote(line, cites)
        if line.lstrip().startswith('>') and prev.lstrip().startswith('>'):
            out.append('')              # 相邻两行引用是两条原话：中间空一行，不然 Markdown 把它们并成一段
        out.append(line)
        prev = line
    return '\n'.join(out), cites


# ---------- 一期的 B 站链接 ----------

def bvid_for_task(tid):
    """期的 task_id → BV 号；本身就是 BV 号的原样返回。"""
    if _BV.fullmatch(tid or ''):
        return tid
    if not re.fullmatch(r'[0-9A-Za-z_-]{1,80}', tid or ''):
        return ''
    try:
        with open(os.path.join(config.RESULTS_FOLDER, tid, 'meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:
        return ''
    vid = str(meta.get('video_id') or '')
    return vid if _BV.fullmatch(vid) else bvid_of(meta.get('source_url') or '')


# ---------- 后台抓（来源那一行 ⋯「抓评论区」）----------

import threading                                   # noqa: E402

_write_slots = threading.BoundedSemaphore(3)       # 同时最多写 3 份总结

_jobs = {}                  # BV -> {'have', 'reported', 'error', 'running'}；只在本进程里，重启就没了（再点一次就行）
_jobs_lock = threading.Lock()


def job_state(key):
    with _jobs_lock:
        j = _jobs.get(bvid_for_task(key))
        return dict(j) if j else None


def _run_fetch(bvid, then=None):
    def prog(have, reported):
        with _jobs_lock:
            _jobs[bvid].update(have=have, reported=reported)
    try:
        save(bvid, fetch(bvid, progress=prog))
        with _jobs_lock:
            _jobs.pop(bvid, None)
    except Exception as e:  # noqa: BLE001
        with _jobs_lock:
            _jobs[bvid].update(error=str(e)[:200], running=False)
        return
    if then is not None:
        auto_digest(bvid, **then)


def auto_digest(key, length='medium', lang='zh', run=None):
    """只下载评论区（不转写）时抓完自动总结一份（10-13 用户：「不下载视频的话，默认做一个总结」）。
    已经有一份总结（做好的或在做的）就不重做；要重做由用户在卡片里点「总结」。"""
    d = load_digest(key)
    if d and d.get('status') in ('done', 'running'):
        return d
    return start_episode_digest(key, length, '', lang, run=run, auto=True)


def start_fetch(key, run=None, then=None):
    """开始抓一个视频的评论；已经在抓就不重复。then={'length','lang'} → 抓完自动总结。返回 (ok, error)。"""
    bvid = bvid_for_task(key)
    if not bvid:
        return False, 'not_bili'
    with _jobs_lock:
        j = _jobs.get(bvid)
        if j and j.get('running'):
            return True, None
        _jobs[bvid] = {'have': 0, 'reported': 0, 'error': None, 'running': True}
    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(lambda: _run_fetch(bvid, then))
    return True, None


def resolve(url):
    """转写页贴的链接 → BV 号。b23.tv 短链跟一次跳转。"""
    bv = bvid_of(url)
    if bv or not re.search(r'b23\.tv/', url or ''):
        return bv
    if _api:
        return bvid_of(_api.expand(url))
    import requests
    try:
        return bvid_of(requests.head(url, allow_redirects=True, timeout=10, headers={'User-Agent': 'Mozilla/5.0'}).url)
    except Exception:
        return ''


# ---------- 工作台「评论区整理」 ----------

def estimate(tids):
    """(美元, 是否走 Claude 订阅)。抓过的按真实提示词算；没抓过的按 2,000 条、每条约 45 个 token 估。"""
    import ask
    import claude_cli
    if claude_cli.text_engine():
        return 0.0, True
    import usage
    have = [(t, d) for t, d in ((t, load(t)) for t in tids) if d]
    tokens = (ask.est_tokens(build_prompt(have)[0]) if have else 0) + 90_000 * (len(tids) - len(have)) + 1500
    return round(usage.estimate_cost(ask.ASK_MODEL, min(tokens, ask.est_tokens('评' * _MAX_INPUT)), 3500) or 0, 4), False


# 模型（10-13 问过 claude-gate 窗口）：都用 Opus，一次整段喂（切块汇总会丢原话和编号）。
# 自动总结 comments-auto：effort low、算 batch 档（给用户自己的提问让额度）；手动点的 comments-digest：effort medium。
# 没有 Claude 时退回 Gemini flash（比 flash-lite 归纳和照抄都稳）。同时最多 3 份，其余排队（不让 20 个线程去抢 Claude 的位子）。
AUTO_PURPOSE, MANUAL_PURPOSE = 'comments-auto', 'comments-digest'
FALLBACK_MODEL = config.GEMINI_EXTRACT_MODEL


def _written_by(ref):
    """这份是哪个模型写的（usage 记账里最后一次调用）。"""
    import usage
    try:
        with usage._conn() as c:
            r = c.execute('SELECT model FROM calls WHERE ref = ? ORDER BY id DESC LIMIT 1', (ref,)).fetchone()
        return r['model'] if r else ''
    except Exception:
        return ''


def _write(out, tids, length, focus, lang, save_out, ref, chain=None, purpose=MANUAL_PURPOSE):
    """整理的主体（工作台和转写页共用）：没抓的期先抓，再让模型写。结果写回 out；save_out() 每步存一次。"""
    import ask
    import usage
    try:
        todo = [t for t in tids if not load(t)]
        for i, t in enumerate(todo):
            out['progress'] = {'stage': 'fetch', 'done': i, 'total': len(todo)}
            save_out()
            save(t, fetch(bvid_for_task(t)))
        out['progress'] = {'stage': 'queue', 'done': 0, 'total': 1}
        save_out()
        with _write_slots:
            out['progress'] = {'stage': 'write', 'done': 0, 'total': 1}
            save_out()
            data = [(t, load(t)) for t in tids]
            prompt, refs = build_prompt(data, length, focus, lang)
            with usage.scope(ref=ref, chain=chain):
                text = ask._llm(prompt, model=FALLBACK_MODEL, purpose=purpose)
        out['model'] = _written_by(ref)
        md, cites = parse_digest(text or '', refs)
        if not md:
            raise RuntimeError('empty')     # 多半是 Gemini 安全拦截（时政评论）返回空；界面上说清楚
        out.update(status='done', result={'md': md}, citations=cites,
                   n=sum(len(d.get('items') or []) for _, d in data),
                   title=' / '.join(d.get('title') or '' for _, d in data)[:120])
        out.pop('progress', None)
    except Exception as e:  # noqa: BLE001
        out.update(status='failed', error=str(e)[:300])
    finally:
        out['cost_usd'] = (usage.cost_for(ref=ref) or {}).get('cost_usd', 0)
        save_out()


def _clean(tids, length):
    tids = [t for t in dict.fromkeys(tids or []) if bvid_for_task(t)]
    if not tids:
        raise ValueError('no_bili')
    return tids, (length if length in LENGTHS else 'medium')


def start_digest(cdir, chain_id, tids, length='medium', focus='', lang='zh', run=None, titles=None):
    """工作台「评论区整理」：几期一起整理，结果存成工作台的一条 kind=comments。"""
    import study
    import uuid
    tids, length = _clean(tids, length)
    oid = uuid.uuid4().hex[:12]
    out = {'id': oid, 'kind': 'comments', 'status': 'running', 'created_at': study._now(), 'chain': chain_id,
           'tids': tids, 'length': length, 'focus': str(focus or '')[:300],
           'titles': [str((titles or {}).get(t) or '') for t in tids]}
    if any(not load(t) for t in tids):
        out['progress'] = {'stage': 'fetch', 'done': 0, 'total': sum(1 for t in tids if not load(t))}
    study._save(cdir, out)
    with study._lock:
        study._running.add(oid)

    def job():
        try:
            _write(out, tids, length, focus, lang, lambda: study._save(cdir, out), 'study:' + oid, chain_id)
        finally:
            with study._lock:
                study._running.discard(oid)
    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(job)
    return out


# ---------- 一个视频自己的总结（转写页贴链接、转写详情页；存在 results/_comments/<BV>.digest.json）----------

_digesting = set()          # 本进程里在总结的 BV；重启后残留的 running 当成中断


def load_digest(key):
    p = _path(key, '.digest.json')
    try:
        with open(p, encoding='utf-8') as f:
            o = json.load(f)
    except Exception:
        return None
    if o.get('status') == 'running' and bvid_for_task(key) not in _digesting:
        o.update(status='failed', error='interrupted')
    return o


def _save_digest(key, out):
    p = _path(key, '.digest.json')
    os.makedirs(_dir(), exist_ok=True)
    with open(p + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(p + '.tmp', p)


def start_episode_digest(key, length='medium', focus='', lang='zh', run=None, auto=False):
    """一个视频的「总结」：新的盖掉旧的。在总结就不重复开。没抓过评论的先抓。"""
    import study
    bv = bvid_for_task(key)
    tids, length = _clean([key], length)
    with _jobs_lock:
        if bv in _digesting:
            return load_digest(key)
        _digesting.add(bv)
    out = {'kind': 'comments', 'status': 'running', 'created_at': study._now(), 'tids': tids,
           'length': length, 'focus': str(focus or '')[:300], 'auto': bool(auto)}
    if not load(key):
        out['progress'] = {'stage': 'fetch', 'done': 0, 'total': 1}
    _save_digest(key, out)

    def job():
        try:
            _write(out, tids, length, focus, lang, lambda: _save_digest(key, out), f'comments:{bv}:{time.time():.0f}',
                   purpose=AUTO_PURPOSE if auto else MANUAL_PURPOSE)
        finally:
            with _jobs_lock:
                _digesting.discard(bv)
    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(job)
    return out
