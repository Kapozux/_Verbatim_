"""用户自己的 DeepSeek / Kimi 官方 key：分析预设改走官方、省钱路由在阿里云之后接上官方 DeepSeek、
设置页存 key / 测试连接、记账按服务商分开。

跑生产代码：config.resolve_analysis、analyze._llm → _call_openai_compat（真拼请求、真记账）、llmroute.text、
/api/settings（GET / POST）、/api/settings/test。
桩（只在网络边界）：requests.post / requests.get（回假的 OpenAI 兼容响应）。usage.db 在临时目录。
"""
import os

from _support import Checks, isolate

TMP = isolate('compat', cheap_route=True)
import requests  # noqa: E402

import analyze  # noqa: E402
import app as A  # noqa: E402
import config  # noqa: E402
import llmroute  # noqa: E402
import usage  # noqa: E402

t = Checks()
posts = []


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def fake_post(url, headers=None, json=None, timeout=None, **k):
    posts.append({'url': url, 'auth': (headers or {}).get('Authorization'), 'model': (json or {}).get('model'),
                  'payload': json})
    if 'dashscope' in url and fake_post.aliyun_fail:
        return Resp(400, {'error': 'Arrearage - account overdue'})
    return Resp(200, {'choices': [{'message': {'content': 'OK from ' + url.split('/')[2]}}],
                      'usage': {'prompt_tokens': 1000, 'completion_tokens': 100}})


fake_post.aliyun_fail = False
requests.post = fake_post

# ---- 1 分析预设：没 key 走阿里云，有 key 走官方，模型名能改 ----
os.environ.pop('DEEPSEEK_API_KEY', None)
os.environ.pop('MOONSHOT_API_KEY', None)
t.check('没填 key：DeepSeek / Kimi 照旧走阿里云', config.resolve_analysis('deepseek')[0] == 'aliyun'
        and config.resolve_analysis('kimi')[0] == 'aliyun')
os.environ['DEEPSEEK_API_KEY'] = 'sk-ds-test'
os.environ['MOONSHOT_API_KEY'] = 'sk-kimi-test'
t.check('填了 key：走官方，默认模型', config.resolve_analysis('deepseek') == ('deepseek', 'deepseek-v4-flash', 'deepseek-v4-pro')
        and config.resolve_analysis('kimi') == ('moonshot', 'kimi-k2.6', 'kimi-k2.6'))
os.environ['KIMI_MODEL'] = 'kimi-k3'
t.check('模型名能在设置里改', config.resolve_analysis('kimi') == ('moonshot', 'kimi-k3', 'kimi-k3'))
os.environ.pop('KIMI_MODEL')
t.check('别的预设不受影响', config.resolve_analysis('qwen')[0] == 'aliyun' and config.resolve_analysis('gemini')[0] == 'gemini')

# ---- 2 调用：打到官方端点、带用户的 key、记账记在 deepseek / moonshot 名下 ----
posts.clear()
a = analyze._llm('hello', 'deepseek', 'deepseek-v4-flash', purpose='cards')
b = analyze._llm('hello', 'moonshot', 'kimi-k2.6', purpose='cards')
t.check('DeepSeek / Kimi 打官方端点、带自己的 key',
        posts[0]['url'] == 'https://api.deepseek.com/v1/chat/completions' and posts[0]['auth'] == 'Bearer sk-ds-test'
        and posts[1]['url'] == 'https://api.moonshot.cn/v1/chat/completions' and posts[1]['auth'] == 'Bearer sk-kimi-test'
        and a == 'OK from api.deepseek.com' and b == 'OK from api.moonshot.cn')
import sqlite3  # noqa: E402
with sqlite3.connect(usage.DB_PATH) as db:
    provs = {r[0] for r in db.execute('select provider from calls')}
t.check('记账分服务商', {'deepseek', 'moonshot'} <= provs, provs)

