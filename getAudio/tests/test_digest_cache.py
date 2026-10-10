"""画像 / 镜头的卡片压缩（_build_digest）存盘复用：同一批卡片只压一次。
10-11 用户：立党 528 期点「锐评」「金句」好慢——每个镜头都把 528 期重新分 22 批压一遍（5–10 分钟），
写画像时压过的也没留下。现在按「作者 + 模型 + 预算 + 每期卡片」算指纹存盘；卡片变了才重压；有一批失败就不存。
桩：analyze._llm（数 brief 调用次数）。"""
from _support import Checks, isolate

isolate('digest_cache')
import analyze  # noqa: E402

t = Checks()
calls = {'brief': 0}
fail = {'on': False}


def fake_llm(prompt, provider, model, grounded=False, purpose='analysis'):
    if purpose == 'brief':
        calls['brief'] += 1
        if fail['on']:
            raise RuntimeError('boom')
        return f'简报 {calls["brief"]}'
    return 'x'


analyze._llm = fake_llm
eps = [{'title': f'第{i}期', 'cards': [{'quote': f'原话{i}', 'claim': f'主张{i}'}]} for i in range(30)]

d1 = analyze._build_digest(eps, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
n1 = calls['brief']
d2 = analyze._build_digest(eps, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
t.check('多批的第一次照常压（30 期 → 2 批）', n1 == 2 and '简报' in d1, str(n1))
t.check('同一批卡片第二次直接复用，不再调模型', calls['brief'] == n1 and d2 == d1, str(calls))

eps[3]['cards'].append({'quote': '新原话', 'claim': '新主张'})
analyze._build_digest(eps, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
t.check('卡片变了就重压', calls['brief'] == n1 + 2, str(calls))

before = calls['brief']
analyze._build_digest(eps, '立党', 'gemini', 'gemini-2.5-flash')
t.check('换了模型也重压', calls['brief'] == before + 2, str(calls))

fail['on'] = True
eps[5]['cards'].append({'quote': '再一句', 'claim': '再一个'})
analyze._build_digest(eps, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
fail['on'] = False
before = calls['brief']
analyze._build_digest(eps, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
t.check('有批次失败的结果不存，下次重压', calls['brief'] == before + 2, str(calls))

small = eps[:5]
before = calls['brief']
analyze._build_digest(small, '立党', 'openrouter', 'deepseek/deepseek-v4-flash')
t.check('期数少（不分批）照旧直接进卡片、不调模型', calls['brief'] == before)
t.finish()
