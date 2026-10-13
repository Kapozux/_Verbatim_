# 日常文字活交给本机 Claude Code（TEXT_ENGINE=claude）

## 速览
- **现状**：设了 `TEXT_ENGINE=claude` 且有 `claude` 命令时，所有文字活先给本机 Claude Code：`llmroute` 的用途、经 `_call_gemini` 的（播客、幻灯片、声纹、小红书报告……）、`analyze._llm` 的（画像合成 / 镜头 / 简报 / 证伪，不管项目选哪家）。Opus 管要判断和写长文的（`llmroute.OPUS_PURPOSES`，大输入用 1M 上下文版），其余 Haiku；失败照旧 DeepSeek → Gemini。联网核实给 Opus + WebSearch，看图给 Haiku。不走：抽卡（`CARDS_ENGINE`）、自己的模型。默认不开；作者电脑 10-13 打开。
- **怎么跑**：`./run_tests.sh claude_text_route claude_all_text claude_vision`（8 项，假的 claude 事件流）。
- **已知问题**：每次调用要起一个 `claude` 进程（Haiku 一段摘要约 3 秒）；同时最多 `CLAUDE_PARALLEL`（默认 6）个，批量转写几百期时摘要会排队，比 DeepSeek 高并发慢。
- **下一步**：批量转写时看实际排队多久，需要就调大 `CLAUDE_PARALLEL`（墨页实测 40 路都没被限流，每进程约 230MB）。

## 为什么
用户 10-13：「开销越少越好，订阅额度必须都用上」。订阅额度不另花钱；Haiku 在墨页认字上比 Kimi 准、额度极省（每页约 $0.001 等值）。
抽证据卡、画像不归 llmroute，仍按项目的分析模型（DeepSeek / Gemini），用户 10-13 定的。

## 实测（10-13）
- 一句话摘要：3.2 秒，Haiku，输入 636 / 输出 47 token，记 claude-code $0。
