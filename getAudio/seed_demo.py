"""
生成「演示工作区」：给外界看的 Verbatim 数据目录，只放公开来源、非敏感的转写。

  python3 seed_demo.py                 # 写到 ~/Documents/CODEelse/verbatim-demo
  python3 seed_demo.py --keep-dates    # 保留真实转写日期（默认会把日期均匀铺到最近 90 天，回顾面板才好看）
  python3 seed_demo.py --max 200       # 最多放多少条（默认 200）

筛选规则：必须有 source_url（公开视频）和 AI 标题；标签或标题命中敏感词的整条跳过
（时政 / 政治人物 / 两性情感 / 短剧 等）。音频用硬链接，不占第二份磁盘。
链条（博主分析）、上传目录、任务库都不带过去——演示实例是只读的。
"""
import json
import os
import random
import re
import shutil
import sys
from datetime import datetime, timedelta

import config

DEMO_DIR = os.path.expanduser('~/Documents/CODEelse/verbatim-demo')
BLOCK_TAGS = re.compile(r'时政|政治|权力|中共|两性|情感|亲密|婚|恋|性|国际关系|社会评论|社会议题|外交|军|台湾|香港|新疆|历史分析|人物分析|深度分析|社会观察|社会分析|案例分析|心理分析|短剧|CP')
BLOCK_TEXT = re.compile(r'习近平|习李|中共|共产党|政治|政权|领导人|薄熙来|王沪宁|王岐山|李克强|周永康|张又侠|军队|清洗|台湾|香港|新疆|移民|出入境|中南海|外交部|习|党')


def pick(results_dir, max_items):
    keep = []
    for name in sorted(os.listdir(results_dir)):
        p = os.path.join(results_dir, name, 'meta.json')
        if name.startswith('_') or not os.path.isfile(p):
            continue
        try:
            m = json.load(open(p, encoding='utf-8'))
        except Exception:
            continue
        if not m.get('source_url') or not m.get('ai_title') or not m.get('duration_seconds'):
            continue
        if BLOCK_TAGS.search(' '.join(m.get('ai_tags') or [])):
            continue
        if BLOCK_TEXT.search((m.get('ai_title') or '') + (m.get('ai_one_line') or '') + (m.get('filename') or '')):
            continue
        keep.append((name, m))
    keep.sort(key=lambda x: x[1].get('date', ''))
    if len(keep) > max_items:
        step = len(keep) / max_items
        keep = [keep[int(i * step)] for i in range(max_items)]
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


def main():
    keep_dates = '--keep-dates' in sys.argv
    max_items = int(sys.argv[sys.argv.index('--max') + 1]) if '--max' in sys.argv else 200
    random.seed(7)
    src = config.RESULTS_FOLDER
    dst = os.path.join(DEMO_DIR, 'results')
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    items = pick(src, max_items)
    print(f'picked {len(items)} of public, non-sensitive transcripts')
    if not keep_dates:
        items = spread_dates(items)
    else:
        items = [(n, m, datetime.strptime(m['date'][:19], '%Y-%m-%d %H:%M:%S')) for n, m in items]

    linked = 0
    for name, m, d in items:
        sdir = os.path.join(src, name)
        ddir = os.path.join(dst, name)
        os.makedirs(ddir)
        meta = dict(m)
        if not keep_dates:
            meta['original_date'] = m.get('date')
        meta['date'] = d.strftime('%Y-%m-%d %H:%M:%S')
        with open(os.path.join(ddir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        for fn in ('transcript.json', 'summary.json', 'transcript_raw.json'):
            if os.path.isfile(os.path.join(sdir, fn)):
                shutil.copy2(os.path.join(sdir, fn), os.path.join(ddir, fn))
        audio = 'audio' + (m.get('audio_ext') or '.ogg')
        if os.path.isfile(os.path.join(sdir, audio)):
            try:
                os.link(os.path.join(sdir, audio), os.path.join(ddir, audio))
                linked += 1
            except OSError:
                shutil.copy2(os.path.join(sdir, audio), os.path.join(ddir, audio))
    if os.path.isfile(os.path.join(src, '_tagmap.json')):
        shutil.copy2(os.path.join(src, '_tagmap.json'), os.path.join(dst, '_tagmap.json'))
    os.makedirs(os.path.join(DEMO_DIR, 'uploads'), exist_ok=True)
    print(f'wrote {len(items)} transcripts to {dst} (audio hard-linked: {linked})')
    if not keep_dates:
        ds = [d for _, _, d in items]
        print('dates:', min(ds).date(), '→', max(ds).date())


if __name__ == '__main__':
    main()
