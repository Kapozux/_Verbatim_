"""
Reflect：回顾你这段时间在听什么（仿 Claude 的 Reflect 面板）。

纯统计部分（最活跃星期、高峰时段、按日曲线、主题占比）每次现算，很快；
叙事标题、一段话、每个主题的一句说明由 Claude Opus 4.6（走 OpenRouter）写，
没配 OpenRouter key 时回落到 Gemini Flash。中文、英文两个请求并行发出、一起写入
results/_reflect_cache.json（按 时段:语言 存）。

生成是提前做的，打开面板不用等：
  - 服务启动 90 秒后、之后每 6 小时，后台把四个时段里数据变了的重算一遍；
  - 每有转写完成，10 分钟防抖后再算一次（一批下载 100 条只触发一次）；
  - 打开面板时如果缓存已过期，先把旧的那份返回给前端显示，同时后台重算，
    前端轮询到新的再替换；只有手动点刷新才同步等待。
"""

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from datetime import datetime, timedelta

from config import GEMINI_API_KEY, GEMINI_ENRICH_MODEL, OPENROUTER_COMPAT_BASE, make_gemini_client
from enrich import _parse_json_obj

NARRATIVE_MODEL = 'anthropic/claude-opus-4.6'   # OpenRouter 模型名

RANGES = {'1m': 1, '3m': 3, '6m': 6, '12m': 12}
TOP_N = 5            # 主题条最多显示几段（其余并入"其它"）
MAX_ITEMS_FOR_LLM = 260

WEEKDAYS = {
    'en': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
    'zh': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'],
}


# ---------- 读数据 ----------

