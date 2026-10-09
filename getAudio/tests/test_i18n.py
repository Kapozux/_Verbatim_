"""界面文案字典（static/i18n.js）的一致性（借 Open Notebook 的 locales/index.test.ts）：
中英两套键一样、占位符 {x} 一样、同一套里没有重复的键（JS 对象里重复的键后写的悄悄覆盖前面的），
以及浏览器禁了存储（隐私模式 / 被拦的站点数据）时，第一个加载的脚本不能在顶层就抛错。

跑生产代码：用 node 执行 static/i18n.js（字典 + 顶层那几行）。没装 node 时跳过。
"""
import json
import os
import re
import shutil
import subprocess

from _support import Checks, SRC

t = Checks()
PATH = os.path.join(SRC, 'static', 'i18n.js')
src = open(PATH, encoding='utf-8').read()

# ---- 重复的键：按源码数，每套字典分开 ----
body = src[src.index('const STRINGS = {'):src.index('\n};\n')]
zh_at = body.index('\n    zh: {')
key_re = re.compile(r'''^\s+(['"])([\w.\-]+)\1:''', re.M)
for name, block in (('en', body[:zh_at]), ('zh', body[zh_at:])):
    seen, dups = set(), []
    for m in key_re.finditer(block):
        (dups.append if m.group(2) in seen else seen.add)(m.group(2))
    t.check(f'{name} 字典没有重复的键', not dups, ', '.join(dups))

node = shutil.which('node')
if not node:
    t.skip('键集合 / 占位符 / 禁存储', '没装 node')
    t.finish()

# ---- 键集合和占位符：交给 node 求值，跟浏览器里看到的一样 ----
dump = r'''
const fs = require('fs');
const s = fs.readFileSync(process.argv[1], 'utf8');
const src = s.slice(s.indexOf('const STRINGS = {'), s.indexOf('\n};\n') + 3);
const S = new Function(src + '\nreturn STRINGS;')();
console.log(JSON.stringify(S));
'''
S = json.loads(subprocess.run([node, '-e', dump, PATH], capture_output=True, text=True, check=True).stdout)
en, zh = S['en'], S['zh']
t.check('中文缺的键', not (set(en) - set(zh)), ', '.join(sorted(set(en) - set(zh))[:20]))
t.check('英文缺的键', not (set(zh) - set(en)), ', '.join(sorted(set(zh) - set(en))[:20]))


def ph(v):
    return sorted(set(re.findall(r'\{(\w+)\}', v)))


bad = [k for k in set(en) & set(zh) if ph(en[k]) != ph(zh[k])]
t.check('中英占位符一致', not bad, '; '.join(f'{k}: {ph(en[k])} vs {ph(zh[k])}' for k in bad[:10]))

# ---- 存储被禁：localStorage 一读就抛错时，整个脚本照样跑完、T() 能用 ----
blocked = r'''
const fs = require('fs');
const code = fs.readFileSync(process.argv[1], 'utf8');
const boom = () => { throw new Error('SecurityError: storage disabled'); };
const sandbox = {
  window: {}, navigator: {language: 'zh-CN'},
  document: {addEventListener() {}, documentElement: {}, querySelectorAll: () => [],
             dispatchEvent() {}},
  CustomEvent: function () {},
};
Object.defineProperty(sandbox, 'localStorage', {get: boom});
const vm = require('vm');
vm.createContext(sandbox);
vm.runInContext(code + '\n;this.__lang = currentLang; this.__t = T("common.cancel"); setLang("en"); this.__after = currentLang;', sandbox);
console.log(JSON.stringify([sandbox.__lang, sandbox.__t, sandbox.__after]));
'''
r = subprocess.run([node, '-e', blocked, PATH], capture_output=True, text=True)
t.check('存储被禁时脚本不在顶层抛错', r.returncode == 0, (r.stderr or '').strip().splitlines()[-1:] and
        (r.stderr or '').strip().splitlines()[-1])
if r.returncode == 0:
    lang, cancel, after = json.loads(r.stdout)
    t.check('存储被禁时按浏览器语言选中文', lang == 'zh' and cancel == zh['common.cancel'], r.stdout)
    t.check('存储被禁时还能切语言', after == 'en', r.stdout)

t.finish()
