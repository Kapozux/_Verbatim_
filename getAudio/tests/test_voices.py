"""声纹认人：每节课的「说话人1」不一定是同一个人——用声纹把整个项目里的人认成一套固定的叫法。
跑生产代码：voices.fingerprint_samples / identify / act、ask.build_speakers → ask.load 的卡片「谁说的」、
/api/chain/<id>/voices 一组接口、试听音频。
桩（只在模型边界）：声纹引擎换成假的（按「音频」里写好的说话人编号给向量，相似度精确可控）；
起名字的模型调用换成假的。ffmpeg 是本机程序，真跑。"""
import json
import os
import subprocess
import time
import wave

import numpy as np

from _support import Checks, fake_embed, isolate, make_transcript, write_json

isolate('voices')
import app as A  # noqa: E402
import ask  # noqa: E402
import voices  # noqa: E402

ask._embed_texts = fake_embed


def no_llm(*a, **k):                    # 项目刷新时后台会问「有没有被点名」：这里一律当失败（第 8 节用假的单独测）
    raise RuntimeError('stub: no model in tests')


ask._llm = no_llm
t = Checks()
SR = 100                     # 假引擎：每秒 100 个采样就够分辨谁在说话
DIM = 32
rng = np.random.default_rng(7)


def unit(v):
    return v / np.linalg.norm(v)


def like(ref, sim, seed):
    """和 ref 的余弦相似度正好是 sim 的单位向量。"""
    r = np.random.default_rng(seed).normal(size=DIM)
    r = unit(r - (r @ ref) * ref)
    return unit(sim * ref + np.sqrt(1 - sim * sim) * r)


TEACHER = unit(rng.normal(size=DIM))
S1 = like(TEACHER, 0.2, 1)
VOICE = {
    1: TEACHER,
    2: S1,                                  # 学生甲（第 1 课）
    3: like(TEACHER, 0.15, 3),              # 学生乙（第 1 课）
    4: like(S1, 0.92, 4),                   # 学生甲换了间教室（第 2 课）：很像 → 自动连上
    5: like(S1, 0.68, 5),                   # 第 3 课的某人：有点像学生甲 → 只提示「可能是」
    6: like(TEACHER, 0.1, 6),               # 第 2 课只说了两句短话的人 → 未区分
    7: like(TEACHER, 0.05, 17),             # 第 1 课放的一段歌
    8: like(S1, 0.82, 8),                   # 第 4 课：和学生甲约 0.8（用户后来说不是同一个人）
    9: like(S1, 0.79, 9),                   # 第 5 课：和学生甲约 0.78
}


class FakeEngine:
    """「音频」每个采样值 = 说话人编号 / 100。分离：连续同一编号是一段；声纹：编号对应的向量。
    每段的本地编号故意拆成两个（真引擎也会把同一个人拆成好几类）。"""
    model_id = 'fake-voice-1'
    sample_rate = SR

    def __init__(self):
        self.calls = 0

    def diarize(self, samples):
        self.calls += 1
        codes = np.rint(samples * 100).astype(int)
        out, start = [], None
        for i in range(len(codes) + 1):
            c = codes[i] if i < len(codes) else 0
            if start is not None and c != codes[start]:
                out.append((start / SR, i / SR, int(codes[start]) * 10 + len(out) % 2))
                start = None
            if start is None and c:
                start = i
        return out

    def embed(self, samples):
        code = int(np.rint(np.median(samples) * 100))
        return VOICE[code] + np.random.default_rng(len(samples)).normal(scale=0.01, size=DIM)


fake = FakeEngine()
voices.set_engine(fake)


