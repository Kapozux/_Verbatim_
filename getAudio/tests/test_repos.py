"""代码库来源（repos.py）：快照、挑掉密钥、跑读码 agent、逐字核对收卡、进语料、出处认 r 开头的 id。

跑生产代码：repos.create_repo / run / ingest / verify；app 的 /sources/repo、/sources/repo/<id>/read、/sources、
/api/repos/<id>、/api/repos/<id>/file、删来源；ask.load(passages) 的 _add_repo_cards、answer → _finish；citations。
桩：读码 agent 换成本地假脚本（写几张对的、行号偏的、编造的、越界的卡，再偷偷改一个文件）；ask._llm、ask._embed_texts。
仓库是临时目录里现建的 git 仓库，不碰任何真实仓库。
"""
import json
import os
import re
import subprocess
import sys
import time

from _support import Checks, fake_embed, isolate

TMP = isolate('repos')
import app as A  # noqa: E402
import ask  # noqa: E402
import repos as R  # noqa: E402

t = Checks()
assert R.REPOS_DIR.startswith(TMP)

# ---- 一个小仓库：说明、代码、一个提交了的 .env、一个带密钥的配置 ----
SRC = os.path.join(TMP, 'proj')
os.makedirs(os.path.join(SRC, 'pkg'))
files = {
    'README.md': '# Demo\n\nThe cache expires after ten minutes.\n',
    'pkg/cache.py': 'import time\n\nTTL = 600\n\n\ndef fresh(stamp):\n    return time.time() - stamp < TTL\n',
    '.env': 'TOKEN=abc\n',
    'pkg/keys.py': 'KEY = "' + 'sk-' + 'a1b2' * 6 + '"\n',
}
for rel, body in files.items():
    with open(os.path.join(SRC, rel), 'w') as f:
        f.write(body)


def git(*args):
    subprocess.run(['git', *args], cwd=SRC, check=True, capture_output=True)


git('init', '-q')
git('add', '-A')
git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init')
with open(os.path.join(SRC, 'pkg/cache.py'), 'a') as f:      # 没提交的改动不进快照
    f.write('# local edit\n')

# 1 快照：只有提交过的、挑掉密钥、记 commit 和 dirty
m = R.create_repo(SRC)
snap = R.snapshot_dir(m['id'])
t.check('快照只带提交过的文件、跳过 .env 和带密钥的文件',
        os.path.isfile(os.path.join(snap, 'pkg/cache.py')) and not os.path.exists(os.path.join(snap, '.env'))
        and not os.path.exists(os.path.join(snap, 'pkg/keys.py'))
        and sorted(m['skipped']) == ['.env', 'pkg/keys.py'], m['skipped'])
t.check('没提交的改动不在快照里，meta 标 dirty',
        '# local edit' not in open(os.path.join(snap, 'pkg/cache.py')).read() and m['dirty']
        and len(m['commit']) == 40)
t.check('同一仓库同一 commit 复用快照', R.create_repo(SRC)['id'] == m['id'])
bad = 0
for p in [os.path.join(TMP, 'nope'), TMP]:
    try:
        R.create_repo(p)
    except ValueError:
        bad += 1
t.check('不存在的目录、不是 git 仓库都拒绝', bad == 2)

# ---- 假的读码 agent：按 task.txt 写卡，第一次还偷偷改了一个文件 ----
FAKE = os.path.join(TMP, 'fake_daemon.py')
with open(FAKE, 'w') as f:
    f.write(r'''
import json, os, sys
task = open(sys.argv[1]).read()
assert os.path.exists('DAEMON.md')
if 'TTL' in task:
    cards = [{"path": "pkg/cache.py", "start": 6, "end": 7, "quote": "def fresh(stamp):\n    return time.time() - stamp < TTL",
              "obs": "fresh compares age with TTL", "layer": "自证", "topic": "缓存"}]
else:
    cards = [
        {"path": "README.md", "start": 3, "end": 3, "quote": "The cache expires after ten minutes.",
         "obs": "README promises ten minutes", "layer": "主张", "topic": "缓存"},
        {"path": "pkg/cache.py", "start": 1, "end": 1, "quote": "TTL = 600",
         "obs": "600 seconds = ten minutes", "layer": "自证", "topic": "缓存"},
        {"path": "pkg/cache.py", "start": 3, "end": 3, "quote": "TTL = 900", "obs": "made up", "layer": "自证"},
        {"path": "../proj/README.md", "start": 1, "end": 1, "quote": "# Demo", "obs": "outside", "layer": "主张"},
    ]
    with open('pkg/cache.py', 'a') as fh:
        fh.write('# agent was here\n')
with open('verbatim_cards.jsonl', 'a') as fh:
    for c in cards:
        fh.write(json.dumps(c, ensure_ascii=False) + '\n')
    if 'TTL' not in task:
        fh.write('{broken json\n')
''')
BUNDLED = [sys.executable, os.path.join(R.BUNDLED_DAEMON, 'My_agent.py')]

