"""资料库「问」（libask.py + look.py，2026-10-19）：一句话描述 → 最像的几份转写 + 理由 + 跳到哪一秒。
用户：「借鉴 Ephemeris 的问，但是用来找转写」「Moye 也做了个，互相参考」。
跑生产代码：look.run 的各条命令（真的 library.db）、libask.find / find_catalog / pick、POST / GET /api/find、/api/find/stop。
桩（只在模型边界）：claude_cli 的 runner（假的 claude -p 事件流）、find_catalog 的 ask（假的便宜模型）。"""
import json
import os
import threading
import time
import uuid

from _support import Checks, isolate, make_transcript, write_json

isolate('libask')
import app as A  # noqa: E402
import claude_cli  # noqa: E402
import libask  # noqa: E402
import library  # noqa: E402
import look  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
client = A.app.test_client()


def transcript(segs, title, date, **meta):
    tid = make_transcript(R, segs, title=title, tid=str(uuid.uuid4()))
    p = os.path.join(R, tid, 'meta.json')
    m = json.load(open(p, encoding='utf-8'))
    m.update(date=date, **meta)
    write_json(p, m)
    return tid


DORM = transcript([('00:04:07', '说话人1：我们之间还是不一样。'), ('00:07:41', '说话人2：У нас есть набор признаков, SHAP это всё объясняет.'),
                   ('00:18:46', '说话人2：к TOEFL подготовиться'), ('00:26:40', '说话人3：你数学不是考试不是考的10题吗？')],
                  '学生多线程对话：模型解释性与考前复习', '2026-10-18 22:53:33', ai_one_line='宿舍里三个人聊考试', ai_tags=['考前复习'],
                  source_url='')
write_json(os.path.join(R, DORM, 'summary.json'), {'overview': '前半段讲模型可解释性。',
                                                   'sections': [{'title': '模型可解释性', 'time_range': '00:07:41 - 00:08:23', 'summary': 'SHAP'}]})
TED = transcript([('00:01:20', 'shape and feeling that solves the problem'), ('00:03:00', 'dynamic thinking can shape how we connect')],
                 'TEDx student talks', '2026-10-17 09:00:00', source_url='https://youtu.be/x', creator='TEDx')
OLD = transcript([('00:00:10', '考试周之前的复习计划')], '九月复习课', '2026-09-03 08:00:00')
library.sync()


def run(*argv):
    return look.run(list(argv))


print('look.py 命令')
out = run('search', '考试')
t.check('search 正文命中，带短号和那一句的时间', f'[[t:{DORM[:8]}@00:26:40]]' in out and f'[[t:{OLD[:8]}]]' in out, out)
t.check('search 默认几个词都要出现', OLD[:8] not in run('search', '考试', 'SHAP') and DORM[:8] in run('search', '考试', 'SHAP'))
t.check('search --any 任一个词', OLD[:8] in run('search', '考试', 'SHAP', '--any'))
t.check('search 按日期筛', OLD[:8] not in run('search', '考试', '--since', '2026-10-01')
        and DORM[:8] in run('search', '考试', '--until', '2026-10-18'))
t.check('search 按博主筛', TED[:8] in run('search', 'shape', '--creator', 'ted') and DORM[:8] not in run('search', 'shape', '--creator', 'ted'))
t.check('search 标题 / 标签也算', DORM[:8] in run('search', '考前复习'))
t.check('hits 列出每一句和时间', run('hits', TED[:8], 'shape').count(f'[[t:{TED[:8]}@') == 2)
r = run('read', DORM[:8], '--from', '00:18:00', '--n', '1')
t.check('read --from 从那个时间往后读', '00:18:46' in r and '00:07:41' not in r and '--from 00:26:40' in r, r)
t.check('read --at 跳到词前后', 'TOEFL' in run('read', DORM, '--at', 'TOEFL'))
info = run('info', DORM[:8])
t.check('info 有一句话、标签、摘要和分节（分节带时间记号）', '宿舍里三个人' in info and '考前复习' in info
        and f'[[t:{DORM[:8]}@00:07:41]]' in info, info)
t.check('list 按时间，最新的在前', run('list').index(DORM[:8]) < run('list').index(OLD[:8]))
t.check('stats 总量', '一共 3 份' in run('stats'))
t.check('sql 只读查询', '3' in run('sql', 'SELECT COUNT(*) FROM docs'))
t.check('sql 不许改库', '只能写一条 SELECT' in run('sql', 'DELETE FROM docs') and '只能' in run('sql', 'SELECT 1; DELETE FROM docs')
        and '3' in run('sql', 'SELECT COUNT(*) FROM docs'))
t.check('sql 连 WITH 里塞写操作也写不进（只读打开）', 'SQL 出错' in run('sql', 'WITH x AS (SELECT 1) DELETE FROM docs')
        or '只能' in run('sql', 'WITH x AS (SELECT 1) DELETE FROM docs'))
t.check('认不出的短号直说', '认不出' in run('info', 'zzzz') and '认不出' in run('read', 'abcdef99'))
t.check('命令写错给用法，不崩', '命令写错了' in run('bogus'))


print('\nClaude 自己查（假的 claude -p）')
seen = {'args': None, 'stdin': ''}


def tool(cmd):
    return {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'input': {'command': cmd}}]}}


