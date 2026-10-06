"""别的网页不能让本机的 Verbatim 替用户花钱、也不能读它的数据。
2026-10-06：没设 GETAUDIO_TOKEN（本机默认）时什么都不查。用户在浏览器里打开一个恶意网页，它就能对
http://localhost:5001 发表单 / fetch(no-cors) 的「简单请求」：/upload、/api/transcribe_urls、/api/chain…
照样执行，用用户的 key 花钱（浏览器的同源策略只拦读结果，不拦发请求）。再加上 DNS 重绑定（恶意域名解析到
127.0.0.1），连结果都能读。
改法：改动类请求看 Origin（没有就看 Sec-Fetch-Site），必须是 Verbatim 自己的地址；只监听本机时 Host 必须是
localhost / 127.0.0.1 / ::1。不带这些头的非浏览器客户端（curl、脚本、MCP server）照常放行。
跑生产代码：Flask 应用的请求钩子。桩：没有（被拦下的请求不会走到路由；放行的请求看是不是 403）。"""
import os

from _support import Checks, isolate

isolate('crosssite')
import app as A  # noqa: E402

t = Checks()
c = A.app.test_client()
BASE = 'http://localhost:5001'
spent = []
A.app.view_functions['api_transcribe_urls'] = lambda: (spent.append(1), ('ok', 200))[1]


def post(path='/api/transcribe_urls', **headers):
    spent.clear()
    r = c.post(path, base_url=BASE, headers=headers, data='{"urls": "https://youtu.be/x"}',
               content_type='text/plain')
    return r.status_code


t.check('别的网站发来的请求：拒绝，没执行', post(Origin='https://evil.example') == 403 and not spent)
t.check('本机别的端口（别的应用）发来的：也拒绝', post(Origin='http://localhost:3000') == 403 and not spent)
t.check('只标了跨站（没有 Origin）的：拒绝', post(**{'Sec-Fetch-Site': 'cross-site'}) == 403 and not spent)
t.check('Verbatim 自己的页面发的：照常', post(Origin=BASE) == 200 and spent)
t.check('用 127.0.0.1 打开的页面发的：照常',
        c.post('/api/transcribe_urls', base_url='http://127.0.0.1:5001', headers={'Origin': 'http://127.0.0.1:5001'},
               data='{}', content_type='text/plain').status_code == 200)
t.check('同源（Sec-Fetch-Site: same-origin）：照常', post(**{'Sec-Fetch-Site': 'same-origin'}) == 200)
t.check('curl / 脚本 / MCP（没有这些头）：照常', post() == 200 and spent)
t.check('上传文件（表单）从别的网站发来：拒绝',
        c.post('/upload', base_url=BASE, headers={'Origin': 'https://evil.example'},
               data={}, content_type='multipart/form-data').status_code == 403)

# DNS 重绑定：恶意域名解析到 127.0.0.1，Host 头是它的域名
t.check('只监听本机时：Host 不是本机名的请求（DNS 重绑定）拒绝，读也不行',
        c.get('/api/history', base_url='http://evil.example:5001').status_code == 403)
t.check('Host 是 localhost / 127.0.0.1 / [::1]：照常',
        all(c.get('/api/history', base_url=b).status_code == 200
            for b in ('http://localhost:5001', 'http://127.0.0.1:5001', 'http://[::1]:5001')))
os.environ['HOST'] = '0.0.0.0'
t.check('对外部署（HOST=0.0.0.0）：任何 Host 都照常（局域网 IP、域名）',
        c.get('/api/history', base_url='http://192.168.1.20:5001').status_code == 200)
t.check('对外部署时，别的网站发来的改动请求照样拒绝',
        c.post('/api/transcribe_urls', base_url='http://192.168.1.20:5001', headers={'Origin': 'https://evil.example'},
               data='{}', content_type='text/plain').status_code == 403)
t.check('对外部署时，从这个地址打开的页面发的：照常',
        c.post('/api/transcribe_urls', base_url='http://192.168.1.20:5001', headers={'Origin': 'http://192.168.1.20:5001'},
               data='{}', content_type='text/plain').status_code == 200)
t.finish()
