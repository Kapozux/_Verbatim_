# 笔记本对话：历史对话 + 选模型

## 速览
- **现状**：分支 `feature/chat-threads`（worktree `.claude/worktrees/chat-threads`），本地已提交，**没合进 main、5001 没重启**。新测试 `tests/test_chat_threads.py` 18 项过。
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
`ask.model_choices()`：装了 claude 且没撞上限 → Opus 5.5 / Sonnet 5.5 / Haiku 5.5（订阅）；`CODEX_ENGINE=on` → GPT（订阅）；
总有 Gemini 3.5 Flash-Lite / Flash（API）。不挑 = 默认，跟以前一样（`ASK_ENGINE=claude` → Opus，否则 `ASK_MODEL`）。
Claude / GPT 还没出字就失败 → 退回 `ASK_MODEL`，回答上记的是真答的那个。没改任何全局默认。
