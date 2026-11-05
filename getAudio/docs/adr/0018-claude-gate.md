# 0018 三个应用共用一份 Claude 额度：分配器（claude_gate）

日期：2026-10-13

## 背景

0015～0017 之后 Verbatim 的文字活、看图、抽卡都先走本机 Claude；同一台电脑上 Ephemeris、墨页也一样，三边合起来可能同时开四五十个 claude 进程，
用的是同一份订阅额度。各自的并发上限、撞上限后的「停一阵」都只管自己：一边撞了，另外两边还会各撞一遍；批量抽卡跑满时，问一句也要排队；
也没有地方看三边一共用了多少。用户 10-13：「有一个监控所有 usage 的分配器」。

## 决定

1. 不起常驻服务。三边读写同一个 SQLite（`CLAUDE_GATE`，作者电脑上是 `~/.claude-gate/gate.db`）；没设就整个不起作用。
2. 起 claude 进程前占位子（`claude_gate.slot`）：总数上限 `CLAUDE_GATE_CAP`（默认 24），留 `CLAUDE_GATE_RESERVE`（默认 4）个只给交互的活（live）。
   Verbatim：问 = live；抽卡 / 摘要 / 标签 / 体检 / 看图 / 简报 / 证伪 = batch；其余 normal。
3. 给人自己留额度（同日补，用户：「给我自己留 100 吧」）：`CLAUDE_GATE_LIMIT_5H` 是 5 小时上限折合的等值美元（10-13 上午撞上限时，那个窗口里 Claude Code 合计约 $362，作者电脑设 360）；「三个应用 + 人自己在 Claude Code 里用的」合计到了 LIMIT - `CLAUDE_GATE_KEEP`（默认 100），normal / batch 等着，live 照常。人自己的用量增量读 `~/.claude/projects` 的对话记录（每个文件记读到哪，一分钟最多读一次，首次约 2 秒），应用自己起的会话目录（`~/.verbatim-claude` 等）不算——它们已经记在 calls 里。等值价按 claude -p 的 total_cost_usd 反推：Haiku 5.5 $0.10 / $0.50、Sonnet 5.5 $2 / $10、Opus 5.5 $4 / $20（每百万 token 入 / 出）。
4. 谁撞上限谁写共用的 pause，三边都停到重置时间、各自走原来的模型。
5. 每次调用记一行（应用、用途、档、模型、token、等值美元、秒数、成没成）；`python3 claude_gate.py` 看现在谁占着、5 小时 / 7 天各用了多少。
6. 代码各放一份（Verbatim、Ephemeris 是同一个 `claude_gate.py`，墨页是 Node 版 `server/claude-gate.mjs`），表结构一样，改要三处一起改。

## 后果

- 各自原来的并发上限（`CLAUDE_PARALLEL` 等）照旧在，分配器管的是三边加起来。
- 占不到位子最多等 30 分钟，再等不到就当 Claude 用不了、走原来的模型（会花钱，日志里有）。

## 补充（2026-11-05）：第三家 agy，正本 + 同步脚本

用户：「能不能同时让多个项目都能监控，不只有 claude gate，应该加入 Codex + AG（Gemini）」。

- 加 provider `agy`（Antigravity CLI，用户的 Gemini 订阅）：并发 `CLAUDE_GATE_CAP_AGY` / `CLAUDE_GATE_RESERVE_AGY`（6 / 1）；`slot(provider='agy', model=…)` 按模型看额度桶（Argon 一个周桶；Flash / Pro 看 Gemini 的 5 小时 + 周桶）。
- 用户平时自己也用 Gemini，所以给他留：桶剩得少于 `CLAUDE_GATE_AGY_KEEP`（默认 0.4）普通 / 批量的活就不起，交互的到 `CLAUDE_GATE_AGY_KEEP_LIVE`（0.1）。读数来自 `agy -p /usage`（不花额度），五分钟最多探一次，存在 meta 里三边共用；探不到不挡。
- 表结构不变（读数放 meta，账记在 calls、provider = agy、usd 0），墨页 Node 版不用跟着改。
- 副本变成四份（加了 uni-globe），手动三处一起改容易漏：定 `getAudio/claude_gate.py` 为正本，`scripts/sync_gate.py` 复制过去、`--check` 查有没有不一样。
