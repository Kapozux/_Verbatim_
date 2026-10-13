# 日常文字活交给本机 Claude Code（TEXT_ENGINE=claude）

## 速览
- **现状**：设了 `TEXT_ENGINE=claude` 且有 `claude` 命令时，`llmroute` 管的用途（摘要、标签、标题、翻译、说话人、复习工具、笔记、回顾、对比、立场……）先给本机 Claude Code：提取类用 Haiku 5.5，`OPUS_PURPOSES`（review / reflect / digest / compare / beliefs）用 Opus 5.5；失败照旧 DeepSeek → Gemini。默认不开；作者电脑 10-13 打开。
- **怎么跑**：`./run_tests.sh claude_text_route`（8 项，假的 claude 事件流）。
- **已知问题**：每次调用要起一个 `claude` 进程（Haiku 一段摘要约 3 秒）；同时最多 `CLAUDE_PARALLEL`（默认 6）个，批量转写几百期时摘要会排队，比 DeepSeek 高并发慢。
- **下一步**：批量转写时看实际排队多久，需要就调大 `CLAUDE_PARALLEL`（墨页实测 40 路都没被限流，每进程约 230MB）。

## 为什么
用户 10-13：「开销越少越好，订阅额度必须都用上」。订阅额度不另花钱；Haiku 在墨页认字上比 Kimi 准、额度极省（每页约 $0.001 等值）。
抽证据卡、画像不归 llmroute，仍按项目的分析模型（DeepSeek / Gemini），用户 10-13 定的。

## 实测（10-13）
- 一句话摘要：3.2 秒，Haiku，输入 636 / 输出 47 token，记 claude-code $0。
