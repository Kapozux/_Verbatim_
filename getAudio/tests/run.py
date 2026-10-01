"""跑全部测试：tests/ 下每个 test_*.py 单独一个进程（原因见 _support.py），并行跑，最后汇总。

    ./run_tests.sh               全部
    ./run_tests.sh people study  只跑文件名里带这些词的
    ./run_tests.sh -v            失败的之外也打印全部输出
"""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = int(os.environ.get('TEST_TIMEOUT', '600'))


def run_one(path):
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, '-u', path], cwd=HERE, capture_output=True, text=True,
                           timeout=TIMEOUT, env=dict(os.environ, PYTHONWARNINGS='ignore'))
        return path, p.returncode, p.stdout + p.stderr, time.time() - t0
    except subprocess.TimeoutExpired as e:
        return path, -1, (e.stdout or '') + f'\n超时（{TIMEOUT}s）', time.time() - t0


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    verbose = '-v' in sys.argv
    files = sorted(os.path.join(HERE, f) for f in os.listdir(HERE)
                   if f.startswith('test_') and f.endswith('.py') and (not args or any(a in f for a in args)))
    if not files:
        print('没有匹配的测试')
        return 1
    with ThreadPoolExecutor(max_workers=min(6, len(files))) as pool:
        results = list(pool.map(run_one, files))
    failed = 0
    for path, code, out, dt in results:
        name = os.path.basename(path)
        summary = next((ln for ln in reversed(out.splitlines()) if ln.startswith('结论')), '').strip()
        status = '通过' if code == 0 else '失败'
        print(f'{status}  {name:<28} {dt:5.1f}s  {summary}')
        if code != 0:
            failed += 1
        if code != 0 or verbose:
            print('\n'.join('    ' + ln for ln in out.splitlines() if 'Warning' not in ln and 'warnings.warn' not in ln))
    print(f'\n{len(files) - failed}/{len(files)} 个测试文件通过')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
