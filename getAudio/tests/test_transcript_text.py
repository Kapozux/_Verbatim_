"""转写文字的时间戳处理：各引擎、字幕、清洗、片段平移这些地方都改用 timecode 之后行为不变。
纯函数，不调模型、不连网（引擎模块只 import，不调它们的转写函数）。"""
import os

from _support import Checks, isolate

TMP = isolate('transcripttext')
import app as A  # noqa: E402
import downloader  # noqa: E402
import sanitize  # noqa: E402
import transcribe_gemini as tg  # noqa: E402
import transcribe_gemini35 as tg35  # noqa: E402
import transcribe_precise as tp  # noqa: E402

t = Checks()

# ---- Gemini 文本输出：[MM:SS] / [HH:MM:SS] 统一成 HH:MM:SS，分块偏移、越界夹住
segs = tg.parse_timestamped_text('[00:05] Hello there.\n[1:02:03] Later on.')
t.check('gemini 文本：时间戳统一成 HH:MM:SS', [s['timestamp'] for s in segs] == ['00:00:05', '01:02:03'], str(segs))
t.check('gemini 文本：分块偏移', tg.shift_timestamps('[00:10] a [01:00] b', 600) == '[00:10:10] a [00:11:00] b')
t.check('gemini 文本：块内越界夹到块长', tg.shift_timestamps('[09:00] a', 600, chunk_seconds=300) == '[00:15:00] a')

# ---- Gemini 3.5 的 word_info 分段：偏移加上块起点
data = {'steps': [{'content': [{'text': 'Hi there. New point', 'annotations': [
    {'type': 'word_info', 'speaker': '1', 'start_offset': '1.0s', 'end_offset': '1.4s', 'start_index': 0, 'end_index': 9},
    {'type': 'word_info', 'speaker': '2', 'start_offset': '5.0s', 'end_offset': '5.8s', 'start_index': 10, 'end_index': 19}]}]}]}
segs35 = tg35._parse_response(data, 3600)
t.check('gemini35：按说话人分段、加块起点', [s['timestamp'] for s in segs35] == ['01:00:01', '01:00:05'], str(segs35))
t.check('gemini35：没有标注时整段兜底', tg35._parse_response(
    {'steps': [{'content': [{'text': 'only text'}]}]}, 65) == [{'timestamp': '00:01:05', 'text': 'only text'}])

# ---- precise：合并窗口的时间戳校验
t.check('precise：窗口内的时间戳算合格', tp._window_ts_ok('[10:05] a [12:00] b', 1) is True)
t.check('precise：时间轴被重置到 0 的窗口不合格', tp._window_ts_ok('[00:05] a [00:30] b', 1) is False)

# ---- 清洗：越界时间戳按左右锚点插值修回，合法的不碰
fixed, n = sanitize.repair_timeline([{'timestamp': '00:00:10', 'text': 'a'}, {'timestamp': '21:00:00', 'text': 'b'},
                                     {'timestamp': '00:00:30', 'text': 'c'}], duration=60)
t.check('清洗：越界的修回、合法的不动', n == 1 and fixed[0]['timestamp'] == '00:00:10'
        and fixed[2]['timestamp'] == '00:00:30' and '00:00:10' < fixed[1]['timestamp'] < '00:00:30', str(fixed))

# ---- 字幕：SRT 解析、够不够格、合并成句
srt = os.path.join(TMP, 'a.srt')
with open(srt, 'w') as f:
    f.write('1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n2\n01:02:03,500 --> 01:02:05,000\nLate line\n')
cues = downloader.parse_srt(srt)
t.check('字幕：SRT 时间 → 时间戳（一小时以上带小时）', [(c['timestamp'], c['end']) for c in cues]
        == [('00:01', '00:03'), ('01:02:03', '01:02:05')], str(cues))
ok_long, _ = downloader.subtitle_usable([{'timestamp': '00:00', 'end': '09:00', 'text': '字' * 500}], 600)
bad_short, why = downloader.subtitle_usable([{'timestamp': '00:00', 'end': '01:00', 'text': '字' * 500}], 600)
t.check('字幕：覆盖率够才算能用', ok_long is True and bad_short is False and '10%' in why, why)
merged = downloader.merge_caption_cues([{'timestamp': '00:01', 'end': '00:02', 'text': 'Hello'},
                                        {'timestamp': '00:02', 'end': '00:04', 'text': 'world.'},
                                        {'timestamp': '00:05', 'end': '00:06', 'text': 'Next'}])
t.check('字幕：cue 合并成句、时间取第一条', [m['timestamp'] for m in merged] == ['00:01', '00:05'], str(merged))

# ---- 片段转写：时间戳整体平移；链接后缀的时间段（含纯秒数）
segs = A._offset_segments([{'timestamp': '00:30', 'end': '00:59'}, {'timestamp': 'x'}], 600)
t.check('片段：时间戳加偏移、认不出的原样', segs == [{'timestamp': '10:30', 'end': '10:59'}, {'timestamp': 'x'}], str(segs))
t.check('链接后缀：MM:SS 范围', A._parse_url_section('https://a.b/v @10:00-25:00') == ('https://a.b/v', '*600-1500', 600))
t.check('链接后缀：没有结束时间到片尾', A._parse_url_section('https://a.b/v @1:00:00-') == ('https://a.b/v', '*3600-inf', 3600))
t.check('链接后缀：纯秒数（文档里说支持）', A._parse_url_section('https://a.b/v @75-120') == ('https://a.b/v', '*75-120', 75))
t.check('链接后缀：结束不晚于开始就忽略', A._parse_url_section('https://a.b/v @5:00-1:00') == ('https://a.b/v', None, 0))
t.finish()
