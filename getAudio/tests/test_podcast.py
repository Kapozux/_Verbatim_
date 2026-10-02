"""音频播客（podcast.py + /studio/podcast、/studio/<id>/audio 接口）。

跑生产代码：clip_candidates / check / clean_text / clip_window / align_quote / snap_to_pauses / fetch_clip（真 ffmpeg 从本机音频裁）
/ synthesize（真 ffmpeg 拼接）/ podcast.start（后台线程照常起）/ usage 记账 / 接口。
桩（只在边界）：ask._llm（按提示词里真实出现的段落和候选原声造脚本，外加编造的出处、不认识的说话人、不在候选里的原声）、
podcast._post（语音合成：按句数回一段 PCM）、podcast._words（本机 Whisper：不加载模型，回 None 走停顿下刀）、
downloader._resolve_ytdlp（换成 /usr/bin/false：没本机音频的那期下不到，测「主持人念原话」）。
数据：两期合成转写；第一期在 results/<tid>/audio.wav 放一段 ffmpeg 现做的音频（有声、停顿交替）。
"""
import base64
import json
import os
import re
import subprocess
import time
import wave

from _support import Checks, isolate, make_creator

TMP = isolate('podcast')
import app as A  # noqa: E402
import ask  # noqa: E402
import downloader  # noqa: E402
import podcast  # noqa: E402
import usage  # noqa: E402

ok = {}
SHORT = ['Small teams ship faster than big ones.', 'Talk to users every single week.',
         'Hiring too early kills most startups.']
LONG = 'This is a very long quote that goes on and on about many different things ' * 4
CID = make_creator(A, 'Host', 'host', [SHORT + [LONG], SHORT[:2]], transcripts=True)
cdir = A._chain_dir(CID)
corpus = ask.load(cdir, passages=True)
TID0 = corpus['episodes'][0]['task_id']

# 第一期的本机音频：0–70 秒，每 10 秒里前 7 秒有声、后 3 秒静音（卡片时间点 00:0j:10 都落在有声段开头）
AUD = os.path.join(A.config.RESULTS_FOLDER, TID0, 'audio.wav')
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=frequency=300:sample_rate=24000:duration=400',
                '-af', "volume='if(lt(mod(t,10),7),1,0)':eval=frame", '-ac', '1', AUD], check=True)

# 1 原声候选：证据卡、有时间点、够短、本机有音频或有链接；长原话不算
cands = podcast.clip_candidates(corpus, corpus['cards'])
cand_q = [c['quote'] for c in cands]
ok['candidates'] = (LONG.strip() not in [q.strip() for q in cand_q] and SHORT[0] in cand_q
                    and all(c.get('layer') != 'source' for c in cands))

# 2 语气标记白名单
ok['clean'] = podcast.clean_text('Hi [#1-2] <laugh> there <evil>x</evil> **bold** <short pause>') \
    == 'Hi <laugh> there x bold <short pause>'

# 3 原话跟识别出的词对齐（识别文字跟原话不全一样）
words = [('um', 0.0, 0.4), ('Small', 0.5, 0.8), ('teams', 0.8, 1.1), ('ship', 1.1, 1.3), ('faster', 1.3, 1.7),
         ('than', 1.7, 1.9), ('big', 1.9, 2.1), ('ones', 2.1, 2.5), ('next', 2.8, 3.0)]
span = podcast.align_quote(words, SHORT[0])
ok['align'] = span == (0.5, 2.5) and podcast.align_quote(words, 'completely unrelated words here') is None

# ---------- 模型桩 ----------
prompts = []


