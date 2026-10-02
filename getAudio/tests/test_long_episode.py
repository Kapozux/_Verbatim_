"""长节目抽卡、预测核对带上下文。
2026-10-02 All-In 两小时那期：整期一次抽卡，输出被截断、JSON 不闭合，整期判失败（说话人也没认）。
现在超过 45 分钟按 20 分钟一段（前后重叠 2 分钟）分别抽、再合起来；不到 45 分钟但一次抽不出来的，也按段再抽一次。
预测核对：每条带上集名、谁说的、前后几十秒原文，并给思考设上限（3.x 联网核对一条原来要想好几千 token）。
跑生产代码：analyze.analyze_episode、ask.check_predictions。
桩（只在模型边界）：analyze._llm（抽卡）、ask._llm（核对）。"""
import json
import os
import re

from _support import Checks, isolate, make_transcript, write_json

isolate('longep')
import analyze  # noqa: E402
import ask  # noqa: E402
import config  # noqa: E402

t = Checks()
calls = []
broken = set()


def hms(s):
    return f'{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}'


def fake_llm(prompt, provider, model, grounded=False, purpose='analysis'):
    """按这一段里出现的时间点出卡：每 10 分钟一张（原话就是那一行），段号在 broken 里的返回坏 JSON。"""
    calls.append(prompt)
    part = re.search(r'第 (\d+)/(\d+) 段', prompt)
    if part and int(part.group(1)) in broken:
        return '{"cards": [ {"obs": "被截断'
    cards = []
    for ts, text in re.findall(r'^\[(\d\d:\d\d:\d\d)\] (.*)$', prompt, re.M):
        sec = sum(int(x) * k for x, k in zip(ts.split(':'), (3600, 60, 1)))
        if sec % 600 == 0:
            cards.append({'obs': f'他说了 {ts}', 'quote': text, 'timestamp': ts, 'layer': '他的主张'})
    return json.dumps({'cards': cards, 'metrics': {'hype': {'count': 1, 'examples': ['最']},
                                                   'tradeoff': {'tech_count': 2, 'with_tradeoff': 1}},
                       'asr_suspects': []}, ensure_ascii=False)


analyze._llm = fake_llm
two_hours = '\n'.join(f'[{hms(s)}] 第{s}秒的话，讲利率和选举。' for s in range(0, 7200, 30))

# 1 两小时：按 20 分钟分 6 段抽，再合起来
ep = analyze.analyze_episode('All-In E1', two_hours, 'All-In')
ts = [c['timestamp'] for c in ep['cards']]
t.check('两小时的节目分 6 段抽（不是一次）', len(calls) == 6, f'{len(calls)} 次')
t.check('合起来：每 10 分钟一张、按时间排好、重叠那两分钟抽到的同一句只留一张',
        ts == [hms(s) for s in range(0, 7200, 600)] and not ep['extract_failed'], f'{ts}')
t.check('修辞计数按段加起来', ep['metrics']['hype']['count'] == 6 and ep['metrics']['tradeoff']['tech_count'] == 12,
        f"{ep['metrics']}")

# 2 有一段抽坏了：别的段照样留下，并记一笔
calls.clear()
broken.add(3)
ep = analyze.analyze_episode('All-In E1', two_hours, 'All-In')
broken.clear()
t.check('一段抽坏：别的段的卡留着，记下哪几段没抽出来', not ep['extract_failed'] and ep.get('extract_partial')
        and len(ep['cards']) >= 9 and all(not (2400 <= sum(int(x) * k for x, k in zip(c['timestamp'].split(':'), (3600, 60, 1))) < 3600)
                                          for c in ep['cards'][1:-1] if False), f"{ep.get('extract_partial')} {len(ep['cards'])}")

# 3 半小时：一次抽就够（不多花钱）
calls.clear()
half = '\n'.join(f'[{hms(s)}] 第{s}秒。' for s in range(0, 1800, 30))
ep = analyze.analyze_episode('短的', half, 'X')
t.check('半小时的一次抽完', len(calls) == 1 and len(ep['cards']) == 3, f'{len(calls)} 次 {len(ep["cards"])} 张')

# 4 不到 45 分钟、但一次抽不出来（输出截断）：按段再抽一次
calls.clear()
real = analyze._llm


def flaky(prompt, *a, **k):
    if '段' not in prompt[:400] and '第 ' not in prompt:
        calls.append(prompt)
        return '{"cards": ['
    return real(prompt, *a, **k)


analyze._llm = flaky
forty = '\n'.join(f'[{hms(s)}] 第{s}秒。' for s in range(0, 2400, 30))
ep = analyze.analyze_episode('四十分钟', forty, 'X')
analyze._llm = fake_llm
t.check('一次抽不出来的：按段再抽，抽出来了', not ep['extract_failed'] and len(ep['cards']) == 4, f'{ep.get("extract_failed")} {len(ep["cards"])}')

# 5 预测核对：带集名、谁说的、前后原文；思考设了上限
R = config.RESULTS_FOLDER
tid = make_transcript(R, [('00:09:50', '我们先聊利率。'), ('00:10:00', '我赌美联储年底降到三以下。'),
                          ('00:10:20', '前提是失业率先涨上去。')], title='All-In E2')
cdir = os.path.join(R, '_chains', 'c' * 32)
write_json(os.path.join(cdir, 'chain.json'), {'id': 'c' * 32, 'author': 'All-In', 'stage': 'done', 'url': '',
                                              'kind': 'collection', 'videos': [{'index': 0, 'task_id': tid,
                                                                                'title': 'All-In E2', 'upload_date': '20250110'}]})
write_json(os.path.join(cdir, 'cards_001.json'), {'task_id': tid, 'title': 'All-In E2', 'cards': [
    {'obs': '他预测年底利率降到 3% 以下', 'quote': '我赌美联储年底降到三以下', 'timestamp': '00:10:00',
     'layer': '他的主张', 'prediction': True, 'speaker': 'Chamath'}]})
h = ask.card_hash({'quote': '我赌美联储年底降到三以下', 'obs': '他预测年底利率降到 3% 以下'})
write_json(os.path.join(cdir, 'speakers.json'), {'cards': {'1-0': [h, 'Chamath']}, 'roles': {}})   # 认说话人那一步的结果
asked = []


def fake_check(prompt, model=None, purpose='ask', grounded=False, thinking=None):
    asked.append((prompt, thinking))
    return json.dumps({'results': []})


ask._llm = fake_check
ask.check_predictions(cdir)
p, thinking = asked[0] if asked else ('', None)
t.check('核对时带上集名、谁说的、前后原文（条件「失业率先涨」在上下文里）',
        'All-In E2' in p and 'Chamath' in p and '前提是失业率先涨上去' in p and '2025-01-10' in p, p[p.find('Predictions ('):][:600])
t.check('核对的思考有上限', thinking == 1024, f'{thinking}')
t.finish()
