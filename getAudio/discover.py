"""找来源（像 Gemini Notebook 的「发现来源」）：给一个主题，搜网页 / YouTube / B 站，列出候选让人挑。

- 网页：Gemini 带 Google 搜索（grounding）。网址只用搜索结果里真实出现的（grounding_chunks，跳转链接解析成
  原网址），不用模型自己写的网址；每条的说明取模型对应那一句（grounding_supports）。
  搜索按次收费（每次请求通常搜 1–3 次），单独记一笔，单价可用 SEARCH_FEE_USD 改。
- YouTube：yt-dlp 的 ytsearch，不花钱。
- B 站：bilibili_api 的搜索接口，不花钱。
挑好的网页走 sources.create_web_doc，视频走原来的转写。
"""
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

import config
import usage

SEARCH_MODEL = os.environ.get('DISCOVER_MODEL') or 'gemini-3.5-flash-lite'
SEARCH_FEE_USD = float(os.environ.get('SEARCH_FEE_USD') or 0.014)    # 每次 Google 搜索
VIDEO_N = 12

WEB_PROMPT = """Search the web for the most useful sources about: "{query}".
Prefer substantive pages that can be read as source material (articles, explainers, papers, reports, official documents, transcripts, encyclopedia entries) over lists of links, forums, shops and video pages. Search in the language(s) the topic is usually discussed in.
Then list the {n} best ones, one line each, in the language of the topic: title — one sentence on what it offers."""


def _strip_tags(s):
    return re.sub(r'<[^>]+>', '', str(s or '')).replace('&quot;', '"').replace('&amp;', '&').strip()


def _resolve(uri):
    """grounding 给的是 vertexaisearch 跳转链接：取 302 的目标就是原网址。"""
    import requests
    if 'vertexaisearch.cloud.google.com' not in (uri or ''):
        return uri
    try:
        r = requests.get(uri, allow_redirects=False, timeout=10)
        return r.headers.get('location') or ''
    except requests.RequestException:
        return ''


def search_web(query, n=10):
    """→ [{url, title, site, snippet}]，按模型列出的顺序。"""
    from google.genai import types
    from urllib.parse import urlparse
    key = config.gemini_key()
    if not key:
        raise RuntimeError('Add a Gemini API key in Settings first')
    client = config.make_gemini_client(key, timeout_ms=90_000)
    cfg = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
    last = None
    for _ in range(2):
        try:
            resp = client.models.generate_content(model=SEARCH_MODEL, contents=WEB_PROMPT.format(query=query, n=n),
                                                  config=cfg)
            break
        except Exception as e:  # noqa: BLE001  限流 / 断连：再试一次
            last = e
    else:
        raise RuntimeError(f'Web search failed: {str(last)[:200]}')
    usage.record_gemini(resp, SEARCH_MODEL, 'discover')
    gm = getattr(resp.candidates[0], 'grounding_metadata', None) if resp.candidates else None
    queries = list(getattr(gm, 'web_search_queries', None) or [])
    if queries:
        usage.record('google', 'google-search', 'discover', cost_usd=round(len(queries) * SEARCH_FEE_USD, 6))
    chunks = list(getattr(gm, 'grounding_chunks', None) or [])
    if not chunks:
        return []
    # 每个来源对应模型写的那一行（「标题 — 能提供什么」）
    lines = {}
    for sup in getattr(gm, 'grounding_supports', None) or []:
        for i in sup.grounding_chunk_indices or []:
            lines.setdefault(i, sup.segment.text)
    with ThreadPoolExecutor(8) as pool:
        urls = list(pool.map(lambda c: _resolve(c.web.uri if c.web else ''), chunks))
    out, seen = [], set()
    for i, (c, url) in enumerate(zip(chunks, urls)):
        if not url or url in seen:
            continue
        seen.add(url)
        line = re.sub(r'\*\*|^\s*[-*\d.]+\s*', '', lines.get(i, '')).strip()
        title, _, snippet = line.partition(' — ')
        if not snippet:
            title, snippet = '', line
        site = (urlparse(url).hostname or (c.web.title if c.web else '') or '').removeprefix('www.')
        out.append({'type': 'web', 'url': url, 'title': title.strip() or site, 'site': site,
                    'snippet': snippet.strip()[:240]})
    return out[:n]


def _fmt_dur(sec):
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ''
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def search_youtube(query, n=VIDEO_N):
    from downloader import _proxy_args, _resolve_ytdlp
    cmd = [_resolve_ytdlp(), '--flat-playlist', '-J', '--no-warnings', *_proxy_args(), f'ytsearch{n}:{query}']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError('YouTube search failed: ' + (r.stderr or '').strip()[-200:])
    out = []
    for e in (json.loads(r.stdout).get('entries') or []):
        if not e or not e.get('id') or e.get('_type') == 'playlist' or str(e.get('id')).startswith('UC'):
            continue
        out.append({'type': 'video', 'url': e.get('url') or f"https://www.youtube.com/watch?v={e['id']}",
                    'title': e.get('title') or '', 'site': 'YouTube',
                    'channel': e.get('channel') or e.get('uploader') or '',
                    'duration': _fmt_dur(e.get('duration')), 'views': e.get('view_count') or 0})
    return out


def search_bilibili(query, n=VIDEO_N):
    from bilibili_api import search, sync
    res = sync(search.search_by_type(query, search_type=search.SearchObjectType.VIDEO, page=1))
    out = []
    for v in (res.get('result') or [])[:n]:
        if not v.get('bvid'):
            continue
        dur = str(v.get('duration') or '')
        if re.fullmatch(r'\d+:\d+', dur):                     # B 站给的是「37:1」这种：补齐成 37:01
            m, s = dur.split(':')
            dur = f'{int(m)}:{int(s):02d}'
        out.append({'type': 'video', 'url': f"https://www.bilibili.com/video/{v['bvid']}",
                    'title': _strip_tags(v.get('title')), 'site': 'Bilibili', 'channel': v.get('author') or '',
                    'duration': dur, 'views': v.get('play') or 0})
    return out


def search(query, kind='web'):
    query = str(query or '').strip()[:200]
    if not query:
        raise ValueError('Type what you are looking for')
    if kind == 'youtube':
        return search_youtube(query)
    if kind == 'bilibili':
        return search_bilibili(query)
    return search_web(query)