# 副本的危险命令拦截：只读的放行（别的仓库里的 llm.py、os.environ），删库 / .env / 改它自己的文件照拦
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location('daemon_actions', os.path.join(R.BUNDLED_DAEMON, 'actions.py'))
DA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DA)
_cwd = os.getcwd()
os.chdir(SRC)
allowed = ['read llm.py 1-20', 'search os.environ .', 'grep -n os.environ config.py', "nl -ba actions.py | sed -n '1,9p'"]
blocked = ['rm -rf build', 'cat .env', 'git push --force', 'write .env 1 X=1',
           f"write {os.path.join(R.BUNDLED_DAEMON, 'llm.py')} 1 x"]
t.check('副本的危险命令拦截：只读放行、危险的照拦', not any(DA.is_dangerous(x) for x in allowed)
        and all(DA.is_dangerous(x) for x in blocked), [(x, DA.is_dangerous(x)) for x in allowed + blocked])
os.chdir(_cwd)
t.check('默认用自带的 Daemon 副本、Verbatim 自己的 Python', R.daemon_command() == BUNDLED, R.daemon_command())
R.daemon_command = lambda: [sys.executable, FAKE]

# 2 跑一次：对的收、行号偏的改正、编造的 / 越界的 / 坏行丢掉、记下动过的文件
rec = R.run(m['id'], R.daemon_command())
cards = R.repo_cards(m['id'])
t.check('对的卡收下、行号偏的改成真行号', rec['kept'] == 2 and cards[1]['start'] == 3 and cards[1]['end'] == 3
        and cards[0]['layer'] == '主张', rec)
t.check('编造的原文、仓库外的路径、坏 JSON 都丢掉', len(rec['dropped']) == 3, rec['dropped'])
t.check('记下 agent 动过的文件', rec['touched'] == ['pkg/cache.py'], rec['touched'])

# 3 再读一次（带问题）：上次的改动先还原，新卡追加、旧下标不变
rec2 = R.run(m['id'], R.daemon_command(), question='How is TTL used?')
cards = R.repo_cards(m['id'])
t.check('再读：新卡追加、旧下标不变、记着问题',
        rec2['kept'] == 1 and [c['i'] for c in cards] == [0, 1, 2] and cards[2]['question'] == 'How is TTL used?'
        and cards[2]['start'] == 6, cards)
t.check('再读前还原上次的改动', '# agent was here' not in open(os.path.join(snap, 'pkg/cache.py')).read())
kept, _ = R.ingest(m['id'], json.dumps({'path': 'pkg/cache.py', 'start': 6, 'end': 7,
                                        'quote': 'def fresh(stamp):\nreturn time.time() - stamp < TTL'}))
t.check('只丢了缩进：收下、原话换成文件里的', kept == 0 and len(R.repo_cards(m['id'])) == 3)   # 6-7 已有，算重复
v = R.verify(snap, {'path': 'pkg/cache.py', 'start': 1, 'quote': '   return time.time() - stamp < TTL  '})
t.check('只差首尾空白：按原文定位、quote 用文件里的', v['start'] == 7 and v['quote'] == '    return time.time() - stamp < TTL', v)
t.check('被已有的卡整段包住的也算重复', R.ingest(m['id'], json.dumps({'path': 'pkg/cache.py', 'start': 7, 'end': 7,
        'quote': '    return time.time() - stamp < TTL'}))[0] == 0)
t.check('同一段不重复收', R.ingest(m['id'], json.dumps({'path': 'README.md', 'start': 3, 'end': 3,
                                                        'quote': 'The cache expires after ten minutes.'}))[0] == 0)

# 4 项目：加仓库来源（顺手开读）、来源列表、语料、出处
ask._embed_texts = fake_embed
c = A.app.test_client()
P = c.post('/api/projects', json={'name': 'Code'}).get_json()['id']
SRC2 = os.path.join(TMP, 'proj2')
subprocess.run(['git', 'clone', '-q', SRC, SRC2], check=True, capture_output=True)
r = c.post(f'/api/chain/{P}/sources/repo', json={'path': SRC2, 'read': True}).get_json()
rid = r.get('repo_id')
end = time.time() + 30
while time.time() < end and R.reading(rid):
    time.sleep(0.2)