def claude_ok(args, stdin):
    seen['args'], seen['stdin'] = args, stdin
    answer = json.dumps({'results': [
        {'t': DORM[:8], 'at': '00:07:50', 'why': '10 月 18 日宿舍录音，讲 SHAP'},
        {'t': 'deadbeef', 'at': '', 'why': '编的'},
        {'t': DORM, 'at': '', 'why': '重复'},
        {'t': TED[:8], 'at': '99:99', 'why': '时间不对'},
        {'t': OLD[:8], 'at': '', 'why': '只看了摘要'}], 'note': ''}, ensure_ascii=False)
    return {'events': [tool(f'{libask.LOOK} search SHAP --since 2026-10-01'), tool(f'{libask.LOOK} read {DORM[:8]} --at SHAP'),
                       tool(f'{libask.LOOK} hits {TED[:8]} shape'), tool(f'{libask.LOOK} info {OLD[:8]}'), tool('rm -rf /')],
            'result': '好的，结果：\n' + answer, 'session_id': 's1',
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}}


claude_cli.use_runner(claude_ok)
steps = []
res = libask.find('上周宿舍那个讲 SHAP 的俄语录音', progress=steps.append)
a = seen['args']
t.check('只给 Bash、只放行 look.py 这一条命令', a[a.index('--tools') + 1] == 'Bash'
        and a[a.index('--allowedTools') + 1] == f'Bash({libask.LOOK}:*)', a)
t.check('找东西用 Sonnet', a[a.index('--model') + 1] == libask.MODEL)
t.check('提示词里有问题、今天、库的大小', '讲 SHAP 的俄语录音' in seen['stdin'] and '一共 3 份' in seen['stdin'])
t.check('进度：搜了什么、读了哪份（显示标题），别的命令不算', [(s['k'], s['d']) for s in steps] == [
    ('search', 'SHAP'), ('read', '学生多线程对话：模型解释性与考前复习'), ('hits', 'TEDx student talks · shape'),
    ('info', '九月复习课')], steps)
ids = [x['id'] for x in res['results']]
t.check('编的、重复的丢掉，短号认回完整 id；只看了摘要（info）没读原文的也丢掉', ids == [DORM, TED], ids)
first = res['results'][0]
t.check('时间对到那份里真有的一句（之前最近的一句）', first['at'] == '00:07:41' and first['sec'] == 461
        and 'SHAP' in first['quote'], first)
t.check('时间对不上就留空', res['results'][1]['at'] == '' and res['results'][1]['sec'] is None)
t.check('结果带卡片要的字段', first['ai_title'].startswith('学生多线程') and first['why'].startswith('10 月 18 日'))
t.check('记账：订阅额度记 $0，用途 find', any(r.get('purpose') == 'find' for r in A.usage.recent(20))
        if hasattr(A.usage, 'recent') else True)


print('\n没有 Claude Code：只看目录')
got = {}


def cheap(prompt):
    got['p'] = prompt
    return '{"results":[{"n":3,"why":"九月的复习课"},{"n":9,"why":"编的"},{"n":3,"why":"重复"}]}'


res = libask.find_catalog('九月那节复习课', ask=cheap)
t.check('目录最新的在前、有标题和一句话', got['p'].index('学生多线程') < got['p'].index('九月复习课') and '宿舍里三个人' in got['p'])
t.check('编号对回真实的转写，编的、重复的丢掉', [x['id'] for x in res['results']] == [OLD] and res['mode'] == 'catalog')
_, by_n, total = libask.catalog(budget=80)
t.check('--mine 只看自己上传的', OLD[:8] in run('search', '考试', '--mine') and TED[:8] not in run('search', 'shape', '--mine')
        and TED[:8] not in run('list', '--mine') and DORM[:8] in run('list', '--mine'))
t.check('目录放不下就从旧的那头截', len(by_n) == 1 and by_n[1] == DORM and total == 3, by_n)


print('\n接口')
t.check('空问题 400', client.post('/api/find', json={'q': '  '}).status_code == 400)
gate = threading.Event()


def claude_slow(args, stdin):
    gate.wait(5)
    return claude_ok(args, stdin)


claude_cli.use_runner(claude_slow)
r = client.post('/api/find', json={'q': '讲 SHAP 的录音'})
t.check('开始问', r.status_code == 200 and r.get_json()['running'])
t.check('同一时间只问一个', client.post('/api/find', json={'q': '再问'}).status_code == 409)
t.check('这台电脑能查正文', client.get('/api/find').get_json()['mode_now'] == 'claude')
gate.set()
for _ in range(50):
    st = client.get('/api/find').get_json()
    if not st['running']:
        break
    time.sleep(0.1)
t.check('问完：结果、进度、用时', not st['running'] and [x['id'] for x in st['result']['results']] == [DORM, TED]
        and len(st['steps']) == 4 and st['ended'] >= st['started'], st)

gate.clear()
client.post('/api/find', json={'q': '停下'})
t.check('停止', client.post('/api/find/stop').get_json()['stopped'])
gate.set()
for _ in range(50):
    st = client.get('/api/find').get_json()
    if not st['running']:
        break
    time.sleep(0.1)
t.check('停下了不给结果', st['result'] is None and st['error'] == 'stopped', st)

claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': 'boom', 'events': []})
client.post('/api/find', json={'q': '出错'})
for _ in range(50):
    st = client.get('/api/find').get_json()
    if not st['running']:
        break
    time.sleep(0.1)
t.check('Claude 报错：说出原因，不卡在问的状态', not st['running'] and 'boom' in st['error'], st)

real = claude_cli.available
claude_cli.available = lambda: False
t.check('没有 Claude Code：模式是只看目录', client.get('/api/find').get_json()['mode_now'] == 'catalog')
claude_cli.available = real

t.finish()
