"""
一次性回填：给 results/ 里还没有 timing 字段的老记录，用 taskdb 的 created_at → updated_at
时间差写一个近似耗时 {'wall_s': 秒, 'approx': True}。

这个差值**含排队等待**（批量链条一次投 100 条，后面的会等很久），所以只能算「大概多久拿到结果」，
不能当引擎速度。统计面板把 approx 单独一行浅色显示、预估不用它。
新转写在 run_transcription 里就会写好精确的分阶段 timing，不用再跑这个。

用法：python3 backfill_timing.py           # 只补缺 timing 的
      python3 backfill_timing.py --dry     # 只数不写
"""
import json
import os
import sys
from datetime import datetime

import config
import taskdb


def main():
    dry = '--dry' in sys.argv
    root = config.RESULTS_FOLDER
    done = wrote = skipped = 0
    import sqlite3
    conn = sqlite3.connect(taskdb.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, created_at, updated_at FROM tasks WHERE status='done'").fetchall()
    for r in rows:
        done += 1
        p = os.path.join(root, r['id'], 'meta.json')
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                m = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if m.get('timing') or m.get('engine') == 'subtitle':
            skipped += 1
            continue
        try:
            wall = (datetime.fromisoformat(r['updated_at'])
                    - datetime.fromisoformat(r['created_at'])).total_seconds()
        except Exception:  # noqa: BLE001
            continue
        if wall <= 0:
            continue
        m['timing'] = {'wall_s': round(wall, 1), 'approx': True}
        if not dry:
            tmp = p + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(m, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        wrote += 1
    print(f'done tasks in db: {done}; already had timing / subtitle: {skipped}; '
          f'{"would write" if dry else "wrote"}: {wrote}')


if __name__ == '__main__':
    main()
