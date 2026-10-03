"""英文内容出英文的摘要、标题、综述；中文内容出中文。
2026-10-03 截 README 图时发现：All-In 四期英文节目，摘要、标题、笔记本综述全是中文。提示词是中文写的，只写了
「跟原文同一种语言」，便宜模型（DeepSeek / flash-lite）照着提示词的语言写了中文。改成先用代码判断原文语言，再在
提示词里明确写「用英文 / 用中文」。
跑生产代码：summarize.summarize_transcript、enrich.enrich_task、analyze.synthesize_collection / synthesize。
桩（只在模型边界）：llmroute.text、analyze._llm——记下发出去的提示词，不真调模型。
这里查的是「提示词里有没有明确的语言要求」；模型照不照做，在真实数据上看过（README 截图那批）。"""
import json
import os

from _support import Checks, isolate, make_transcript, write_json

isolate('outputlang')
import analyze  # noqa: E402
import config  # noqa: E402
import enrich  # noqa: E402
import llmroute  # noqa: E402
import summarize  # noqa: E402
import textlang  # noqa: E402

t = Checks()
EN = ("[00:00:01] So my prediction for 2024 is that the Fed cuts rates at least three times. "
      "[00:00:09] I think Nvidia keeps running, and the IPO window opens up in the second half. ") * 30
ZH = "[00:00:01] 我预测明年美联储至少降息三次，英伟达还会继续涨。[00:00:09] 下半年 IPO 窗口会重新打开。" * 30
ZH_MIXED = "[00:00:01] 这个 AI model 的 performance 很好，但是 inference cost 还是太高了，我们得想办法。" * 30
EN_NAMES = "[00:00:01] We visited 北京 and 上海 last year, and the market there is very different. " * 30

t.check('英文 → en', textlang.detect(EN) == 'en')
t.check('中文 → zh', textlang.detect(ZH) == 'zh')
t.check('中文夹英文术语 → zh', textlang.detect(ZH_MIXED) == 'zh')
t.check('英文里带几个中文地名 → en', textlang.detect(EN_NAMES) == 'en')
t.check('没有文字 → None（照旧让模型跟随原文）', textlang.detect('[00:00:01] 123 ...') is None)

sent = []
llmroute.text = lambda prompt, purpose, **k: sent.append(prompt) or json.dumps(
    {'overview': 'x' * 30, 'sections': [], 'title': 't', 'one_line': 'o', 'tags': ['a'], 'filename_meaningful': True})

summarize.summarize_transcript(EN)
t.check('英文转写的摘要：提示词明确要求英文', 'English' in sent[-1] and '用中文' not in sent[-1], sent[-1][-300:])
summarize.summarize_transcript(ZH)
t.check('中文转写的摘要：提示词明确要求中文', '用中文' in sent[-1] and 'in English' not in sent[-1], sent[-1][-300:])

# 标题标签：按转写原文的语言，不按摘要——以前英文节目已经生成了中文摘要，补标题也要出英文
R = config.RESULTS_FOLDER
tid = make_transcript(R, [('00:00:01', EN[:400]), ('00:00:09', EN[400:800])], title='E160 2024 Predictions')
write_json(os.path.join(R, tid, 'summary.json'), {'overview': '本期节目里几位主持人预测了 2024 年的市场走势、科技公司和美国大选。' * 2,
                                                  'sections': []})
enrich.enrich_task(os.path.join(R, tid))
t.check('英文节目（摘要是中文的）补标题：提示词明确要求英文，标签也给英文例子', 'English' in sent[-1]
        and '"podcast"' in sent[-1], sent[-1][-300:])

# 笔记本综述 / 人物画像：lang='auto' 时按证据卡里的原话判断；用户在笔记本里指定了语言就照指定的
prompts = []
analyze._llm = lambda prompt, *a, **k: prompts.append(prompt) or '# ok\n' + 'x' * 400
analyze._build_digest = lambda episodes, *a, **k: 'digest'
ep_en = {'title': 'E1', 'cards': [{'obs': 'predicts cuts', 'quote': 'the Fed cuts rates at least three times this year',
                                   'timestamp': '00:00:01', 'layer': '他的主张'}] * 5}
ep_zh = {'title': '第一期', 'cards': [{'obs': '预测降息', 'quote': '我觉得美联储今年至少降息三次，英伟达还会涨',
                                       'timestamp': '00:00:01', 'layer': '他的主张'}] * 5}
analyze.synthesize_collection([ep_en, ep_en], 'All-In')
t.check('英文笔记本的综述（auto）：提示词明确要求英文', analyze._LANG_LINES['en'] in prompts[-1])
analyze.synthesize_collection([ep_zh, ep_zh], '某合集')
t.check('中文笔记本的综述（auto）：提示词明确要求中文', analyze._LANG_LINES['zh'] in prompts[-1])
analyze.synthesize_collection([ep_en], 'All-In', lang='zh')
t.check('笔记本指定了中文：英文内容也写中文', analyze._LANG_LINES['zh'] in prompts[-1])
analyze._extract_cards('E160', EN, 'All-In', 'gemini', 'gemini-2.5-flash')
t.check('英文节目抽证据卡：转写后面明确要求英文 obs / topic，layer 原样', prompts[-1].endswith(
    'keep layer values exactly as listed (转写自证 / 他的主张 / 外部核实).'), prompts[-1][-200:])
analyze._extract_cards('第一期', ZH, '某博主', 'gemini', 'gemini-2.5-flash')
t.check('中文节目抽证据卡：明确要求中文', prompts[-1].endswith('layer 只用列出的三个值。'), prompts[-1][-120:])
n0 = len(prompts)
analyze.synthesize([ep_en, ep_en], 'Jim Cramer')
t.check('英文博主的画像（auto）：提示词明确要求英文', any(analyze._LANG_LINES['en'] in p for p in prompts[n0:]))
t.finish()