def fake_llm(prompt, model=None, purpose='ask', grounded=False):
    prompts.append((purpose, prompt))
    if 'you are the editor of a podcast' in prompt:
        draft = json.loads(prompt.split('Draft:\n', 1)[1].split('\n\n1. Check it', 1)[0])
        return json.dumps(draft)
    ids = re.findall(r'^\[#([dt]?\d+-\d+)\] \|', prompt, re.M)
    cand_ids = re.findall(r'^(\d+-\d+) \| ', prompt.split('Choose ONLY from these candidates', 1)[-1], re.M)
    long_id = next(c['id'] for c in corpus['cards'] if c['quote'].strip() == LONG.strip())
    ep0 = [c for c in cand_ids if c.startswith('0-')]
    ep1 = [c for c in cand_ids if c.startswith('1-')]
    lines = [
        {'speaker': 'Alex', 'text': 'So <laugh> small teams win? [#' + ids[0] + ']', 'style': 'curious', 'cite': [ids[0], '9-99']},
        {'speaker': 'Sam', 'text': 'Listen to how they put it.', 'style': 'warm', 'cite': []},
        {'clip': ep0[0]},
        {'speaker': 'Sam', 'text': 'That says it all. <evil>', 'style': 'amused', 'cite': [ids[1]]},
        {'speaker': 'Narrator', 'text': 'I should not be here.', 'cite': []},
        {'clip': long_id},                     # 不在候选里：丢掉
        {'clip': ep0[0]},                      # 重复的原声：丢掉
        {'speaker': 'Alex', 'text': 'And here is the second one.', 'style': 'calm', 'cite': [ids[-1]]},
        {'clip': ep1[0]},                      # 那期没本机音频、下载失败：主持人念原话
        {'speaker': 'Sam', 'text': 'Right.', 'style': '', 'cite': []},
    ] + [{'speaker': 'Alex' if k % 2 else 'Sam', 'text': f'Filler line {k}.', 'style': 'easy', 'cite': [ids[0]]}
         for k in range(12)]
    return json.dumps({'title': 'Small teams', 'plan': ['hook', 'point'], 'lines': lines})


tts_calls = []


def fake_post(body, model):
    parts = body['contents'][0]['parts']
    sc = body['generationConfig']['speechConfig']
    voices = {v['speaker']: v['voiceConfig']['prebuiltVoiceConfig']['voiceName']
              for v in sc.get('multiSpeakerVoiceConfig', {}).get('speakerVoiceConfigs', [])} \
        or {'_single': sc['voiceConfig']['prebuiltVoiceConfig']['voiceName']}
    tts_calls.append({'n': len(parts), 'speakers': sorted({p['speechMetadata']['speaker'] for p in parts}),
                      'voices': voices, 'texts': [p['text'] for p in parts]})
    pcm = b'\x10\x00' * int(24000 * 0.5 * len(parts))          # 每句 0.5 秒
    return {'candidates': [{'content': {'parts': [{'inlineData': {'mimeType': 'audio/L16;rate=24000',
                                                                   'data': base64.b64encode(pcm).decode()}}]}}],
            'usageMetadata': {'promptTokenCount': 100, 'candidatesTokenCount': 12 * len(parts)}}


ask._llm = fake_llm
podcast._post = fake_post
podcast._words = lambda path, zh: None
downloader._resolve_ytdlp = lambda: '/usr/bin/false'

# 4 直接跑一遍（同步）
o = podcast.start(cdir, CID, 'deep_dive', 'short', run=lambda f: f())
r = o.get('result') or {}
lines = r.get('lines') or []
clips = [ln for ln in lines if 'clip' in ln]
talk = [ln for ln in lines if 'speaker' in ln]
mp3 = podcast.audio_path(cdir, o['id'])
dur = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', mp3],
                           capture_output=True, text=True).stdout or 0) if os.path.exists(mp3) else 0
ts = [ln['t'] for ln in lines]
print('4 生成：', o['status'], o.get('error'), '| 行', len(lines), '| 原声', [(c['clip'], c.get('fallback'), c.get('dur')) for c in clips],
      '| mp3', round(dur, 1), 's / 记录', o.get('duration'), '| 丢出处', o.get('dropped_citations'), '| 合成调用', [(c['n'], c['speakers']) for c in tts_calls])
ok['generate'] = o['status'] == 'done' and os.path.exists(mp3) and dur > 5 and abs(dur - o['duration']) < 1.5
ok['speakers'] = all(ln['speaker'] in ('Alex', 'Sam') for ln in talk) and not any('Narrator' in str(ln) for ln in lines)
ok['text_clean'] = (talk[0]['text'] == 'So <laugh> small teams win?' and talk[2]['text'] == 'That says it all.'
                    and not any('[#' in ln['text'] for ln in talk))
