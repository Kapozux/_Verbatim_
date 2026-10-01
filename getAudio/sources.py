"""项目里的「来源」：文档（PDF / Word / PPT / 图片 / Markdown / 粘贴的文字）和转写的原文段落。

文档跟转写一样是全局存的（results/_docs/<doc_id>/），同一份讲义可以放进好几个项目，不复制：
  meta.json      {id, title, filename, ext, status: converting|ready|failed, converter, pages, chars, error}
  original.<ext> 上传的原件
  doc.md         整份 Markdown（给人看 / 阅读器渲染）
  pages.json     [{page, text}]：PDF 类按页存，出处能写到「第几页」；纯文本没有这个文件
  passages.json  切好的原文段落 [{i, text, page, heading}]

转换：优先交给墨页（本机 http://127.0.0.1:8765）。只用 fast（读 PDF 文字层）和 balanced（本机 Surya OCR）
两档——墨页的 ai 档会把页面发给 Kimi / Qwen 等模型，时政内容不能走那条路，所以这里永远不用。
墨页没开：PDF 用 pdftotext、Word 用 macOS 的 textutil 兜底；都没有就报错让人开墨页。

网页（ext=web）：抓下来、挑出正文转成 Markdown 存 doc.md，meta.url 记原网址；链接指向 PDF / Word 的
就下载成 original.<ext> 走上面的转换。只认 http(s)，不抓本机 / 内网地址。

原文段落：按段落攒到 ~500 字一块，记下所在页码和最近的小标题；转写按时间顺序攒，记下开头的时间点。
切段不调模型、不花钱，结果缓存在 passages.json，内容变了（指纹不同）就重切。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime

import config

DOCS_DIR = os.path.join(config.RESULTS_FOLDER, '_docs')
MOYE_URL = os.environ.get('MOYE_URL', 'http://127.0.0.1:8765').rstrip('/')
TEXT_EXTS = {'md', 'markdown', 'txt'}
MOYE_EXTS = {'pdf', 'doc', 'docx', 'ppt', 'pptx', 'png', 'jpg', 'jpeg', 'webp', 'heic'}
ALLOWED_EXTS = TEXT_EXTS | MOYE_EXTS | {'rtf', 'html', 'htm'}
MAX_DOC_BYTES = 200 * 1024 * 1024
PASSAGE_CHARS = 500          # 中文约 500 字一块；英文按词数折算（见 _size）
_DOC_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_jobs = set()
_jobs_lock = threading.Lock()


def valid_doc_id(doc_id):
    return bool(_DOC_ID_RE.match(doc_id or ''))


def doc_dir(doc_id):
    return os.path.join(DOCS_DIR, doc_id)


def _read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def doc_meta(doc_id):
    return _read_json(os.path.join(doc_dir(doc_id), 'meta.json')) if valid_doc_id(doc_id) else None


def _save_meta(meta):
    _write_json(os.path.join(doc_dir(meta['id']), 'meta.json'), meta)


def doc_markdown(doc_id):
    try:
        with open(os.path.join(doc_dir(doc_id), 'doc.md'), 'r', encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def doc_pages(doc_id):
    return _read_json(os.path.join(doc_dir(doc_id), 'pages.json')) or None


# ================= 建文档 =================

def create_doc(filename=None, data=None, text=None, title=None):
    """存一份新文档，后台开始转换。filename+data（上传的文件）或 text（粘贴的文字）二选一。→ meta"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_id = uuid.uuid4().hex
    d = doc_dir(doc_id)
    os.makedirs(d)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if text is not None:
        text = str(text).strip()
        if not text:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError('Empty text')
        name = (title or '').strip() or _first_line(text) or 'Note'
        meta = {'id': doc_id, 'title': name[:120], 'filename': '', 'ext': 'text', 'status': 'ready',
                'converter': 'pasted', 'pages': None, 'chars': len(text), 'created_at': now}
        with open(os.path.join(d, 'doc.md'), 'w', encoding='utf-8') as f:
            f.write(text)
        _save_meta(meta)
        return meta
    filename = os.path.basename(filename or 'document')
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in ALLOWED_EXTS:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError(f'Unsupported file type: .{ext or "?"}')
    if not data:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError('Empty file')
    if len(data) > MAX_DOC_BYTES:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError('File too large (max 200 MB)')
    with open(os.path.join(d, f'original.{ext}'), 'wb') as f:
        f.write(data)
    stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
    meta = {'id': doc_id, 'title': ((title or '').strip() or stem)[:120], 'filename': filename, 'ext': ext,
            'status': 'converting', 'converter': '', 'pages': None, 'chars': 0, 'created_at': now}
    _save_meta(meta)
    if ext in TEXT_EXTS:          # 纯文本当场就好
        _finish_text(meta, data.decode('utf-8', errors='replace'))
    else:
        start_convert(doc_id)
    return doc_meta(doc_id)


