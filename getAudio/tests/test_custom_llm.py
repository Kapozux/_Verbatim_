"""自己的模型（借 Open Notebook 的「任意 OpenAI 兼容接口 + Ollama」）：Settings 里填一个 OpenAI 兼容端点
（Ollama 的 http://localhost:11434/v1、LM Studio、OpenAI……）和模型名，就能
  · 在分析大脑里选「自己的模型」——选了就只用它，报错不偷偷换成云端模型（选本机模型多半是为了不出本机）；
  · 打开「日常文字也先用它」后，问答 / 摘要 / 标签这类先发给它，失败再照旧回 Gemini；
  · 本机端点记账记 $0。

跑生产代码：config.resolve_analysis / custom_llm、analyze._llm → _call_openai_compat、llmroute.text / stream、
usage（记账）、/api/settings（GET / POST）、/api/settings/test。
假的模型服务：本机起一个 HTTP 服务（127.0.0.1，测试允许连），实现 /v1/models 和 /v1/chat/completions（含流式），记下收到的请求。
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from _support import Checks, isolate

TMP = isolate('custom_llm')
import analyze  # noqa: E402
import app as A  # noqa: E402
import config  # noqa: E402
import llmroute  # noqa: E402
import usage  # noqa: E402

t = Checks()
seen = []
fail = {'on': False}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json'):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/v1/models':
            self._send(200, json.dumps({'data': [{'id': 'llama3.2'}, {'id': 'qwen3:8b'}]}))
        else:
            self._send(404, '{}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        seen.append({'path': self.path, 'model': body.get('model'), 'stream': body.get('stream'),
                     'auth': self.headers.get('Authorization')})
        if fail['on']:
            self._send(500, '{"error": "model crashed"}')
            return
        if body.get('stream'):
            chunks = [{'choices': [{'delta': {'content': 'Hello '}}]}, {'choices': [{'delta': {'content': 'local'}}]},
                      {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}]
            data = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
            self._send(200, data, 'text/event-stream')
            return
        self._send(200, json.dumps({'choices': [{'message': {'content': 'answer from local'}}],
                                    'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}))


srv = HTTPServer(('127.0.0.1', 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f'http://127.0.0.1:{srv.server_port}/v1'
client = A.app.test_client()

gemini_calls = []
real_gemini = analyze._call_gemini


def fake_gemini(prompt, grounded=False, model=None, purpose='analysis', thinking=None):
    out = llmroute.text(prompt, purpose) if not grounded else None
    if out:
        return out
    gemini_calls.append(purpose)
    return 'answer from gemini'


analyze._call_gemini = fake_gemini

# ---- 1 没配置时：选「自己的模型」照旧落回 Gemini 预设，不报错 ----
t.check('没配置：custom 预设落回 gemini', config.resolve_analysis('custom')[0] == 'gemini')

# ---- 2 Settings 存进去、读回来（key 打码，端点 / 模型名原样） ----
r = client.post('/api/settings', json={'custom_base_url': BASE, 'custom_model': 'llama3.2', 'custom_key': 'sk-local-123'})
t.check('保存设置成功', r.status_code == 200, r.get_data(as_text=True))
s = client.get('/api/settings').get_json()
t.check('读回端点和模型名', s.get('custom_base_url') == BASE and s.get('custom_model') == 'llama3.2', str(s))
t.check('key 打码', (s.get('custom') or {}).get('set') and 'sk-local' not in json.dumps(s), str(s.get('custom')))

# ---- 3 测试连接：列出模型、核对模型名 ----
r = client.post('/api/settings/test', json={'engine': 'custom'}).get_json()
t.check('测试连接：通', r.get('ok') and 'llama3.2' in (r.get('models') or []), str(r))
r = client.post('/api/settings/test', json={'engine': 'custom', 'custom_model': 'nope'}).get_json()
t.check('测试连接：模型名不在列表里会说', not r.get('ok') and 'nope' in r.get('reason', ''), str(r))

# ---- 4 分析大脑选它：走自己的端点、带 key、本机记 $0 ----
prov, ex, syn = config.resolve_analysis('custom')
t.check('custom 预设 → 自己的模型', (prov, ex, syn) == ('custom', 'llama3.2', 'llama3.2'), str((prov, ex, syn)))
seen.clear()
with usage.scope(ref='t-custom'):
    out = analyze._llm('Summarise this', prov, ex, purpose='cards')
t.check('分析走自己的端点', out == 'answer from local' and seen and seen[-1]['model'] == 'llama3.2', str(seen))
t.check('带上 key', seen and seen[-1]['auth'] == 'Bearer sk-local-123', str(seen))
with usage._conn() as c:
    rows = c.execute("SELECT provider, cost_usd FROM calls WHERE ref='t-custom'").fetchall()
t.check('本机端点记一笔 $0、服务商叫 local', [tuple(r) for r in rows] == [('local', 0.0)], str([tuple(r) for r in rows]))

# ---- 5 选了它就不偷偷换云端：报错就是报错 ----
fail['on'] = True
gemini_calls.clear()
try:
    analyze._llm('Summarise this', prov, ex, purpose='cards')
    raised = False
except Exception as e:  # noqa: BLE001
    raised = 'llama3.2' in str(e) or 'model' in str(e).lower()
t.check('自己的模型报错：抛出、说清楚', raised)
t.check('自己的模型报错：不改用 Gemini', not gemini_calls, str(gemini_calls))
fail['on'] = False

# ---- 6 日常文字：默认不走它；打开开关后先走它、失败回 Gemini ----
seen.clear()
gemini_calls.clear()
t.check('开关没开：问答不走自己的模型', analyze._call_gemini('q', purpose='ask') == 'answer from gemini' and not seen)
client.post('/api/settings', json={'custom_everyday': '1'})
seen.clear()
t.check('开关打开：问答先走自己的模型', analyze._call_gemini('q', purpose='ask') == 'answer from local' and seen,
        str(seen))
g = llmroute.stream('q', 'ask')
t.check('开关打开：流式也走它', g is not None and ''.join(g) == 'Hello local', str(seen[-1:]))
fail['on'] = True
gemini_calls.clear()
t.check('自己的模型挂了：日常文字回 Gemini', analyze._call_gemini('q2', purpose='summary') == 'answer from gemini'
        and gemini_calls == ['summary'], str(gemini_calls))
fail['on'] = False
llmroute._cu_state.update(paused_until=0.0)

# ---- 7 清空端点：恢复原样 ----
client.post('/api/settings', json={'custom_base_url': '', 'custom_model': '', 'custom_everyday': ''})
t.check('清空后 custom 预设落回 gemini', config.resolve_analysis('custom')[0] == 'gemini')
seen.clear()
t.check('清空后问答不走它', analyze._call_gemini('q', purpose='ask') == 'answer from gemini' and not seen)

srv.shutdown()
t.finish()
