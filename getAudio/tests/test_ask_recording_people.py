"""项目里另外加的录音是别的 UP 主的：问答按人归属，不把几个人揉成一个「他」。
10-14：懒猫徒手健身的频道（项目改名叫 Chinese social analyze）+ 4 个别的 UP 主的视频，回答把努力奋斗小凯的话算成同一个人的。
跑生产代码：ask._add_recording_people、ask._subject、ask._persona_view。"""
import json
import os

from _support import Checks, isolate

TMP = isolate('ask_recording_people')
import ask  # noqa: E402
import config  # noqa: E402

t = Checks()
cdir = os.path.join(config.RESULTS_FOLDER, '_chains', 'c1')
os.makedirs(cdir)
json.dump({'recordings': [{'task_id': 'r1'}, {'task_id': 'r2'}, {'task_id': 'r3'}]}, open(os.path.join(cdir, 'sources.json'), 'w'))
for tid, who in (('r1', '努力奋斗小凯'), ('r2', '非法字符二号机'), ('r3', '懒猫徒手健身')):
    os.makedirs(os.path.join(config.RESULTS_FOLDER, tid))
    json.dump({'creator': who}, open(os.path.join(config.RESULTS_FOLDER, tid, 'meta.json'), 'w'), ensure_ascii=False)
state = {'url': 'https://space.bilibili.com/1', 'author': 'Chinese social analyze', 'channel_name': '懒猫徒手健身'}


def corpus():
    return {'author': 'Chinese social analyze', 'dir': cdir, 'kind': 'creator', 'template': 'creator',
            'episodes': [{'task_id': 'v1', 'kind': 'episode'}, {'task_id': 'r1', 'kind': 'episode'},
                         {'task_id': 'r2', 'kind': 'episode'}, {'task_id': 'r3', 'kind': 'episode'},
                         {'task_id': '', 'kind': 'doc'}], 'cards': []}


c = corpus()
ask._add_recording_people(c, state)
t.check('每期记上是谁的：频道的期用频道真名，加的录音用各自 UP 主',
        [e.get('person') for e in c['episodes']] == ['懒猫徒手健身', '努力奋斗小凯', '非法字符二号机', '懒猫徒手健身', None],
        str([e.get('person') for e in c['episodes']]))
t.check('people：频道主在前、另外两个 UP 主各一个（同名不重复）',
        [p['name'] for p in c['people']] == ['懒猫徒手健身', '努力奋斗小凯', '非法字符二号机'], str(c.get('people')))
t.check('提示词按「几个人的项目」写，点了每个人的名字', 'several creators' in ask._subject(c) and '努力奋斗小凯' in ask._subject(c))
keep, _ = ask._persona_view(c, 'c1')
t.check('用频道主口吻回答：只用他自己的期', keep == {0, 3}, str(keep))

c2 = corpus()
c2['episodes'] = [{'task_id': 'v1', 'kind': 'episode'}, {'task_id': 'r3', 'kind': 'episode'}]
ask._add_recording_people(c2, state)
t.check('加的录音都是频道主自己的：还是一个人，不改提示词', not c2.get('people') and ask._subject(c2).startswith('the creator'))

t.finish()
