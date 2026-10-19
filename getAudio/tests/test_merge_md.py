"""勾选几份转写合并成一份 .md（POST /api/transcripts/merge）。10-19 资料库也能勾了（照墨页的选择 + 下载合并 .md），
合并的每份下面加一行出处：转写日期 · 时长 · 博主 · 原链接或文件名。
跑生产代码：接口本身；数据是临时目录里的合成转写。"""
import json
import os
import uuid

from _support import Checks, isolate, make_transcript, write_json

isolate('mergemd')
import app as A  # noqa: E402

t = Checks()
R = A.config.RESULTS_FOLDER
c = A.app.test_client()

a = make_transcript(R, [('00:00:01', '第一句。'), ('00:00:05', '第二句。')], title='宿舍录音', tid=str(uuid.uuid4()))
b = make_transcript(R, [('00:00:01', 'Solo founders are rising.')], title='YC 播客', creator='Y Combinator',
                    url='https://youtu.be/abc', tid=str(uuid.uuid4()))
m = json.load(open(os.path.join(R, a, 'meta.json'), encoding='utf-8'))
m.update(date='2026-10-18 22:53:33', duration_seconds=2416)
write_json(os.path.join(R, a, 'meta.json'), m)
empty = make_transcript(R, [], title='空的', tid=str(uuid.uuid4()))


def merge(ids):
    return c.post('/api/transcripts/merge', json={'task_ids': ids})


r = merge([a, b, a, '../etc', empty]).get_json()
md = r['markdown']
t.check('按勾选顺序，去重', md.index('## 宿舍录音') < md.index('## YC 播客') and md.count('## 宿舍录音') == 1, md)
t.check('正文拼成纯文本', '第一句。 第二句。' in md and 'Solo founders are rising.' in md)
t.check('每份下面一行出处：日期 · 时长 · 文件名', '> 2026-10-18 · 40:16 · 宿舍录音.m4a' in md, md)
t.check('有博主、有链接的写博主和原链接', '· Y Combinator · https://youtu.be/abc' in md, md)
t.check('没有正文的跳过并说明', r['count'] == 2 and r['missing'] == 1 and '1 期没有可用转写' in md)
t.check('文件名带份数', r['filename'] == '合并转写_2期.md')
t.check('一份都没选 400', merge([]).status_code == 400)
t.check('非法 id 全挡掉 400', merge(['../x', 'a/b']).status_code == 400)
t.finish()
