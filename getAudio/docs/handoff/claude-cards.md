# 抽证据卡交给本机 Claude Haiku（CARDS_ENGINE=claude）

## 速览
- **现状**：设了 `CARDS_ENGINE=claude` 且有 `claude` 命令时，每期抽证据卡（`analyze._extract_cards`，`purpose='cards'`）先给 Claude Haiku 5.5（effort medium），失败照旧走项目的分析模型（DeepSeek / Gemini）；自己的模型不换。默认不开。
- **怎么跑**：`./run_tests.sh claude_cards`（6 项，假的 claude 事件流）。
- **已知问题**：比 DeepSeek 慢（一段 1～3 分钟），并发受 `CLAUDE_PARALLEL`（默认 6，和日常文字活共用）限制；几百期批量要几个小时。
- **下一步**：第一次大批量时看排队多久，需要就把 `CLAUDE_PARALLEL` 调到 12～20。

## 为什么
用户 10-13：「Verbatim 一次量太大可以 haiku（看性能）」。实测 5 期 Haiku medium 卡更多（254 vs 228）、原话对得上 99%（DeepSeek 89%），细节和表格见 ADR 0017。
实测脚本和产出在那次会话的 scratchpad（`vb-cards/`），没进仓库。
