"""删除前先看影响、删了之后把引用清干净（借 Open Notebook 的 get_delete_preview 和删除级联）。

· 删一条转写：哪些笔记本在用它要先说；删了以后从这些笔记本的来源里拿掉、tasks.db 那一行也删。
  以前笔记本登记表里还挂着它，来源栏把它显示成「转写中」，轮询永远停不下来。
· 删一个笔记本：哪些文档会跟着删（别的笔记本没在用的）、哪些留着、工作台产出几份、
  哪些别的笔记本把它当博主引用着；删了以后别的笔记本里对它的引用也拿掉（字母作废）。

跑生产代码：/api/history/<id>（GET delete_preview、DELETE）、/api/chain/<id>（GET delete_preview、DELETE）、
/api/chain/<id>/sources、登记表 sources.json、taskdb。不调模型。
数据：两条转写、两个笔记本（A 有一条录音和两份文档，B 和 A 共用一份文档并引用博主 C）、一个博主 C。
"""
import os
import uuid

from _support import Checks, isolate, make_creator, make_transcript, write_json

TMP = isolate('delete_preview')
import app as A  # noqa: E402
import sources  # noqa: E402
import taskdb  # noqa: E402

t = Checks()
client = A.app.test_client()
R = A.config.RESULTS_FOLDER

tid = make_transcript(R, [('00:00:01', 'hello')], title='Lecture 1', tid=str(uuid.uuid4()))
tid2 = make_transcript(R, [('00:00:01', 'other')], title='Lecture 2', tid=str(uuid.uuid4()))
taskdb.create(tid, 'Lecture 1', 'gemini35', None, '')
taskdb.set_status(tid, 'done')

c_id = make_creator(A, 'Carol', 'carol', [['one', 'two']])


def notebook(name):
    r = client.post('/api/projects', json={'name': name})
    return (r.get_json() or {}).get('id')


nb_a, nb_b = notebook('Notebook A'), notebook('Notebook B')


def add_doc(doc_id):
    os.makedirs(sources.doc_dir(doc_id), exist_ok=True)
    write_json(os.path.join(sources.doc_dir(doc_id), 'meta.json'), {'id': doc_id, 'status': 'ready', 'name': doc_id})


only_a, shared = 'a' * 32, 'b' * 32
add_doc(only_a)
add_doc(shared)
reg = A._reg(nb_a)
reg['docs'] = [{'doc_id': only_a}, {'doc_id': shared}]
reg['recordings'] = [{'task_id': tid, 'added_at': '2026-10-09'}]
reg['folders'] = [{'name': 'f', 'items': [tid, only_a]}]
A._reg_save(nb_a, reg)
reg = A._reg(nb_b)
reg['docs'] = [{'doc_id': shared}]
reg['channels'] = [{'chain_id': c_id, 'tag': 'B'}]
A._reg_save(nb_b, reg)
os.makedirs(os.path.join(A._chain_dir(nb_a), 'studio'), exist_ok=True)
write_json(os.path.join(A._chain_dir(nb_a), 'studio', 'x1.json'), {'id': 'x1', 'kind': 'report', 'status': 'done'})

# ---- 1 删转写之前：看得到哪些笔记本在用 ----
p = client.get(f'/api/history/{tid}/delete_preview').get_json() or {}
t.check('转写预览：列出在用它的笔记本', [n['name'] for n in p.get('notebooks') or []] == ['Notebook A'], str(p))
p2 = client.get(f'/api/history/{tid2}/delete_preview').get_json() or {}
t.check('没人用的转写：列表是空的', p2.get('notebooks') == [], str(p2))

# ---- 2 删转写：从笔记本里拿掉、tasks.db 那一行删掉，来源栏不再显示「转写中」 ----
r = client.delete(f'/api/history/{tid}')
t.check('删转写成功', r.status_code == 200, r.get_data(as_text=True))
reg = A._reg(nb_a)
t.check('笔记本登记表里拿掉了这条录音', not reg['recordings'], str(reg['recordings']))
t.check('文件夹里也拿掉了', tid not in reg['folders'][0]['items'], str(reg['folders']))
t.check('tasks.db 那一行删了', taskdb.get(tid) is None)
src = client.get(f'/api/chain/{nb_a}/sources').get_json() or {}
t.check('来源栏没有永远「转写中」的条目',
        not any(x.get('status') == 'transcribing' for x in src.get('recordings') or []), str(src.get('recordings')))

# ---- 3 删笔记本之前：会删哪些文档、留哪些、几份产出 ----
p = client.get(f'/api/chain/{nb_a}/delete_preview').get_json() or {}
t.check('笔记本预览：只有 A 在用的文档会删', p.get('docs_deleted') == 1, str(p))
t.check('笔记本预览：共用的文档留着', p.get('docs_kept') == 1, str(p))
t.check('笔记本预览：工作台产出数', p.get('studio_outputs') == 1, str(p))
t.check('笔记本预览：没人引用它', p.get('cited_by') == [], str(p))

pc = client.get(f'/api/chain/{c_id}/delete_preview').get_json() or {}
t.check('博主预览：列出把它当博主引用的笔记本', [n['name'] for n in pc.get('cited_by') or []] == ['Notebook B'], str(pc))

# ---- 4 删笔记本 A：独占文档删掉、共用的留着 ----
client.delete(f'/api/chain/{nb_a}')
t.check('独占文档删了', not os.path.isdir(sources.doc_dir(only_a)))
t.check('共用文档留着', os.path.isdir(sources.doc_dir(shared)))

# ---- 5 删博主 C：B 里对它的引用拿掉、字母作废 ----
client.delete(f'/api/chain/{c_id}')
reg = A._reg(nb_b)
t.check('别的笔记本里对它的引用拿掉了', not reg['channels'], str(reg['channels']))
t.check('字母作废、不再发给别人', 'B' in (reg.get('retired_tags') or []), str(reg.get('retired_tags')))

# ---- 6 不合法的 id ----
t.check('坏 id 400', client.get('/api/history/..%2Fx/delete_preview').status_code in (400, 404)
        and client.get('/api/chain/zzz/delete_preview').status_code in (400, 404))

t.finish()
