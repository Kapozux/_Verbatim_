"""把正本 getAudio/claude_gate.py 复制到另外几个项目的副本（项目之间不互相 import，所以各放一份）。

    python3 getAudio/scripts/sync_gate.py           # 同步
    python3 getAudio/scripts/sync_gate.py --check   # 只看哪份不一样（不一样退出码 1）

副本的位置按主仓库（CODEelse）算：在 worktree 里跑也同步到主目录下的那几份。
墨页的 Node 版（~/Documents/Playground/pdf2md-web/server/claude-gate.mjs）是手写的，不在这里；只要表结构不变就不用改。
"""
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # getAudio
SRC = os.path.join(HERE, 'claude_gate.py')
TARGETS = ('ephemeris/src/claude_gate.py', 'uni-globe/server/claude_gate.py')


def root():
    """主仓库根目录（worktree 的话是它所属的那个）。"""
    try:
        common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=HERE,
                                capture_output=True, text=True, check=True).stdout.strip()
        return os.path.dirname(common)
    except (OSError, subprocess.CalledProcessError):
        return os.path.dirname(HERE)


def digest(p):
    with open(p, 'rb') as f:
        return hashlib.md5(f.read()).hexdigest()


def main():
    check = '--check' in sys.argv
    want, base, drift = digest(SRC), root(), 0
    for rel in TARGETS:
        p = os.path.join(base, rel)
        if not os.path.exists(os.path.dirname(p)):
            print(f'  跳过（没有这个项目）：{rel}')
            continue
        if os.path.exists(p) and digest(p) == want:
            print(f'  一样：{rel}')
            continue
        drift += 1
        if check:
            print(f'  不一样：{rel}')
        else:
            shutil.copyfile(SRC, p)
            print(f'  已同步：{rel}')
    return 1 if check and drift else 0


if __name__ == '__main__':
    sys.exit(main())
