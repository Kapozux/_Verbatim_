"""看图也先给本机 Claude（TEXT_ENGINE=claude，2026-10-13 用户：「图片也换吧」）：
视频截帧读图（frames / notes，purpose visual）、小红书笔记读图（xhs）原来只走 Gemini 多模态；开了之后先给 Claude Haiku，
图片和提示词作为一条消息从标准输入进去（--input-format stream-json，不给读文件的工具）。失败照旧 Gemini。
桩：claude_cli 的 runner、Gemini 客户端（假的，碰了就记一笔）。"""
import json
import os

from _support import Checks, isolate

TMP = isolate('claudevision', cheap_route=True)
import analyze  # noqa: E402
import claude_cli  # noqa: E402

img = os.path.join(TMP, 'f.jpg')
with open(img, 'wb') as f:
    f.write(b'\xff\xd8\xff\xe0fakejpeg')
seen = []


def claude_ok(args, stdin):
    seen.append((args, stdin))
    return {'events': [{'type': 'stream_event', 'event': {'type': 'content_block_delta',
                                                          'delta': {'type': 'text_delta', 'text': '{"cards": []}'}}}],
            'result': '{"cards": []}', 'usage': {'input_tokens': 1500, 'output_tokens': 20}}


gemini = {'n': 0}


class FakeGemini:
    class models:
        @staticmethod
        def generate_content(**kw):
            gemini['n'] += 1
            return type('R', (), {'text': 'Gemini 读的', 'usage_metadata': None})()


analyze.make_gemini_client = lambda key: FakeGemini()
analyze.usage.record_gemini = lambda *a, **k: None
claude_cli.use_runner(claude_ok)
t = Checks()

t.check('默认不开：照旧 Gemini', analyze._call_gemini_mm('读图', [img], purpose='visual') == 'Gemini 读的' and not seen)

os.environ['TEXT_ENGINE'] = 'claude'
out = analyze._call_gemini_mm('读这一帧', [img], purpose='visual')
args, stdin = seen[-1]
msg = json.loads(stdin)
t.check('开了：Claude Haiku 读图', out == '{"cards": []}' and args[args.index('--model') + 1] == 'claude-haiku-5-5'
        and gemini['n'] == 1, str(args))
t.check('图片 + 提示词一条消息进去', [b['type'] for b in msg['message']['content']] == ['image', 'text']
        and msg['message']['content'][0]['source']['media_type'] == 'image/jpeg'
        and msg['message']['content'][1]['text'] == '读这一帧' and '--input-format' in args)
t.check('不给工具', args[args.index('--tools') + 1] == '')

claude_cli.use_runner(lambda a, s: {'is_error': True, 'result': 'boom', 'events': []})
t.check('Claude 报错：照旧 Gemini', analyze._call_gemini_mm('读图', [img], purpose='xhs') == 'Gemini 读的' and gemini['n'] == 2)
t.finish()
