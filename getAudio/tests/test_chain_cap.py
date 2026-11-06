"""频道最多处理几期：默认 600（防手滑），.env 里 MAX_CHAIN_VIDEOS 可以调高（11-06：邵艾伦 679 期要全转）。"""
import os
import subprocess
import sys

from _support import Checks, isolate

isolate('chaincap')
t = Checks()
code = "import app; print(app._MAX_CHAIN_VIDEOS)"
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def cap(v):
    env = dict(os.environ, MAX_CHAIN_VIDEOS=v)
    return subprocess.run([sys.executable, '-c', code], cwd=here, env=env, capture_output=True, text=True).stdout.strip().splitlines()[-1]


t.check('默认 600', cap('') == '600', cap(''))
t.check('.env 调到 1000', cap('1000') == '1000')
t.finish()
