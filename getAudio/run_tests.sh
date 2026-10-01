#!/bin/bash
# 跑全部后端测试：./run_tests.sh [文件名关键词…] [-v]
# 测试只用临时数据目录和假 key，禁止连外网，不会动你的资料库、不会花钱。
cd "$(dirname "$0")"
PY=python3
[ -x ../venv/bin/python ] && PY=../venv/bin/python
exec "$PY" tests/run.py "$@"
