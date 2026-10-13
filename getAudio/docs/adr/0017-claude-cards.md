# 0017 每期抽证据卡可以交给本机 Claude Haiku（CARDS_ENGINE=claude）

日期：2026-10-13

## 背景

0016 让日常文字活先走 Claude，但抽证据卡不归 llmroute，还是 DeepSeek / Gemini。抽卡是这条流水线上量最大的调用：
10-10 立党 520 期一天 888 次 OpenRouter DeepSeek（$1.6）+ 470 次 Gemini 2.5 Flash（deepseek_guard 改道的时政期，$33）。
用户 10-13：「Verbatim 一次量太大可以 haiku（看性能）」。

## 实测（10-13，同一段转写、同一个提示词，只测不写库）

| 期 | 现有 DeepSeek 卡 / 原话对得上 | Haiku low | Haiku medium |
|---|---|---|---|
| 立党 · YC 家庭（单段） | 15 / 9 | 66 / 65 | 68 / 68 |
| 润学讲义 001 | 10 / 9 | 20 / 20 | 23 / 23 |
| 留学中介 | 19 / 14 | 22 / 19 | 31 / 30 |
| 立党直播（第 1/3 段） | 160 / 158 | 76 / 73 | 116 / 115 |
| 经济学辅导课（第 1/4 段） | 24 / 14 | 17 / 15 | 16 / 16 |
| 合计 | 228 / 204（89%） | 201 / 192（96%） | 254 / 252（99%） |

DeepSeek 当时读的是体检删过句的转写，对不上的有一部分是这个原因。抽样读：Haiku 的 obs 更具体（经济课里 DeepSeek 把「给你们两分钟读一下」也抽成了卡，Haiku 抽的是得分点和计算步骤）。
Haiku medium 每段 30～170 秒，10 段等值 $0.10（订阅额度，不花钱）。

## 决定

1. 开关 `CARDS_ENGINE=claude`（默认空 = 跟以前一样）。开了、且有 `claude` 命令时，`analyze._llm` 的 `purpose='cards'` 先给 Claude Haiku 5.5（effort medium）。
2. 失败（没登录、撞上限、报错）照旧走原来的模型；撞上限后 Claude 停到重置时间（同 0015）。
3. 用户自己的模型（custom）选了就不换（0013）。
4. 体检（review）、画像合成等其他用途不受这个开关影响。
5. 记账 claude-code / $0，token 照记。

## 后果

- 时政期也不用再改走 Gemini（Claude 不审查这类内容），省下 deepseek_guard 那部分钱。
- 比 DeepSeek 慢：一段 45 分钟转写 Haiku 要 1～3 分钟，同时最多 `CLAUDE_PARALLEL`（默认 6，跟日常文字活共用）个进程。几百期的批量要几个小时；需要就调大 `CLAUDE_PARALLEL`（墨页实测 Haiku 40 路没被限流，每进程约 230MB）。
