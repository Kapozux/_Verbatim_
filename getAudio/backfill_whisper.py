"""给 gemini35 的老期补逐词时间轴并改好段落时间（10-04：gemini35 的段落时间不可信，短文件大多只有真实的 1/3）。

每期：YouTube 来源先拿 json3 字幕（免费、快）；拿不到或 B 站 → 重新下载音频 → 本机 Whisper 带逐词时间转一遍
→ 存 words.json → 按时间轴改 transcript.json 的段落时间（原时间留在 model_timestamp）→ 删音频。不花钱。
已有 words.json 的只补改段落时间。本地上传、没有链接的期没法重下，跳过。
下载连着失败 3 次（多半被 B 站限流）就歇 30 分钟再试，歇满 3 轮还不行就停手。可以随时中断、重跑接着来。

    GETAUDIO_DATA_DIR=<资料库目录> python3 backfill_whisper.py [--limit N] [--gap 4]
"""
import argparse
import glob
import json
import os
import queue
import random
import shutil
import tempfile
import threading
import time

import config
import downloader
import wordtime


def todo_list():
    rows = []
    for p in glob.glob(os.path.join(config.RESULTS_FOLDER, '*', 'meta.json')):
        try:
            with open(p, encoding='utf-8') as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if m.get('engine') != 'gemini35':
            continue
        tid = os.path.basename(os.path.dirname(p))
        tpath = os.path.join(os.path.dirname(p), 'transcript.json')
        try:
            with open(tpath, encoding='utf-8') as f:
                segs = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(segs, list) and segs and all('model_timestamp' in s for s in segs):
            continue                                # 改过了
        if not m.get('source_url') and not wordtime.has(tid):
            continue                                # 本地上传、没有链接：没法重下
        rows.append((m.get('date') or '', tid, m.get('source_url') or '', m.get('video_id') or '',
                     (m.get('ai_title') or m.get('filename') or '')[:40]))
    rows.sort(reverse=True)                         # 新的先来
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--gap', type=float, default=4, help='两次下载之间至少隔几秒（加随机抖动）')
    a = ap.parse_args()
    rows = todo_list()
    if a.limit:
        rows = rows[:a.limit]
    print(f'数据目录 {config.DATA_DIR}；要处理 {len(rows)} 期', flush=True)
    work = tempfile.mkdtemp(prefix='backfill_whisper_')
    ready = queue.Queue(maxsize=1)          # 边转边下：Whisper 转这一期时，下一期已经在下载
    stats = {'ok': 0, 'skipped': 0, 'failed': 0}

    def fetch():
        """下载线程：拿 YouTube 字幕或下载音频，放进 ready；Whisper 那边转完一期才会腾出位置。"""
        strikes = rests = 0
        try:
            for k, (_, tid, url, vid, title) in enumerate(rows, 1):
                tag = f'[{k}/{len(rows)}] {title}'
                if not wordtime.has(tid) and url and wordtime.is_youtube(url):
                    n, _why = wordtime.fetch_youtube(tid, url)
                    if n:
                        print(f'{tag}  YouTube 字幕 {n} 词', flush=True)
                if wordtime.has(tid):
                    ready.put((tag, tid, None))
                    continue
                time.sleep(a.gap + random.uniform(0, a.gap))
                d = os.path.join(work, tid)
                info = None
                try:
                    info = downloader.download_one({'video_url': url, 'video_id': vid}, d)
                except Exception as e:  # noqa: BLE001
                    print(f'{tag}  下载出错：{str(e)[:120]}', flush=True)
                if not info or not os.path.isfile(info.get('path') or ''):
                    stats['failed'] += 1
                    strikes += 1
                    shutil.rmtree(d, ignore_errors=True)
                    print(f'{tag}  下载失败（连续 {strikes} 次）', flush=True)
                    if strikes >= 3:
                        rests += 1
                        if rests > 3:
                            print('歇了 3 轮还是下不来，停手；过一阵重跑会接着来', flush=True)
                            break
                        print('连着失败 3 次，歇 30 分钟', flush=True)
                        time.sleep(1800)
                        strikes = 0
                    continue
                strikes = 0
                ready.put((tag, tid, info['path']))
        finally:
            ready.put(None)

    threading.Thread(target=fetch, name='backfill-fetch', daemon=True).start()
    try:
        while True:
            item = ready.get()
            if item is None:
                break
            tag, tid, path = item
            t0 = time.time()
            if path:
                try:
                    words = wordtime.whisper_words(path)
                    if words:
                        wordtime.save(tid, words, source='whisper')
                except Exception as e:  # noqa: BLE001
                    print(f'{tag}  Whisper 出错：{str(e)[:120]}', flush=True)
                finally:
                    shutil.rmtree(os.path.dirname(path), ignore_errors=True)
            n = wordtime.apply_retime(tid)
            if n:
                stats['ok'] += 1
                print(f'{tag}  改好 {n} 段（{time.time() - t0:.0f} 秒）', flush=True)
            else:
                stats['failed'] += 1
                print(f'{tag}  时间轴里一段都没对上，没改', flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"完成：改好 {stats['ok']} 期，失败 {stats['failed']} 期", flush=True)


if __name__ == '__main__':
    main()
