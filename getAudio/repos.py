"""项目里的「代码库」来源：一个本机 git 仓库，由用户自己的读码 agent（Daemon）读，读出来的证据卡进语料。

全局存（results/_repos/<repo_id>/），同一个仓库可以放进好几个项目，跟文档一样只登记 id：
  meta.json        {id, title, path, commit, dirty, status: ready|reading|failed, files, skipped, error, runs}
  snapshot/        `git archive <commit>` 解出来的快照：只有提交过的文件，.gitignore 掉的 .env / venv 不在里面；
                   再按文件名和内容挑掉像密钥的文件。Daemon 只在这里干活，碰不到原仓库，commit 也就钉住了。
  cards.json       [{i, path, start, end, quote, obs, layer, topic, question, run}]，只追加，下标 i 不变（出处靠它）
  runs/<n>.log     每次读的 agent 输出

读：往快照里放 DAEMON.md（规矩和卡片格式）+ task.txt（这次要弄清楚什么），无头跑
`<python> My_agent.py task.txt`（cwd = 快照，stdin 关掉——Daemon 碰到危险命令要人点头，没人就拒绝）。
跑完收 verbatim_cards.jsonl，每张卡拿 quote 去快照里逐字核对：行号对 → 收；原文在别处且只有一处 →
改成那里的行号再收；对不上 → 丢掉，记在这次 run 的 dropped 里。核对不调模型、不花钱。
快照自己是个 git 仓库：跑完 git status 看 Daemon 有没有动别的文件，动了就记下来（不影响收卡）。

默认用自带的副本 daemon/（走 Settings 里的 DashScope key，费用记进 usage.db，purpose = repo_read）；
VERBATIM_DAEMON_DIR 指向外面那份 Daemon 时，它用自己的 key，钱不进 usage.db。它会执行 shell 命令：只由用户手动点「读」触发，演示模式不开。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import io
import threading
import uuid
from datetime import datetime

import config

REPOS_DIR = os.path.join(config.RESULTS_FOLDER, '_repos')
BUNDLED_DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daemon')   # 自带的副本，见 daemon/README.md
DAEMON_DIR = os.path.expanduser(os.environ.get('VERBATIM_DAEMON_DIR') or BUNDLED_DAEMON)
DAEMON_MODEL = 'deepseek-v4-pro'          # 自带副本默认用的模型（daemon/llm.py 的 deepseek-pro），估价用
RUN_TIMEOUT = int(os.environ.get('VERBATIM_DAEMON_TIMEOUT', '2400'))
CARDS_FILE = 'verbatim_cards.jsonl'
MAX_CARD_LINES = 40
MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
_ID_RE = re.compile(r'^[0-9a-f]{32}$')
_SECRET_NAMES = re.compile(r'(^|/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|settings\.local\.json|'
                           r'credentials.*\.json|.*\.p12)$', re.I)
_SECRET_TEXT = re.compile(r'AIza[0-9A-Za-z_-]{30,}|sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|'
                          r'github_pat_[A-Za-z0-9_]{20,}|xox[bp]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|'
                          r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
LAYERS = {'主张', '自证', '核实'}
_running = set()
_lock = threading.Lock()

DAEMON_MD = """这是一次【只读】的读代码任务，产出是给另一个程序（Verbatim）收的「证据卡」。

规则：
1. 不许修改、删除、新建任何文件，唯一例外是往 verbatim_cards.jsonl 追加内容。不许 spawn 子 agent。不许联网。
2. 看行号用：nl -ba 文件 | sed -n '起,止p'   （read 命令不显示行号，别用它定行号）
3. 每张卡是一行 JSON，字段：
   path   相对路径
   start  起始行号（含）
   end    结束行号（含），一张卡最多 12 行
   quote  这几行的原文，一字不改（包括缩进），行之间用 \\n
   obs    你的观察，中文，一两句
   layer  "主张"（README/注释/文档字符串怎么说）| "自证"（代码实际怎么做）| "核实"（有测试能证明）
   topic  两到六个字的话题
   说「文档说了、代码没做」时，文档那一侧也要单独出一张「主张」卡。
