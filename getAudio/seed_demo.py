"""
生成「演示工作区」：给外界看的 Verbatim 数据目录。演示界面一律英文，主打博主证据卡。

  python3 seed_demo.py                 # 写到 ~/Documents/CODEelse/verbatim-demo
  python3 seed_demo.py --keep-dates    # 保留真实转写日期（默认会把日期均匀铺到最近 90 天，回顾面板才好看）
  python3 seed_demo.py --max 200       # 资料库最多放多少条（默认 200）

资料库：只放英文内容、有公开来源、转写可用的条目；标签或标题命中敏感词的整条跳过。
博主：DEMO_CHAINS 里的英文博主，带证据卡 / 画像 / 五个镜头 / 逐期文档。

原数据一个字不动。英文内容（证据卡的观察、标题/简介/标签/摘要、画像、镜头）是**另生成的英文副本**，
只写进演示目录；翻译和生成结果缓存在 verbatim-demo/_en_cache/，重跑不再花钱
（整套首次生成约 $1–2：翻译用 flash-lite，画像和镜头用分析模型）。
"""
import glob
import json
import os
import random
import re
import shutil
import tempfile
import sys
from datetime import datetime, timedelta

import config
import usage

DEMO_DIR = os.path.expanduser('~/Documents/CODEelse/verbatim-demo')
EN_CACHE = os.path.join(DEMO_DIR, '_en_cache')
# 演示用的英文博主（非时政）。Johnny Harris 讲伊朗/中国洗钱，是时政，不放。
DEMO_CHAINS = [
    '37ee1e12c2e741408a9b11a359d2b739',   # Y Combinator
    '9e621e129cbc43ae9aa027054bd39249',   # Family Friendly
]
BLOCK_TAGS = re.compile(r'时政|政治|权力|中共|两性|情感|亲密|婚|恋|性|国际关系|社会评论|社会议题|外交|军|台湾|香港|新疆|历史分析|人物分析|深度分析|社会观察|社会分析|案例分析|心理分析|短剧|CP')
BLOCK_TEXT = re.compile(r'习近平|习李|中共|共产党|政治|政权|领导人|薄熙来|王沪宁|王岐山|李克强|周永康|张又侠|军队|清洗|台湾|香港|新疆|移民|出入境|中南海|外交部|习|党')
CJK = re.compile(r'[一-鿿]')


def _cjk_ratio(text):
    return len(CJK.findall(text)) / max(1, len(text))


def _is_english_content(task_dir):
    """看转写正文本身（元数据里的 AI 标题/标签是中文，不能拿来判断）。"""
    try:
        with open(os.path.join(task_dir, 'transcript.json'), encoding='utf-8') as f:
            segs = json.load(f)
    except Exception:
        return False, None
    text = ' '.join((s.get('text') or '') for s in segs[:80] if isinstance(s, dict))
    if len(text) < 200 or _cjk_ratio(text) >= 0.01:
        return False, segs
    return True, segs


def pick(results_dir, max_items, must=()):
    from sanitize import transcript_quality
    keep = []
    for name in sorted(os.listdir(results_dir)):
        p = os.path.join(results_dir, name, 'meta.json')
        if name.startswith('_') or not os.path.isfile(p):
            continue
        try:
            m = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        forced = name in must
        if not forced:
            if not m.get('source_url') or not m.get('ai_title') or not m.get('duration_seconds'):
                continue
            if BLOCK_TAGS.search(' '.join(m.get('ai_tags') or [])):
                continue
            if BLOCK_TEXT.search((m.get('ai_title') or '') + (m.get('ai_one_line') or '') + (m.get('filename') or '')):
                continue
        ok, segs = _is_english_content(os.path.join(results_dir, name))
        if not ok and not forced:
            continue
        if not forced:
            good, _reason, _ = transcript_quality(segs, m.get('duration_seconds'))
            if not good:          # 纯音乐 / 复读死循环这类废稿别拿出来展示
                continue
        keep.append((name, m))
    keep.sort(key=lambda x: x[1].get('date', ''))
    if len(keep) > max_items:
        forced = [k for k in keep if k[0] in must]
        rest = [k for k in keep if k[0] not in must]
        n = max(0, max_items - len(forced))
        step = len(rest) / max(1, n)
        keep = sorted(forced + [rest[int(i * step)] for i in range(n)], key=lambda x: x[1].get('date', ''))
    return keep


