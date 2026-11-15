"""隐私文案要跟默认引擎对得上（GitHub #2）：填了云端 key 后新转写默认走云端（config.default_engine），
页脚和 README 就不能再说「默认什么都不出本机」，要说清默认上传、选本地 Whisper 才不出本机。
跑生产代码：config.default_engine；读 static/i18n.js、templates/index.html、README.md 的原文。"""
import os
import re

from _support import Checks, SRC, isolate

TMP = isolate('privacy_copy')
os.environ.pop('DEFAULT_ENGINE', None)
os.environ['OPENROUTER_API_KEY'] = 'fake-or-key'
import config  # noqa: E402

t = Checks()
t.check('填了 OR key 默认就是云端引擎（前提）', config.default_engine() != 'whisper', config.default_engine())

i18n = open(os.path.join(SRC, 'static', 'i18n.js'), encoding='utf-8').read()
foot = re.findall(r"'footer\.local':\s*'([^']*)'", i18n)
t.check('中英两条页脚都在', len(foot) == 2, foot)
en, zh = (foot + ['', ''])[:2]
t.check('英文页脚不再说 Nothing leaves it', 'Nothing leaves' not in en, en)
t.check('英文页脚说默认云端、Whisper 留本机', 'cloud' in en and 'Whisper' in en and 'default' in en, en)
t.check('中文页脚说默认云端、Whisper 不出本机', '默认' in zh and '云端' in zh and 'Whisper' in zh, zh)

html = open(os.path.join(SRC, 'templates', 'index.html'), encoding='utf-8').read()
m = re.search(r'data-i18n="footer\.local">([^<]*)<', html)
t.check('页面里的兜底英文跟字典一致', m and m.group(1) == en, m and m.group(1))

readme = open(os.path.join(SRC, 'README.md'), encoding='utf-8').read()
t.check('README 不再说 nothing leaves your machine unless…', 'nothing leaves your machine unless' not in readme)

t.finish()
