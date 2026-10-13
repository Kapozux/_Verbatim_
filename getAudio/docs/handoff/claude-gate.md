# 三个应用共用 Claude 额度的分配器（claude_gate）

## 速览
- **现状**：设了 `CLAUDE_GATE`（作者电脑：`~/.claude-gate/gate.db`）时，`claude_cli` 每次起 claude 进程前占位子（三边合计上限 24、留 4 个给问），用完记一行账；撞上限三边一起停。Ephemeris、墨页接了同一份。
- **怎么跑**：`./run_tests.sh claude_gate`；看用量：`python3 claude_gate.py`。
- **已知问题**：还不知道订阅 5 小时 / 每周上限折合多少等值美元，`CLAUDE_GATE_BATCH_5H` 没设；位子是按进程数分的，不按 token。
- **下一步**：攒几天账，下次撞上限时看 `python3 claude_gate.py` 的 5 小时合计，按它的八成设批量预算。

细节和为什么见 ADR 0018。
