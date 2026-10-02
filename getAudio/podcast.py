"""工作台的「音频播客」：把勾选的来源做成一期两人对谈（像 NotebookLM 的 Audio Overview），
关键的话直接插本人原声，下面给逐句文字稿、每句带出处。

流程（一条产出 kind=podcast，存在 <chain_dir>/studio/<id>.json，音频是同目录的 <id>.mp3）：
  1. 写脚本（flash-lite，一次调用里先 plan 后逐句 JSON）；
  2. 编辑：对照原段落挑错、改无聊的地方、加口语（再一次 flash-lite）；
  3. 代码检查：说话人、出处、语气标记白名单、原声只能是我们筛好的候选；
  4. 合成：gemini-3.8-flash-tts，每块最多 2 个说话人、最多 MAX_TURNS_PER_CALL 句，几块并行；
  5. 原声：从本机留的音频裁（没有就按链接现下那几秒），取不到就由主持人念原话；
  6. ffmpeg 拼成一个 mp3，每句记下开始时间，前端放到哪句高亮哪句、点句子跳过去。

提示词接在 study._prompt 的 HEAD 后面（只用下面的段落、出处 [#id]、多人按人归属），出处照旧走 citations。
"""
import base64
import glob
import json
import os
import re
import subprocess
import threading
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor

import ask
import citations
import config
import study
import usage

FORMATS = ('deep_dive', 'brief', 'debate')
LENGTHS = {'short': 4, 'default': 8, 'long': 15}          # 分钟
HOSTS = {'zh': ('小林', '阿周'), 'en': ('Alex', 'Sam')}
VOICES = ('Kore', 'Puck')                                 # 第一位主持人女声、第二位男声
TTS_MODEL = 'gemini-3.8-flash-tts'
TTS_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
TAGS = ('<laugh>', '<short pause>', '<sigh>')
MAX_TURNS_PER_CALL = 10
CLIPS = 3
CLIP_MAX_SEC = 22
RATE = 24000
AUDIO_TOKENS_PER_SEC = 25

# ================= 提示词 =================

PODCAST_SCRIPT = """Task: write the script of a podcast episode about this material, about {minutes} minutes long when read aloud: about {chars} {unit} of dialogue in about {turns} turns. Write the full length: a script much shorter than this is a failure.
Format: {format}.
- "deep_dive": two hosts, {host_a} and {host_b}, explore the material together. {host_a} knows it well and explains; {host_b} is sharp and curious, asks what a listener would ask, pushes back when something sounds too neat, and sums up in plain words. Neither is a yes-man.
- "brief": one host, {host_a}, gives the key points straight, like a short news segment. No small talk.
- "debate": two hosts argue it out. {host_a} makes the case for {side_a}, {host_b} for {side_b}. Each argues from what that side actually said in the passages, concedes real points, and never invents a position for them. End without declaring a winner: say where they really differ and what would settle it.

First write the plan, then the lines.
title: a short episode title (at most 20 Chinese characters or 10 English words) that makes a specific point.
plan: what the listener should come away with, and the 3–5 segments that get them there, in order, each one sentence. Start with a hook (a surprising fact, a quote, a question), not with "welcome to the show". End with the one or two things worth remembering.

Lines:
- Each line is one turn: {{"speaker": "...", "text": "...", "style": "...", "cite": ["4-12"]}}.
- text is spoken language: short sentences, no lists, no headings, no brackets, no Markdown, no ids. Never say "passage", "source" or "citation".
- When you mention where something comes from, name the person, or describe the video or recording by its topic ("in the video on remembering words forever"). Never call it by its label: no "EP4", "episode 4", "第四期", "DOC2".
- cite: the ids of the passages this line's facts rest on, exactly as shown in brackets at the start of each passage line but without "[#" and "]" (e.g. "4-12", "t3-7"). Every factual claim needs one; reactions and questions can have an empty list.
- style: how the line is delivered, a few English words for a voice model ("curious, rising", "dry, amused", "slow, serious"). Vary it with the content.
- A turn is 1–4 sentences. Hosts interrupt, react and build on each other; avoid long monologues and avoid ping-pong of one-word replies.
- Numbers, names and claims must come from the passages. Where people disagree or someone changed their mind, say so plainly.
- When people are talked about, keep straight who said what; never put one person's words in another's mouth.
{clip_rules}
Return JSON only: {{"title": "...", "plan": ["...", "..."], "lines": [{{"speaker": "{host_a}", "text": "...", "style": "...", "cite": ["4-12"]}}{clip_example}]}}"""

