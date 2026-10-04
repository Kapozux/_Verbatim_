"""阿里云转写顺带存逐词时间轴（ADR 0012）：请求开 enable_words，返回的词片段拼回整词，转写存好时写 words.json。
10-04 实测：Qwen-Audio-3.1 / Fun-ASR 开 enable_words 后跟 YouTube 字幕逐词比，时间差中位数 0.04 秒。
词片段的样子照实测原样：英文新词开头带空格（' bit'），一个词可能拆成几片（'Frank' + 'ly'）；中文一片一两个字、不带空格。"""
import json
import os
import uuid

from _support import Checks, isolate

TMP = isolate('wordtime_qwen')
import app as A  # noqa: E402
import transcribe_dashscope as D  # noqa: E402
import wordtime  # noqa: E402

t = Checks()

# ---- 拼词 ----
en = [{'begin_time': 280, 'text': 'Frank', 'punctuation': ''}, {'begin_time': 480, 'text': 'ly', 'punctuation': ','},
      {'begin_time': 900, 'text': ' some', 'punctuation': ''}, {'begin_time': 1100, 'text': ' of', 'punctuation': ''},
      {'begin_time': 1300, 'text': ' yc', 'punctuation': '.'}, {'begin_time': 1700, 'text': 'So', 'punctuation': ''}]
t.check('英文：开头带空格才是新词，拆开的片段拼回去，标点后断开',
        wordtime.merge_pieces(en) == [(0.28, 'Frankly'), (0.9, 'some'), (1.1, 'of'), (1.3, 'yc'), (1.7, 'So')],
        str(wordtime.merge_pieces(en)))
zh = [{'begin_time': 0, 'text': '大家', 'punctuation': ''}, {'begin_time': 400, 'text': '好', 'punctuation': '，'},
      {'begin_time': 900, 'text': '今天', 'punctuation': ''}]
t.check('中文：每片各算一个，不会整句粘成一个词', wordtime.merge_pieces(zh) == [(0.0, '大家'), (0.4, '好'), (0.9, '今天')])

# ---- 阿里云：请求带 enable_words，解析时把逐词时间交出来 ----
sent = []


class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


def fake_post(url, json=None, **k):
    sent.append(json)
    return Resp({'output': {'task_id': 'x'}})


RESULT = {'transcripts': [{'sentences': [
    {'begin_time': 280, 'text': 'Frankly, some of yc.', 'words': en[:5]},
    {'begin_time': 1700, 'text': 'So', 'words': en[5:]}]}]}
D.http_requests.post = fake_post
D.http_requests.get = lambda url, **k: Resp(RESULT)
D._submit_task('http://f', 'k')
t.check('提交转写时开 enable_words', (sent[-1].get('parameters') or {}).get('enable_words') is True, str(sent[-1]))
out = []
segs = D._parse_result({'output': {'results': [{'subtask_status': 'SUCCEEDED', 'transcription_url': 'http://r'}]}}, words_out=out)
t.check('解析：句子照旧，逐词时间放进 words_out', len(segs) == 2 and out[:2] == [(0.28, 'Frankly'), (0.9, 'some')] and len(out) == 5,
        str(out))

# ---- 端到端：qwenasr 转写存好时写 words.json，片段截取的偏移也加上 ----
def fake_tx(path, progress_callback=None, diarization=False, speaker_count=None, model=None, words_out=None):
    if words_out is not None:
        words_out.extend([(1.5, 'Hello'), (2.0, 'world')])
    return [{'timestamp': '00:00:01', 'text': 'Hello world'}]


D.transcribe_audio = fake_tx
audio = os.path.join(TMP, 'a.mp3')
open(audio, 'wb').write(b'\0' * 2048)
tid = str(uuid.uuid4())
A.run_transcription(tid, audio, 'qwenasr', 'a.mp3', A.queue.Queue() if hasattr(A, 'queue') else __import__('queue').Queue(),
                    summarize=False, offset_sec=600)
p = os.path.join(TMP, 'results', tid, 'words.json')
w = json.load(open(p)) if os.path.isfile(p) else {}
t.check('qwenasr 转写：存了 words.json（来源 qwen，时间加上 10 分钟偏移）',
        w.get('source') == 'qwen' and w.get('words') == [[601.5, 'Hello'], [602.0, 'world']], json.dumps(w)[:200])

t.finish()