def spread_dates(items, days=90):
    """把日期按原顺序均匀铺到最近 days 天，但保留每天的时刻；周末稍少，像真人。"""
    now = datetime.now()
    out = []
    n = len(items)
    for i, (name, m) in enumerate(items):
        # 越靠近现在越密一点（最近一个月大约占 45%）
        frac = (i / max(1, n - 1)) ** 0.8
        d = now - timedelta(days=days * (1 - frac))
        try:
            orig = datetime.strptime(m['date'][:19], '%Y-%m-%d %H:%M:%S')
            d = d.replace(hour=orig.hour, minute=orig.minute, second=orig.second)
        except Exception:
            pass
        if d.weekday() >= 5 and random.random() < 0.5:
            d -= timedelta(days=random.choice([1, 2]))
        if d > now:
            d = now - timedelta(minutes=random.randint(5, 300))
        out.append((name, m, d))
    return out


# ---------- 英文副本：模型调用 + 缓存 ----------

def _cached(key, make):
    """按 key 缓存到 _en_cache/<key>.json；make() 返回可 JSON 化的对象，None 表示失败不缓存。"""
    path = os.path.join(EN_CACHE, key + '.json')
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    val = make()
    if val is not None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(val, f, ensure_ascii=False, indent=2)
    return val


def _gemini_json(prompt):
    from analyze import _call_gemini
    raw = _call_gemini(prompt, model=config.REFLECT_MODEL, purpose='demo')
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip())
    return json.loads(raw)


def _translate_list(strings, context):
    """一批中文短句 → 同样条数的英文。条数对不上就判失败（宁可不用，也别错位）。"""
    if not strings:
        return []
    prompt = (
        f'Translate each string in this JSON array into natural, concise English. {context}\n'
        'Keep names, numbers and any English words as they are. Do not add commentary.\n'
        'Return ONLY a JSON array of strings, same length and same order.\n\n'
        + json.dumps(strings, ensure_ascii=False))
    for _ in range(2):
        try:
            out = _gemini_json(prompt)
            if isinstance(out, list) and len(out) == len(strings) and all(isinstance(x, str) for x in out):
                return out
        except Exception as e:  # noqa: BLE001
            print('   translate retry:', str(e)[:120])
    return None


def _sections(summary):
    return [s for s in (summary or {}).get('sections') or [] if isinstance(s, dict)]


def _en_summary(summary, en):
    """把英文译文套回原摘要结构（保留时间段）；译文格式不对就返回 None（这条不带摘要）。"""
    src = _sections(summary)
    secs = (en.get('summary') or {}).get('sections') if isinstance(en.get('summary'), dict) else None
    if not summary or not isinstance(secs, list) or len(secs) != len(src) \
            or not all(isinstance(e, dict) for e in secs):
        return None
    return {'overview': en['summary'].get('overview', ''),
            'sections': [{**s, 'title': e.get('title', ''), 'summary': e.get('summary', '')}
                         for s, e in zip(src, secs)]}


