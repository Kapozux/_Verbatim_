"""笔记本对话：多段对话（历史记录，照 Ephemeris）+ 每问选模型（2026-11-06）。
跑生产代码：app 路由、ask 的对话存取 / 旧 chat.json 迁移 / answer_stream 按模型分派、出处校验。
桩（只在模型边界）：analyze._call_gemini_stream、claude_cli 的 runner、codex_cli 的 runner。"""
import json
import os
import re

from _support import Checks, isolate, make_creator, write_json

isolate('chatthreads')
os.environ['CODEX_ENGINE'] = ''          # isolate 已把 ASK_ENGINE 置空；这两个也置空，别让 .env 里开着的起真进程
import analyze  # noqa: E402
import app as A  # noqa: E402
import ask  # noqa: E402
import claude_cli  # noqa: E402
import codex_cli  # noqa: E402
import agy_cli  # noqa: E402

CID = make_creator(A, 'Y Combinator', 'ycombinator', [
    ['Solo founders are rising fast.', 'Experienced founders are back.', 'Talk to users every week.'],
    ['The harness matters more than the model.', 'Agents need good tools.', 'Ship small and often.']])
CDIR = A._chain_dir(CID)
client = A.app.test_client()
t = Checks()
seen = {'gemini': [], 'claude': [], 'codex': 0, 'codex_models': [], 'agy': [], 'prompts': []}


def ids(prompt):
    return re.findall(r'^\[#([^\]]+)\] \| EP', prompt, re.M)[:2]


def fake_gemini(prompt, model=None, purpose='analysis'):
    seen['gemini'].append(model)
    seen['prompts'].append(prompt)
    yield f'Gemini says solo founders rise [#{ids(prompt)[0]}].'


def claude_ok(args, stdin):
    seen['claude'].append(args[args.index('--model') + 1])
    text = f'Claude: harness matters [#{ids(stdin)[1]}].'
    return {'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                        'delta': {'type': 'text_delta', 'text': text}}}],
            'result': text, 'usage': {'input_tokens': 10, 'output_tokens': 5}}