def _first_line(text):
    for ln in text.splitlines():
        ln = re.sub(r'^[#>\-*\s]+', '', ln).strip()
        if ln:
            return ln[:60]
    return ''


def _finish_text(meta, md, pages=None, converter='direct'):
    d = doc_dir(meta['id'])
    with open(os.path.join(d, 'doc.md'), 'w', encoding='utf-8') as f:
        f.write(md)
    if pages:
        _write_json(os.path.join(d, 'pages.json'), pages)
    meta.update(status='ready', converter=converter, chars=len(md), pages=len(pages) if pages else None,
                error=None, finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    _save_meta(meta)


def start_convert(doc_id):
    with _jobs_lock:
        if doc_id in _jobs:
            return
        _jobs.add(doc_id)

    def run():
        try:
            _convert(doc_id)
        finally:
            with _jobs_lock:
                _jobs.discard(doc_id)
    threading.Thread(target=run, daemon=True).start()


def _convert(doc_id):
    meta = doc_meta(doc_id)
    if not meta:
        return
    if meta.get('ext') == 'web':
        return _fetch_web(meta)
    src = os.path.join(doc_dir(doc_id), f"original.{meta['ext']}")
    meta.update(status='converting', error=None)
    _save_meta(meta)
    errors = []
    try:
        if moye_alive():
            try:
                md, pages, conv = _via_moye(src, meta)
                _finish_text(meta, md, pages, conv)
                return
            except Exception as e:  # noqa: BLE001  墨页转失败：退到本机工具
                errors.append(f'Moye: {e}')
        md, pages, conv = _via_local(src, meta['ext'])
        _finish_text(meta, md, pages, conv)
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        meta.update(status='failed', error='; '.join(errors)[:400])
        _save_meta(meta)


# ================= 网页 =================

WEB_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) '
          'Chrome/128.0 Safari/537.36')
