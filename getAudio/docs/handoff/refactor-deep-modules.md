# 交接：深模块重构（refactor/deep-modules）

## 速览（2026-10-03）

| | |
|---|---|
| 现状 | 阶段 0–3 做完，在分支 `refactor/deep-modules`（worktree `.claude/worktrees/deep-modules`）上本地提交，**还没合进 main**，5001 跑的不是它 |
| 怎么跑 | `cd .claude/worktrees/deep-modules/getAudio && ./run_tests.sh`（worktree 里 `venv` 是指向仓库根 venv 的软链） |
| 已知问题 | main 这两天进了很多新代码（说话人、画面卡、播客、幻灯片、代码库来源、字幕快速通道、几个 bug 修复），跟分支差得越来越远 |
| 下一步 | ① 把 main 合进这个分支并接上新模块（见下）；② 阶段 4–6；③ 用户验收后再合回 main |

## 做了什么

起因：2026-10-02 按 mattpocock「improve-codebase-architecture」的方法给全部 AI 写的代码做了体检，用户说「你都修了 + 加上可以测试的功能」。
每一步：先写会失败的测试 → 实现 → 两三个角度审查（行为不变 / 并发和数据安全 / 模块深度和测试质量）→ 修 → 跑全量测试 → 本地提交。

| 阶段 | 提交 | 内容 |
|---|---|---|
| 0 | `7a0383f` | 测试地基（`tests/`、`run_tests.sh`、一个文件一个进程、禁外网）、术语表、ADR 0001–0002 |
| 1 | `860d28b` | `citations.py`、`timecode.py` |
| 2 | `5bcec90` `a2cca94` | `chainstore.py`（chain.json 只有它写；认领防两条跑；三方合并防丢字段）、`transcripts.py`（results/<tid>/ 四个文件只有它写）、`atomicfile.py` |
| 3 | `ba351e3` `a2576c6` `d71333f` | `projects.py`（sources.json、字母规则）、`cards.py` + `_finish_portrait`（证据卡文件、重新分析先备份画像、垃圾转写写占位卡）、`chainplan.py`（选期纯函数；同步不再把旧期改指回被替换的转写） |

决定和理由：`docs/adr/0003`、`0006`、`0007`、`0008`（分支上）。

## 合 main 时要接上的地方

- 画面证据卡的 `_visual_rows` 用了已删的 `_ref_rows` / `_rec_rows` → 改用 `projects.sources(cid)`。
- 代码库来源写 sources.json 的 `repos`（`_reg` / `_reg_save`）→ `projects` 里 `_LISTS` 加 `'repos'`，加 `add_repos`，`sources()` 带出 `repos`。
- `voices.py` 和 `app._voices_projects_of` 直接读 chain.json / meta.json / transcript.json → 改用 `chainstore` / `transcripts`。
- main 上 `analyze.analyze_episode` 加了长节目分段抽卡，要跟 `cards.ensure` 注入的 `extract` 对上。
- main 上建合集的十分钟去重、补发布日期（`_fill_missing_dates`）、预测核对带上下文，都要搬到分支的新结构里。
- 字母、决策记录编号以 `docs/BOARD.md` 的登记为准。

## 阶段 4–6（还没做）

- 4：转写引擎接缝（五个引擎统一接口）、任务执行器（7 处提交合成一个）、取字幕 / 下音频（**跟 main 上的字幕快速通道是同一块，做之前先合 main**）。
- 5：模型网关（含 DeepSeek 答的记成 Gemini 的 bug）、语料接口、对话模块（含非流式不存 persona）。
- 6：前端（离开笔记本后轮询不停、面板控制器、工作台独立、Esc 栈、chainTraits、CSS 合并、i18n 清理）。

## 规矩

- 只在 worktree 里改；不碰 main 主目录、不重启 5001；合回 main 之前先在别的窗口那边问一声（见 `getAudio/CLAUDE.md`「多窗口一起开发」）。
- 后台 Workflow 会话一重开就被杀（阶段 2 断了 4 次）：改用前台 Agent 一步一步跑，每步做完就提交。