def codex_ok(args, stdin):
    seen['codex'] += 1
    seen['codex_models'].append(args[args.index('-m') + 1] if '-m' in args else None)
    return [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': f'GPT: ship small [#{ids(stdin)[0]}].'}},
            {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}], ''


def agy_ok(args):
    seen['agy'].append(args[args.index('--model') + 1])
    prompt = args[args.index('-p') + 1]
    return {'status': 'SUCCESS', 'response': f'Argon: agents need tools [#{ids(prompt)[1]}].',
            'usage': {'input_tokens': 10, 'output_tokens': 5}}


analyze._call_gemini_stream = fake_gemini
ask._llm = lambda prompt, **k: prompt
claude_cli.BIN = 'no-such-claude-binary'   # 本机装了 claude / codex 也当没装：要用的地方用 runner 打桩
codex_cli.BIN = 'no-such-codex-binary'
agy_cli.BIN = 'no-such-agy-binary'
os.environ.pop('AGY_BIN', None)
agy_cli.installed = lambda: False


def stream(q, **extra):
    r = client.post(f'/api/chain/{CID}/ask/stream', json={'question': q, 'ui_lang': 'en', **extra})
    evs = [json.loads(ln) for ch in r.response for ln in ch.decode().splitlines() if ln.strip()]
    return r.status_code, evs


# 1. 旧的单段 chat.json 自动变成第一段对话，内容不丢
ask.clear_history(CDIR)
write_json(os.path.join(CDIR, 'chat.json'), {'messages': [
    {'role': 'user', 'content': 'Old question about founders', 'at': '2026-10-01 10:00:00'},
    {'role': 'assistant', 'content': 'Old answer', 'citations': {}, 'at': '2026-10-01 10:00:00'}]})
h = client.get(f'/api/chain/{CID}/ask').get_json()
t.check('旧记录迁移成一段对话', len(h['threads']) == 1 and h['threads'][0]['title'].startswith('Old question')
        and len(h['messages']) == 2 and h['thread'] == h['threads'][0]['id']
        and not os.path.exists(os.path.join(CDIR, 'chat.json')), h)
old_tid = h['thread']

# 2. 不带 thread 问 → 接在最近那段后面
code, ev = stream('What about solo founders?')
t.check('不带 thread 接在当前那段', code == 200 and ev[-1]['type'] == 'done'
        and ev[-1]['message'].get('thread') == old_tid and len(ask.load_history(CDIR, old_tid)) == 4, ev[-1])

# 3. 新对话：看不到旧那段的上下文；列表里两段、最近的在前、标题取第一问
new_tid = client.post(f'/api/chain/{CID}/ask/threads').get_json()['thread']
seen['prompts'].clear()
code, ev = stream('Tell me about agents', thread=new_tid)
h = client.get(f'/api/chain/{CID}/ask').get_json()
t.check('新对话不带旧上下文', 'Old question' not in seen['prompts'][-1] and 'solo founders?' not in seen['prompts'][-1])
t.check('列表两段、最近的在前、默认打开最近那段',
        [x['id'] for x in h['threads']] == [new_tid, old_tid] and h['thread'] == new_tid
        and h['threads'][0]['title'] == 'Tell me about agents' and h['threads'][0]['n'] == 2, h['threads'])
h_old = client.get(f'/api/chain/{CID}/ask?thread={old_tid}').get_json()
t.check('能翻回旧那段', h_old['thread'] == old_tid and len(h_old['messages']) == 4)

# 4. 空的新对话不进列表；再开新的会把空的那段收掉
empty = client.post(f'/api/chain/{CID}/ask/threads').get_json()['thread']
empty2 = client.post(f'/api/chain/{CID}/ask/threads').get_json()['thread']
h = client.get(f'/api/chain/{CID}/ask').get_json()
t.check('空对话不进列表、只留一段空的', h['thread'] == empty2 and empty2 not in [x['id'] for x in h['threads']]
        and not os.path.exists(os.path.join(CDIR, 'chats', empty + '.json')), h['threads'])

# 5. 改名、删除；乱写的 thread 号 400
r = client.post(f'/api/chain/{CID}/ask/threads/{old_tid}', json={'title': '  Founders talk  '}).get_json()
t.check('改名', next(x for x in r['threads'] if x['id'] == old_tid)['title'] == 'Founders talk')
client.delete(f'/api/chain/{CID}/ask?thread={old_tid}')
h = client.get(f'/api/chain/{CID}/ask?thread={old_tid}').get_json()
t.check('删一段，别的留着', old_tid not in [x['id'] for x in h['threads']] and new_tid in [x['id'] for x in h['threads']])
bad = client.get(f'/api/chain/{CID}/ask?thread=../../etc').status_code
bad2 = client.post(f'/api/chain/{CID}/ask/threads/..%2Fx', json={'title': 'x'}).status_code
t.check('乱写的 thread 号拒掉', bad == 400 and bad2 in (400, 404), (bad, bad2))

# 6. 模型列表：没 Claude / GPT 时只有 Gemini；装了就列出来，默认跟以前一样
m = client.get(f'/api/chain/{CID}/ask/models').get_json()
t.check('没 Claude 时只列 Gemini、默认 ASK_MODEL', m['default'] == ask.ASK_MODEL
        and all(x['id'].startswith('gemini') for x in m['models']), m)
claude_cli.use_runner(claude_ok)
codex_cli.use_runner(codex_ok)
agy_cli.use_runner(agy_ok)
agy_cli.installed = lambda: True
codex_cli.models_cache_path = lambda: write_json(os.path.join(os.environ['GETAUDIO_DATA_DIR'], 'mc.json'), {'models': [
    {'slug': 'gpt-6.1-sol', 'display_name': 'GPT-6.1-Sol', 'visibility': 'list'},
    {'slug': 'gpt-reserve', 'display_name': 'GPT-Reserve', 'visibility': 'hide'}]}) or os.path.join(os.environ['GETAUDIO_DATA_DIR'], 'mc.json')
os.environ['ASK_ENGINE'] = 'claude'
os.environ['CODEX_ENGINE'] = 'on'
m = client.get(f'/api/chain/{CID}/ask/models').get_json()
mids = [x['id'] for x in m['models']]
t.check('三家订阅都列出来、默认 Opus', m['default'] == claude_cli.MODEL
        and {'claude-sonnet-5-5', 'claude-haiku-5-5', 'codex:gpt-6.1-sol', 'agy:gemini-4-argon-high',
             'agy:gemini-3.8-flash-low', ask.ASK_MODEL} <= set(mids) and 'codex:gpt-reserve' not in mids, mids)
t.check('每个都标了是哪家', {x['group'] for x in m['models']} == {'claude', 'codex', 'agy', 'gemini'}, m['models'])

# 7. 选了哪个就用哪个，回答上记着是谁答的
code, ev = stream('Harness?', thread=new_tid, model='claude-haiku-5-5')
t.check('选 Haiku 就调 Haiku', seen['claude'][-1:] == ['claude-haiku-5-5']
        and ev[-1]['message']['model'] == 'claude-haiku-5-5' and ev[-1]['message']['citations'], ev[-1])
n = len(seen['gemini'])
code, ev = stream('Shipping?', thread=new_tid, model='gemini-3.5-flash')
t.check('选 Gemini 3.5 Flash 就走它', seen['gemini'][n:] == ['gemini-3.5-flash']
        and ev[-1]['message']['model'] == 'gemini-3.5-flash')
code, ev = stream('GPT please', thread=new_tid, model='codex:gpt-6.1-sol')
t.check('选 GPT-6.1 走 codex -m gpt-6.1-sol', seen['codex_models'] == ['gpt-6.1-sol']
        and ev[-1]['message']['model'] == 'codex:gpt-6.1-sol' and 'GPT: ship small' in ev[-1]['message']['content'], ev[-1])
code, ev = stream('Argon please', thread=new_tid, model='agy:gemini-4-argon-high')
t.check('选 Argon 走 agy --model gemini-4-argon-high', seen['agy'] == ['gemini-4-argon-high']
        and ev[-1]['message']['model'] == 'agy:gemini-4-argon-high' and 'Argon: agents' in ev[-1]['message']['content']
        and ev[-1]['message']['citations'], ev[-1])
agy_cli.use_runner(lambda a: {'status': 'ERROR', 'response': 'quota exhausted'})
n = len(seen['gemini'])
code, ev = stream('Argon down', thread=new_tid, model='agy:gemini-3.8-flash-low')
t.check('agy 挂了退回 Gemini API', len(seen['gemini']) == n + 1 and ev[-1]['message']['model'] == ask.ASK_MODEL, ev[-1])
nc = len(seen['claude'])
code, ev = stream('Default model?', thread=new_tid)
t.check('不选 = 默认（ASK_ENGINE=claude → Opus）', seen['claude'][nc:] == [claude_cli.MODEL])
code, _ = stream('x', thread=new_tid, model='gpt-9000')
t.check('不认识的模型 400', code == 400)

# 8. Claude 用不了时选了 Claude：还没出字就退回 Gemini，记的是真答的那个
claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': 'boom', 'events': []})
n = len(seen['gemini'])
code, ev = stream('Fallback?', thread=new_tid, model='claude-sonnet-5-5')
t.check('Claude 挂了退回 Gemini', len(seen['gemini']) == n + 1 and ev[-1]['message']['model'] == ask.ASK_MODEL, ev[-1])

# 9. 存进工作台：按 thread 找那条回答
hist = ask.load_history(CDIR, new_tid)
bot = next(x for x in reversed(hist) if x['role'] == 'assistant' and x.get('content'))
r = client.post(f'/api/chain/{CID}/studio/note', json={'at': bot['at'], 'head': bot['content'][:20], 'thread': new_tid})
t.check('存进工作台认 thread', r.status_code == 200, r.get_json())

t.finish()