def _load_items(results_dir):
    items = []
    if not os.path.isdir(results_dir):
        return items
    for name in os.listdir(results_dir):
        if name.startswith('_'):
            continue
        p = os.path.join(results_dir, name, 'meta.json')
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                m = json.load(f)
        except Exception:
            continue
        try:
            dt = datetime.strptime((m.get('date') or '')[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue
        items.append({
            'id': m.get('id') or name,
            'dt': dt,
            'minutes': (m.get('duration_seconds') or 0) / 60.0,
            'title': m.get('ai_title') or m.get('filename') or '',
            'one_line': m.get('ai_one_line') or '',
            'tags': [t for t in (m.get('ai_tags') or []) if t],
        })
    return items


def _months_ago(now, months):
    """now 往前推 months 个月（日期溢出就压到该月最后一天）。"""
    y, mo = now.year, now.month - months
    while mo <= 0:
        y -= 1
        mo += 12
    import calendar
    d = min(now.day, calendar.monthrange(y, mo)[1])
    return now.replace(year=y, month=mo, day=d)


# ---------- 统计 ----------

def _daily_series(items, start, end):
    """按自然日铺满 [start, end]，每天 {date, count, minutes}。"""
    cnt = defaultdict(int)
    mins = defaultdict(float)
    for it in items:
        k = it['dt'].strftime('%Y-%m-%d')
        cnt[k] += 1
        mins[k] += it['minutes']
    out = []
    d = start.date()
    while d <= end.date():
        k = d.strftime('%Y-%m-%d')
        out.append({'date': k, 'count': cnt.get(k, 0),
                    'minutes': round(mins.get(k, 0.0), 1)})
        d += timedelta(days=1)
    return out


def _topic_shares(items):
    """主题占比。

    每条转写有 2-4 个标签，直接均分会被几百个长尾标签摊薄（"其它"占七成）。
    所以先按总权重给标签排名，再把每条整体计入它所带标签里排名最高的那个，
    长尾标签自然被吸进大主题。取前 TOP_N + 其它。
    """
    freq = defaultdict(float)
    for it in items:
        w = it['minutes'] if it['minutes'] > 0 else 1.0   # 没时长的按 1 分钟算
        tags = it['tags'] or ['未分类']
        for t in tags:
            freq[t] += w / len(tags)
    rank = {t: k for k, (t, _) in enumerate(sorted(freq.items(), key=lambda kv: -kv[1]))}

    weight = defaultdict(float)
    count = defaultdict(int)
    total = 0.0
    for it in items:
        w = it['minutes'] if it['minutes'] > 0 else 1.0
        total += w
        t = min(it['tags'] or ['未分类'], key=lambda x: rank.get(x, 1e9))
        weight[t] += w
        count[t] += 1
    if total <= 0:
        return []
    ranked = sorted(weight.items(), key=lambda kv: -kv[1])
    top = ranked[:TOP_N]
    rest = ranked[TOP_N:]
    topics = [{'tag': t, 'minutes': round(w, 1), 'count': count[t],
               'share': w / total} for t, w in top]
    if rest:
        rw = sum(w for _, w in rest)
        topics.append({'tag': '__other__', 'minutes': round(rw, 1),
                       'count': sum(count[t] for t, _ in rest), 'share': rw / total})
    # 百分比四舍五入后补差，保证加起来 100
    pcts = [int(round(t['share'] * 100)) for t in topics]
    if pcts:
        pcts[0] += 100 - sum(pcts)
    for t, p in zip(topics, pcts):
        t['percent'] = max(p, 0)
        del t['share']
    return topics


def compute(results_dir, range_key='1m', now=None):
    months = RANGES.get(range_key, 1)
    now = now or datetime.now()
    start = _months_ago(now, months)
    prev_start = _months_ago(now, months * 2)

    all_items = _load_items(results_dir)
    cur = [i for i in all_items if start <= i['dt'] <= now]
    prev = [i for i in all_items if prev_start <= i['dt'] < start]
    cur.sort(key=lambda i: i['dt'])

    wd = defaultdict(int)
    hr = defaultdict(int)
    for it in cur:
        wd[it['dt'].weekday()] += 1
        hr[it['dt'].hour] += 1
    top_wd = max(wd, key=wd.get) if wd else None
    top_hr = max(hr, key=hr.get) if hr else None

    series = _daily_series(cur, start, now)
    prev_series = _daily_series(prev, prev_start, start - timedelta(days=1))
    # 上一周期按"第几天"对齐到本期，长度截到一致
    prev_series = prev_series[-len(series):] if len(prev_series) >= len(series) else prev_series

    return {
        'range': range_key,
        'period': {'start': start.strftime('%Y-%m-%d'), 'end': now.strftime('%Y-%m-%d')},
        'totals': {
            'count': len(cur),
            'hours': round(sum(i['minutes'] for i in cur) / 60, 1),
            'prev_count': len(prev),
            'prev_hours': round(sum(i['minutes'] for i in prev) / 60, 1),
        },
        'most_active_weekday': top_wd,           # 0=Mon … 6=Sun，None=没数据
        'peak_hour': top_hr,                      # 0-23
        'weekday_counts': [wd.get(i, 0) for i in range(7)],
        'hour_counts': [hr.get(i, 0) for i in range(24)],
        'series': series,
        'prev_series': prev_series,
        'topics': _topic_shares(cur),
        '_items': cur,                            # 给 LLM 用，出接口前删掉
    }


# ---------- 叙事（LLM） ----------

NARRATIVE_PROMPT = {
    'zh': """下面是一个人在 {period} 期间用转写工具转写过的音视频清单（每行：日期 | 时长分钟 | 标题 | 一句话简介 | 标签），
以及按时长算出的主题占比。请写一份简短的回顾。

风格要求：直接、具体、说人话。像朋友看完你的收听记录后直接告诉你"你这段时间主要在听什么"。
不要比喻，不要抒情，不要"仿佛置身""沉潜""画卷"这类修辞，不要评价好坏，不要给建议，不要罗列数字。
可以直接点名博主、节目、具体话题。

输出三部分：
1. headline：一句话概括这段时间在听什么，不超过 20 个字，直接陈述，例如「主要在听中国政治评论和立党的求职讲座」。不要冒号、感叹号、书名号。
2. narrative：一段 80-130 字。第一句说最主要在听什么；然后说第二、第三大的内容是什么；如果有明显变化（比如后半段转向了别的主题）说一句；最后可以提一个反复出现的具体话题或人。
3. topics：对下面每个主题标签，给一个具体的名字（name，不超过 10 个字，说清在这个标签下实际听的是什么）和一句说明（desc，不超过 35 字，直接说内容）。"__other__" 这项 name 固定写「其它」，desc 一句话说剩下零散的是什么。

主题占比：
{topics}

内容清单（共 {n} 条{truncated}）：
{items}

严格按以下 JSON 输出，不要输出其他任何内容：
{{"headline": "...", "narrative": "...", "topics": [{{"tag": "原标签", "name": "...", "desc": "..."}}]}}""",

    'en': """Below is a list of audio/video a person transcribed during {period} (one per line: date | minutes |
title | one-line summary | tags), plus the share of listening time per topic. Write a short recap.

Style: direct, concrete, plain. Like a friend who looked at your listening history and tells you straight
what you mostly listened to. No metaphors, no lyrical language, no judgement, no advice, no listing numbers.
Name creators, shows and specific topics directly.

Output three parts:
1. headline: one plain sentence saying what this period was mostly about, at most 12 words, e.g.
   "Mostly Chinese political commentary and Lidang's career talks". No colons, no exclamation marks.
2. narrative: one paragraph of 60-100 words. First sentence: the main thing they listened to. Then the
   second and third biggest things. If there was a clear shift (e.g. the later weeks moved to another
   subject), say so in one sentence. Optionally end with one specific recurring topic or person.
3. topics: for each topic tag below, a concrete name (name, at most 5 words, what they actually listened
   to under that tag) and one sentence (desc, at most 16 words, state the content directly). For the
   "__other__" entry, name must be exactly "Everything else" and desc says what the long tail was.

Topic shares:
{topics}

Items ({n} total{truncated}):
{items}

Output strictly this JSON and nothing else:
{{"headline": "...", "narrative": "...", "topics": [{{"tag": "original tag", "name": "...", "desc": "..."}}]}}""",
}


def _fingerprint(items):
    """条目指纹：同一批条目对应同一份叙事，与语言无关（中英各存一份）。"""
    h = hashlib.sha1()
    for it in items:
        h.update(it['id'].encode('utf-8', 'ignore'))
    return h.hexdigest()[:16]


_CACHE_LOCK = threading.Lock()   # 多个时段并行生成时，读-改-写要串行，否则互相覆盖


def _cache_path(results_dir):
    return os.path.join(results_dir, '_reflect_cache.json')


def _read_cache(results_dir):
    try:
        with open(_cache_path(results_dir), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_cache(results_dir, data):
    tmp = _cache_path(results_dir) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _cache_path(results_dir))


def _fallback_text(data, lang):
    topics = data['topics']
    lead = [t['tag'] for t in topics[:2] if t['tag'] != '__other__']
    if lang == 'zh':
        headline = '这段时间主要在听' + '和'.join(lead) if lead else '这段时间还没有转写'
        narrative = ('还没有生成回顾。在设置里配好 OpenRouter 或 Gemini 的 key，'
                     '再点右上角刷新。')
        other = '其它'
    else:
        headline = 'A stretch of ' + ' and '.join(lead) if lead else 'Nothing transcribed yet'
        narrative = ('No recap yet. Add an OpenRouter or Gemini key in Settings, then hit refresh.')
        other = 'Everything else'
    return {
        'headline': headline,
        'narrative': narrative,
        'topics': {t['tag']: {'name': other if t['tag'] == '__other__' else t['tag'], 'desc': ''}
                   for t in topics},
        'generated': False,
    }


def _call_model(prompt):
    """优先 OpenRouter 上的 Claude Opus 4.6；没 key 或调用失败就回落 Gemini Flash。返回原始文本或 None。"""
    or_key = (os.environ.get('OPENROUTER_API_KEY') or '').strip()
    if or_key:
        try:
            from analyze import _call_openai_compat
            return _call_openai_compat(
                prompt, NARRATIVE_MODEL, OPENROUTER_COMPAT_BASE, or_key,
                extra_payload={'reasoning': {'effort': 'low'}},   # 总结任务，不需要长思考
                label='Reflect')
        except Exception:
            pass
    g_key = GEMINI_API_KEY or os.environ.get('GEMINI_API_KEY', '')
    if not g_key:
        return None
    try:
        client = make_gemini_client(g_key)
        resp = client.models.generate_content(model=GEMINI_ENRICH_MODEL, contents=prompt)
        return (resp.text or '').strip()
    except Exception:
        return None


def _generate_text(data, lang):
    """写一种语言的叙事；失败返回 None。"""
    items = data['_items']
    if not items:
        return None

    truncated = ''
    sample = items
    if len(items) > MAX_ITEMS_FOR_LLM:
        # 太多就均匀抽样，保住时间跨度
        step = len(items) / MAX_ITEMS_FOR_LLM
        sample = [items[int(i * step)] for i in range(MAX_ITEMS_FOR_LLM)]
        truncated = ('，已均匀抽样' if lang == 'zh' else ', evenly sampled')

    lines = []
    for it in sample:
        lines.append('%s | %d | %s | %s | %s' % (
            it['dt'].strftime('%m-%d'), int(it['minutes']), it['title'][:40],
            it['one_line'][:60], '/'.join(it['tags'][:4])))
    topic_lines = '\n'.join('%s: %d%% (%d items)' % (t['tag'], t['percent'], t['count'])
                            for t in data['topics'])
    period = '%s ~ %s' % (data['period']['start'], data['period']['end'])
    prompt = NARRATIVE_PROMPT[lang].format(
        period=period, topics=topic_lines, n=len(items),
        truncated=truncated, items='\n'.join(lines))

    raw = _call_model(prompt)
    obj = _parse_json_obj(raw) if raw else None
    if not isinstance(obj, dict) or not obj.get('headline'):
        return None

    tmap = {}
    for t in obj.get('topics') or []:
        if isinstance(t, dict) and t.get('tag'):
            tmap[str(t['tag'])] = {'name': str(t.get('name') or t['tag']).strip()[:40],
                                   'desc': str(t.get('desc') or '').strip()[:120]}
    return {
        'headline': str(obj['headline']).strip().rstrip('。.')[:60],
        'narrative': str(obj.get('narrative') or '').strip()[:800],
        'topics': tmap,
        'generated': True,
    }


def regenerate(results_dir, range_key, data=None):
    """同步重算某个时段的中英叙事并写缓存。返回 {lang: text} （失败的语言为 None）。"""
    data = data or compute(results_dir, range_key)
    fp = _fingerprint(data['_items'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {l: pool.submit(_generate_text, data, l) for l in ('zh', 'en')}
        results = {l: f.result() for l, f in futs.items()}
    if any(results.values()):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with _CACHE_LOCK:
            cache = _read_cache(results_dir)      # 锁内重读，合并别的时段刚写的
            for l, r in results.items():
                if r:
                    cache['%s:%s' % (range_key, l)] = {'fp': fp, 'text': r, 'at': now}
            try:
                _write_cache(results_dir, cache)
            except Exception:
                pass
    return results


# ---------- 后台预生成 ----------

_BG = ThreadPoolExecutor(max_workers=1)      # 串行跑，避免几个时段同时各发两路请求
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()
_TOUCH_TIMER = None
TOUCH_DELAY_SEC = 600          # 转写完成后等 10 分钟再算（一批下载只触发一次）
PERIODIC_SEC = 6 * 3600        # 平时每 6 小时检查一次（时间窗每天在挪，条目会变）
STARTUP_DELAY_SEC = 90


def _bg_job(results_dir, range_key):
    try:
        regenerate(results_dir, range_key)
    except Exception:
        pass
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(range_key)


def schedule_regenerate(results_dir, range_key):
    """把某个时段丢到后台重算；已经在排队/在跑就不重复。返回是否新排了任务。"""
    with _INFLIGHT_LOCK:
        if range_key in _INFLIGHT:
            return False
        _INFLIGHT.add(range_key)
    _BG.submit(_bg_job, results_dir, range_key)
    return True


def is_regenerating(range_key):
    with _INFLIGHT_LOCK:
        return range_key in _INFLIGHT


def refresh_stale(results_dir):
    """四个时段里缓存过期（条目变了）或缺失的，排进后台重算。"""
    cache = _read_cache(results_dir)
    for rk in RANGES:
        data = compute(results_dir, rk)
        if not data['_items']:
            continue
        fp = _fingerprint(data['_items'])
        fresh = all((cache.get('%s:%s' % (rk, l)) or {}).get('fp') == fp for l in ('zh', 'en'))
        if not fresh:
            schedule_regenerate(results_dir, rk)


def touch(results_dir):
    """有转写完成时调用：防抖 10 分钟后跑一次 refresh_stale。"""
    global _TOUCH_TIMER
    if _TOUCH_TIMER is not None:
        _TOUCH_TIMER.cancel()
    _TOUCH_TIMER = threading.Timer(TOUCH_DELAY_SEC, refresh_stale, args=(results_dir,))
    _TOUCH_TIMER.daemon = True
    _TOUCH_TIMER.start()


def start_scheduler(results_dir):
    """服务启动时调一次：延迟首跑 + 周期检查。"""
    def loop():
        import time
        time.sleep(STARTUP_DELAY_SEC)
        while True:
            try:
                refresh_stale(results_dir)
            except Exception:
                pass
            time.sleep(PERIODIC_SEC)
    threading.Thread(target=loop, daemon=True, name='reflect-scheduler').start()


# ---------- 对外 ----------

def build(results_dir, range_key='1m', lang='zh', refresh=False):
    """算统计 + 取叙事。

    refresh=True（手动点刷新）：同步重算，等结果。
    否则：缓存新鲜就直接用；过期就先返回旧的（stale=True）并排后台重算，
    没有任何缓存则返回占位文案（regenerating=True，前端轮询）。
    """
    lang = 'zh' if lang == 'zh' else 'en'
    data = compute(results_dir, range_key)
    fp = _fingerprint(data['_items'])
    key = '%s:%s' % (range_key, lang)

    hit = _read_cache(results_dir).get(key)
    fresh = bool(hit) and hit.get('fp') == fp
    stale = False
    if refresh:
        results = regenerate(results_dir, range_key, data)
        text = results[lang] or (hit['text'] if hit else None)
    elif fresh:
        text = hit['text']
    else:
        if data['_items']:
            schedule_regenerate(results_dir, range_key)
        text = hit['text'] if hit else None
        stale = bool(hit)
    if not text:
        text = _fallback_text(data, lang)

    other_name = '其它' if lang == 'zh' else 'Everything else'
    for t in data['topics']:
        info = text['topics'].get(t['tag']) or {}
        t['name'] = info.get('name') or (other_name if t['tag'] == '__other__' else t['tag'])
        t['desc'] = info.get('desc') or ''

    wd = data['most_active_weekday']
    data['most_active_weekday_label'] = WEEKDAYS[lang][wd] if wd is not None else None
    data['headline'] = text['headline']
    data['narrative'] = text['narrative']
    data['generated'] = bool(text.get('generated'))
    data['stale'] = stale
    data['regenerating'] = is_regenerating(range_key)
    data['lang'] = lang
    del data['_items']
    return data
