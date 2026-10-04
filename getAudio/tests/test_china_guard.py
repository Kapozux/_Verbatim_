"""红线：跟中国相关的内容不发国内模型（阿里云百炼 / 官方 DeepSeek / Kimi），改走 Gemini。
2026-10-04 YC 频道用 deepseek 预设抽卡时发现：笔记本选了国内分析预设，抽卡、转写体检、写画像都直接把全部内容发过去，
没经过 llmroute.china_related——一期讲「美国制造业怎么赢中国」的 YC 访谈被体检步骤发给了 DeepSeek。
llmroute 只管摘要 / 问答 / 打标签这些用途；预设走的是 analyze._llm，这里补上同一道检查。
同一批里还修：DeepSeek 偶尔把 layer 写成英文（his claim）、时间戳多套方括号（[[37:07]]）——在代码里整理回标准格式。
跑生产代码：analyze._llm、analyze._extract_cards、analyze.review_transcript。
桩（只在模型边界）：analyze._call_openai_compat、analyze._call_gemini——记下发给了谁，不真调。"""
import json
import os

from _support import Checks, isolate

isolate('chinaguard', cheap_route=True)
os.environ['DEEPSEEK_API_KEY'] = 'test-key'
os.environ['MOONSHOT_API_KEY'] = 'test-key'
import analyze  # noqa: E402

t = Checks()
sent = []


def fake_compat(prompt, model, base, key, extra_payload=None, label='', purpose=''):
    sent.append(('cn', model, purpose))
    return CARDS_JSON


def fake_gemini(prompt, grounded=False, model=None, purpose='analysis', thinking=None):
    sent.append(('gemini', model, purpose))
    return CARDS_JSON


CARDS_JSON = json.dumps({'cards': [
    {'obs': 'He says X.', 'quote': 'we will win', 'timestamp': '[[37:07]]', 'layer': 'his claim', 'topic': 't',
     'stance': 'pro', 'prediction': True},
    {'obs': 'Framing.', 'quote': 'imagine this', 'timestamp': '[00:45:00]', 'layer': 'Transcript evidence', 'topic': 't',
     'stance': 'none', 'prediction': False},
    {'obs': '他说。', 'quote': '我们会赢', 'timestamp': '01:02', 'layer': '他的主张', 'topic': 't',
     'stance': 'pro', 'prediction': False}]})
analyze._call_openai_compat = fake_compat
analyze._call_gemini = fake_gemini

NEUTRAL = '[00:01] The best startups talk to users every week. [00:09] Ship fast and measure retention. ' * 20
CHINA = ('[00:01] How do we compete with China on manufacturing at scale? You don\'t beat China at its own game. '
         '[00:09] So much manufacturing went to China, and that is how we win versus China. ') * 5
POLITICAL = '[00:01] 这期聊聊习近平和中共中央的最新决定。' * 5

for provider in ('aliyun', 'deepseek', 'moonshot'):
    sent.clear()
    analyze._llm('抽卡\n转写：\n' + CHINA, provider, 'deepseek-v4-flash', purpose='cards')
    t.check(f'{provider}：跟中国相关的内容改走 Gemini', sent and sent[-1][0] == 'gemini', f'{sent}')
    sent.clear()
    analyze._llm('抽卡\n转写：\n' + NEUTRAL, provider, 'deepseek-v4-flash', purpose='cards')
    t.check(f'{provider}：无关的内容照常走国内模型', sent and sent[-1][0] == 'cn', f'{sent}')

sent.clear()
analyze._llm('写画像\n' + POLITICAL, 'aliyun', 'deepseek-v4-pro', purpose='synth')
t.check('政治内容写画像：改走 Gemini 的合成档模型', sent and sent[-1][0] == 'gemini'
        and sent[-1][1] == analyze.resolve_analysis('gemini')[2], f'{sent}')
sent.clear()
analyze._llm('抽卡\n转写：\n' + CHINA, 'aliyun', 'deepseek-v4-flash', purpose='cards')
t.check('抽卡改走 Gemini 时用抽取档模型', sent and sent[-1][1] == analyze.resolve_analysis('gemini')[1], f'{sent}')

# 转写体检（review）也在这条路上
sent.clear()
segs = [{'timestamp': f'00:{i:02d}', 'text': x} for i, x in enumerate(CHINA.split('. '))]
try:
    analyze.review_transcript(segs, preset='deepseek')
except Exception:  # noqa: BLE001  桩返回的不是体检要的格式，这里只看发给了谁
    pass
t.check('转写体检：跟中国相关的录音不发 DeepSeek', sent and all(s[0] == 'gemini' for s in sent), f'{sent}')

# 按整期判：60 分钟的节目只有中间那 20 分钟反复提中国（YC 那期的样子），整期都不发国内模型
lines = []
for m in range(60):
    txt = ('How do we compete with China? So much manufacturing went to China.' if 20 <= m < 40
           else 'Talk to users and ship fast.')
    lines.append(f'[{m:02d}:00] {txt}')
sent.clear()
try:
    analyze.analyze_episode('Boom', '\n'.join(lines), 'YC', preset='deepseek')
except Exception:  # noqa: BLE001
    pass
cards_calls = [s for s in sent if s[2] == 'cards']
t.check('整期跟中国相关：每一段抽卡都走 Gemini（不只中间那段）', len(cards_calls) >= 3
        and all(s[0] == 'gemini' for s in cards_calls), f'{cards_calls}')

# 抽卡结果整理：layer 写成英文、时间戳多套括号 → 标准格式
data = analyze._extract_cards('E1', NEUTRAL, 'YC', 'aliyun', 'deepseek-v4-flash')
cards = (data or {}).get('cards') or []
t.check('layer 写成英文的整理回中文取值', [c.get('layer') for c in cards] == ['他的主张', '转写自证', '他的主张'],
        f'{[c.get("layer") for c in cards]}')
t.check('时间戳去掉多余的方括号', [c.get('timestamp') for c in cards] == ['37:07', '00:45:00', '01:02'],
        f'{[c.get("timestamp") for c in cards]}')
t.finish()
