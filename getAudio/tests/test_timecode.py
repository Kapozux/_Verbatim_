"""时间码（timecode.py）：解析 / 格式化 / 平移 / 显示。原来 6 个模块各写一份，这里一份。纯函数。"""
from _support import Checks, isolate

isolate('timecode')
import timecode as tc  # noqa: E402

t = Checks()
t.check('parse：MM:SS、HH:MM:SS、H:MM:SS', tc.parse('01:05') == 65 and tc.parse('01:00:05') == 3605
        and tc.parse('1:00:05') == 3605)
t.check('parse：三位数分钟（Whisper 长音频）', tc.parse('173:12') == 173 * 60 + 12)
t.check('parse：坏的返回 None', tc.parse('') is None and tc.parse(None) is None and tc.parse('ab:cd') is None
        and tc.parse('1:2:3:4') is None)
t.check('parse：纯秒数默认不认、bare_seconds=True 才认', tc.parse('75') is None and tc.parse('75', bare_seconds=True) == 75)
t.check('hms：总是 HH:MM:SS，截断小数，负数归零', tc.hms(3725.9) == '01:02:05' and tc.hms(5) == '00:00:05'
        and tc.hms(-3) == '00:00:00')
t.check('hms(round=True)：四舍五入', tc.hms(4.6, round_=True) == '00:00:05')
t.check('clock：一小时内 MM:SS，以上 HH:MM:SS（不出现三位分钟）', tc.clock(125) == '02:05' and tc.clock(3605) == '01:00:05'
        and tc.clock(173 * 60 + 12) == '02:53:12')
t.check('shift：整体平移、取不到原样返回', tc.shift('00:59', 2) == '01:01' and tc.shift('x', 5) == 'x'
        and tc.shift('59:59', 2) == '01:00:01')
t.check('display：去掉开头的 00:、找不到给空', tc.display('00:01:56') == '01:56' and tc.display('01:02:03') == '01:02:03'
        and tc.display('[00:03:10] hi') == '03:10' and tc.display('none') == '')
t.check('seconds：显示用的宽松解析', tc.seconds('00:01:56') == 116 and tc.seconds('at 3:10') == 190 and tc.seconds('x') is None)
t.finish()