def en_item(task_id, meta, summary, video_title):
    """一条转写的英文元数据：标题 / 一句话 / 标签 / 摘要。"""
    def make():
        src = {'video_title': video_title, 'ai_title': meta.get('ai_title') or '',
               'one_line': meta.get('ai_one_line') or '', 'tags': meta.get('ai_tags') or [],
               'summary': {'overview': (summary or {}).get('overview', ''),
                           'sections': [{'title': s.get('title', ''), 'summary': s.get('summary', '')}
                                        for s in _sections(summary)]}}
        prompt = (
            'This is metadata for an English-language video, but it was written in Chinese. '
            'Rewrite every field in natural English. Keep the same meaning and structure; '
            'sections must stay the same count and order. "ai_title" is a short descriptive title '
            '(under 10 words). "tags" are 2–4 short topic tags.\n'
            'Return ONLY JSON with keys: ai_title, one_line, tags, summary{overview, sections[{title, summary}]}.\n\n'
            + json.dumps(src, ensure_ascii=False))
        for _ in range(2):
            try:
                out = _gemini_json(prompt)
                if out.get('ai_title') and (not summary or _en_summary(summary, out)):
                    return out
            except Exception as e:  # noqa: BLE001
                print('   item retry:', str(e)[:120])
        return None
    return _cached(f'items/{task_id}', make)


# ---------- 资料库 ----------