def lesson(turns):
    """turns: [(说话人, 秒数)] → (采样, [(起, 止, 说话人)])；每段之间空 0.5 秒。"""
    parts, spans, t0 = [], [], 0.0
    for code, sec in turns:
        parts += [np.full(int(sec * SR), code / 100, dtype=np.float32), np.zeros(SR // 2, dtype=np.float32)]
        spans.append((t0, t0 + sec, code))
        t0 += sec + 0.5
    return np.concatenate(parts), spans


def hms(s):
    s = int(s)
    return f'{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}'


SAY = {1: '老师讲：鲁迅的小说要看叙述者', 2: '学生甲：我觉得是在批判看客', 3: '学生乙：华老栓其实很可怜',
       4: '学生甲：人血馒头是愚昧的象征', 5: '某同学：结尾的乌鸦是希望吗', 6: '嗯', 7: '（歌声）生命的火已点燃',
       8: '另一位同学：我同意', 9: '又一位同学：夏瑜是革命者'}
LESSONS = [                              # 学生每人每节 ≥ 10 秒才单独编号（MIN_SPEAKER）
    [(1, 40), (2, 8), (1, 30), (3, 12), (1, 20), (7, 12), (1, 25), (2, 6)],
    [(1, 50), (4, 12), (1, 30), (6, 1), (1, 10), (6, 1), (1, 20)],
    [(1, 60), (5, 12), (1, 30)],
]
SPECIAL = {(1, 2): '老师讲：小红，你来说说华老栓'}       # 第 1 课：老师点名，下一句是学生乙
R = A.config.RESULTS_FOLDER
tids, spans_of = [], {}
for n, turns in enumerate(LESSONS, 1):
    samples, spans = lesson(turns)
    tid = make_transcript(R, [(hms(s), SPECIAL.get((n, i)) or f'{SAY[c]}（第{n}课第{i}句）')
                              for i, (s, e, c) in enumerate(spans)], title=f'第{n}课')
    tids.append(tid)
    spans_of[tid] = spans
    voices.fingerprint_samples(tid, samples)

# ---- 1. 每条录音存一份声纹，同一个模型不重算 ----
calls = fake.calls
voices.fingerprint_samples(tids[0], lesson(LESSONS[0])[0])
rec = voices.recording(tids[0])
t.check('录音的声纹存下来了：每段话一条、带模型名', rec is not None and len(rec.start) == len(LESSONS[0])
        and rec.model == 'fake-voice-1')
t.check('同一个模型做过的不再重算', fake.calls == calls)

# ---- 2. 项目里认人 ----
pid = A.app.test_client().post('/api/projects', json={'name': 'IB 中文A 祖老师的课'}).get_json()['id']
c = A.app.test_client()


def add(task_ids):
    c.post(f'/api/chain/{pid}/sources/transcripts', json={'task_ids': task_ids})
    end = time.time() + 30
    while time.time() < end and pid in A._project_jobs:
        time.sleep(0.05)


add(tids)
cdir = A._chain_dir(pid)
voices.identify(cdir)


def who(tid):
    """每句转写 → 显示的名字。"""
    return [x[1] for x in voices.speaker_segments(cdir, tid)]


def name_of_code(tid, code):
    names = {nm for (s, e, cd), nm in zip(spans_of[tid], who(tid)) if cd == code}
    return names.pop() if len(names) == 1 else names


teacher = [name_of_code(tid, 1) for tid in tids]
t.check('主讲在每节课都是同一个名字，名字取自项目名「祖老师」', teacher == ['祖老师'] * 3, f'{teacher}')
s1, s1b = name_of_code(tids[0], 2), name_of_code(tids[1], 4)
t.check('同一个学生在两节课里叫法一样（声纹很像就连上）', s1 == s1b and s1.startswith('学生'), f'{s1} / {s1b}')
s2, s5 = name_of_code(tids[0], 3), name_of_code(tids[2], 5)
t.check('不同的学生叫法不同', len({s1, s2, s5}) == 3 and all(x.startswith('学生') for x in (s2, s5)), f'{s1} {s2} {s5}')
t.check('只说了几个字的人归「学生（未区分）」，不硬编号', name_of_code(tids[1], 6) == '学生（未区分）',
        f'{name_of_code(tids[1], 6)}')
ppl = voices.roster(cdir)
by_name = {p['name']: p for p in ppl['speakers']}
sug = by_name[s5].get('suggest') or {}
t.check('有点像但不够像的只给「可能是」，不自动合并', sug.get('name') == s1 and 0.6 <= sug.get('sim', 0) < 0.75, f'{sug}')
t.check('每个说话人有可试听的片段', all(p['clips'] for p in ppl['speakers']) and ppl['speakers'][0]['role'] == 'main')

# ---- 3. 卡片标上是谁说的 ----
analysis = {tids[0]: [(SAY[1] + '（第1课第0句）', 0), (SAY[2] + '（第1课第1句）', 1)],
            tids[1]: [(SAY[4] + '（第2课第1句）', 1)]}
for i, tid in enumerate(tids):
    rows = analysis.get(tid, [])
    write_json(os.path.join(cdir, f'cards_{i + 1:03d}.json'), {'task_id': tid, 'title': f'第{i + 1}课', 'cards': [
        {'quote': q, 'obs': 'x', 'timestamp': hms(spans_of[tid][j][0]), 'layer': '他的主张'} for q, j in rows]})
ask.build_speakers(cdir)


def card_speakers():
    return [x.get('speaker') for x in ask.load(cdir)['cards']]


t.check('卡片的「谁说的」用声纹认出的名字', card_speakers() == ['祖老师', s1, s1], f'{card_speakers()}')

# ---- 4. 用户改名：立刻生效，重算后也保留 ----
p1 = by_name[s1]['id']
before = {tid: [x[2] for x in voices.speaker_segments(cdir, tid)] for tid in tids}
r = c.post(f'/api/chain/{pid}/voices', json={'action': 'rename', 'speaker': p1, 'name': '小明'}).get_json()
t.check('给一个人改名不会改变任何一句话的归属', before == {tid: [x[2] for x in voices.speaker_segments(cdir, tid)]
                                                for tid in tids})
t.check('改名接口返回新名单', any(p['name'] == '小明' for p in (r or {}).get('speakers', [])), f'{r}')
t.check('改名后卡片马上显示新名字（不用重新认）', card_speakers() == ['祖老师', '小明', '小明'], f'{card_speakers()}')
voices.identify(cdir)
t.check('重算以后名字还在', name_of_code(tids[0], 2) == '小明' and name_of_code(tids[1], 4) == '小明')

# ---- 5. 纠错 + 校准：用户说「这组不是 TA」→ 拆开，且同样像的也不再自动连 ----
samples4, spans4 = lesson([(1, 40), (8, 12), (1, 20)])
tid4 = make_transcript(R, [(hms(s), f'{SAY[cd]}（第4课第{i}句）') for i, (s, e, cd) in enumerate(spans4)], title='第4课')
spans_of[tid4] = spans4
voices.fingerprint_samples(tid4, samples4)
add([tid4])
voices.identify(cdir)
t.check('0.8 像的新声音先被自动连成小明（默认阈值 0.75）', name_of_code(tid4, 8) == '小明', f'{name_of_code(tid4, 8)}')
grp = next(g for g in voices.roster(cdir)['speakers'] if g['id'] == p1)['groups']
g4 = next(g['ref'] for g in grp if g['tid'] == tid4)
c.post(f'/api/chain/{pid}/voices', json={'action': 'detach', 'speaker': p1, 'group': g4})
n4 = name_of_code(tid4, 8)
t.check('「这组不是 TA」后第 4 课那位不再叫小明', n4 != '小明' and n4.startswith('学生'), f'{n4}')
voices.identify(cdir)
t.check('拆开在重算后保持', name_of_code(tid4, 8) != '小明')
samples5, spans5 = lesson([(1, 40), (9, 12), (1, 20)])
tid5 = make_transcript(R, [(hms(s), f'{SAY[cd]}（第5课第{i}句）') for i, (s, e, cd) in enumerate(spans5)], title='第5课')
spans_of[tid5] = spans5
voices.fingerprint_samples(tid5, samples5)
add([tid5])
voices.identify(cdir)
n5 = name_of_code(tid5, 9)
t.check('校准：用户否掉过 0.8 的，0.78 像的新人也不再自动连（阈值上调）', n5 != '小明', f'{n5}，阈值 {voices.roster(cdir)["t_link"]}')
t.check('真正的同一个人（0.92）还连着', name_of_code(tids[1], 4) == '小明')

# ---- 6. 接受「可能是」→ 连成一个人 ----
p5 = by_name[s5]['id']
c.post(f'/api/chain/{pid}/voices', json={'action': 'merge', 'speaker': p5, 'into': p1})
t.check('确认「是同一个人」后，第 3 课那位也叫小明', name_of_code(tids[2], 5) == '小明')
voices.identify(cdir)
t.check('合并在重算后保持', name_of_code(tids[2], 5) == '小明')

# ---- 7. 角色：歌声标成「其他声音」----
song = name_of_code(tids[0], 7)
sp = next(p for p in voices.roster(cdir)['speakers'] if p['name'] == song)
c.post(f'/api/chain/{pid}/voices', json={'action': 'role', 'speaker': sp['id'], 'role': 'other'})
t.check('标成「其他声音」后显示这个名字', name_of_code(tids[0], 7) == '其他声音', f'{name_of_code(tids[0], 7)}')

# ---- 8. 起名字：只认老师原话里真点到的名字 ----
def fake_namer(prompt):
    if '小红，你来说说' in prompt:
        return json.dumps({'name': '小红', 'quote': '小红，你来说说华老栓'}, ensure_ascii=False)
    return json.dumps({'name': '张三', 'quote': '张三你来说'}, ensure_ascii=False)


hints = voices.suggest_names(cdir, llm=fake_namer)
got = {h['name']: (k, h) for k, h in hints.items()}
yi = next(p['id'] for p in voices.roster(cdir)['speakers'] if p['name'] == s2)
t.check('老师原话里点到的名字给出建议、附原话、给对了人', '小红' in got and got['小红'][0] == yi
        and got['小红'][1]['quote'] == '小红，你来说说华老栓', f'{hints}')
t.check('原话里没有的（模型编的）不采用', '张三' not in got, f'{hints}')
t.check('建议不自动改名，名单里带着建议', all(p['name'] != '小红' for p in voices.roster(cdir)['speakers'])
        and next(p for p in voices.roster(cdir)['speakers'] if p['id'] == yi).get('hint', {}).get('name') == '小红')

# ---- 9. 接口：名单、每句是谁、试听音频 ----
g = c.get(f'/api/chain/{pid}/voices').get_json()
t.check('名单接口：说话人、覆盖了几节课', g['lessons_total'] == 5 and g['lessons_with_voices'] == 5 and g['speakers'], f'{g.get("lessons_total")}')
segs = c.get(f'/api/chain/{pid}/voices/{tids[0]}').get_json()
t.check('每句转写是谁说的', len(segs['labels']) == len(spans_of[tids[0]]) and segs['labels'][0]['name'] == '祖老师',
        f'{segs}')

# 真 ffmpeg：解码 + 留一份可试听的压缩音频
wav = os.path.join(A.config.UPLOAD_FOLDER, 'tone.wav')
with wave.open(wav, 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes((np.sin(np.arange(32000) / 8) * 8000).astype('<i2').tobytes())
pcm = voices.decode(wav, 16000)
t.check('ffmpeg 解码成单声道 16k', abs(len(pcm) - 32000) < 400, f'{len(pcm)}')
out = voices.keep_audio(tids[0], wav)
t.check('留了一份试听音频', out and os.path.getsize(out) > 0)
r = c.get(f'/api/voices/{tids[0]}/audio', headers={'Range': 'bytes=0-99'})
t.check('试听音频支持拖动（Range 206）', r.status_code == 206 and len(r.data) == 100, f'{r.status_code}')
probe = subprocess.run([A.config.FFPROBE_BIN, '-v', 'error', '-show_entries', 'stream=channels', '-of', 'csv=p=0', out],
                       capture_output=True, text=True).stdout.strip()
t.check('试听音频是单声道', probe == '1', probe)
t.finish()
