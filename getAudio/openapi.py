"""接口文档（借 Open Notebook 的 OpenAPI）：从 Flask 的路由表和每个接口函数的 docstring 现生成。

不另写一份 schema：Verbatim 的 140 来个接口是一个个加出来的，单独维护的文档迟早跟代码对不上。
这里只写得出路由表里有的东西——路径、方法、路径参数、说明（docstring 第一行当 summary，其余当 description）；
请求体 / 返回体没有声明，文档里也不假装有。/api/openapi.json 给工具用，/api/docs 是不连外网的一页。"""
import inspect
import re

_PARAM = re.compile(r'<(?:([^:<>]+):)?([^<>]+)>')
_TYPES = {'int': 'integer', 'float': 'number'}


def _oa_path(rule):
    return _PARAM.sub(r'{\2}', rule)


def _tag(path):
    parts = [p for p in path.split('/') if p and not p.startswith('{')]
    if parts and parts[0] == 'api' and len(parts) > 1:
        return parts[1]
    return parts[0] if parts else 'root'


def _join(lines):
    """折行接回去：中文之间不加空格，英文之间加一个。"""
    out = ''
    for ln in (x.strip() for x in lines):
        if out and ln and not (_cjk(out[-1]) or _cjk(ln[0])):
            out += ' '
        out += ln
    return out


def _cjk(ch):
    return '\u2e80' <= ch <= '\u9fff' or '\uff00' <= ch <= '\uffef' or '\u3000' <= ch <= '\u303f'


def spec(app, version='1'):
    paths = {}
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.endpoint == 'static':
            continue
        fn = app.view_functions[rule.endpoint]
        doc = inspect.cleandoc(fn.__doc__ or '')
        head, _, rest = doc.partition('\n\n')        # 第一段当 summary（中文 docstring 常常一句折成两行）
        summary = _join(head.splitlines())
        path = _oa_path(rule.rule)
        params = [{'name': name, 'in': 'path', 'required': True,
                   'schema': {'type': _TYPES.get(conv or '', 'string')}}
                  for conv, name in _PARAM.findall(rule.rule)]
        for m in sorted((rule.methods or set()) - {'HEAD', 'OPTIONS'}):
            op = {'operationId': f'{rule.endpoint}_{m.lower()}', 'tags': [_tag(path)],
                  'responses': {'200': {'description': 'OK'}}}
            if summary:
                op['summary'] = summary.strip()
            if rest.strip():
                op['description'] = rest.strip()
            if params:
                op['parameters'] = params
            paths.setdefault(path, {})[m.lower()] = op
    return {
        'openapi': '3.0.3',
        'info': {'title': 'Verbatim', 'version': version,
                 'description': 'Generated from the running app\'s routes and docstrings. Request and response bodies '
                                'are not declared; long jobs (transcription, analysis) return an id to poll.'},
        'paths': paths,
        'components': {'securitySchemes': {
            'bearer': {'type': 'http', 'scheme': 'bearer',
                       'description': 'GETAUDIO_TOKEN or VERBATIM_PASSWORD, when one is set. Not needed otherwise.'}}},
        'security': [{}, {'bearer': []}],
    }


DOCS_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Verbatim API</title>
<style>
:root { --bg:#f6f4ef; --card:#fff; --fg:#1f1d1a; --muted:#6b665e; --line:#ddd7cc; --acc:#c4623e; }
@media (prefers-color-scheme: dark) { :root { --bg:#181715; --card:#22201d; --fg:#ece8e1; --muted:#9a948a; --line:#38342f; --acc:#d9774f; } }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 60px; }
h1 { font-size:22px; margin:0 0 4px; } .muted { color:var(--muted); } a { color:var(--acc); }
input { width:100%; margin:16px 0; padding:9px 12px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); font-size:14px; }
h2 { font-size:15px; margin:22px 0 6px; text-transform:capitalize; }
.op { display:flex; gap:10px; padding:7px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); margin:4px 0; }
.m { flex:0 0 58px; font:600 12px/20px ui-monospace, Menlo, monospace; } .p { font:13px/20px ui-monospace, Menlo, monospace; word-break:break-all; }
.s { color:var(--muted); } .get{color:#2e7d32} .post{color:#c4623e} .delete{color:#b3261e} .put,.patch{color:#7b5ea7}
@media (prefers-color-scheme: dark) { .get{color:#81c784} .delete{color:#f2948a} }
</style></head><body><main>
<h1>Verbatim API</h1>
<p class="muted">Generated from the app's own routes. Machine-readable: <a href="/api/openapi.json">/api/openapi.json</a>.
Request bodies are not declared; read the handler in app.py when you need one.</p>
<input id="q" type="search" placeholder="Filter: path, method or words in the description" autofocus>
<div id="list" class="muted">Loading…</div>
</main><script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
fetch('/api/openapi.json').then(r => r.json()).then(spec => {
  const ops = [];
  for (const [p, ms] of Object.entries(spec.paths)) for (const [m, o] of Object.entries(ms)) ops.push({ p, m, o });
  const draw = q => {
    q = q.toLowerCase();
    const groups = {};
    ops.filter(x => !q || (x.p + ' ' + x.m + ' ' + (x.o.summary || '') + ' ' + (x.o.description || '')).toLowerCase().includes(q))
       .forEach(x => (groups[x.o.tags[0]] = groups[x.o.tags[0]] || []).push(x));
    document.getElementById('list').innerHTML = Object.keys(groups).sort().map(g => `<h2>${esc(g)}</h2>` + groups[g].map(x =>
      `<div class="op" title="${esc(x.o.description || '')}"><span class="m ${x.m}">${x.m.toUpperCase()}</span>`
      + `<span><span class="p">${esc(x.p)}</span><br><span class="s">${esc(x.o.summary || '')}</span></span></div>`).join('')).join('')
      || '<p class="muted">Nothing matches.</p>';
  };
  draw('');
  document.getElementById('q').addEventListener('input', e => draw(e.target.value));
});
</script></body></html>"""