4. 追加卡片用这种写法，避免引号转义出错：
   python3 - <<'EOF'
   import json
   card = {"path": "...", "start": 1, "end": 3, "quote": \"\"\"...\"\"\", "obs": "...", "layer": "自证", "topic": "..."}
   open("verbatim_cards.jsonl", "a", encoding="utf-8").write(json.dumps(card, ensure_ascii=False) + "\\n")
   EOF
5. 写完 8–12 张卡后，用 python3 逐行 json.loads 检查文件能解析，然后输出 exit-verified。
"""

DEFAULT_TASK = ("先通读这个仓库的说明文档和目录结构，弄清楚每个主要模块做什么、入口在哪、模块之间怎么调用。"
                "按 DAEMON.md 的格式写 8–12 张证据卡：说明文档 / 注释的说法记「主张」，代码实际做法记「自证」，"
                "tests/ 里能证明的记「核实」。")


def valid_id(repo_id):
    return bool(_ID_RE.match(repo_id or ''))


def repo_dir(repo_id):
    return os.path.join(REPOS_DIR, repo_id)


def snapshot_dir(repo_id):
    return os.path.join(repo_dir(repo_id), 'snapshot')


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


def repo_meta(repo_id):
    return _read_json(os.path.join(repo_dir(repo_id), 'meta.json')) if valid_id(repo_id) else None


def _save_meta(meta):
    _write_json(os.path.join(repo_dir(meta['id']), 'meta.json'), meta)


def repo_cards(repo_id):
    return (_read_json(os.path.join(repo_dir(repo_id), 'cards.json'), []) or []) if valid_id(repo_id) else []


def _git(args, cwd, **kw):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, timeout=120, **kw)


# ================= 加仓库：快照 =================

def create_repo(path):
    """本机文件夹（必须是 git 仓库）→ 快照 + meta。同一个仓库同一个 commit 已经有了就直接复用。"""
    path = os.path.realpath(os.path.expanduser(str(path or '').strip()))
    if not os.path.isdir(path):
        raise ValueError('Folder not found')
    top = _git(['rev-parse', '--show-toplevel'], path, text=True)
    if top.returncode != 0:
        raise ValueError('Not a git repository (commit the code first)')
    top = top.stdout.strip()
    commit = _git(['rev-parse', 'HEAD'], path, text=True)
    if commit.returncode != 0:
        raise ValueError('The repository has no commits yet')
    commit = commit.stdout.strip()
    sub = os.path.relpath(path, top)                     # 选的是仓库里的子目录：只快照这一块
    sub = '' if sub == '.' else sub
    for rid in os.listdir(REPOS_DIR) if os.path.isdir(REPOS_DIR) else []:
        m = repo_meta(rid)
        if m and m.get('path') == path and m.get('commit') == commit and m.get('status') != 'failed':
            return m
    dirty = bool(_git(['status', '--porcelain', '--', sub or '.'], path, text=True).stdout.strip())
    repo_id = uuid.uuid4().hex
    snap = snapshot_dir(repo_id)
    os.makedirs(snap)
    try:
        arch = _git(['archive', '--format=tar', commit, *([sub] if sub else [])], top)
        if arch.returncode != 0:
            raise ValueError(arch.stderr.decode('utf-8', 'replace')[:300] or 'git archive failed')
        files, skipped, lines = _extract(arch.stdout, snap, sub)
        _git(['init', '-q'], snap)                       # 快照自己是个仓库：跑完看 Daemon 动没动别的文件
        _git(['add', '-A'], snap)
        _git(['-c', 'user.name=verbatim', '-c', 'user.email=verbatim@localhost', 'commit', '-qm', 'snapshot'], snap)
    except Exception:
        shutil.rmtree(repo_dir(repo_id), ignore_errors=True)
        raise
    meta = {'id': repo_id, 'title': os.path.basename(path), 'path': path, 'commit': commit, 'dirty': dirty,
            'status': 'ready', 'files': files, 'lines': lines, 'skipped': skipped, 'error': '', 'runs': [],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    _save_meta(meta)
    _write_json(os.path.join(repo_dir(repo_id), 'cards.json'), [])
    return meta


def _extract(data, dest, sub):
    """git archive 的 tar → dest，去掉子目录前缀；像密钥的文件不解出来。→ (文件数, [跳过的路径], 文本行数)"""
    files, skipped, total, lines = 0, [], 0, 0
    prefix = sub.rstrip('/') + '/' if sub else ''
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.startswith(prefix):
                continue
            rel = m.name[len(prefix):]
            if not rel or rel.startswith('/') or '..' in rel.split('/'):
                continue
            body = tar.extractfile(m).read()
            total += len(body)
            if total > MAX_SNAPSHOT_BYTES:
                raise ValueError('Repository is too large (over 200 MB of tracked files)')
            if _SECRET_NAMES.search(rel) or _SECRET_TEXT.search(body[:2_000_000].decode('utf-8', 'ignore')):
                skipped.append(rel)
                continue
            out = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, 'wb') as f:
                f.write(body)
            files += 1
            if b'\0' not in body[:8192]:          # 文本文件才数行（估价用）
                lines += body.count(b'\n')
    return files, skipped, lines


def _count_lines(root):
    n = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ('.git', 'chat_history')]     # Daemon 跑完留下的不算
        for fn in fns:
            if dp == root and fn in (CARDS_FILE, 'DAEMON.md', 'task.txt', 'patch.txt'):
                continue
            try:
                with open(os.path.join(dp, fn), 'rb') as f:
                    body = f.read()
            except OSError:
                continue
            if b'\0' not in body[:8192]:
                n += body.count(b'\n')
    return n


def estimate(meta):
    """读一次大概多少钱（美元）。agent 每轮重发整段对话，花费主要是输入：按 2026-10-02 读 getAudio
    （4.6 万行、47 次调用、输入 91 万 / 输出 1.4 万 token）拟合「15 万 + 每行 16 个」，封顶 150 万；
    钱按 usage 的价格表算（prices.json 改了价这里跟着变）。没价 → None。"""
    import usage
    if meta and meta.get('id') and 'lines' not in meta:      # 早先建的快照没记行数：现数一次存回去
        meta['lines'] = _count_lines(snapshot_dir(meta['id']))
        _save_meta(meta)
    tokens_in = min(1_500_000, 150_000 + 16 * int((meta or {}).get('lines') or 0))
    return usage.estimate_cost(DAEMON_MODEL, tokens_in, 14_000)


# ================= 读：跑 Daemon、收卡 =================

def daemon_command():
    """→ [python, My_agent.py]；Daemon 不在就 None。自带的副本用 Verbatim 自己的 Python（它的 llm.py 要 import
    config / usage）；外面那份用它自己的 .venv。"""
    agent = os.path.join(DAEMON_DIR, 'My_agent.py')
    if not os.path.isfile(agent):
        return None
    if os.path.realpath(DAEMON_DIR) == os.path.realpath(BUNDLED_DAEMON):
        return [sys.executable, agent]
    py = os.path.join(DAEMON_DIR, '.venv', 'bin', 'python')
    return [py if os.path.isfile(py) else 'python3', agent]


def start_read(repo_id, question='', on_done=None):
    """后台读一次。question 空 = 导览（DEFAULT_TASK）。正在读就报错。on_done()：收完卡调（项目补向量）。"""
    meta = repo_meta(repo_id)
    if not meta:
        raise ValueError('Repository not found')
    cmd = daemon_command()
    if not cmd:
        raise ValueError(f'Daemon not found in {DAEMON_DIR} (set VERBATIM_DAEMON_DIR)')
    with _lock:
        if repo_id in _running:
            raise ValueError('Already reading this repository')
        _running.add(repo_id)
    meta['status'], meta['error'] = 'reading', ''
    _save_meta(meta)
    threading.Thread(target=_run_safe, args=(repo_id, cmd, question.strip(), on_done), daemon=True).start()


def _run_safe(repo_id, cmd, question, on_done=None):
    try:
        run(repo_id, cmd, question)
        if on_done:
            on_done()
    except Exception as e:  # noqa: BLE001
        meta = repo_meta(repo_id) or {}
        if meta:
            meta['status'], meta['error'] = 'failed', str(e)[:300]
            _save_meta(meta)
    finally:
        with _lock:
            _running.discard(repo_id)


def run(repo_id, cmd, question=''):
    """同步跑一次 Daemon 并收卡（测试直接调这个）。→ 这次 run 的记录。"""
    meta = repo_meta(repo_id)
    snap = snapshot_dir(repo_id)
    _git(['checkout', '-q', '--', '.'], snap)            # 上一次跑留下的改动先还原
    _git(['clean', '-qfd'], snap)
    with open(os.path.join(snap, 'DAEMON.md'), 'w', encoding='utf-8') as f:
        f.write(DAEMON_MD)
    with open(os.path.join(snap, 'task.txt'), 'w', encoding='utf-8') as f:
        f.write(question or DEFAULT_TASK)
    n = len(meta.get('runs') or []) + 1
    logs = os.path.join(repo_dir(repo_id), 'runs')
    os.makedirs(logs, exist_ok=True)
    started = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(os.path.join(logs, f'{n}.log'), 'w', encoding='utf-8') as log:
        try:
            p = subprocess.run([*cmd, 'task.txt'], cwd=snap, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT,
                               env=dict(os.environ, VERBATIM_REPO_ID=repo_id, PYTHONUNBUFFERED='1'))
            code = p.returncode
        except subprocess.TimeoutExpired:
            code = 'timeout'
    try:
        with open(os.path.join(snap, CARDS_FILE), encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        raw = ''
    touched = [ln[3:] for ln in _git(['status', '--porcelain'], snap, text=True).stdout.splitlines()
               if ln[3:] not in (CARDS_FILE, 'DAEMON.md', 'task.txt', 'patch.txt')
               and not ln[3:].startswith('chat_history')]
    kept, dropped = ingest(repo_id, raw, question=question, run=n)
    rec = {'n': n, 'question': question, 'started_at': started,
           'finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'exit': code,
           'kept': kept, 'dropped': dropped, 'touched': touched}
    meta = repo_meta(repo_id)
    meta['runs'] = (meta.get('runs') or []) + [rec]
    meta['status'] = 'ready'
    meta['error'] = '' if kept or not raw else 'No card matched the code'
    if not raw:
        meta['error'] = 'Daemon wrote no cards' if code == 0 else f'Daemon stopped ({code}) without cards'
    _save_meta(meta)
    return rec


def ingest(repo_id, raw, question='', run=0):
    """JSONL 卡片 → 核对后追加进 cards.json。→ (收下几张, [{line, reason}])"""
    snap = snapshot_dir(repo_id)
    cards = repo_cards(repo_id)
    seen = [(c['path'], c['start'], c['end']) for c in cards]
    kept, dropped = 0, []
    for ln, line in enumerate((raw or '').splitlines(), 1):
        if not line.strip():
            continue
        try:
            c = json.loads(line)
            card = verify(snap, c)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            dropped.append({'line': ln, 'reason': str(e)[:120]})
            continue
        # 同一段，或被已有的卡整段包住（两次读常给同一处 73–75 / 73–74 两张）：算重复
        if any(p == card['path'] and s <= card['start'] and card['end'] <= e for p, s, e in seen):
            dropped.append({'line': ln, 'reason': 'duplicate'})
            continue
        seen.append((card['path'], card['start'], card['end']))
        card.update(i=len(cards), question=question, run=run)
        cards.append(card)
        kept += 1
    _write_json(os.path.join(repo_dir(repo_id), 'cards.json'), cards)
    return kept, dropped


def verify(snap, c):
    """一张卡对快照：路径要在快照里；quote 逐字在 start–end → 原样收；只在别处出现一次 → 改行号；否则 ValueError。"""
    rel = str(c['path']).strip()
    rel = os.path.normpath(rel[2:] if rel.startswith('./') else rel)
    full = os.path.realpath(os.path.join(snap, rel))
    if not full.startswith(os.path.realpath(snap) + os.sep) or not os.path.isfile(full):
        raise ValueError(f'no such file: {rel}')
    with open(full, encoding='utf-8', errors='replace') as f:
        lines = f.read().split('\n')
    quote = str(c['quote']).strip('\n')
    if not quote.strip():
        raise ValueError('empty quote')
    qlines = quote.split('\n')
    if len(qlines) > MAX_CARD_LINES:
        raise ValueError('quote too long')
    start = int(c.get('start') or 0)
    if '\n'.join(lines[start - 1:start - 1 + len(qlines)]) != quote or start < 1:
        hits = [i for i in range(len(lines) - len(qlines) + 1) if lines[i:i + len(qlines)] == qlines]
        if not hits:                         # 只差缩进 / 行尾空白（模型抄第一行常丢缩进）：按去掉空白比，收文件里的原文
            bare = [q.strip() for q in qlines]
            hits = [i for i in range(len(lines) - len(qlines) + 1)
                    if [x.strip() for x in lines[i:i + len(qlines)]] == bare]
        if len(hits) != 1:
            raise ValueError('quote not found in file' if not hits else 'quote appears more than once')
        start = hits[0] + 1
        quote = '\n'.join(lines[hits[0]:hits[0] + len(qlines)])
    layer = str(c.get('layer') or '').strip()
    return {'path': rel, 'start': start, 'end': start + len(qlines) - 1, 'quote': quote,
            'obs': str(c.get('obs') or '').strip()[:600], 'layer': layer if layer in LAYERS else '自证',
            'topic': str(c.get('topic') or '').strip()[:30]}


def list_repos(ids):
    return [m for m in (repo_meta(i) for i in ids) if m]


def reading(repo_id):
    with _lock:
        return repo_id in _running
