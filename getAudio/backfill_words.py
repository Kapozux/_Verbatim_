"""给已有的期补逐词时间轴（wordtime，ADR 0012）：YouTube 视频拉 json3 字幕。不花钱，但 YouTube 会限流——
每期之间隔 gap 秒（默认 20，带随机抖动），连着两次 429 就停，过一阵再跑（已有 words.json 的跳过）。

    python3 backfill_words.py <chain_id> [--gap 20] [--limit N]
"""
import argparse
import glob
import json
import os
import random
import re
import time

import config
import wordtime

YT = re.compile(r'(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/)|youtu\.be/)[\w-]{11}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('chain_id')
    ap.add_argument('--gap', type=float, default=20)
    ap.add_argument('--limit', type=int, default=0)
    a = ap.parse_args()
    chain = json.load(open(glob.glob(os.path.join(config.RESULTS_FOLDER, '_chains', a.chain_id + '*', 'chain.json'))[0]))
    todo = [v for v in chain.get('videos') or [] if v.get('task_id') and YT.search(v.get('video_url') or '')
            and not wordtime.has(v['task_id'])]
    if a.limit:
        todo = todo[:a.limit]
    print(f'要补 {len(todo)} 期', flush=True)
    ok = fails = strikes = 0
    for k, v in enumerate(todo):
        if k:
            time.sleep(a.gap + random.uniform(0, a.gap / 2))
        n, why = wordtime.fetch_youtube(v['task_id'], v['video_url'])
        title = (v.get('title') or '')[:50]
        if n:
            ok += 1
            strikes = 0
            print(f'[{k + 1}/{len(todo)}] {n} 词  {title}', flush=True)
            continue
        fails += 1
        print(f'[{k + 1}/{len(todo)}] 没拿到（{why}）  {title}', flush=True)
        if why and '429' in why:
            strikes += 1
            if strikes >= 2:
                print('连着两次 429，停手；过一阵再跑', flush=True)
                break
            time.sleep(a.gap * 6)
    print(f'完成：{ok} 期有了，{fails} 期没拿到', flush=True)


if __name__ == '__main__':
    main()
