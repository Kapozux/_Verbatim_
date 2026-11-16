# 交接：进行中（关了页面也看得到）+ 看图并发

## 速览（2026-11-16）

| | |
|---|---|
| 现状 | 分支 `feature/task-board`（worktree `.claude/worktrees/task-board`），本地提交；scratch 5077 实测：关着页面开的转写，打开后补回队列、走到「完成 · 查看」 |
| 怎么跑 | 开个转写 → 关掉页面 → 再打开：队列卡片和顶栏 ⟳N 都有。测试 `./run_tests.sh activity` |
| 已知问题 | 服务重启后画面描述的排队（内存里）丢了，列表里也就没了；工作台产出只在生成中时出现，做完不留 |
| 下一步 | 按实测结果定 `VISION_EPISODES`（见下）；合 main |

## 一句话

用户 11-16：「能不能例如我某个地方开启转写，我即使页面关了也能显示呢？」任务本来就在后台跑（关页面不停、重启 5001 也接着跑），
只是页面上的队列是浏览器内存里的。现在服务器记下每个任务最后一条进度，页面打开时去问。

## 怎么做的

- `app._TaskQueue`：转写任务的消息队列（`/stream` 照旧读），put 时把 queued / progress / done / error 记进 `_task_last[tid]`（消息、百分比、`phase`）。6 处建队列的地方都换了；博主链建的任务 `taskdb.create(..., origin='chain')`。
- `taskdb.activity()`：没跑完的 + 最近 20 分钟结束的，不含 `origin='chain'`。`tasks` 表新加 `origin` 列（老库自动补）。
- `scenes.recent()`：画面描述排队 / 在做 / 20 分钟内结束的（带开始、更新时间）。
- `study._save` 顺手登记生成中的产出（`study.live()`），所有工作台格子都走这里，不用改各自的代码。
- `GET /api/activity` → `{items: [{kind: transcribe|scenes|studio, id, title, status, message, percent, phase, ...}]}`。
- 前端 `static/activity.js`：补队列卡片的行（`data-srv=1`，跟着轮询更新；自己提交的行仍由 SSE 管，`connectBatchSSE` 给行记上 `data-task-id`），顶栏 ⟳N 加 `transcribe-bg` / `scenes` / `studio` 三个来源（只算队列卡片里没有行的，免得重复）。弹层的行可带 `href`。
- 没人说话改读画面时进度带 `phase: 'scenes'`，队列显示「没人说话 · 在读画面…」。

## 看图并发（画面描述 + 画面证据卡）

用户同一天问「画面证据卡的并发怎么设置的」。原来：证据卡一期一期、一期里一批一批（全串行）；画面描述后台一个线程。
现在两者共用两个设置：

- `VISION_EPISODES`（默认 2）：同时做几期——画面描述后台线程数、证据卡 `run_batch` 同时几期。每期都要重下视频（B 站风控）。
- `VISION_BATCHES`（默认 4）：一期里几批同时读——证据卡的缩略图分拣、读原图；画面描述字幕模式。普通模式画面描述仍一批接一批（要接上一批的最后一句）。
- 总量还有两道闸：`CLAUDE_PARALLEL`（`.env` 里 20）、三个应用共用的 `claude_gate`（默认 24、留 4 给交互）；`visual` 是 batch 优先级，给提问让路。
- 线程池里的调用用 `usage.bound` 带上记账归属（`usage.scope` 是线程局部的）。

实测（8 条真视频：B 站 4 + YouTube 4，2–8 分钟，只走 Haiku）：数据在 `.claude/scratch-data/vision-conc/results.jsonl`，结果见下一节。
