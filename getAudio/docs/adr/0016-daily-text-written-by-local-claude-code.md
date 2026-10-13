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

## 补充（同日）：扩到所有文字活

用户：「画像也可以 Claude 吧（类似所有文字处理的）」。所以 Claude 这一步不再只管 `llmroute.PURPOSES`：
- 经过 `_call_gemini` 的（`ask._llm` 的播客 / 幻灯片 / 声纹命名 / 学习……、小红书报告）和 `analyze._llm` 的（画像合成 `synth`、镜头 `lens`、批次简报 `brief`、证伪 `verify` / 改画像 `revise`，不管项目选 DeepSeek、OpenRouter 还是 Gemini）都先给 Claude。
- Opus（effort medium）：review / reflect / digest / compare / beliefs / synth / lens / revise / podcast / slides / xhs；输入超过 30 万字符用 `claude-opus-5-5[1m]`（1M 上下文，画像材料能到 60 万字）。其余 Haiku（effort low）。
- 不走：联网核实（grounded）、抽证据卡（`CARDS_ENGINE`，0017）、看图（`_call_gemini_mm`）、用户自己的模型（0013）。
- 改画像那一步原来记账用途也叫 `verify`，改叫 `revise`，才分得出 Opus 档。`claude_cli.TIMEOUT` 600 → 1200 秒。
- 联网核实（grounded：画像的事实核查脚注 `factcheck`、预测核验 `predict`）也先给 Claude：Opus（effort medium）+ Claude Code 自带的 `WebSearch` 工具（`--tools WebSearch --allowedTools WebSearch`，只给这一个）。10-13 实测一条预测 14.5 秒、给出判断和来源网址。失败照旧 Gemini + Google 搜索。
- 看图（`analyze._call_gemini_mm`：视频截帧 `visual`、读书笔记里的图、小红书笔记 `xhs`）也先给 Claude Haiku（`claude_cli.vision`：图片 + 提示词一条消息走 `--input-format stream-json`，不给工具；单张超过约 3.7MB 的不送，一张都送不了就直接 Gemini）。10-13 实测一帧图表 4.1 秒、坐标轴和标注都读对。失败照旧 Gemini。
