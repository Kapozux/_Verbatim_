"""国内模型先发、报错再回 Gemini（10-04 用户：「直接丢给 DeepSeek，如果它报错了再 fallback，不要预先判断」）。
以前按词表预判「跟中国相关」就整期改走 Gemini：词表里有「央行 / 政府 / 国家」这类泛词，财经节目（小Lin说讲日本央行）
几乎期期命中，10 期里大半被送去 Gemini，每期贵十倍。现在：笔记本选了国内分析预设（百炼 / 官方 DeepSeek / Kimi），
抽卡、转写体检、写画像一律先发国内模型；国内模型报错（审核拦截、超时、接口错）就这一次改用 Gemini 同档模型。
同一批里还修：DeepSeek 偶尔把 layer 写成英文（his claim）、时间戳多套方括号（[[37:07]]）——在代码里整理回标准格式。
跑生产代码：analyze._llm、analyze._extract_cards、analyze.review_transcript、analyze.analyze_episode。
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
fail = {'cn': None}


def fake_compat(prompt, model, base, key, extra_payload=None, label='', purpose=''):
    sent.append(('cn', model, purpose))
    if fail['cn']:
        raise RuntimeError(fail['cn'])
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
CHINA = ('[00:01] 日本央行和日本政府当时的应对，跟 07 年中国股市泡沫、国家队救市比一比。') * 6
POLITICAL = '[00:01] 这期聊聊习近平和中共中央的最新决定。' * 5

for provider in ('aliyun', 'deepseek', 'moonshot'):
    fail['cn'] = None
    sent.clear()
    analyze._llm('抽卡\n转写：\n' + CHINA, provider, 'deepseek-v4-flash', purpose='cards')
    t.check(f'{provider}：提到中国的内容也先发国内模型（不预判）', sent == [('cn', 'deepseek-v4-flash', 'cards')], f'{sent}')
    fail['cn'] = '400: data_inspection_failed — Input data may contain inappropriate content.'
    sent.clear()
    out = analyze._llm('抽卡\n转写：\n' + POLITICAL, provider, 'deepseek-v4-flash', purpose='cards')
    t.check(f'{provider}：国内模型审核拦截 → 这一次改用 Gemini 抽取档', [s[0] for s in sent] == ['cn', 'gemini']
            and sent[-1][1] == analyze.resolve_analysis('gemini')[1] and out == CARDS_JSON, f'{sent}')

fail['cn'] = 'Read timed out'
sent.clear()
analyze._llm('写画像\n' + NEUTRAL, 'aliyun', 'deepseek-v4-pro', purpose='synth')
t.check('写画像时国内模型超时 → Gemini 合成档', [s[0] for s in sent] == ['cn', 'gemini']
        and sent[-1][1] == analyze.resolve_analysis('gemini')[2], f'{sent}')

# 转写体检（review）也在这条路上：先发国内模型
fail['cn'] = None
sent.clear()
segs = [{'timestamp': f'00:{i:02d}', 'text': x} for i, x in enumerate(CHINA.split('。'))]
try:
    analyze.review_transcript(segs, preset='deepseek')
except Exception:  # noqa: BLE001  桩返回的不是体检要的格式，这里只看发给了谁
    pass
t.check('转写体检：提到中国的录音也先发国内模型', sent and all(s[0] == 'cn' for s in sent), f'{sent}')

# 整期也不再预判：每一段抽卡都先发国内模型
lines = [f'[{m:02d}:00] ' + ('How do we compete with China? So much manufacturing went to China.' if 20 <= m < 40
                             else 'Talk to users and ship fast.') for m in range(60)]
sent.clear()
try:
    analyze.analyze_episode('Boom', '\n'.join(lines), 'YC', preset='deepseek')
except Exception:  # noqa: BLE001
    pass
cards_calls = [s for s in sent if s[2] == 'cards']
t.check('整期讲中国的节目：抽卡也先发国内模型（不整期改走 Gemini）', len(cards_calls) >= 3 and all(s[0] == 'cn' for s in cards_calls),
        f'{cards_calls}')

# 抽卡结果整理：layer 写成英文、时间戳多套括号 → 标准格式
data = analyze._extract_cards('E1', NEUTRAL, 'YC', 'aliyun', 'deepseek-v4-flash')
cards = (data or {}).get('cards') or []
t.check('layer 写成英文的整理回中文取值', [c.get('layer') for c in cards] == ['他的主张', '转写自证', '他的主张'],
        f'{[c.get("layer") for c in cards]}')
t.check('时间戳去掉多余的方括号', [c.get('timestamp') for c in cards] == ['37:07', '00:45:00', '01:02'],
        f'{[c.get("timestamp") for c in cards]}')
t.finish()