_FILE_TYPES = {'application/pdf': 'pdf', 'application/msword': 'doc',
               'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'docx',
               'application/vnd.ms-powerpoint': 'ppt',
               'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'pptx'}
MIN_WEB_CHARS = 200


def _public_url(url):
    """只抓公网 http(s)：挡掉 file://、本机和内网地址（不然一个链接就能读到墨页、路由器之类的本机服务）。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    u = urlparse(url or '')
    if u.scheme not in ('http', 'https') or not u.hostname:
        return False
    fake = ipaddress.ip_network('198.18.0.0/15')      # Clash / Surge 的 fake-ip：域名都解析到这一段，其实是外网
    try:
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip in fake:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


def create_web_doc(url):
    """网页来源：先登记（标题暂用域名），后台抓取。→ meta"""
    from urllib.parse import urlparse
    url = (url or '').strip()
    if not re.match(r'^https?://', url):
        raise ValueError('Not a web link')
    os.makedirs(DOCS_DIR, exist_ok=True)
    doc_id = uuid.uuid4().hex
    os.makedirs(doc_dir(doc_id))
    host = (urlparse(url).hostname or url).removeprefix('www.')
    meta = {'id': doc_id, 'title': host[:120], 'filename': '', 'ext': 'web', 'url': url, 'status': 'converting',
            'converter': 'web', 'pages': None, 'chars': 0,
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    _save_meta(meta)
    start_convert(doc_id)
    return meta


def _fetch_web(meta):
    import requests
    url = meta['url']
    try:
        if not _public_url(url):
            raise ValueError('Only public web pages can be added')
        r = requests.get(url, headers={'User-Agent': WEB_UA, 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'},
                         timeout=25, stream=True)
        if r.status_code >= 400:
            raise ValueError(f'The site answered {r.status_code}')
        if not _public_url(r.url):                     # 跳转到了内网
            raise ValueError('Only public web pages can be added')
        ctype = (r.headers.get('content-type') or '').split(';')[0].strip().lower()
        data = r.raw.read(MAX_DOC_BYTES + 1, decode_content=True)
        if len(data) > MAX_DOC_BYTES:
            raise ValueError('File too large (max 200 MB)')
        ext = _FILE_TYPES.get(ctype) or ('pdf' if re.search(r'\.pdf($|\?)', r.url, re.I) and 'html' not in ctype else '')
        if ext:                                         # 链接指向的是文件：存成原件，按文档转换
            name = re.sub(r'[?#].*$', '', r.url.rstrip('/').rsplit('/', 1)[-1]) or 'document'
            if not name.lower().endswith('.' + ext):
                name += '.' + ext
            with open(os.path.join(doc_dir(meta['id']), f'original.{ext}'), 'wb') as f:
                f.write(data)
            meta.update(ext=ext, filename=name, title=(name.rsplit('.', 1)[0] or meta['title'])[:120])
            _save_meta(meta)
            return _convert(meta['id'])
        if ctype.startswith('text/plain') or ctype in ('text/markdown', 'text/x-markdown'):
            title, md = '', data.decode(r.encoding or 'utf-8', errors='replace')
        elif 'html' in ctype or 'xml' in ctype or not ctype:
            enc = r.encoding if r.encoding and r.encoding.lower() != 'iso-8859-1' else None
            title, md = html_to_markdown(data, enc)
        else:
            raise ValueError(f'Not a page or document ({ctype})')
        md = _tidy(md)
        if len(md) < MIN_WEB_CHARS:
            raise ValueError('Could not read the text of this page (it may need a login or JavaScript)')
        if title:
            meta['title'] = title[:120]
        _finish_text(meta, md, None, 'web')
    except Exception as e:  # noqa: BLE001
        meta.update(status='failed', error=str(e)[:300])
        _save_meta(meta)


_JUNK = re.compile(r'(^|[-_ ])(comment|comments|sidebar|share|social|related|recommend|footer|nav|menu|breadcrumb|'
                   r'advert|ads?|banner|cookie|subscribe|newsletter|popup|modal|login|toolbar)([-_ ]|$)', re.I)


# 行内链接拆开的中文（「拍着 閏兒 ，」）：两边都是中文字 / 全角标点时去掉中间的空格
_CJK_GAP = re.compile(r'(?<=[\u3000-\u303f\u3400-\u9fff\uff00-\uffef])\s+(?=[\u3000-\u303f\u3400-\u9fff\uff00-\uffef])')


def html_to_markdown(data, encoding=None):
    """网页 → (标题, Markdown)。readability 的简化版：找段落文字最多的那个容器当正文，
    再按标题 / 段落 / 列表 / 引用 / 表格依次转成 Markdown。"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, 'lxml', from_encoding=encoding)
    title = ''
    og = soup.find('meta', attrs={'property': 'og:title'})
    if og and og.get('content'):
        title = og['content'].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    # 「荷塘月色 - 维基文库，自由的图书馆」：最后一段是站名就去掉
    parts = re.split(r'\s+[-|–—_]\s+', title)
    if len(parts) > 1 and len(parts[-1]) <= 24 and len(' - '.join(parts[:-1])) >= 2:
        title = ' - '.join(parts[:-1])
    for t in soup(['script', 'style', 'noscript', 'nav', 'footer', 'header', 'aside', 'form', 'iframe', 'svg',
                   'button', 'select', 'template']):
        t.decompose()
    # class / id 像「分享栏、评论区、侧栏」的小块删掉。外层容器不碰（维基百科的 <html> 上就挂着
    # vector-feature-main-menu-… 一长串），占全页三成以上文字的也不碰——那多半就是正文
    total = len((soup.body or soup).get_text(strip=True))
    for t in soup.find_all(True):
        if t.attrs is None or t.name in ('html', 'body', 'main', 'article'):
            continue
        key = ' '.join(t.get('class') or []) + ' ' + (t.get('id') or '')
        if _JUNK.search(key) and len(t.get_text(strip=True)) < min(2000, total * 0.3):
            t.decompose()
    root = None
    for cand in soup.find_all(['article', 'main']):
        if len(cand.get_text(strip=True)) > 500:
            root = cand
            break
    if root is None:                   # 段落最多的父容器
        score = {}                     # id(父容器) → [父容器, 它直属段落的总字数]
        for p in soup.find_all('p'):
            if p.parent is not None:
                score.setdefault(id(p.parent), [p.parent, 0])[1] += len(p.get_text(strip=True))
        if score:
            root = max(score.values(), key=lambda x: x[1])[0]
    if root is None:
        root = soup.body or soup
    lines = []
    blocks = ['h1', 'h2', 'h3', 'h4', 'p', 'li', 'blockquote', 'pre', 'tr']
    for el in root.find_all(blocks):
        if el.find_parent(['li', 'blockquote', 'pre', 'tr']) and el.name in ('p', 'li', 'tr'):
            if el.name != 'li' or el.find_parent(['blockquote', 'pre', 'tr']):
                continue
        if el.name == 'pre':
            lines.append('```\n' + el.get_text().strip('\n') + '\n```')
            continue
        text = _CJK_GAP.sub('', re.sub(r'\s+', ' ', el.get_text(' ', strip=True)))
        if not text:
            continue
        if el.name in ('h1', 'h2', 'h3', 'h4'):
            lines.append('#' * int(el.name[1]) + ' ' + text)
        elif el.name == 'li':
            lines.append('- ' + text)
        elif el.name == 'blockquote':
            lines.append('> ' + text)
        elif el.name == 'tr':
            cells = [re.sub(r'\s+', ' ', c.get_text(' ', strip=True)) for c in el.find_all(['td', 'th'])]
            if any(cells):
                lines.append('| ' + ' | '.join(cells) + ' |')
        else:
            lines.append(text)
    md = '\n\n'.join(lines)
    if len(md) < MIN_WEB_CHARS:        # 没有 <p> 的站（文字直接在 div 里用 <br> 断行）
        md = '\n\n'.join(ln.strip() for ln in root.get_text('\n').splitlines() if ln.strip())
    if title and not md.lstrip().startswith('# '):
        md = f'# {title}\n\n{md}'
    return title, md


# ================= 墨页 =================

def moye_alive(timeout=2):
    import requests
    try:
        r = requests.get(MOYE_URL + '/health', timeout=timeout, proxies={'http': None, 'https': None})
        return r.ok and bool((r.json() or {}).get('ready', True))
    except Exception:  # noqa: BLE001
        return False


def _moye_job(path, filename, mode, timeout_s=1800):
    """交一份给墨页，等它转完。→ result（{markdown, pages:[{page, markdown}], …}）"""
    import requests
    from urllib.parse import quote
    no_proxy = {'http': None, 'https': None}
    with open(path, 'rb') as f:
        r = requests.post(MOYE_URL + '/api/jobs', data=f, timeout=600, proxies=no_proxy,
                          headers={'x-filename': quote(filename), 'x-mode': mode,
                                   'Content-Type': 'application/octet-stream'})
    if r.status_code >= 400:
        raise RuntimeError(f'submit HTTP {r.status_code}: {r.text[:200]}')
    job_id = (r.json().get('job') or {}).get('id')
    if not job_id:
        raise RuntimeError('no job id')
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(2)
        g = requests.get(f'{MOYE_URL}/api/library/{job_id}', timeout=30, proxies=no_proxy)
        if g.status_code == 404:          # 「结果尚未生成」也是 404：看 job 状态
            continue
        body = g.json()
        st = (body.get('job') or {}).get('status')
        if st == 'done' and body.get('result'):
            try:                          # Verbatim 自己留了一份：别在墨页的资料库里堆一条
                requests.delete(f'{MOYE_URL}/api/library/{job_id}', timeout=10, proxies=no_proxy)
            except Exception:  # noqa: BLE001
                pass
            return body['result']
        if st in ('failed', 'cancelled'):
            raise RuntimeError((body.get('job') or {}).get('error') or st)
    raise RuntimeError('timed out')


def _via_moye(src, meta):
    """先 fast（读文字层，秒级）；读出来几乎没字说明是扫描件，再用本机 OCR（balanced）。"""
    name = meta['filename'] or os.path.basename(src)
    res = _moye_job(src, name, 'fast')
    pages = [{'page': p.get('page') or i + 1, 'text': (p.get('markdown') or '').strip()}
             for i, p in enumerate(res.get('pages') or [])]
    body = sum(len(p['text']) for p in pages)
    conv = 'moye-fast'
    if pages and body < 40 * len(pages):
        res = _moye_job(src, name, 'balanced')
        pages = [{'page': p.get('page') or i + 1, 'text': (p.get('markdown') or '').strip()}
                 for i, p in enumerate(res.get('pages') or [])]
        conv = 'moye-ocr'
    md = res.get('markdown') or '\n\n'.join(p['text'] for p in pages)
    if not md.strip():
        raise RuntimeError('no text found')
    return md, (pages or None), conv


# ================= 本机兜底 =================

def _via_local(src, ext):
    if ext == 'pdf' and shutil.which('pdftotext'):
        out = subprocess.run(['pdftotext', '-layout', '-enc', 'UTF-8', src, '-'], capture_output=True,
                             timeout=600)
        raw = out.stdout.decode('utf-8', errors='replace')
        pages = [{'page': i + 1, 'text': _tidy(t)} for i, t in enumerate(raw.split('\f'))]
        pages = [p for p in pages if p['text']] or []
        if sum(len(p['text']) for p in pages) < 40 * max(1, len(pages)):
            raise RuntimeError('This PDF looks scanned (no text layer). Start Moye to OCR it.')
        md = '\n\n'.join(f"<!-- page {p['page']} -->\n{p['text']}" for p in pages)
        return md, pages, 'pdftotext'
    if ext in ('doc', 'docx', 'rtf', 'html', 'htm') and shutil.which('textutil'):
        out = subprocess.run(['textutil', '-convert', 'txt', '-stdout', src], capture_output=True, timeout=300)
        txt = _tidy(out.stdout.decode('utf-8', errors='replace'))
        if not txt:
            raise RuntimeError('No text extracted')
        return txt, None, 'textutil'
    raise RuntimeError(f'Converting .{ext} needs Moye (the PDF-to-Markdown app) running')


def _tidy(t):
    t = re.sub(r'[ \t]+\n', '\n', t or '')
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip()


# ================= 原文段落 =================

def _size(text):
    """块大小按「字」算：中文一个字一个，英文按词折成约 2.5 字。"""
    cjk = len(re.findall(r'[一-鿿]', text))
    words = len(re.findall(r'[A-Za-z0-9]+', text))
    return cjk + int(words * 2.5)


def _fingerprint(text):
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


_HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$')


def _split_long(par, limit):
    """一段太长（比如没有换行的整页）：按句号切开再攒。"""
    if _size(par) <= limit * 1.4:
        return [par]
    out, cur = [], ''
    for s in re.split(r'(?<=[。！？!?；;.])\s*', par):
        # 一句话本身就太长（逗号连成一大串、没有句号）：再按逗号切
        pieces = re.split(r'(?<=[，,、])', s) if _size(s) > limit else [s]
        for p in pieces:
            if cur and _size(cur + p) > limit:
                out.append(cur)
                cur = ''
            cur += p
    if cur:
        out.append(cur)
    return out


def chunk_text(text, page=None, heading=''):
    """一段 Markdown → [{text, page, heading}]。按段落攒到 PASSAGE_CHARS，标题单独记下来挂在后面的块上。"""
    out, cur, cur_h = [], [], heading
    h = heading

    def flush():
        nonlocal cur
        body = '\n'.join(cur).strip()
        if body:
            out.append({'text': body, 'page': page, 'heading': cur_h})
        cur = []

    for par in re.split(r'\n\s*\n', text or ''):
        par = par.strip()
        if not par or par.startswith('<!--'):
            continue
        m = _HEADING.match(par.splitlines()[0])
        if m and len(par.splitlines()) == 1:
            flush()
            h = m.group(2).strip()[:80]
            cur_h = h
            continue
        for piece in _split_long(par, PASSAGE_CHARS):
            if cur and _size('\n'.join(cur) + piece) > PASSAGE_CHARS:
                flush()
                cur_h = h
            if not cur:
                cur_h = h
            cur.append(piece)
    flush()
    return out


def doc_passages(doc_id):
    """→ [{i, text, page, heading}]（缓存；文档内容变了重切）。"""
    meta = doc_meta(doc_id)
    if not meta or meta.get('status') != 'ready':
        return []
    pages = doc_pages(doc_id)
    md = doc_markdown(doc_id)
    fp = _fingerprint(json.dumps(pages, ensure_ascii=False) if pages else md)
    cache_path = os.path.join(doc_dir(doc_id), 'passages.json')
    cache = _read_json(cache_path)
    if cache and cache.get('fp') == fp:
        return cache['passages']
    out, heading = [], ''
    if pages:
        for p in pages:
            chunks = chunk_text(p['text'], page=p['page'], heading=heading)
            if chunks:
                heading = chunks[-1]['heading']
            out += chunks
    else:
        out = chunk_text(md)
    for i, c in enumerate(out):
        c['i'] = i
    _write_json(cache_path, {'fp': fp, 'passages': out})
    return out


def transcript_passages(task_id):
    """一期转写 → [{i, text, ts, sec}]：按时间顺序攒到 PASSAGE_CHARS，记下这一块开头的时间点。"""
    tpath = os.path.join(config.RESULTS_FOLDER, task_id, 'transcript.json')
    try:
        mt = os.path.getmtime(tpath)
    except OSError:
        return []
    cache_path = os.path.join(config.RESULTS_FOLDER, task_id, 'passages.json')
    cache = _read_json(cache_path)
    if cache and cache.get('mtime') == mt:
        return cache['passages']
    segs = _read_json(tpath) or []
    out, cur, start = [], [], None
    for s in segs:
        if not isinstance(s, dict):
            continue
        t = str(s.get('text') or '').strip()
        if not t:
            continue
        if cur and _size(' '.join(cur) + t) > PASSAGE_CHARS:
            out.append({'text': ' '.join(cur), 'ts': start})
            cur = []
        if not cur:
            start = s.get('timestamp') or ''
        cur.append(t)
    if cur:
        out.append({'text': ' '.join(cur), 'ts': start})
    for i, c in enumerate(out):
        c['i'] = i
    try:
        _write_json(cache_path, {'mtime': mt, 'passages': out})
    except OSError:
        pass
    return out


def list_docs(ids):
    return [m for m in (doc_meta(i) for i in ids) if m]
