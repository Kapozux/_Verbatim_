"""复习笔记的模板库（借 Open Notebook：transformation 是全局的，写一次、每个笔记本都能用）：
以前模板只存在一个笔记本里，换一门课新建笔记本就得把模板再抄一遍。

跑生产代码：revnotes.library / library_save / library_delete、/api/revnote_library（GET / POST / DELETE）、
/api/chain/<id>/revnotes（GET 里带上模板库）。不调模型。
"""
from _support import Checks, isolate

TMP = isolate('tpl_library')
import app as A  # noqa: E402
import revnotes  # noqa: E402

t = Checks()
c = A.app.test_client()

t.check('一开始模板库是空的', c.get('/api/revnote_library').get_json() == {'templates': []})

r = c.post('/api/revnote_library', json={'name': 'Chinese class', 'template': '## Summary\n## Quotes'})
t.check('存进模板库', r.status_code == 200 and (r.get_json() or {}).get('id'), r.get_data(as_text=True))
tid = (r.get_json() or {}).get('id')
c.post('/api/revnote_library', json={'name': 'Maths class', 'template': '## Formulas\n## Worked examples'})
lib = c.get('/api/revnote_library').get_json()['templates']
t.check('按名字排、两份都在', [x['name'] for x in lib] == ['Chinese class', 'Maths class'], str(lib))

# 同名再存 = 改这一份，不另起一份
r = c.post('/api/revnote_library', json={'name': 'chinese class ', 'template': '## Summary v2'})
lib = c.get('/api/revnote_library').get_json()['templates']
t.check('同名（不分大小写、去空格）= 覆盖', len(lib) == 2 and (r.get_json() or {}).get('id') == tid
        and next(x for x in lib if x['id'] == tid)['template'] == '## Summary v2', str(lib))

# 别的笔记本打开复习笔记弹框时就看得到
pid = c.post('/api/projects', json={'name': 'Another course'}).get_json()['id']
g = c.get(f'/api/chain/{pid}/revnotes?lang=en').get_json()
t.check('笔记本的复习笔记设置里带着模板库', [x['name'] for x in g.get('library') or []] == ['chinese class', 'Maths class'],
        str(g.get('library')))

# 校验
t.check('没名字：拒绝', c.post('/api/revnote_library', json={'name': ' ', 'template': 'x'}).status_code == 400)
t.check('空模板：拒绝', c.post('/api/revnote_library', json={'name': 'n', 'template': ''}).status_code == 400)
t.check('模板太长：拒绝', c.post('/api/revnote_library',
                            json={'name': 'n', 'template': 'x' * (revnotes.TEMPLATE_MAX + 1)}).status_code == 400)

# 删
t.check('删掉一份', c.delete(f'/api/revnote_library/{tid}').status_code == 200
        and [x['name'] for x in c.get('/api/revnote_library').get_json()['templates']] == ['Maths class'])
t.check('删不存在的：404', c.delete('/api/revnote_library/nope').status_code == 404)

# 演示模式只读
A.DEMO_MODE = True
t.check('演示模式不能写', c.post('/api/revnote_library', json={'name': 'x', 'template': 'y'}).status_code in (403, 404)
        and len(c.get('/api/revnote_library').get_json()['templates']) == 1)
A.DEMO_MODE = False

t.finish()
