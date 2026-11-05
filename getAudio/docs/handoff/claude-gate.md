# 三个应用共用 Claude 额度的分配器（claude_gate）

## 速览
- **现状**：设了 `CLAUDE_GATE`（作者电脑：`~/.claude-gate/gate.db`）时，各应用起 claude / codex / agy 进程前占位子、用完记一行账；撞上限那一家三边一起停。三家：Claude（上限 24、留 4 个给问、给用户留周额度）、GPT（codex，4 / 1）、Gemini（agy，6 / 1，11-05 加；看 `agy -p /usage` 的真数，应用最多用到剩 40%，交互的到 10%）。接的：Verbatim、Ephemeris、墨页、uni-globe（atlas）。
- **怎么跑**：`./run_tests.sh claude_gate`；看用量：`python3 claude_gate.py`。**正本是 `getAudio/claude_gate.py`**，改完 `python3 scripts/sync_gate.py` 同步到 Ephemeris / uni-globe（`--check` 只看）；墨页 Node 版手写，表结构不变就不用改。
- **已知问题**：codex 的额度只能从用户自己用 Codex 留下的会话读（应用的调用不留会话），所以只显示、不拦。agy 留几成是 11-05 先按 40% 定的，用户没给数（`CLAUDE_GATE_AGY_KEEP` 改）。
- **下一步**：hub（5058）加一个三家额度的面板；Ephemeris / 墨页如果也用 agy，照 Verbatim 的 `transcribe_agy._ask` 包一层 slot / record。

细节和为什么见 ADR 0018。
