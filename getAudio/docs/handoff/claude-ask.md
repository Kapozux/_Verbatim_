# 问证据卡交给本机 Claude Code 写

## 速览
- **现状**：设了 `ASK_ENGINE=claude` 且电脑上有 `claude` 命令时，「问证据卡」（流式和非流式）由本机 Claude Code（Opus 5.5）写；挑卡、出处校验、改写语言照旧。默认不开，别人装的 Verbatim 行为不变。10-13 合进 main、5001 已重启，作者的 `.env` 已开。
- **怎么跑**：`./run_tests.sh claude_ask`（11 项，假的 claude 事件流，不调真 Claude）；真用：`.env` 里加 `ASK_ENGINE=claude`、重启 5001。
- **已知问题**：Claude 写了一半断了（超时 / 撞上限）会跟原来的模型中途出错一样报错，不会自动改用别的模型续写；每问冷启动约 3～5 秒才出第一个字。
- **下一步**：工作台 / 闪卡 / 回顾这些也走 Claude？（现在只接了 `ask.answer` / `ask.answer_stream`）；像 Ephemeris 那样让 Claude 自己翻转写原文的「深挖」模式。

## 怎么接的
- `claude_cli.py`：`claude -p` 精简模式（自己的系统提示、`--tools ""`、`--setting-sources ""`、`--strict-mcp-config`、不留会话、`--output-format stream-json --include-partial-messages`）。一次性调用设 `DISABLE_PROMPT_CACHING=1`：Claude Code 默认把输入写进 1 小时缓存（价钱是普通输入的 2 倍），之后又没人读，Ephemeris 实测同样的输入贵一倍。
- 同时最多 `CLAUDE_PARALLEL`（默认 3）个进程；撞上限 / 没登录：停到重置时间（读得到）或 30 分钟，期间 `available()` 为假，问答自动走原来的模型。
- `ask.answer_stream`：`claude_cli.engine()` 为真就先用 Claude 流式写；还没出字就失败 → 照旧 `_call_gemini_stream`；写了一半失败 → 往上抛（跟原来一样）。`ask.answer` 同理（失败回 `_llm`）。结果的 `model` 字段是 `claude-opus-5-5`。
- 记账：`usage.record('claude-code', …, cost_usd=0.0)`——用户的订阅额度，不实扣；token 照记（缓存写入算进 input）。

## 实测（10-13）
- 「美投讲美股」问「他对买个股和买指数怎么看？」：4.8 秒出第一个字，12.5 秒写完，11 个出处、0 个编造。
- Ephemeris 那边同样走 Claude 的问答，一问等值 $0.2～0.3（订阅额度）。