CLIP_RULES = """
Original clips: at {clips} key moments, instead of having a host paraphrase, play the person saying it. Add {{"clip": "4-12"}} as its own line. Choose ONLY from these candidates (id | who | what they say), each short enough to play:
{candidates}
Use exactly {clips} different clips, spread across the episode, each where it makes the point best. The host line before a clip sets it up ("listen to how they put it"); the host line after reacts to what was said. Never repeat the quote in the host's own words.
"""

PODCAST_POLISH = """Task: you are the editor of a podcast. Below is a draft script about this material. Review it, then return the improved script.
Draft:
{script}

1. Check it against the passages above. Fix or cut any line whose facts, numbers, names or attribution the passages don't support, and fix any cite that points to the wrong passage. Don't add new facts.
2. Fix what makes it dull or confusing: a weak opening, a segment that repeats another, a host who only agrees, jargon nobody explains, an ending that just trails off. Replace any "EP4" / "episode 4" / "第四期" with the topic of that video.
3. Make it sound like two people actually talking, not reading. Concretely:
   - About every other turn starts with a natural reaction or filler in the script's language ("嗯", "对", "哎", "等一下", "说实话", "我跟你说"; "hmm", "right", "wait", "honestly", "see").
   - At least three times, a host cuts in: the turn starts by finishing or interrupting the other's thought, and the previous turn may end mid-sentence with "——".
   - Use the vocal tags <laugh>, <short pause> or <sigh> inside text 2–4 times in total, where they fit. No other tags.
4. Don't shorten it: the result has at least as many turns and words as the draft. Keep every {{"clip": ...}} line where it is, with the line before setting it up and the line after reacting to it. Keep the title, the same speakers, JSON shape and fields.
Return JSON only: {{"title": "...", "lines": [...]}}"""


# ================= 原声候选 =================

def _sec(ts):
    try:
        parts = [float(x) for x in str(ts).split(':')]
    except ValueError:
        return None
    return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))


def _norm(s):
    return re.sub(r'[\W_]+', '', s or '')


def _clip_len_ok(quote):
    """一两句话、能单独听懂：中文 8–60 字，英文 4–35 词。"""
    q = (quote or '').strip()
    n_cjk = len(re.findall(r'[一-鿿]', q))
    if n_cjk >= len(q) * 0.3:
        return 8 <= len(_norm(q)) <= 60
    return 4 <= len(q.split()) <= 35


def local_audio(task_id):
    """本机留着的音频：results/<tid>/audio.*（压缩过的原音频）、声纹留的那份、还在 uploads/ 里的原文件。"""
    if not re.fullmatch(r'[0-9a-zA-Z-]{8,64}', task_id or ''):
        return None
    d = os.path.join(config.RESULTS_FOLDER, task_id)
    for p in sorted(glob.glob(os.path.join(d, 'audio.*'))) + sorted(glob.glob(os.path.join(d, 'voice*.m4a'))) \
            + sorted(glob.glob(os.path.join(config.UPLOAD_FOLDER, task_id + '.*'))):
        if os.path.isfile(p) and not p.endswith(('.tmp', '.part.m4a')):
            return p
    return None


