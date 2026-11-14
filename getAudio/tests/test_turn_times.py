"""多人说话时每轮一行、带这句开口的时间戳（OR / agy 的转写提示词）。
11-14 以前提示词只要「每 30 秒一个时间戳 + 段首标说话人」，Gemini 一行里换行接着好几轮，前端只能按字数估每轮几秒。
户晨风连麦 5 分钟实测：新提示词（带格式示例）3.8 Flash 三次都一轮一行，agy 38 段没有一段混两个人。
这里检查提示词里有这条规矩，以及这种输出解析出来正好一轮一段、时间是模型给的。
"""
import os
import sys

from _support import Checks, SRC

sys.path.insert(0, SRC)
t = Checks()

import transcribe_openrouter as tor
import transcribe_agy as tag
from transcribe_gemini import parse_timestamped_text, shift_timestamps

t.check('OR 提示词：每换一个人另起一行带时间戳', '每换一个人说话就另起一行' in tor.PROMPT and '[HH:MM:SS] 说话人2：' in tor.PROMPT)
t.check('OR 提示词：有多人格式示例', '[00:00:04] 说话人2：嗯。' in tor.PROMPT)
t.check('OR 提示词：每行都写说话人', '每一行都要写说话人' in tor.PROMPT)
t.check('agy 提示词：每换一个人另起一行带时间戳', '每换一个人就另起一行' in tag._AGY_PROMPT and '[03:12] 说话人2：' in tag._AGY_PROMPT)

out = ('[00:00:00] 说话人1：如果你头疼，去正规医院。\n[00:00:13] 说话人2：嗯。\n'
       '[00:00:14] 说话人1：知道吧？\n[00:00:20] 说话人2：那你说的问题是大多数啊。')
segs = parse_timestamped_text(shift_timestamps(out, 300, chunk_seconds=300))
t.check('一轮一段', len(segs) == 4, segs)
t.check('时间是模型给的、加了块偏移', [s['timestamp'] for s in segs] == ['00:05:00', '00:05:13', '00:05:14', '00:05:20'], segs)
t.check('每段只有一个说话人', all(s['text'].count('说话人') == 1 for s in segs), segs)
# 3.8 Flash 偶尔不换行、时间戳全挤在一行里（实测三次一次），也要能拆开
inline = out.replace('\n', ' ')
t.check('时间戳挤在一行里也拆得开', len(parse_timestamped_text(inline)) == 4)
t.finish()
