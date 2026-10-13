# 0016 日常文字活先交给本机 Claude Code（只在自己电脑上开）

日期：2026-10-13

## 背景

0015 把「问证据卡」交给了本机 Claude Code。用户接着要求：日常的文字活（每期摘要、标签、回顾……）也用订阅额度，真金白银越少越好。
这些活都经过 `llmroute`（原来是 DeepSeek → Gemini）。墨页同日实测 Haiku 5.5 认字比 Kimi 准、355 页连跑没被订阅限流。

## 决定

1. 开关 `TEXT_ENGINE=claude`（默认空 = 跟以前一样）。开了、且有 `claude` 命令时，`llmroute.text / stream` 先试 Claude，失败照旧往下走。
2. 按活分模型：提取、照着材料写的用 Haiku 5.5；要判断、找主线的（`OPUS_PURPOSES`：review / reflect / digest / compare / beliefs）用 Opus 5.5。
3. 记账 claude-code / $0，token 照记（同 0015）。
4. 抽证据卡、画像不归 llmroute，不受影响。
5. 测试隔离把 `ASK_ENGINE` / `TEXT_ENGINE` 显式置空：`.env` 里开着会让测试真去起 claude 进程（禁外网拦不住子进程）。

## 后果

- 别人装的 Verbatim、打包版、演示实例不受影响。
- 批量时受 `CLAUDE_PARALLEL`（默认 6）限制，会比 DeepSeek 慢；钱是 0。
