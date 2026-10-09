"""笔记本里的轮询跟着视图走（借 Open Notebook 的 use-sources.ts：只在有活的时候轮询、离开就停）：
离开笔记本页后来源栏、工作台、读书笔记、声纹的轮询都要停；标签页在后台时先不发请求，切回前台再接着看。
以前离开笔记本只清了 chainDetailTimer，别的轮询拿着旧 id 一直打接口。

跑生产代码：用 node 执行 static/explore.js 的 cxLater / notebookLeave，以及 app.js 里 applyRoute 那一行的判断
（直接从源码里取出来跑）。浏览器对象用最小的假对象。没装 node 时跳过。
"""
import os
import re
import shutil
import subprocess

from _support import Checks, SRC

t = Checks()
node = shutil.which('node')
if not node:
    t.skip('全部', '没装 node')
    t.finish()

app_js = open(os.path.join(SRC, 'static', 'app.js'), encoding='utf-8').read()
m = re.search(r"^\s*if \(!\(parts\[0\] === 'chain'.*notebookLeave\(\);$", app_js, re.M)
t.check('applyRoute 里离开笔记本时调用 notebookLeave', bool(m))
leave_line = m.group(0).strip() if m else ''

script = r'''
const fs = require('fs'), vm = require('vm');
const listeners = {};
const doc = { hidden: false, addEventListener: (e, f, o) => (listeners[e] = listeners[e] || []).push(f),
              getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
const sb = { document: doc, window: { addEventListener() {} }, localStorage: { getItem: () => null, setItem() {} },
             setTimeout, clearTimeout, console, T: k => k, fetch: () => Promise.reject(new Error('no')) };
vm.createContext(sb);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);
vm.runInContext(`
  var srcState = { timer: null }, stState = { timer: null }, ntState = { timer: null }, vcState = { timer: null };
  this.api = { cxLater, notebookLeave, open: id => { cx.id = id; }, id: () => cx.id,
               set: (k, v) => { ({ srcState, stState, ntState, vcState })[k].timer = v; },
               route: parts => { ` + process.argv[2] + ` } };
`, sb);
const api = sb.api, wait = ms => new Promise(r => setTimeout(r, ms));
const out = {};
(async () => {
  let runs = 0;
  api.open('nb1');
  api.cxLater(() => runs++, 10);
  await wait(30);
  out.runsWhileOpen = runs;

  // 离开笔记本：排着的轮询不再跑，cx.id 清空
  runs = 0;
  api.cxLater(() => runs++, 10);
  let other = 0;
  api.set('srcState', setTimeout(() => other++, 10));
  api.set('stState', setTimeout(() => other++, 10));
  api.route(['tab', 'library']);
  await wait(30);
  out.runsAfterLeave = runs; out.otherAfterLeave = other; out.idAfterLeave = api.id();

  // 换了一个笔记本：上一个笔记本排的轮询不跑
  api.open('nb1'); runs = 0;
  api.cxLater(() => runs++, 10);
  api.open('nb2');
  await wait(30);
  out.runsAfterSwitch = runs;

  // 后台标签页：到点不跑，切回前台才跑
  api.open('nb3'); runs = 0;
  doc.hidden = true;
  api.cxLater(() => runs++, 10);
  await wait(30);
  out.runsWhileHidden = runs;
  doc.hidden = false;
  (listeners.visibilitychange || []).splice(0).forEach(f => f());
  out.runsAfterVisible = runs;

  // 留在笔记本里（同一个 chain 路由）不停
  api.open('nb4'); runs = 0;
  api.cxLater(() => runs++, 10);
  api.route(['chain', 'nb4']);
  await wait(30);
  out.runsStayingOnNotebook = runs;
  console.log(JSON.stringify(out));
})();
'''
r = subprocess.run([node, '-e', script, os.path.join(SRC, 'static', 'explore.js'), leave_line or ';'],
                   capture_output=True, text=True)
if r.returncode != 0 or not r.stdout.strip():
    t.check('node 跑通', False, r.stderr.strip()[-400:])
    t.finish()
import json  # noqa: E402
o = json.loads(r.stdout.strip().splitlines()[-1])
t.check('在笔记本里：轮询照常跑', o['runsWhileOpen'] == 1, str(o))
t.check('离开笔记本：排着的轮询不跑', o['runsAfterLeave'] == 0, str(o))
t.check('离开笔记本：来源栏 / 工作台的定时器也清掉', o['otherAfterLeave'] == 0, str(o))
t.check('离开笔记本：cx.id 清空', o['idAfterLeave'] is None, str(o))
t.check('换笔记本：上一个的轮询不跑', o['runsAfterSwitch'] == 0, str(o))
t.check('后台标签页：到点先不跑', o['runsWhileHidden'] == 0, str(o))
t.check('切回前台：接着跑', o['runsAfterVisible'] == 1, str(o))
t.check('还在这个笔记本：不停', o['runsStayingOnNotebook'] == 1, str(o))
t.finish()
