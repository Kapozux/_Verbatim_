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

用户 11-16：「不能按单个视频吧，要类似全部加起来的总帧几十个/百个一起读」。原来证据卡全串行、画面描述一期一个线程一批接一批。现在：

- **共用读图池** `frames.read_all`：所有期的读图请求（证据卡的分拣、读原图；画面描述普通 + 字幕模式）进同一个池子，最多 `VISION_CALLS`（默认 32）个同时读。画面描述的批之间不再接上一批的最后一句（才能一起读）。
- `VISION_EPISODES`（默认 2）：同时几期在**下视频、取帧**（B 站风控，别开太大）。
- 取帧并行（`frames.grab_many`，8 个 ffmpeg）。
- 真正的上限还有两道闸：`CLAUDE_PARALLEL`（`.env` 20）和三个应用共用的 `claude_gate`（`CLAUDE_GATE_CAP` 默认 24、留 4 给交互）——**不调大这两个，VISION_CALLS 开多少都只有 ~20**。`visual` 是 batch 优先级，给提问让路。
- 线程池里的调用用 `usage.bound` 带上记账归属。

### 实测（11-16，scratch `.claude/scratch-data/vision-conc/`）

8 条真视频（B 站 4 + YouTube 4，3–7 分钟）。**下载**（4 条同时）：每条 12–232 秒，最慢的环节；扫画面 + 取帧每条几秒。字幕模式共 1930 帧 / 242 批。

读图（真 Haiku 订阅，每批 8 张，每档读 2N 批，测试进程自己一个闸）：

| 同时 | 帧 | 用时 | 每分钟批数 | 失败 | 单次中位 / 最慢 | claude 进程内存 |
|---|---|---|---|---|---|---|
| 16 | 256 | 39s | 49 | 0 | 17s / 26s | 3.8GB |
| 32 | 512 | 31s | 123 | 0 | 12s / 28s | 7.4GB |
| 64 | 1024 | 46s | 167 | 0 | 19s / 46s | 14.5GB |
| 96 | 1536 | 69s | 167 | 0 | 21s / 69s | 20.5GB |

结论：64 以后不再变快（每分钟 ~167 批 ≈ 1300 帧），只多占内存、拖慢最慢那批；没碰到限流。每个 claude 进程约 230MB。