def write_library(items, dst, src, keep_dates):
    linked = 0
    translated = 0
    for name, m, d in items:
        sdir = os.path.join(src, name)
        ddir = os.path.join(dst, name)
        os.makedirs(ddir)
        meta = dict(m)
        if not keep_dates:
            meta['original_date'] = m.get('date')
        meta['date'] = d.strftime('%Y-%m-%d %H:%M:%S')
        summary = None
        if os.path.isfile(os.path.join(sdir, 'summary.json')):
            with open(os.path.join(sdir, 'summary.json'), encoding='utf-8') as f:
                summary = json.load(f)
        video_title = re.sub(r'\s*\[[^\]]+\]\s*$', '', os.path.splitext(m.get('filename') or '')[0])
        en = en_item(name, m, summary, video_title)
        if en:
            translated += 1
            meta['ai_title'] = en['ai_title']
            meta['ai_one_line'] = en.get('one_line') or ''
            meta['ai_tags'] = [str(t) for t in (en.get('tags') or [])][:4]
            summary = _en_summary(summary, en)     # 译文坏了就不带摘要，别露出中文
        else:
            # 翻不出来：中文的那几项干脆不带，别在英文演示里露出中文
            meta['ai_title'] = video_title
            meta['ai_one_line'] = ''
            meta['ai_tags'] = []
            summary = None
        meta['has_summary'] = bool(summary)
        with open(os.path.join(ddir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        if summary:
            with open(os.path.join(ddir, 'summary.json'), 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
        for fn in ('transcript.json', 'transcript_raw.json'):
            if os.path.isfile(os.path.join(sdir, fn)):
                shutil.copy2(os.path.join(sdir, fn), os.path.join(ddir, fn))
        audio = 'audio' + (m.get('audio_ext') or '.ogg')
        if os.path.isfile(os.path.join(sdir, audio)):
            try:
                os.link(os.path.join(sdir, audio), os.path.join(ddir, audio))
                linked += 1
            except OSError:
                shutil.copy2(os.path.join(sdir, audio), os.path.join(ddir, audio))
    return linked, translated


# ---------- 博主链 ----------

_LAYER_EN = {'他的主张': 'Claim', '转写自证': 'From the transcript', '外部核实': 'Fact-checked'}


def _episode_md(title, cards):
    lines = [f'# {title}', '', '## Evidence cards']
    for c in cards:
        ts = f" [{c['timestamp'].strip('[]')}]" if c.get('timestamp') else ''
        layer = _LAYER_EN.get(c.get('layer'), 'Claim')
        q = c.get('quote', '')
        lines.append(f"- **{layer}** — {c.get('obs', '')}" + (f'  \n  “{q}”{ts}' if q else ''))
    return '\n'.join(lines) + '\n'


def write_chain(chain_id, src_results, dst_results, demo_task_ids):
    from analyze import LENSES, render_lens, synthesize
    sdir = os.path.join(src_results, '_chains', chain_id)
    ddir = os.path.join(dst_results, '_chains', chain_id)
    os.makedirs(ddir)
    with open(os.path.join(sdir, 'chain.json'), encoding='utf-8') as f:
        state = json.load(f)
    author = state.get('author') or 'Creator'
    titles = {v.get('task_id'): v.get('title') for v in state.get('videos', []) if v.get('task_id')}

    episodes = []
    for path in sorted(glob.glob(os.path.join(sdir, 'cards_*.json'))):
        with open(path, encoding='utf-8') as f:
            ep = json.load(f)
        cards = [c for c in ep.get('cards') or [] if isinstance(c, dict)]
        if not cards or ep.get('task_id') not in demo_task_ids:
            continue
        num = os.path.basename(path)[len('cards_'):-len('.json')]
        obs_en = _cached(f'chains/{chain_id}/cards_{num}', lambda: _translate_list(
            [c.get('obs', '') for c in cards],
            f'Each one is a neutral observation about what the creator "{author}" says or does in a video; '
            '"他"/"她" means the speaker — write "Argues that…", "Describes…", "The host…" etc.'))
        if not obs_en:
            print(f'   ! {author} {num}: card translation failed, episode skipped')
            continue
        en_cards = [{**c, 'obs': o, 'layer': c.get('layer')} for c, o in zip(cards, obs_en)]
        title = titles.get(ep.get('task_id')) or ep.get('title') or ''
        out = {**ep, 'title': title, 'cards': en_cards,
               'markdown': _episode_md(title, en_cards)}
        with open(os.path.join(ddir, f'cards_{num}.json'), 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        safe = re.sub(r'[\\/:*?"<>|]', '_', title)[:80]
        with open(os.path.join(ddir, f'分析_{num}_{safe}.md'), 'w', encoding='utf-8') as f:
            f.write(out['markdown'])
        episodes.append(out)
    if not episodes:
        shutil.rmtree(ddir)
        return None

    # 画像 + 五个镜头：拿英文证据卡、lang=en 重新生成（不是逐句翻译中文稿）
    # 画像和五个镜头互不依赖，并行跑（串行要十来分钟）
    from concurrent.futures import ThreadPoolExecutor
    with usage.scope(ref='demo-seed'):
        def _portrait():
            return _cached(f'chains/{chain_id}/portrait', lambda: synthesize(
                episodes, author=author, critique_level=state.get('critique_level') or 'analytical',
                preset='gemini', self_verify=False, lang='en'))

        def _lens(lens):
            return lens, _cached(f'chains/{chain_id}/lens_{lens}', lambda: render_lens(
                episodes, lens, author=author, preset='gemini', lang='en'))
        with ThreadPoolExecutor(max_workers=6) as pool:
            fut_p = pool.submit(usage.bound(_portrait))
            lens_out = list(pool.map(usage.bound(_lens), LENSES))
            portrait = fut_p.result()
    for lens, md in lens_out:
        if md:
            with open(os.path.join(ddir, f'镜头_{lens}.md'), 'w', encoding='utf-8') as f:
                f.write(md)
    if portrait:
        with open(os.path.join(ddir, '总分析.md'), 'w', encoding='utf-8') as f:
            f.write(portrait)

    # 合并原文：正文本来就是英文转写，只换掉中文标题行
    raw_src = os.path.join(sdir, '合并原文.md')
    if os.path.isfile(raw_src):
        with open(raw_src, encoding='utf-8') as f:
            raw = f.read().split('\n', 1)
        body = raw[1] if len(raw) > 1 else ''
        with open(os.path.join(ddir, '合并原文.md'), 'w', encoding='utf-8') as f:
            f.write(f'# {author} — all transcripts merged ({len(episodes)} episodes · plain text, no AI analysis)\n{body}')

    videos = [v for v in state.get('videos', []) if v.get('task_id') in demo_task_ids and v.get('status') == 'done']
    for i, v in enumerate(videos):
        v['index'] = i
    keep = ('id', 'url', 'engine', 'analyze', 'critique_level', 'author', 'avatar', 'followers',
            'created_at', 'finished_at', 'verify', 'self_verify', 'prefer_subs')
    demo_state = {k: state.get(k) for k in keep if k in state}
    demo_state.update({
        'stage': 'done', 'lang': 'en', 'analysis_preset': 'gemini', 'videos': videos,
        'download_total': len(videos), 'download_done': len(videos),
        'analyzed_done': len(episodes), 'analyzed_ok': len(episodes),
        'final_doc': '总分析.md' if portrait else None,
        'raw_doc': '合并原文.md' if os.path.isfile(raw_src) else None,
        # 已经探过频道信息，别在演示实例里再去打网络补
        'followers_checked': True, 'author_checked': True, 'avatar_checked': True,
    })
    with open(os.path.join(ddir, 'chain.json'), 'w', encoding='utf-8') as f:
        json.dump(demo_state, f, ensure_ascii=False, indent=2)
    return len(episodes), sum(len(e['cards']) for e in episodes)


DERIVED_FILES = ('tags.json', 'predictions.json', 'embeddings.npz', 'speakers.json',
                 'chat_starters.json', 'beliefs.json')


def main():
    keep_dates = '--keep-dates' in sys.argv
    max_items = int(sys.argv[sys.argv.index('--max') + 1]) if '--max' in sys.argv else 200
    random.seed(7)
    src = config.RESULTS_FOLDER
    dst = os.path.join(DEMO_DIR, 'results')

    # 博主链的每一期必须进资料库（证据卡要能点回原文）
    must = set()
    for cid in DEMO_CHAINS:
        with open(os.path.join(src, '_chains', cid, 'chain.json'), encoding='utf-8') as f:
            must |= {v['task_id'] for v in json.load(f).get('videos', [])
                     if v.get('task_id') and v.get('status') == 'done'}
    items = pick(src, max_items, must=must)
    print(f'picked {len(items)} English, public, non-sensitive transcripts ({len(must)} creator episodes)')
    if not keep_dates:
        items = spread_dates(items)
    else:
        items = [(n, m, datetime.strptime(m['date'][:19], '%Y-%m-%d %H:%M:%S')) for n, m in items]

    # 重建会整个删掉演示目录；博主页的派生数据（话题标签、预测核对、向量、说话人、开场问题）
    # 是另花钱算的，先挪出来，建完再放回去。ask.py 按卡片内容指纹认，卡变了的条目自己会作废重算
    stash = tempfile.mkdtemp(prefix='demo-derived-')
    for cid in DEMO_CHAINS:
        for name in DERIVED_FILES:
            p = os.path.join(dst, '_chains', cid, name)
            if os.path.isfile(p):
                os.makedirs(os.path.join(stash, cid), exist_ok=True)
                shutil.copy2(p, os.path.join(stash, cid, name))
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    linked, translated = write_library(items, dst, src, keep_dates)
    print(f'wrote {len(items)} transcripts to {dst} (English metadata: {translated}, audio hard-linked: {linked})')

    demo_ids = {n for n, _, _ in items}
    for cid in DEMO_CHAINS:
        got = write_chain(cid, src, dst, demo_ids)
        print(f'creator {cid[:8]}:', f'{got[0]} episodes, {got[1]} evidence cards' if got else 'skipped')
        ddir = os.path.join(dst, '_chains', cid)
        for name in DERIVED_FILES:
            kept = os.path.join(stash, cid, name)
            if got and os.path.isfile(kept) and not os.path.exists(os.path.join(ddir, name)):
                shutil.copy2(kept, os.path.join(ddir, name))
    shutil.rmtree(stash, ignore_errors=True)

    os.makedirs(os.path.join(DEMO_DIR, 'uploads'), exist_ok=True)
    if not keep_dates:
        ds = [d for _, _, d in items]
        print('dates:', min(ds).date(), '→', max(ds).date())


if __name__ == '__main__':
    main()
