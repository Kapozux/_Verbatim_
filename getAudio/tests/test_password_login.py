"""密码登录（借 Open Notebook 的 OPEN_NOTEBOOK_PASSWORD）：放到局域网 / 服务器上时，设 VERBATIM_PASSWORD，
浏览器先到登录页，输对密码种一个签名 cookie（30 天）；脚本 / MCP 用 Authorization: Bearer <密码>。
没设密码（默认）时什么都不变；原来的 GETAUDIO_TOKEN 照旧能用。

跑生产代码：app 的 before_request 鉴权、/login（GET 表单、POST 校验）、/logout、连续输错的限流。
"""
import os

from _support import Checks, isolate

TMP = isolate('password')
import app as A  # noqa: E402

t = Checks()
c = A.app.test_client()

# ---- 没设密码：一切照旧 ----
os.environ.pop('VERBATIM_PASSWORD', None)
t.check('没设密码：首页直接开', c.get('/').status_code == 200)
t.check('没设密码：接口直接用', c.get('/api/settings').status_code == 200)

# ---- 设了密码 ----
os.environ['VERBATIM_PASSWORD'] = 'correct horse battery'
c = A.app.test_client()
r = c.get('/')
t.check('没登录：页面跳到登录页', r.status_code == 302 and '/login' in r.headers.get('Location', ''), str(r.status_code))
t.check('没登录：接口 401', c.get('/api/settings').status_code == 401)
t.check('登录页本身能打开', c.get('/login').status_code == 200 and b'password' in c.get('/login').data.lower())
t.check('静态文件不拦（登录页要用样式）', c.get('/static/i18n.js').status_code == 200)

r = c.post('/login', data={'password': 'wrong', 'next': '/'})
t.check('密码错：回登录页、说错了', r.status_code == 401 and c.get('/api/settings').status_code == 401)

r = c.post('/login', data={'password': 'correct horse battery', 'next': '/#/tab/library'})
t.check('密码对：跳回原来要去的地方', r.status_code == 302 and r.headers.get('Location', '').endswith('/#/tab/library'),
        str(r.headers.get('Location')))
cookie = r.headers.get('Set-Cookie', '')
t.check('cookie 是 HttpOnly、不是明文密码', 'HttpOnly' in cookie and 'correct' not in cookie, cookie)
t.check('登录后接口能用', c.get('/api/settings').status_code == 200)

r = c.post('/login', data={'password': 'correct horse battery', 'next': 'https://evil.example/'})
t.check('next 只许站内地址（防开放跳转）', 'evil.example' not in r.headers.get('Location', ''), r.headers.get('Location'))

c.get('/logout')
t.check('退出后接口又 401', c.get('/api/settings').status_code == 401)

# Bearer：给脚本 / MCP 用
c2 = A.app.test_client()
t.check('Bearer 密码能用', c2.get('/api/settings', headers={'Authorization': 'Bearer correct horse battery'}).status_code == 200)
t.check('Bearer 错的不行', c2.get('/api/settings', headers={'Authorization': 'Bearer nope'}).status_code == 401)

# 改了密码：旧 cookie 作废
c3 = A.app.test_client()
c3.post('/login', data={'password': 'correct horse battery'})
os.environ['VERBATIM_PASSWORD'] = 'a new password'
t.check('改密码后旧 cookie 作废', c3.get('/api/settings').status_code == 401)

# 连续输错：限流
c4 = A.app.test_client()
codes = [c4.post('/login', data={'password': f'guess{i}'}).status_code for i in range(7)]
t.check('连续输错 5 次后限流', codes[:5] == [401] * 5 and codes[5] == 429, str(codes))
t.check('限流时密码对也先不放', c4.post('/login', data={'password': 'a new password'}).status_code == 429)
A._login_fails.clear()

os.environ.pop('VERBATIM_PASSWORD', None)
t.check('去掉密码：恢复无感', A.app.test_client().get('/api/settings').status_code == 200)

t.finish()