def clip_candidates(corpus, cards, limit=40):
    """能当原声放的卡：证据卡（不是原文段落）、有时间点、来自录音 / 视频（不是文档）、本机有音频或有链接、
    原话够短。按期均匀取，主张优先。"""
    by_ep = {}
    for c in cards:
        ep = corpus['episodes'][c['ep']]
        if c.get('layer') == 'source' or ep.get('kind') == 'doc' or _sec(c.get('ts')) is None:
            continue
        if not _clip_len_ok(c.get('quote')):
            continue
        if not (ep.get('video_url') or local_audio(ep.get('task_id'))):
            continue
        by_ep.setdefault(c['ep'], []).append(c)
    for cs in by_ep.values():
        cs.sort(key=lambda c: (c.get('layer') != 'claim', c['id']))
    out, i = [], 0
    while len(out) < limit and any(i < len(cs) for cs in by_ep.values()):
        out += [cs[i] for cs in by_ep.values() if i < len(cs)]
        i += 1
    return out[:limit]


def _who(c, corpus):
    ep = corpus['episodes'][c['ep']]
    return c.get('speaker') or ep.get('person') or corpus.get('author') or ''


# ================= 写脚本 =================

def _lines_ok(obj):
    return isinstance(obj, dict) and isinstance(obj.get('lines'), list) and obj['lines']


def _llm_json(prompt):
    last = None
    for i in range(2):
        raw = ask._llm(prompt, purpose='podcast' if i == 0 else 'podcast-retry')
        obj = ask._json_from(raw) or {}
        if _lines_ok(obj):
            return obj
        last = raw
    raise RuntimeError('The model returned nothing usable' + (f': {str(last)[:120]}' if last else ''))


def write_script(corpus, cards, lang, focus, fmt, minutes, hosts, sides=('', ''), n_clips=CLIPS):
    """→ (脚本 dict {title, plan, lines}, 候选原声卡)"""
    zh = lang.startswith('Chinese')
    cands = clip_candidates(corpus, cards) if n_clips else []
    n_clips = min(n_clips, len(cands))
    clip_rules = CLIP_RULES.format(clips=n_clips, candidates='\n'.join(
        f'{c["id"]} | {_who(c, corpus) or "-"} | "{c["quote"]}"' for c in cands)) if n_clips else \
        '\nDon\'t add any clip lines.\n'
    task = PODCAST_SCRIPT.format(
        minutes=minutes, chars=minutes * (250 if zh else 150), unit='Chinese characters' if zh else 'words',
        turns=minutes * (6 if fmt != 'brief' else 3), format=fmt, host_a=hosts[0], host_b=hosts[1],
        side_a=sides[0] or '-', side_b=sides[1] or '-', clip_rules=clip_rules,
        clip_example=', {"clip": "4-12"}' if n_clips else '')
    head = study._prompt(corpus, cards, lang, focus, '')
    draft = _llm_json(head + task)
    polished = _llm_json(head + PODCAST_POLISH.format(script=json.dumps(
        {'title': draft.get('title', ''), 'lines': draft['lines']}, ensure_ascii=False)))
    polished.setdefault('title', draft.get('title', ''))
    polished['plan'] = draft.get('plan') or []
    return polished, cands


def clean_text(text):
    """只留白名单里的语气标记；出处、方括号、Markdown、别的尖括号都去掉。"""
    s = str(text or '')
    keep = {t: f'\x00{i}\x00' for i, t in enumerate(TAGS)}
    for t, k in keep.items():
        s = s.replace(t, k)
    s = re.sub(r'\[#[^\]]*\]|<[^>]*>|\[[^\]]*\]|[*_#`]', '', s)
    for t, k in keep.items():
        s = s.replace(k, t)
    return re.sub(r'\s+', ' ', s).strip()


def check(script, corpus, hosts, cands, cites):
    """模型脚本 → 能合成的行。丢掉不认识的说话人、不在候选里的原声；出处换成真 id、编造的删掉。"""
    cand = {c['id']: c for c in cands}
    lines, used_clips = [], set()
    for ln in script.get('lines') or []:
        if not isinstance(ln, dict):
            continue
        if 'clip' in ln:
            cid = str(ln['clip']).strip().strip('[]').lstrip('#')
            if cid in cand and cid not in used_clips:
                used_clips.add(cid)
                lines.append({'clip': cid})
            continue
        if ln.get('speaker') not in hosts:
            continue
        text = clean_text(ln.get('text'))
        if not re.sub(r'<[^>]*>', '', text).strip():
            continue
        real = []
        for tok in ln.get('cite') or []:
            r = cites.resolve(tok)
            if r and r not in real:
                real.append(r)
            elif not r:
                cites.dropped += 1
        lines.append({'speaker': ln['speaker'], 'text': text,
                      'style': str(ln.get('style') or '')[:80], 'cite': real})
    return lines


