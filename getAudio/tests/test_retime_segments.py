"""Gemini 3.5 Transcribe 给的段落时间不可信（10-04 查实）：资料库 920 期短文件的「最后一段时间 ÷ 时长」中位数 0.337，
转成 16k 单声道再送一遍还是 0.337；看着正常的几期也是前后乱跳（00:00:09 → 00:16:40 → 00:06:09）。
所以有了本机 Whisper 的逐词时间轴（words.json）之后，拿每段开头的话去时间轴里按顺序找，把段落时间改成真的；
原来模型写的时间留在 model_timestamp 里。找不到的段落夹在前后找到的两段之间，标 approx。"""
import json
import os
import uuid

from _support import Checks, isolate

TMP = isolate('retime_segments')
import wordtime  # noqa: E402

t = Checks()

# 逐词时间轴：三句话分别在 10 秒、95 秒、200 秒（中文按字）
words = []
for start, sent in [(10.0, '大家好欢迎收看今天的节目'), (95.0, '台湾劳动基金九月份亏损了七百亿'), (200.0, '开国大典的时候天安门上的标语')]:
    for k, ch in enumerate(sent):
        words.append((start + 0.25 * k, ch))
segs = [
    {'timestamp': '00:00:03', 'text': '说话人1：大家好，欢迎收看今天的节目。'},
    {'timestamp': '00:00:31', 'text': '说话人2：台湾劳动基金九月份亏损了七百亿新台币。'},
    {'timestamp': '00:00:50', 'text': '说话人2：嗯。'},                       # 太短，找不到 → 夹在中间、标 approx
    {'timestamp': '00:01:06', 'text': '说话人1：开国大典的时候，天安门上的标语。'},
]
out, n = wordtime.retime_segments(segs, words)
t.check('找到 3 段', n == 3, str(n))
t.check('段落时间改成逐词时间轴里的真实时间', [s['timestamp'] for s in out] == ['00:00:10', '00:01:35', out[2]['timestamp'], '00:03:20'],
        json.dumps(out, ensure_ascii=False))
t.check('原来模型写的时间留在 model_timestamp', [s.get('model_timestamp') for s in out] == ['00:00:03', '00:00:31', '00:00:50', '00:01:06'])
t.check('找不到的段落夹在前后两段之间、标 approx', out[2].get('approx') is True and '00:01:35' <= out[2]['timestamp'] <= '00:03:20',
        json.dumps(out[2], ensure_ascii=False))
t.check('正文不动', [s['text'] for s in out] == [s['text'] for s in segs])
t.check('传进来的列表不被改', segs[0]['timestamp'] == '00:00:03' and 'model_timestamp' not in segs[0])

# 按顺序找：同一句话后面又出现一次，前面那段已经用掉了前一次，后面的段落该落在后一次
words2 = [(5.0 + 0.25 * k, ch) for k, ch in enumerate('今天我们来谈一谈利率')] + \
         [(300.0 + 0.25 * k, ch) for k, ch in enumerate('今天我们来谈一谈利率')]
segs2 = [{'timestamp': '00:00:01', 'text': '今天我们来谈一谈利率'}, {'timestamp': '00:00:02', 'text': '今天我们来谈一谈利率'}]
out2, n2 = wordtime.retime_segments(segs2, words2)
t.check('重复的话按顺序各落各的', [s['timestamp'] for s in out2] == ['00:00:05', '00:05:00'], json.dumps(out2, ensure_ascii=False))

# 一段都找不到：原样返回、什么都不加
out3, n3 = wordtime.retime_segments([{'timestamp': '00:00:01', 'text': '完全不相干的一句话内容'}], words)
t.check('一段都找不到就不动', n3 == 0 and out3 == [{'timestamp': '00:00:01', 'text': '完全不相干的一句话内容'}], json.dumps(out3, ensure_ascii=False))

# ---- 落盘：apply_retime 改 transcript.json（只在有 words.json 时），改过的不重复改 ----
tid = str(uuid.uuid4())
d = os.path.join(TMP, 'results', tid)
os.makedirs(d)
json.dump(segs, open(os.path.join(d, 'transcript.json'), 'w'), ensure_ascii=False)
t.check('没有 words.json 时不动', wordtime.apply_retime(tid) == 0)
wordtime.save(tid, words, source='whisper')
t.check('有了就改，返回找到的段数', wordtime.apply_retime(tid) == 3)
saved = json.load(open(os.path.join(d, 'transcript.json')))
t.check('transcript.json 里的时间改好了', saved[0]['timestamp'] == '00:00:10' and saved[0]['model_timestamp'] == '00:00:03')
t.check('再跑一次不会拿已经改过的时间当模型时间', wordtime.apply_retime(tid) == 3
        and json.load(open(os.path.join(d, 'transcript.json')))[0]['model_timestamp'] == '00:00:03')

# ---- 新转写：gemini35 的期 Whisper 补完时间轴就顺手改段落时间；别的引擎不改 ----
import time  # noqa: E402
os.environ['WORDTIME_WHISPER'] = '1'
wordtime.whisper_words = lambda path: words


def new_episode(engine):
    tid = str(uuid.uuid4())
    d = os.path.join(TMP, 'results', tid)
    os.makedirs(d)
    json.dump(segs, open(os.path.join(d, 'transcript.json'), 'w'), ensure_ascii=False)
    json.dump({'id': tid, 'engine': engine}, open(os.path.join(d, 'meta.json'), 'w'))
    audio = os.path.join(TMP, tid + '.m4a')
    open(audio, 'wb').write(b'0' * 1024)
    wordtime.queue_whisper(tid, audio)
    return d


def first_ts(d):
    for _ in range(100):
        if os.path.isfile(os.path.join(d, 'words.json')):
            time.sleep(0.1)
            break
        time.sleep(0.05)
    return json.load(open(os.path.join(d, 'transcript.json')))[0]['timestamp']


d35, dg = new_episode('gemini35'), new_episode('gemini')
t.check('gemini35：补完时间轴就把段落时间改好', first_ts(d35) == '00:00:10')
t.check('别的引擎：段落时间不动', first_ts(dg) == '00:00:03')

t.finish()
