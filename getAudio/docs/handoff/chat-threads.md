# 笔记本对话：历史对话 + 选模型

## 速览
- **现状**：分支 `feature/chat-threads`（worktree `.claude/worktrees/chat-threads`），本地已提交，**没合进 main、5001 没重启**。新测试 `tests/test_chat_threads.py` 21 项过；scratch 5063 实测 Haiku / GPT-6.1-Sol / agy Argon Low 各问一次，都带出处、各约 17 秒、$0。
- **怎么跑**：笔记本中间栏栏头——时钟图标 = 历史对话 / 新对话；设置图标 = 回答方式 + 模型。接口见下。
- **已知问题**：标题直接取第一问前 40 字（没用模型起名，省钱）；一段对话最多留 200 条；演示实例里「新对话」走的是 `DEMO_ASK` 放行的那条。
- **下一步**：用户点头后合 main、重启 5001（按多窗口规矩先问别的窗口、看 tasks.db）。可选：像 Ephemeris 那样用 Haiku 给对话起标题、置顶。

## 存哪
`results/_chains/<id>/chats/<thread>.json`，一段一个文件：`{id, title, named, created, updated, messages}`。
「当前那段」= 最近动过的那段。以前的单段 `chat.json` 第一次读到时自动挪成第一段（内容不变）。
新对话先建空文件（刷新还在），空的不进列表，再开新的会把旧的空对话收掉。

## 接口
- `GET /api/chain/<id>/ask?thread=` → `{messages, thread, threads}`；thread 不在了就开最近那段
- `POST /api/chain/<id>/ask/threads` → 新对话 `{thread}`
- `POST /api/chain/<id>/ask/threads/<tid>` `{title}` → 改名
- `DELETE /api/chain/<id>/ask?thread=` → 删这一段（不给 = 当前那段）
- `GET /api/chain/<id>/ask/models` → `{models: [{id, label, billing: sub|api}], default}`
- 提问（`/ask`、`/ask/stream`）body 多了 `thread`、`model`；回答里记 `model`、`thread`
- `studio/note` 多了 `thread`

## 模型
`ask.model_choices()` 按家列（11-06 用户：「codex、ag、Claude 三个公司的都能选」）：
- **Claude**（`claude` 命令在、没撞上限）：跟 Ephemeris 聊天页同一张表，Opus 5.5 / Sonnet 5.5 / Haiku 5.5 / Opus 5 / 4.8 / 4.7 / 4.6 / Sonnet 5 / 4.6 / Haiku 4.5。id 就是模型名
- **GPT**（`CODEX_ENGINE=on`）：`~/.codex/models_cache.json` 里 visibility=list 的全部。id `codex:<slug>`，走 `codex exec -m <slug>`（老的 `codex` = config 里那个）
- **Gemini · agy**（装了 Antigravity CLI）：`agy models` 里的 gemini-*（Argon / 3.8 Flash × low/medium/high），后台拉、缓存 6 小时，没拉到先用内置那 6 个。id `agy:<模型>`，走新模块 `agy_cli.py`（不给工具、空目录、进共用的闸，`agy_short` 先看额度留没留给用户）。agy 里的 Claude / GPT-OSS 不重复列
- **Gemini API**：3.5 Flash-Lite / Flash（按量付费）

前端设置里按家分组，组头写怎么付钱，顶上一排按钮跳到那一家（列表长，近 30 个）。
不挑 = 默认，跟以前一样（`ASK_ENGINE=claude` → Opus，否则 `ASK_MODEL`）。三家订阅还没出字就失败 → 退回 `ASK_MODEL`，回答上记真答的那个。
codex / agy 不是流式的：写完一次性出来。没改任何全局默认。