src = c.get(f'/api/chain/{P}/sources').get_json()
row = (src.get('repos') or [{}])[0]
t.check('加仓库来源：登记进项目、读完有卡', rid and row.get('repo_id') == rid and row.get('cards') == 2
        and row.get('status') == 'ready' and row.get('commit'), row)
det = c.get(f'/api/chain/{P}').get_json()
t.check('有卡的代码库算进项目的来源数（提问栏据此打开）', det.get('n_sources') == 1 and det.get('n_repos') == 1)
t.check('来源行带估价（按行数和价格表）', row.get('estimate') and 0.2 < row['estimate'] < 0.5, row.get('estimate'))
t.check('加不存在的路径报 400', c.post(f'/api/chain/{P}/sources/repo', json={'path': '/nope/x'}).status_code == 400)

corpus = ask.load(A._chain_dir(P), passages=True)
ep = next((e for e in corpus['episodes'] if e.get('kind') == 'repo'), None)
rc = [x for x in corpus['cards'] if x['id'].startswith('r')]
t.check('语料里有 REPO1 和它的卡（位置是 文件:行）', ep and ep['label'] == 'REPO1' and len(rc) == 2
        and rc[0]['heading'] == 'README.md:3-3' and rc[0]['layer'] == 'claim' and rc[1]['layer'] == 'transcript', rc)

prompts = []


def fake_llm(prompt, **k):
    prompts.append(prompt)
    ids = re.findall(r'^\[#(r\d+-\d+)\]', prompt, re.M)
    return f'The README says ten minutes [#{ids[0]}] and the code uses 600 seconds [#{ids[1]}]. Made up [#r1-99].'


ask._llm = fake_llm
res = ask.answer(A._chain_dir(P), 'How long does the cache last?')
used = res.get('citations') or res.get('cards') or {}
if isinstance(used, list):
    used = {u['id']: u for u in used}
views = list(used.values())
t.check('提示词里代码库卡用短别名、标出 文件:行', prompts and re.search(r'^\[#r1-0\] \| REPO1 \| README\.md:3-3 \| code claim',
                                                         prompts[0], re.M), prompts[0][-800:] if prompts else '')
t.check('回答里的短别名认回真 id、编造的删掉', len(views) == 2 and all(v['kind'] == 'repo' for v in views)
        and '[#r1-99]' not in res['answer'] and all(re.fullmatch(r'r\d+-\d', i) for i in used), (res['answer'], list(used)))
t.check('出处带仓库和行号', {(v['path'], v['line']) for v in views} == {('README.md', 3), ('pkg/cache.py', 3)}
        and all(v['repo_id'] == rid for v in views), views)

pool = ask.scope_pool(corpus, {'type': 'all', 'sources': [rid]})
t.check('勾选范围认代码库 id；按类型筛时算在文档那边', pool and all(x['id'].startswith('r') for x in pool)
        and not any(x['id'].startswith('r') for x in ask.scope_pool(corpus, {'type': 'media'}))
        and len([x for x in ask.scope_pool(corpus, {'type': 'docs'}) if x['id'].startswith('r')]) == 2)

# 有录音的项目：代码库不占 EP 编号、不被算成谁说的
A.app.config['TESTING'] = True
from _support import make_transcript  # noqa: E402
tid = make_transcript(os.path.join(TMP, 'results'), [('00:01', 'Hello there.')], title='Rec')
A._project_add_recordings(P, [tid])
st = A._read_chain(P)
st['index_transcripts'] = True
A._save_chain(st)
corpus2 = ask.load(A._chain_dir(P), passages=True)
rec_ep = next((e for e in corpus2['episodes'] if e.get('task_id') == tid), None)
t.check('代码库不占 EP 编号', rec_ep and rec_ep['label'] == 'EP1', [e['label'] for e in corpus2['episodes']])
t.check('提示词的期列表标出代码库', '(code repository)' in ask._episode_lines(corpus2))

# 读到一半服务重启：meta 停在 reading，这个进程里没在读 → 界面上算失败、能再读
stuck = R.repo_meta(rid)
stuck['status'] = 'reading'
R._save_meta(stuck)
row2 = c.get(f'/api/chain/{P}/sources').get_json()['repos'][0]
t.check('读到一半服务重启：显示为中断、不一直转圈', row2['status'] == 'failed' and 'Interrupted' in row2['error'], row2)
stuck['status'] = 'ready'
R._save_meta(stuck)

