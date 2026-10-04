# 给 Open Notebook 做贡献（以及它和 Verbatim 的对比）

## 速览

- **现状**（10-05）：第一个 PR（简体中文翻译修正，118 条）已经提到 lfnovo/open-notebook，cubic 机器人的两条意见当天改完，等维护者批准 CI 和审核。#1315 讨论帖已由用户回复（出处精确到时间戳、规模经验）。用户中途误点关闭过一次 PR，已重新打开。
- **怎么跑**：本机克隆在 `~/Documents/CODEelse/.claude/scratch-data/open-notebook`（分支 `fix/zh-cn-translation`，远端 `fork` = `https://github.com/Kapozux/open-notebook.git`）。前端测试 `cd frontend && npx vitest run src/lib/locales`；试用新界面：`.claude/scratch-data/on-run/docker-compose.yml`（v1-dev 镜像，UI 8512、API 5065，避开 Ephemeris 的 5055）。
- **已知问题**：没有。PR 等审，维护者一般一周内看。
- **下一步**：等 PR 合并后再提第二个。候选（按「被接受的可能性 × 价值」）：content-core 加 B 站来源（content-core 允许直接提 PR，不用先讨论）；Open Notebook 的明显小 bug（默认 transformation 提示词永远不生效、播客段数上限 3–20 vs 底层 1–10、`/sources/{id}/retry` 防重入被吞、YouTube 没字幕时误导文案「去配 STT」、Source Chat 停止按钮没接上）；引用核对要先在 #1315 里提。两个安全问题（content-core 全局关 SSL 校验、跟随重定向后不再校验 URL）按 SECURITY.md 私下报告，别公开提。

## 规矩（重要）

- **GitHub 上的每个动作都由用户点头、一次一件、一天最多一个**：fork、推分支、开 PR、回审查意见、发讨论帖。用户的 Kapozux 账号是 2026-09-19 新开的，之前的账号因为短时间批量操作被封过（见 CLAUDE.md 红线）。
- Open Notebook 的贡献规则：**新功能必须先发 Discussion（Ideas），维护者开 Issue 并指派后才能写代码**；翻译修正、文档、几行就能修好的小 bug 可以直接提 PR。AI 写的代码可以，但提交的人要能逐行讲清楚，测试输出要贴真实的。
- 维护者 lfnovo 在 **UTC-3**（提交记录全是 -0300），比用户（台湾 UTC+8）晚 11 小时。他最活跃的时段对应台湾晚上 7–8 点、次日早上 9–10 点。别催，满一周没动静才礼貌地问一次。

## 资料

- 两份对比报告都在 `~/verbatim_outreach/`：
  - `Verbatim_vs_OpenNotebook_技术对比_2026-10-04.md`（50 个 agent，12 个技术维度并排、逐条核实，含借鉴清单、可贡献清单、成为音视频模块核心贡献者的路线）
  - `定位评审_2026-10-04.md`（19 个 agent：Verbatim 到底解决谁的什么问题；含私人使用数据，不进 git）
- 回复 #1315 的草稿（已发）：`~/verbatim_outreach/open-notebook_讨论帖草稿.md`；PR 1 的说明：`open-notebook_PR1_翻译修正.md`。
- Open Notebook 的要点：FastAPI + SurrealDB + LangGraph + Next.js，约 4 万星；音视频很浅（YouTube 只拿现成字幕、上传音频转写后丢时间戳、出处只指到整篇来源、不支持 B 站、没有说话人）。所以 Verbatim 的位置是「它管文档，我管视频和录音里的原话」。路线图（VISION.md）里没有这些，#1315 的「证据契约」方向正好需要。

## 这次会话里关于产品方向的结论（给以后的窗口）

- 用户唯一被真实验证过的用途：**把老师的课堂录音（转写）和卷子（墨页）做成复习笔记，几十个同学用过、来感谢**。软件里的使用记录看不到这件事，因为大家用的是分享出去的结果。
- 用户在台湾，高中生（IB）；想「做大事」的路线选的是给 Open Notebook 持续贡献、成为音视频模块的核心贡献者，Verbatim 当实验室。
