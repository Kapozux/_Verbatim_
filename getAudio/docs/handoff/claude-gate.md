# 三个应用共用 Claude 额度的分配器（claude_gate）

## 速览
- **现状**：设了 `CLAUDE_GATE`（作者电脑：`~/.claude-gate/gate.db`）时，`claude_cli` 每次起 claude 进程前占位子（三边合计上限 24、留 4 个给问），用完记一行账；撞上限三边一起停。Ephemeris、墨页接了同一份。
- **怎么跑**：`./run_tests.sh claude_gate`；看用量：`python3 claude_gate.py`。
- **已知问题**：5 小时上限 $360 是从 10-13 一次撞上限反推的；每周上限还不知道。位子按进程数分、不按 token。
- **下一步**：下次撞上限时看 `python3 claude_gate.py` 的 5 小时合计，校准 `CLAUDE_GATE_LIMIT_5H`。

细节和为什么见 ADR 0018。