# 5 看文件：只读快照，出不去
ok_file = c.get(f'/api/repos/{rid}/file?path=pkg/cache.py').get_json()
t.check('出处点开能看快照里的文件', ok_file.get('text', '').startswith('import time'))
t.check('路径越界 / .git 一律 404', all(c.get(f'/api/repos/{rid}/file?path={p}').status_code == 404
                                    for p in ['../../meta.json', '/etc/hosts', '.git/config']))
t.check('/api/repos/<id> 有卡和读的记录', len(c.get(f'/api/repos/{rid}').get_json()['runs']) == 1)

# 6 拿掉来源：项目里没了，全局快照还在（别的项目可能在用）
t.check('删来源', c.delete(f'/api/chain/{P}/sources/{rid}').status_code == 200
        and not c.get(f'/api/chain/{P}/sources').get_json()['repos'] and R.repo_meta(rid))

# 7 自带的 Daemon 副本真跑一遍：模型换成本机的假「阿里云」（127.0.0.1，按剧本回话），子进程连不到外网
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

seen = []
CARD_CMD = ("python3 - <<'EOF'\nimport json\ncard = {\"path\": \"README.md\", \"start\": 3, \"end\": 3, "
            "\"quote\": \"The cache expires after ten minutes.\", \"obs\": \"ten minutes\", \"layer\": \"主张\", "
            "\"topic\": \"缓存\"}\nopen(\"verbatim_cards.jsonl\", \"a\", encoding=\"utf-8\").write("
            "json.dumps(card, ensure_ascii=False) + \"\\n\")\nEOF")


class FakeAliyun(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen.append({'auth': self.headers.get('Authorization'), 'model': body['model'], 'messages': body['messages']})
        msgs = body['messages']
        last = msgs[-1]['content']
        if 'plan-unwanted' in msgs[0]['content']:
            reply = 'plan-unwanted'
        elif last.startswith('开始执行'):
            reply = '先看说明\n```bash-action\ncat README.md\n```'
        elif 'The cache expires' in last:                      # 审查拒收这段输出
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": {"code": "data_inspection_failed"}}')
            return
        elif '内容审查' in last:                                # 换成说明之后：用 DeepSeek 自带的写法写卡
            reply = f'<invoke name="execute">\n<parameter name="command">{CARD_CMD}</parameter>\n</invoke>'
        else:
            reply = '卡写好了。\nexit-verified'                  # 不包代码块的收尾也要认
        out = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': reply}}],
                          'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(out)


srv = HTTPServer(('127.0.0.1', 0), FakeAliyun)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ['ALIYUN_COMPAT_BASE'] = f'http://127.0.0.1:{srv.server_port}/v1'
os.environ['DASHSCOPE_API_KEY'] = 'test-key'
SRC3 = os.path.join(TMP, 'proj3')
subprocess.run(['git', 'clone', '-q', SRC, SRC3], check=True, capture_output=True)
m3 = R.create_repo(SRC3)
rec3 = R.run(m3['id'], BUNDLED)
log3 = open(os.path.join(R.repo_dir(m3['id']), 'runs', '1.log'), encoding='utf-8').read()
t.check('副本跑通：路由 → 执行 → 认出 <invoke> 写法 → 收卡', rec3['exit'] == 0 and rec3['kept'] == 1
        and R.repo_cards(m3['id'])[0]['layer'] == '主张', (rec3, log3[-1500:]))
t.check('用 Verbatim 的 key 和模型名、不带私有字段', seen and all(s['auth'] == 'Bearer test-key' for s in seen)
        and {s['model'] for s in seen} == {'deepseek-v4-pro'}
        and all(set(m) == {'role', 'content'} for s in seen for m in s['messages']), seen[:1])
t.check('审查拒收：那段输出换成说明后接着跑', any('内容审查' in s['messages'][-1]['content'] for s in seen)
        and not any('The cache expires' in m['content'] for s in seen[-1:] for m in s['messages']
                    if m['role'] == 'user'))
import usage  # noqa: E402
rows = [r for r in usage._conn().execute(
    "SELECT purpose, ref, input_tokens, cost_usd FROM calls WHERE purpose='repo_read'")]
t.check('每次调用记进 usage.db（purpose=repo_read，ref=代码库 id）、按价格表算出钱', len(rows) == len(seen) - 1
        and all(r[1] == m3['id'] and r[2] == 100 and r[3] and r[3] > 0 for r in rows), (rows, len(seen)))
t.check('单独一行的 exit-verified 当成收尾：不再被催', not any('先跑相关测试' in m['content'] or '无可执行内容' in m['content']
                                                for s in seen for m in s['messages'] if m['role'] == 'user'))
srv.shutdown()
t.finish()