# ---- 3 省钱路由：阿里云在前；阿里云欠费 → 接着走官方 DeepSeek；政治内容两条都不走 ----
llmroute._state.update(paused_until=0.0, reason='')
llmroute._ds_state.update(paused_until=0.0, reason='')
posts.clear()
r1 = llmroute.text('Founders should talk to users.', 'summary')
t.check('有阿里云 key：先走阿里云', r1 == 'OK from dashscope.aliyuncs.com' and len(posts) == 1)
fake_post.aliyun_fail = True
posts.clear()
r2 = llmroute.text('Founders should talk to users.', 'summary')
r3 = llmroute.text('Ship fast.', 'summary')
t.check('阿里云欠费：同一次就接到官方 DeepSeek，之后不再先撞阿里云',
        r2 == r3 == 'OK from api.deepseek.com' and [p['url'].split('/')[2] for p in posts]
        == ['dashscope.aliyuncs.com', 'api.deepseek.com', 'api.deepseek.com'] and posts[1]['model'] == 'deepseek-v4-flash'
        and 'enable_thinking' not in posts[1]['payload'])
posts.clear()
t.check('提到中国的内容也照常走（不预判；被审核拦了才回 Gemini）', llmroute.text('今天聊一下中共的人事。', 'summary') == 'OK from api.deepseek.com' and len(posts) == 1)
fake_post.aliyun_fail = False
os.environ['DASHSCOPE_API_KEY'] = ''
llmroute._state.update(paused_until=0.0, reason='')
posts.clear()
t.check('只有 DeepSeek key 也能走', llmroute.text('Ship fast.', 'summary') == 'OK from api.deepseek.com'
        and llmroute.status()['deepseek_direct'])
os.environ.pop('DEEPSEEK_API_KEY')
t.check('两把都没有：不走', llmroute.text('Ship fast.', 'summary') is None and not llmroute.enabled())

# ---- 4 设置页：存 key（不回显全文）、改模型名、测试连接 ----
c = A.app.test_client()
c.post('/api/settings', json={'deepseek_key': 'sk-ds-saved-1234', 'kimi_key': 'sk-kimi-saved-9876',
                              'deepseek_model': 'deepseek-v4-flash', 'kimi_model': ''})
g = c.get('/api/settings').get_json()
t.check('存 key：只回末四位', g['deepseek'] == {'set': True, 'hint': '••••1234'} and g['kimi']['hint'] == '••••9876'
        and 'sk-ds-saved' not in str(g) and g['deepseek_model'] == 'deepseek-v4-flash'
        and os.environ.get('DEEPSEEK_API_KEY') == 'sk-ds-saved-1234')
gets = []


def fake_get(url, headers=None, timeout=None, **k):
    gets.append((url, (headers or {}).get('Authorization')))
    if 'moonshot' in url:
        return Resp(200, {'data': [{'id': 'kimi-k2.6'}, {'id': 'moonshot-v1-8k'}]})
    return Resp(200, {'data': [{'id': 'deepseek-v4-flash'}]})


requests.get = fake_get
r_ok = c.post('/api/settings/test', json={'engine': 'kimi'}).get_json()
r_miss = c.post('/api/settings/test', json={'engine': 'deepseek'}).get_json()
t.check('测试连接：只查模型列表、用存好的 key', r_ok['ok'] and gets[0] == ('https://api.moonshot.cn/v1/models',
                                                          'Bearer sk-kimi-saved-9876'))
t.check('测试连接：模型名不在列表里会说出来', not r_miss['ok'] and 'deepseek-v4-pro' in r_miss['reason']
        and 'deepseek-v4-flash' in r_miss['reason'])
requests.get = lambda *a, **k: Resp(401, {})
t.check('测试连接：key 错', c.post('/api/settings/test', json={'engine': 'deepseek', 'deepseek_key': 'bad'}).get_json()['ok'] is False)

t.finish()
