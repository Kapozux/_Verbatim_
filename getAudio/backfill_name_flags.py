"""
一次性回填：给 results/ 里还没有 filename_meaningful 的老记录，让 Gemini Flash 判断
「原文件名本身是不是一个有意义的标题」，写回 meta.json。下载 .md 时靠这个字段决定
用原文件名还是 AI 标题。新转写的记录在 enrich 阶段就会顺带写好，不用再跑这个。

用法：python3 backfill_name_flags.py            # 只处理缺字段的
      python3 backfill_name_flags.py --force    # 全部重判
"""
import json
import os
import sys

import config
from enrich import display_stem, judge_filenames

BATCH = 80


def _merge_write(meta_path, updates):
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            m = json.load(f)
    except Exception:
        m = {}
    m.update(updates)
    tmp = meta_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path)


def main():
    force = '--force' in sys.argv
    root = config.RESULTS_FOLDER
    todo = []          # (meta_path, stem)
    for name in os.listdir(root):
        if name.startswith('_'):
            continue
        p = os.path.join(root, name, 'meta.json')
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                m = json.load(f)
        except Exception:
            continue
        if not force and 'filename_meaningful' in m:
            continue
        todo.append((p, display_stem(m.get('filename', ''))))
    print(f'{len(todo)} records to judge')

    done = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        names = list({stem for _, stem in chunk if stem})
        verdict = judge_filenames(names) or {}
        for p, stem in chunk:
            flag = verdict.get(stem)
            if flag is None:
                flag = len(stem) >= 6 and not stem.replace('-', '').replace('_', '').replace(' ', '').isdigit()
            _merge_write(p, {'filename_meaningful': bool(flag)})
            done += 1
        print(f'  {done}/{len(todo)}')
    print('done')


if __name__ == '__main__':
    main()