# ================= 合成 =================

def _post(body, model):
    import requests
    r = requests.post(TTS_URL.format(model=model), params={'key': config.gemini_key()}, json=body, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f'TTS {r.status_code}: {r.text[:300]}')
    return r.json()


def tts(turns, voices, path, model=TTS_MODEL):
    """一块对话 → 24k 单声道 wav。turns: [{speaker, text, style}]，最多两个说话人。"""
    speakers = list(dict.fromkeys(t['speaker'] for t in turns))
    parts = [{'text': t['text'], 'speechMetadata': {'speaker': t['speaker'], 'style': t['style'] or 'natural, conversational'}}
             for t in turns]
    if len(speakers) == 2:
        sc = {'multiSpeakerVoiceConfig': {'speakerVoiceConfigs': [
            {'speaker': s, 'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voices[s]}}} for s in speakers]}}
    else:
        sc = {'voiceConfig': {'prebuiltVoiceConfig': {'voiceName': voices[speakers[0]]}}}
    j = _post({'contents': [{'role': 'user', 'parts': parts}],
               'generationConfig': {'responseModalities': ['AUDIO'], 'speechConfig': sc}}, model)
    um = j.get('usageMetadata') or {}
    usage.record('gemini', model, 'podcast-tts', input_tokens=um.get('promptTokenCount', 0),
                 output_tokens=um.get('candidatesTokenCount', 0))
    inl = j['candidates'][0]['content']['parts'][0]['inlineData']
    data = base64.b64decode(inl['data'])
    if data[:4] == b'RIFF':
        tmp = path + '.src.wav'
        with open(tmp, 'wb') as f:
            f.write(data)
        _to_wav(tmp, path)
        os.remove(tmp)
    else:                                       # 裸 PCM（audio/L16;rate=24000）
        with wave.open(path, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(data)
    return path


def _ffmpeg(*args):
    subprocess.run([config.FFMPEG_BIN, '-v', 'error', '-nostdin', '-y', *args], capture_output=True, check=True)


def _to_wav(src, dst, start=None, end=None, headers=None):
    pre = (['-headers', headers] if headers else []) + (['-ss', f'{start:.2f}'] if start is not None else []) \
        + (['-to', f'{end:.2f}'] if end is not None else [])
    _ffmpeg(*pre, '-i', src, '-vn', '-ac', '1', '-ar', str(RATE), '-sample_fmt', 's16', dst)


def _wav_seconds(path):
    with wave.open(path) as w:
        return w.getnframes() / float(w.getframerate())


def clip_window(card, ep):
    """原话在原音频里的起止秒。转写一段常有好几句、原话从段中间开始：先在转写文字里找到原话是第几个字起、
    第几个字止，再按那一段的语速（字数 / 段长）折成秒。找不到原话就从卡片时间点起按字数估。"""
    start = _sec(card.get('ts')) or 0.0
    q = _norm(card.get('quote'))
    cps0 = 4.5 if re.search(r'[\u4e00-\u9fff]', card.get('quote') or '') else 14.0     # 每秒几个字（英文按字母）
    end = start + len(q) / cps0 + 0.6
    t = ask._read_json(os.path.join(config.RESULTS_FOLDER, ep.get('task_id') or '_', 'transcript.json'))
    segs = t.get('segments') if isinstance(t, dict) else t
    if not (isinstance(segs, list) and segs and q):
        return max(0.0, start - 0.2), min(end, start + CLIP_MAX_SEC)
    ss = [(_sec(x.get('timestamp') or x.get('start') or 0) or 0.0, _norm(x.get('text', ''))) for x in segs
          if isinstance(x, dict)]
    offs, n = [], 0
    for _, tx in ss:
        offs.append(n)
        n += len(tx)
    cat = ''.join(tx for _, tx in ss)
    i = max((k for k, (s0, _) in enumerate(ss) if s0 <= start + 1), default=0)
    lo = offs[max(0, i - 2)]
    hi = offs[min(len(ss) - 1, i + 3)] + len(ss[min(len(ss) - 1, i + 3)][1])
    p = -1
    for probe in (q[:10], q[:6]):
        p = cat.find(probe, lo, hi + len(q))
        if p >= 0:
            break
    if p < 0:
        return max(0.0, start - 0.2), min(end, start + CLIP_MAX_SEC)

    def at(char):                          # 第 char 个字大约在第几秒
        k = max(j for j in range(len(ss)) if offs[j] <= char)
        nxt = ss[k + 1][0] if k + 1 < len(ss) else None
        cps = len(ss[k][1]) / (nxt - ss[k][0]) if nxt and nxt > ss[k][0] and ss[k][1] else cps0
        return ss[k][0] + (char - offs[k]) / max(cps, cps0 * 0.6)     # 段后面有长停顿时别算出慢得离谱的语速
    s, e = at(p), at(min(p + len(q) - 1, n - 1)) + 0.8          # 按原话最后一个字算，别落到下一段开头
    return max(0.0, s - 0.25), min(e, s + CLIP_MAX_SEC)


def _bili_audio_url(url):
    import requests
    m = re.search(r'BV\w+', url or '')
    if not m:
        return None
    h = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'}
    cid = requests.get('https://api.bilibili.com/x/web-interface/view', params={'bvid': m.group(0)},
                       headers=h, timeout=20).json()['data']['cid']
    j = requests.get('https://api.bilibili.com/x/player/playurl', params={'bvid': m.group(0), 'cid': cid, 'fnval': 16},
                     headers=h, timeout=20).json()
    return j['data']['dash']['audio'][0]['baseUrl']


PAD_BEFORE, PAD_AFTER = 1.5, 2.5     # 转写时间点只到秒、常常早一点：先多裁一截，再在停顿处下刀


def snap_to_pauses(path, want_start, want_end):
    """在 wav 里找停顿（≥0.15 秒的低音量），把开头挪到离 want_start 最近的停顿、结尾挪到 want_end 之后第一个停顿，
    原地改写。want_* 是相对这个文件开头的秒数。"""
    import numpy as np
    with wave.open(path) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    hop = int(sr * 0.02)
    if len(x) < hop * 10:
        return path
    rms = np.sqrt(np.mean(x[:len(x) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    quiet = rms < max(np.percentile(rms, 20) * 1.8, 150)
    pauses, k = [], 0                                   # [(中点秒, 长度秒)]
    while k < len(quiet):
        if quiet[k]:
            j = k
            while j < len(quiet) and quiet[j]:
                j += 1
            if j - k >= 8:
                pauses.append(((k + j) / 2 * 0.02, (j - k) * 0.02))
            k = j
        else:
            k += 1
    # 开头：时间点通常偏早，原话真正开始在它之后——在 [-0.5, +1.8] 秒里挑最长的停顿（句与句之间停得最久）
    near = [(p, d) for p, d in pauses if want_start - 0.5 <= p <= want_start + 1.8]
    s = max(near, key=lambda pd: pd[1])[0] if near else max(0.0, want_start - 0.1)
    after = [p for p, _ in pauses if want_end - 0.4 <= p <= want_end + PAD_AFTER and p > s + 1]
    e = after[0] if after else min(len(x) / sr, want_end + 0.6)
    a, b = int(s * sr), int(e * sr)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x[a:b].astype(np.int16).tobytes())
    return path


_whisper_lock = threading.Lock()


def _words(path, zh):
    """本机 Whisper 的逐词时间 [(词, 起, 止)]；没装就返回 None。"""
    with _whisper_lock:                 # MLX / CTranslate2 都别多线程同时跑
        try:
            import mlx_whisper
            r = mlx_whisper.transcribe(path, path_or_hf_repo='mlx-community/whisper-large-v3-mlx',
                                       word_timestamps=True, language='zh' if zh else None)
            return [(w['word'], w['start'], w['end']) for sg in r.get('segments', []) for w in sg.get('words', [])]
        except Exception:  # noqa: BLE001
            pass
        try:
            from faster_whisper import WhisperModel
            m = WhisperModel('small', device='cpu', compute_type='int8')
            segs, _ = m.transcribe(path, word_timestamps=True, language='zh' if zh else None)
            return [(w.word, w.start, w.end) for sg in segs for w in (sg.words or [])]
        except Exception:  # noqa: BLE001
            return None


def align_quote(words, quote):
    """把原话跟识别出的词对上（识别文字跟转写不全一样：95% / 百分之九十五），→ (起, 止) 秒或 None。"""
    import difflib
    q = _norm(quote).lower()
    chars, owner = [], []
    for k, (w, _, _) in enumerate(words):
        for ch in _norm(w).lower():
            chars.append(ch)
            owner.append(k)
    asr = ''.join(chars)
    if not asr or not q:
        return None
    blocks = [b for b in difflib.SequenceMatcher(None, asr, q, autojunk=False).get_matching_blocks() if b.size >= 2]
    if not blocks or sum(b.size for b in blocks) < len(q) * 0.5:
        return None
    first, last = blocks[0], blocks[-1]
    # 原话开头 / 结尾没对上的几个字：按识别结果往前 / 往后补上同样多的字
    a = max(0, first.a - first.b)
    z = min(len(asr) - 1, last.a + last.size - 1 + (len(q) - last.b - last.size))
    return words[owner[a]][1], words[owner[z]][2]


def _cut(path, s, e):
    with wave.open(path) as w:
        sr = w.getframerate()
        w.setpos(int(s * sr))
        data = w.readframes(int((e - s) * sr))
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def fetch_clip(card, ep, dst):
    """把原话那几秒裁成 wav。本机有音频就从本机裁；没有就按链接只下那一段。取不到返回 None。
    先多裁一截，再精修：本机有 Whisper 就逐词对齐原话，没有就在停顿处下刀。"""
    s0, e0 = clip_window(card, ep)
    s, e = max(0.0, s0 - PAD_BEFORE), e0 + PAD_AFTER
    got = _fetch_range(ep, dst, s, e)
    if not got:
        return None
    try:
        words = _words(got, bool(re.search(r'[\u4e00-\u9fff]', card.get('quote') or '')))
        span = align_quote(words, card.get('quote')) if words else None
        if span:
            dur = _wav_seconds(got)
            _cut(got, max(0.0, span[0] - 0.15), min(dur, span[1] + 0.3))
        else:
            snap_to_pauses(got, s0 - s, e0 - s)
        if _wav_seconds(got) > CLIP_MAX_SEC:
            _cut(got, 0, CLIP_MAX_SEC)
    except Exception as ex:  # noqa: BLE001
        print(f'[podcast] trim {card.get("id")}: {ex}')
    return got


def _fetch_range(ep, dst, s, e):
    src = local_audio(ep.get('task_id'))
    try:
        if src:
            _to_wav(src, dst, s, e)
            return dst
        url = ep.get('video_url') or ''
        if 'bilibili.com' in url:          # yt-dlp 碰到 B 站活动页跳转会失败，直接走播放接口拿音频流
            au = _bili_audio_url(url)
            if au:
                _to_wav(au, dst, s, e, headers='Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0\r\n')
                return dst
        if url:
            import downloader
            tmp = dst + '.dl'
            subprocess.run([downloader._resolve_ytdlp(), '-q', '-f', 'bestaudio', '--download-sections',
                            f'*{s:.1f}-{e:.1f}', '--force-keyframes-at-cuts', '-o', tmp + '.%(ext)s',
                            *downloader._proxy_args(), url], capture_output=True, timeout=180, check=True)
            got = glob.glob(tmp + '.*')
            if got:
                _to_wav(got[0], dst)
                for g in got:
                    os.remove(g)
                return dst
    except Exception as ex:  # noqa: BLE001
        print(f'[podcast] clip {ep.get("task_id")} {s:.0f}s: {str(ex)[:200]}')
    return None


def _silence(path, sec):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b'\x00\x00' * int(RATE * sec))
    return path


def synthesize(lines, corpus, hosts, work, out_mp3, on_progress=None):
    """逐块合成 + 插原声 + 拼接。给每行写上 t（开始秒）。→ 总秒数"""
    voices = dict(zip(hosts, VOICES))
    by_id = {c['id']: c for c in corpus['cards']}
    pieces = []                                  # [(kind, payload)]，kind = tts | clip
    block = []
    for ln in lines:
        if 'clip' in ln or len(block) >= MAX_TURNS_PER_CALL:
            if block:
                pieces.append(('tts', block))
                block = []
        if 'clip' in ln:
            pieces.append(('clip', ln))
        else:
            block.append(ln)
    if block:
        pieces.append(('tts', block))

    done = [0]
    lock = threading.Lock()

    def make(i):
        kind, payload = pieces[i]
        path = os.path.join(work, f'{i:03d}.wav')
        if kind == 'clip':
            c = by_id[payload['clip']]
            if fetch_clip(c, corpus['episodes'][c['ep']], path):
                return path
            payload['fallback'] = True           # 原声取不到：主持人念原话
            say = ('他的原话是：' if re.search(r'[一-鿿]', c['quote']) else 'In their own words: ') + c['quote']
            tts([{'speaker': hosts[0], 'text': say, 'style': 'quoting, measured'}], voices, path)
        else:
            tts(payload, voices, path)
        with lock:
            done[0] += 1
            if on_progress:
                on_progress(done[0], len(pieces))
        return path

    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(usage.bound(make), range(len(pieces))))      # 记账的归属是线程局部的，要带进线程池

    gap = _silence(os.path.join(work, 'gap.wav'), 0.45)
    order, t = [], 0.0
    for (kind, payload), path in zip(pieces, paths):
        dur = _wav_seconds(path)
        if kind == 'clip':
            order += [gap, path, gap]
            payload['t'] = round(t + 0.45, 2)
            payload['dur'] = round(dur, 2)
            t += dur + 0.9
        else:
            # 一块里各句的开始时间按字数分摊（模型不给逐句时间）
            weights = [max(1, len(re.sub(r'<[^>]*>', '', ln['text']))) for ln in payload]
            acc = 0
            for ln, w in zip(payload, weights):
                ln['t'] = round(t + dur * acc / sum(weights), 2)
                acc += w
            order.append(path)
            t += dur
    lst = os.path.join(work, 'concat.txt')
    with open(lst, 'w') as f:
        f.writelines(f"file '{p}'\n" for p in order)
    _ffmpeg('-f', 'concat', '-safe', '0', '-i', lst, '-ar', str(RATE), '-ac', '1', '-b:a', '96k', out_mp3)
    return round(t, 1)


# ================= 产出 =================

def audio_path(cdir, oid):
    study._path(cdir, oid)                       # 只为校验 id
    return os.path.join(study.studio_dir(cdir), oid + '.mp3')


def remove_audio(cdir, oid):
    try:
        os.remove(audio_path(cdir, oid))
    except (ValueError, OSError):
        pass


def estimate(minutes, corpus_tokens=60_000):
    """估价（美元）：两次脚本调用（每次都送一遍原段落）+ 合成音频。"""
    p = usage._price_for(ask.ASK_MODEL) or {'input': 0.3, 'output': 2.5}
    llm = 2 * (corpus_tokens * p.get('input', 0.3) + 6000 * p.get('output', 2.5)) / 1e6
    tp = usage._price_for(TTS_MODEL) or {'input': 1.0, 'output': 18.0}
    audio = minutes * 60 * AUDIO_TOKENS_PER_SEC * tp.get('output', 18.0) / 1e6
    return round(llm + audio, 2)


def start(cdir, chain_id, fmt='deep_dive', length='default', focus='', scope=None, lang=None, sides=None, run=None):
    """建一条 running 的产出，后台写脚本、合成。sides：debate 时两位主持人各代表谁。"""
    fmt = fmt if fmt in FORMATS else 'deep_dive'
    length = length if length in LENGTHS else 'default'
    focus = str(focus or '').strip()[:200]
    sides = [str(s or '').strip()[:60] for s in (sides or [])][:2] + ['', '']
    if fmt == 'debate' and not (sides[0] and sides[1]):
        raise ValueError('Pick the two sides of the debate')
    oid = uuid.uuid4().hex[:12]
    out = {'id': oid, 'kind': 'podcast', 'status': 'running', 'created_at': study._now(), 'chain': chain_id,
           'format': fmt, 'length': length, 'focus': focus, 'sides': sides[:2] if fmt == 'debate' else [],
           'scope': scope if isinstance(scope, dict) else None, 'progress': {'stage': 'script'}}
    study._save(cdir, out)
    with study._lock:
        study._running.add(oid)

    def job():
        try:
            with usage.scope(ref='study:' + oid, chain=chain_id):
                work()
        except Exception as e:  # noqa: BLE001
            out.update(status='failed', error=str(e)[:300], finished_at=study._now())
        finally:
            out['cost_usd'] = (usage.cost_for(ref='study:' + oid) or {}).get('cost_usd', 0)
            out.pop('progress', None)
            study._save(cdir, out)
            with study._lock:
                study._running.discard(oid)

    def work():
        corpus = ask.load(cdir, passages=True)
        pool = ask.scope_pool(corpus, out['scope'])
        pool = corpus['cards'] if pool is None else pool
        if not pool:
            raise RuntimeError('Nothing to work from in the selected sources')
        lng = study._lang(corpus, lang)
        hosts = HOSTS['zh' if lng.startswith('Chinese') else 'en']
        cards, thinned = study._pick(corpus, pool, focus)
        cites = citations.Citations().add(corpus, cards)
        script, cands = write_script(corpus, cards, lng, focus, fmt, LENGTHS[length],
                                     hosts if fmt != 'brief' else (hosts[0], hosts[0]), sides)
        lines = check(script, corpus, hosts if fmt != 'brief' else (hosts[0],), cands, cites)
        if not any('speaker' in ln for ln in lines):
            raise RuntimeError('The script came back empty')
        out['progress'] = {'stage': 'audio', 'done': 0, 'total': 1}
        study._save(cdir, out)

        def prog(d, n):
            out['progress'] = {'stage': 'audio', 'done': d, 'total': n}
            study._save(cdir, out)
        import tempfile
        with tempfile.TemporaryDirectory(prefix='podcast_') as work_dir:
            duration = synthesize(lines, corpus, hosts, work_dir, audio_path(cdir, oid), on_progress=prog)
        by_id = {c['id']: c for c in corpus['cards']}
        for ln in lines:
            if 'clip' in ln:
                c = by_id[ln['clip']]
                cites.resolve(c['id'])
                ln.update(quote=c['quote'], who=_who(c, corpus))
        out.update(status='done', finished_at=study._now(), duration=duration, title=str(script.get('title') or '')[:80],
                   lang='zh' if lng.startswith('Chinese') else 'en',
                   result={'title': str(script.get('title') or '')[:80], 'plan': script.get('plan') or [],
                           'hosts': list(hosts if fmt != 'brief' else hosts[:1]), 'lines': lines},
                   citations=cites.used, dropped_citations=cites.dropped,
                   coverage={'passages_used': len(cards), 'passages_total': len(pool), 'thinned': thinned,
                             'sources': len({c['ep'] for c in pool})})

    (run or (lambda f: threading.Thread(target=f, daemon=True).start()))(job)
    return out
