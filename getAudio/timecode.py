"""时间码：转写里的 'MM:SS' / 'HH:MM:SS' ↔ 秒。原来 app / sanitize / downloader / ask 和三个引擎各写一份，
写法略有不同（坏输入有的给 None、有的给 0、有的抛异常），出过「三位数分钟把整条时间轴压扁」的 bug。

    parse('01:00:05') → 3605        坏的 → None（调用方自己决定兜底成 0 还是跳过）
    hms(3605)        → '01:00:05'  存进转写的统一写法
    clock(125)       → '02:05'     一小时内省掉小时（不会出现 173:12 这种三位分钟）
    shift('00:59', 2)→ '01:01'
    display('00:01:56') → '01:56'  出处上给人看的
"""
import re

_CLOCK = re.compile(r'\d+:\d{2}(?::\d{2})?')


def parse(ts, bare_seconds=False):
    """'MM:SS' / 'HH:MM:SS'（分钟可以超过两位）→ 秒；认不出返回 None。bare_seconds=True 时纯数字也当秒。"""
    parts = str(ts if ts is not None else '').strip().split(':')
    if not parts or not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 1 and not bare_seconds:
        return None
    if len(parts) > 3:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + int(p)
    return sec


def hms(sec, round_=False):
    """秒 → 'HH:MM:SS'（总是带小时）。"""
    v = max(0, int(round(sec)) if round_ else int(sec))
    return f'{v // 3600:02d}:{v % 3600 // 60:02d}:{v % 60:02d}'


def clock(sec):
    """秒 → 'MM:SS'，一小时以上 'HH:MM:SS'。"""
    v = max(0, int(sec))
    h, m, s = v // 3600, v % 3600 // 60, v % 60
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


def shift(ts, offset_sec):
    """整体加偏移秒再写回去；认不出原样返回。"""
    sec = parse(ts)
    return ts if sec is None else clock(sec + offset_sec)


def display(ts):
    """从任意文字里取出第一个时间点，去掉开头的 00:（'00:01:56' → '01:56'）；取不到返回 ''。"""
    m = _CLOCK.search(str(ts or ''))
    return re.sub(r'^0{1,2}:(?=\d{2}:\d{2}$)', '', m.group(0)) if m else ''


def seconds(ts):
    """display 的宽松解析：文字里第一个时间点 → 秒；没有返回 None。"""
    s = display(ts)
    return parse(s) if s else None