ok['cites'] = (all(c in o['citations'] for ln in talk for c in ln['cite']) and o['dropped_citations'] >= 1
               and '9-99' not in json.dumps(talk))
ok['clips'] = (len(clips) == 2 and not clips[0].get('fallback') and clips[1].get('fallback') is True
               and clips[0]['quote'] in SHORT and clips[0]['clip'] in o['citations'] and 0.5 < clips[0]['dur'] <= podcast.CLIP_MAX_SEC)
ok['timing'] = ts == sorted(ts) and ts[0] == 0 and ts[-1] < o['duration']
ok['tts_blocks'] = (all(c['n'] <= podcast.MAX_TURNS_PER_CALL and len(c['speakers']) <= 2 for c in tts_calls)
                    and any(c['voices'].get('Alex') == 'Kore' and c['voices'].get('Sam') == 'Puck' for c in tts_calls)
                    and any(t.startswith('In their own words: ') for c in tts_calls for t in c['texts']))
script_prompt = next(p for purpose, p in prompts if purpose == 'podcast' and 'Task: write the script' in p)
ok['prompt'] = ('Choose ONLY from these candidates' in script_prompt and LONG.strip()[:40] not in script_prompt.split('Choose ONLY')[1]
                and 'about 4 minutes' in script_prompt and 'Never call it by its label' in script_prompt)
cost = usage.cost_for(ref='study:' + o['id']) or {}
ok['usage'] = o.get('cost_usd', 0) > 0 and cost.get('cost_usd', 0) > 0       # 合成在线程池里跑，记账归属也要带过去

# 5 裁原声：从本机音频裁、在停顿处下刀（第一期第 0 张卡在 00:00:10，那一段 10–17 秒有声）
card0 = next(c for c in corpus['cards'] if c['ep'] == 0 and c['quote'] == SHORT[0])
w = podcast.fetch_clip(card0, corpus['episodes'][0], os.path.join(TMP, 'clip0.wav'))
with wave.open(w) as f:
    clip_sec = f.getnframes() / f.getframerate()
ok['fetch_local'] = w is not None and 1.0 < clip_sec < podcast.CLIP_MAX_SEC

# 6 接口：估价、做一份（后台）、列表、取音频、删掉连 mp3 一起删、辩论没填双方 400
c = A.app.test_client()
est = c.get(f'/api/chain/{CID}/studio/podcast/estimate').get_json()
ok['estimate'] = set(est) == {'short', 'default', 'long'} and 0 < est['short'] < est['default'] < est['long']
bad = c.post(f'/api/chain/{CID}/studio/podcast', json={'format': 'debate'})
res = c.post(f'/api/chain/{CID}/studio/podcast', json={'format': 'brief', 'length': 'short'}).get_json()
oid = res['item']['id']
for _ in range(100):
    items = c.get(f'/api/chain/{CID}/studio').get_json()['items']
    it = next(x for x in items if x['id'] == oid)
    if it['status'] != 'running':
        break
    time.sleep(0.1)
au = c.get(f'/api/chain/{CID}/studio/{oid}/audio')
full = c.get(f'/api/chain/{CID}/studio/{oid}').get_json()
brief_speakers = {ln['speaker'] for ln in full['result']['lines'] if 'speaker' in ln}
c.delete(f'/api/chain/{CID}/studio/{oid}')
print('6 接口：', bad.status_code, it['status'], it.get('title'), au.status_code, au.mimetype, brief_speakers,
      os.path.exists(podcast.audio_path(cdir, oid)))
ok['api'] = (bad.status_code == 400 and it['status'] == 'done' and it.get('kind') == 'podcast' and it.get('title') == 'Small teams'
             and au.status_code == 200 and au.mimetype == 'audio/mpeg' and len(au.data) > 1000
             and brief_speakers == {'Alex'} and not os.path.exists(podcast.audio_path(cdir, oid))
             and c.get(f'/api/chain/{CID}/studio/{oid}/audio').status_code == 404
             and c.get(f'/api/chain/{CID}/studio/../audio').status_code == 404)

t = Checks()
for k, v in ok.items():
    t.check(k, v)
t.finish()
