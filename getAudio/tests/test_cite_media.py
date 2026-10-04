"""出处就地看原视频 / 听原音（projects.js）：认得出 YouTube / B 站 / 本机音频，播放器从出处那一秒开始。
前端纯函数用 node 跑（从 static/*.js 里原样抠出来，不复制一份）；后端只查 /api/history 给了前端要的字段。"""
import json
import os
import re
import shutil
import subprocess
import uuid

from _support import Checks, SRC, isolate, make_transcript, write_json

TMP = isolate('cite_media')
import app as A  # noqa: E402

t = Checks()
STATIC = os.environ.get('CITE_STATIC') or os.path.join(SRC, 'static')   # 指到旧版 static 可以确认测试会变红


def grab(fn, name):
    """从 JS 文件里抠出一个顶层函数（到下一个行首的 } 为止）。"""
    src = open(os.path.join(STATIC, fn), encoding='utf-8').read()
    m = re.search(r'^function ' + name + r'\([\s\S]*?^}\n', src, re.M)
    return m.group(0) if m else ''


def node(js):
    p = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    return p.stdout.strip(), p.stderr.strip()


# ---- 认来源、起点、外链 ----
NODE = shutil.which('node')
if not NODE:
    t.skip('前端函数', '本机没有 node')
else:
    fns = [grab('projects.js', n) for n in ('mediaSourceOf', 'mediaClock', 'mediaLink', 'mediaSrc')]
    t.check('projects.js 里有认来源 / 起点 / 外链这几个函数', all(fns))
    cases = {
        'watch': {'source_url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1'},
        'short': {'source_url': 'https://youtu.be/dQw4w9WgXcQ?si=abc'},
        'shorts': {'source_url': 'https://www.youtube.com/shorts/dQw4w9WgXcQ'},
        'vid_only': {'video_id': 'dQw4w9WgXcQ'},
        'bili': {'source_url': 'https://www.bilibili.com/video/BV1xx411c7mD?p=3'},
        'bili_id': {'video_id': 'BV1xx411c7mD'},
        'audio': {'id': 'abc', 'source_url': 'https://example.com/a.mp3', 'has_audio': True},
        'none': {'source_url': 'https://example.com/a.mp3'},
    }
    js = '\n'.join(fns) + f"""
const location = {{ origin: 'http://127.0.0.1:5001' }};
const C = {json.dumps(cases)};
const out = {{}};
for (const k in C) out[k] = mediaSourceOf(C[k]);
const yt = mediaSourceOf(C.watch), bv = mediaSourceOf(C.bili);
out.ytSrc = mediaSrc(yt, 2040); out.ytLink = mediaLink(yt, 2040);
out.bvSrc = mediaSrc(bv, 75); out.bvLink = mediaLink(bv, 75);
out.clock = [mediaClock(5), mediaClock(2040.7), mediaClock(3725)];
console.log(JSON.stringify(out));
"""
    so, se = node(js) if all(fns) else ('', 'missing')
    try:
        r = json.loads(so)
    except ValueError:
        r = {}
    yt = {'kind': 'yt', 'id': 'dQw4w9WgXcQ'}
    t.check('YouTube 各种链接都认得（watch / youtu.be / shorts / 只有 video_id）',
            all(r.get(k) == yt for k in ('watch', 'short', 'shorts', 'vid_only')), se or so[:300])
    t.check('B 站认 BV 号和分 P', r.get('bili') == {'kind': 'bili', 'id': 'BV1xx411c7mD', 'p': 3}
            and r.get('bili_id') == {'kind': 'bili', 'id': 'BV1xx411c7mD', 'p': 1}, so[:300])
    t.check('别的网站：本机有音频就放音频，没有就不放播放器',
            r.get('audio') == {'kind': 'audio', 'src': '/api/history/abc/audio'} and r.get('none') is None, so[:300])
    t.check('YouTube 播放器从出处那一秒开始、能接收跳转命令、不嵌外站的隐私 cookie',
            'youtube-nocookie.com/embed/dQw4w9WgXcQ?start=2040' in r.get('ytSrc', '')
            and 'enablejsapi=1' in r.get('ytSrc', '') and 'origin=http%3A%2F%2F127.0.0.1%3A5001' in r.get('ytSrc', ''))
    t.check('外链带时间：YouTube &t=2040s，B 站 ?p=3&t=75',
            r.get('ytLink') == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=2040s'
            and r.get('bvLink') == 'https://www.bilibili.com/video/BV1xx411c7mD?p=3&t=75'
            and 'bvid=BV1xx411c7mD&p=3&t=75' in r.get('bvSrc', ''))
    t.check('时间写法：0:05 / 34:00 / 1:02:05', r.get('clock') == ['0:05', '34:00', '1:02:05'], str(r.get('clock')))

    # ---- 回答下面的出处卡片：YouTube 先放缩略图 + ▶，点了就地换成播放器 ----
    thumb = grab('projects.js', 'mediaThumb')
    so, se = node(thumb + "\nconsole.log(JSON.stringify([mediaThumb({kind:'yt', id:'dQw4w9WgXcQ'}),"
                  " mediaThumb({kind:'bili', id:'BV1xx411c7mD', p:1}), mediaThumb({kind:'audio', src:'/a'})]))") \
        if thumb else ('', 'missing')
    t.check('出处卡片的缩略图：YouTube 用官方缩略图，B 站 / 音频没有就不放图',
            so == '["https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg","",""]', se or so)
    ex = open(os.path.join(STATIC, 'explore.js'), encoding='utf-8').read()
    item = grab('explore.js', 'sourceItemHtml')
    t.check('出处卡片带 ▶ 播放（data-play），点了只开这一张（其余的停）',
            'data-play=' in item and 'playCiteCard' in ex and 'stopCiteCards' in ex)
    css = open(os.path.join(STATIC, 'style.css'), encoding='utf-8').read()
    t.check('左栏有视频时标题也固定在栏头下面（字幕在下面滚）', '.rd-has-media .src-reader-title' in css
            and 'rd-has-media' in grab('projects.js', 'mountReaderMedia'))

    # ---- 回答里的编号列表：每项之间空一行时，别全显示成 1. ----
    md = [grab('app.js', n) for n in ('escapeHtml', 'renderInline', 'renderMarkdown')]
    so, se = node('\n'.join(md) + "\nconsole.log(renderMarkdown('1. a\\n\\n2. b\\n\\n3. c'));") if all(md) else ('', '')
    t.check('编号列表隔行写也照原编号：第二项是 <ol start="2">', '<ol start="2">' in so and '<ol start="3">' in so
            and so.count('<ol>') == 1, se or so[:300])

# ---- 后端：前端要的 source_url / video_id / has_audio 都在 ----
tid = make_transcript(os.path.join(TMP, 'results'), [('00:00', 'hi'), ('34:00', 'the quote')],
                      url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', tid=str(uuid.uuid4()))
meta_p = os.path.join(TMP, 'results', tid, 'meta.json')
meta = json.load(open(meta_p, encoding='utf-8'))
write_json(meta_p, {**meta, 'video_id': 'dQw4w9WgXcQ'})
d = A.app.test_client().get(f'/api/history/{tid}').get_json()
t.check('/api/history 给了 source_url、video_id、has_audio',
        d.get('source_url', '').endswith('dQw4w9WgXcQ') and d.get('video_id') == 'dQw4w9WgXcQ' and d.get('has_audio') is False,
        json.dumps(d, ensure_ascii=False)[:300])

t.finish()
