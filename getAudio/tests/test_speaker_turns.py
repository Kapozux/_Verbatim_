"""转写稿按说话人拆行（static/app.js 的 splitTurns / textParagraphs）。
OpenRouter / agy 的 Gemini 转写一段约 2.5 分钟，一段里换行接着好几轮「说话人N：」，
详情页原来整段一块显示，几个人的话压在一起（2026-11-14）。拆出来的每轮时间按字数比例估，
必须落在这段起点和下一段起点之间、递增；只有一个人的段原样不动；长段按句切开、拼回去一字不差。

跑生产代码：从 app.js 里把这几个函数原样取出来用 node 跑。没装 node 时跳过。
"""
import json
import os
import shutil
import subprocess

from _support import Checks, SRC

t = Checks()
node = shutil.which('node')
if not node:
    t.skip('全部', '没装 node')
    t.finish()

script = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const grab = name => {
  const i = src.indexOf(name); if (i < 0) throw new Error('missing ' + name);
  let d = 0; for (let k = src.indexOf('{', i); ; k++) { if (src[k] === '{') d++; if (src[k] === '}' && --d === 0) return src.slice(i, k + 1); }
};
const turnRe = src.match(/^const TURN_RE = .*$/m);
eval([grab('function parseTimestampToSeconds'), grab('function secondsToClock'),
      turnRe ? turnRe[0].replace('const', 'var') : '', grab('function splitTurns'), grab('function textParagraphs')].join('\n'));
const sec = parseTimestampToSeconds;
const out = {};
const segs = [
  { timestamp: '00:00:00', text: '说话人1：开头一句话说了挺长的一段内容。\n说话人2：嗯。\n说话人1：接着说。\n这一行没有标签，算上一轮的。' },
  { timestamp: '00:02:30', text: '说话人2：只有一个人。' },
  { timestamp: '00:05:00', text: '说话人1：最后一段第一轮。\n说话人2：最后一段第二轮。' },
];
const rows = splitTurns(segs, 400);
out.n = rows.length;
out.first = rows[0];
out.secs = rows.map(r => sec(r.timestamp));
out.approx = rows.map(r => !!r.approx);
out.orig = rows.map(r => r.orig);
out.cont = rows[2].text;
out.single = rows[3];
out.plain = splitTurns([{ timestamp: '00:00:01', text: 'hello\nworld' }], 10);
const long = '这是一句挺长的话，用来测试切段落。'.repeat(30);
const paras = textParagraphs(long);
out.paraN = paras.length; out.paraJoin = paras.join('') === long; out.paraMin = Math.min(...paras.map(p => p.length));
out.shortPara = textParagraphs('短的。');
console.log(JSON.stringify(out));
'''
r = subprocess.run([node, '-e', script, os.path.join(SRC, 'static', 'app.js')], capture_output=True, text=True, timeout=30)
t.check('node 跑通', r.returncode == 0, r.stderr[-400:])
if r.returncode != 0:
    t.finish()
o = json.loads(r.stdout)

t.check('三段拆成 6 行（第一段 3 轮、第二段 1、第三段 2）', o['n'] == 6, o['n'])
t.check('每轮第一行保留原时间、不标估计', o['first']['timestamp'] == '00:00:00' and not o['first'].get('approx'))
t.check('拆出来的时间递增', o['secs'] == sorted(o['secs']), o['secs'])
t.check('第一段拆出来的时间落在 0 到 150 秒之间', all(0 <= s < 150 for s in o['secs'][:3]), o['secs'])
t.check('最后一段按总时长 400 秒估、不超过', all(300 <= s < 400 for s in o['secs'][4:]), o['secs'])
t.check('后几轮标 approx', o['approx'] == [False, True, True, False, False, True], o['approx'])
t.check('orig 记原段下标', o['orig'] == [0, 0, 0, 1, 2, 2], o['orig'])
t.check('没标签的续行跟着上一轮', o['cont'].startswith('说话人1：接着说。') and '这一行没有标签' in o['cont'], o['cont'])
t.check('只有一个人的段原样不动', o['single']['text'] == '说话人2：只有一个人。' and not o['single'].get('split'), o['single'])
t.check('没有说话人的转写原样不动', o['plain'] == [{'timestamp': '00:00:01', 'text': 'hello\nworld', 'orig': 0}], o['plain'])
t.check('长段按句切成几段', o['paraN'] >= 3, o['paraN'])
t.check('切开拼回去一字不差', o['paraJoin'])
t.check('切出来的段不会太碎', o['paraMin'] >= 60, o['paraMin'])
t.check('短段不切', o['shortPara'] == ['短的。'], o['shortPara'])
t.finish()
