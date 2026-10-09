"""接口文档（借 Open Notebook 的 OpenAPI）：从 Flask 自己的路由表和每个接口的 docstring 现生成，不另写一份，
所以加了接口、改了说明，文档自动跟着变，不会过时。/api/openapi.json 给工具用，/api/docs 给人看（不连外网）。

跑生产代码：openapi.spec、/api/openapi.json、/api/docs。
"""
import re

from _support import Checks, isolate

TMP = isolate('openapi')
import app as A  # noqa: E402

t = Checks()
c = A.app.test_client()

r = c.get('/api/openapi.json')
t.check('openapi.json 能取', r.status_code == 200)
spec = r.get_json() or {}
t.check('是 OpenAPI 3', str(spec.get('openapi', '')).startswith('3.'), str(spec.get('openapi')))
paths = spec.get('paths') or {}

rules = [x for x in A.app.url_map.iter_rules() if x.endpoint != 'static']
missing = []
for rule in rules:
    oa = re.sub(r'<(?:[^:<>]+:)?([^<>]+)>', r'{\1}', rule.rule)
    for m in (rule.methods or set()) - {'HEAD', 'OPTIONS'}:
        if m.lower() not in (paths.get(oa) or {}):
            missing.append(f'{m} {oa}')
t.check('每个路由、每个方法都在文档里', not missing, ', '.join(missing[:10]))

op = (paths.get('/api/chain/{chain_id}/delete_preview') or {}).get('get') or {}
t.check('路径参数写出来', [x.get('name') for x in op.get('parameters') or []] == ['chain_id'], str(op))
t.check('summary 取 docstring 第一行', '删笔记本' in (op.get('summary') or ''), op.get('summary'))
t.check('按路径分组（tags）', op.get('tags') == ['chain'], str(op.get('tags')))
t.check('写明鉴权方式', 'bearer' in str(spec.get('components', {}).get('securitySchemes', {})).lower())

d = c.get('/api/docs')
t.check('/api/docs 能开', d.status_code == 200 and b'/api/openapi.json' in d.data)
t.check('/api/docs 不连外网（没有外部脚本）', b'<script src="http' not in d.data and b'cdn' not in d.data.lower())

t.finish()
