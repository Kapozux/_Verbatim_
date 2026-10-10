"""分析预设 deepseek_guard：抽卡走 DeepSeek，时政词多的那几期改走 Gemini（2026-10-10 用户给立党 520 期选的）。
立党大多在骂 CS 专业和就业，几十期讲土地财政、大下岗、中国经济——这些期百炼可能拦、也可能悄悄删减，所以按关键词先挑出来。
词表用 llmroute 的政治词，剔掉博主自己的名字和职业话题里常见的「体制内」「人大」（人民大学）。
普通 deepseek 预设照旧不预判（10-04 用户的决定）。纯函数，不调模型。"""
import os

from _support import Checks, isolate

isolate('route_guard')
import analyze  # noqa: E402
import config  # noqa: E402

os.environ.pop('DEEPSEEK_API_KEY', None)
t = Checks()
CS = '立党说计算机专业是唯一的出路，体制内考公是消耗，人大的学生也该转码。' * 5
POL = '这一期讲土地财政，习近平和李克强时期的国务院政策怎么变的。' * 2
g = config.resolve_analysis('gemini')

t.check('deepseek_guard 跟 deepseek 一样解析（没官方 key 走百炼）',
        config.resolve_analysis('deepseek_guard') == config.resolve_analysis('deepseek'))
os.environ['DEEPSEEK_API_KEY'] = 'sk-test'
t.check('填了官方 key 也一样走官方 DeepSeek', config.resolve_analysis('deepseek_guard')[0] == 'deepseek')
os.environ.pop('DEEPSEEK_API_KEY')
p, m, _ = config.resolve_analysis('deepseek_guard')
t.check('骂 CS 的期（带博主名、体制内、人大）留在 DeepSeek',
        analyze._route_episode(p, m, CS, 'deepseek_guard') == (p, m))
t.check('时政词 ≥2 次的期改走 Gemini 抽取档', analyze._route_episode(p, m, POL, 'deepseek_guard') == ('gemini', g[1]))
t.check('只出现 1 次不改', analyze._route_episode(p, m, CS + '顺便提一句新疆。', 'deepseek_guard') == (p, m))
t.check('普通 deepseek 预设不预判', analyze._route_episode(p, m, POL, 'deepseek') == (p, m))
t.check('Gemini 预设原样', analyze._route_episode(*g[:2], POL, 'gemini') == g[:2])
t.finish()
