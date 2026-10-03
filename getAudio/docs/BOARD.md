# Verbatim 总表

谁在做什么、做到哪、下一步是什么，都在这一张表里。**每个窗口开工先读这张表**，认领一件事就在「谁」里写上自己的窗口名；收工前更新状态和下一步。
新冒出来的 bug、想法、要用户拍板的事，也先记到这里，不要只写在聊天里或别的交接文档里。规矩见 `getAudio/CLAUDE.md`「多窗口一起开发」。

最后更新：2026-10-03

## 正在做

| 事 | 谁 | 在哪 | 状态 |
|---|---|---|---|
| （空） | | | |

## 等用户拍板

| 事 | 为什么要拍板 | 建议 |
|---|---|---|
| 推送远端 | main 有 11 个本地提交没推到 GitHub Kapozux（`kapozux/main` 之后），GitLab 落后 25 个；Daemon 原版（`~/Documents/Code_no_big_AI`）3 个本地提交没推 | 一次只推一个远端、推前 fetch，按 CLAUDE.md 红线来；GitHub 新账号一天最多推一次 |
| 画面证据卡质量评测 | 8 期覆盖幻灯片 / 录屏 / 白板 / 真人出镜等，人工标答案，量漏帧、重复、抄错、编造，约 $0.2 | 跑，再按结果调挑帧和去重 |
| All-In 预测对账发布 | 51 条已核完，标了「需人工复核」的几条要人看；发布形式（X 串推 / Show HN / 公开页面）没定 | 见 `~/verbatim_outreach/HANDOFF.md` 第 6 节 |
| 转写音频的单价 | Gemini 3.5 Transcribe（约 377 小时）和阿里云 Qwen-Audio（约 422 小时）没有单价，费用页里这部分是 0 | 用户从控制台抄单价，写进 `prices.json` |
| deepseek-v4-pro 价格 | 读代码用它，价格来自第三方文章 | 用户在百炼控制台核实 |
| 重构阶段 4–6 什么时候做 | 阶段 4 要改转写排队、取字幕，跟别的窗口的功能是同一块 | 先把 main 合进重构分支，再挑没有别的窗口在改转写的时候做 |

## 待办（按优先级）

| 优先 | 事 | 范围 | 交接 / 出处 |
|---|---|---|---|
| P1 | 把 main 合进重构分支，接上新功能（画面卡、代码库来源、说话人、分段抽卡、建合集去重、补发布日期） | 重构 | `docs/handoff/refactor-deep-modules.md` |
| P2 | 重构阶段 4：引擎接缝、任务执行器、取字幕 / 下音频 | 重构 | 同上 |
| P2 | 重构阶段 5：模型网关（含 DeepSeek 答的记成 Gemini）、语料接口、对话模块（含非流式不存 persona） | 重构 | 同上 |
| P2 | 重构阶段 6：前端（离开笔记本后轮询不停、面板控制器、Esc 栈等） | 重构 | 同上 |
| P2 | 代码库来源：导览卡多要「代码实际怎么做」（自证卡）；读码放独立进程 / 重启后接着收卡；估价多测几个仓库再校准 | 代码库来源 | `docs/handoff/code-repo-source.md` |
| P2 | 画面证据卡：评测后调挑帧、加红框的同一张图去重、obs 留不留、「全部」加数量上限和间隔、中途重启不整份判失败 | 画面卡 | `docs/handoff/visual-cards.md` |
| P2 | 打包版（DMG / EXE）：声纹模型下载（37 MB）、跑不了自带 Daemon（`sys.executable` 不是 Python） | 打包 | `docs/handoff/voices.md`、`code-repo-source.md` |
| P3 | 播客改单句；幻灯片 Windows 字体验证；AI 配图 / 导出 Google Slides | 播客 / 幻灯片 | `docs/handoff/podcast-slides.md` |
| P3 | 费用页：TTS 免费期（2026 年底前）记 $0 | 费用 | — |
| P3 | 镜头输出偶尔是繁体中文 | 分析 | 10-01 记的 |
| P3 | 414 条转写时间戳中途乱跳 | 转写 | 10-01 开过单独任务，状态待查 |
| P3 | 演示实例 5002 一直崩（launchd 读不了 `~/Documents`） | 演示 | 用户说先把 5001 稳定了再管 |
| — | 墨页（Moye）的架构修复 | 墨页 | 另一个会话在做，先别动 |

## 各功能现状

| 功能 | 状态 | 主要代码 | 交接 |
|---|---|---|---|
| 笔记本（三栏、多博主、复习工具、对比） | 在用 | `projects.js` `explore.js` `study.py` `ask.py` | 记忆「Verbatim 项目页/问答/复习工具」 |
| 说话人（本机声纹） | 在用 | `voices.py` `static/voices.js` | `docs/handoff/voices.md` |
| 画面证据卡 | 测试版 | `frames.py` `static/visual.js` | `docs/handoff/visual-cards.md` |
| 音频播客、幻灯片 | 在用 | `podcast.py` `slides.py` | `docs/handoff/podcast-slides.md` |
| 代码库当来源（Daemon 读码） | 在用 | `repos.py` `daemon/` `static/repos.js` | `docs/handoff/code-repo-source.md` |
| 链接转写的字幕快速通道 | 在用 | `app.py` `_download_then_transcribe` | `tests/test_subtitle_lane.py` |
| 预测对账 | 在用；All-In 那份走外面的 `ledger_proto.py` | `ask.py` `check_predictions` | `~/verbatim_outreach/HANDOFF.md` §6 |
| 深模块重构 | 阶段 0–3 完成，在分支上，没进 5001 | worktree `.claude/worktrees/deep-modules` | `docs/handoff/refactor-deep-modules.md` |

## 全局设置（改之前先在「等用户拍板」登记）

| 设置 | 现在 | 谁定的 |
|---|---|---|
| 抽卡模型 `GEMINI_EXTRACT_MODEL` | 默认 `gemini-2.5-flash`（`.env` 里 3.8-flash 那两行已注释掉） | 用户 10-03：3.8-flash 只在 All-In 对账用 |
| 预测核对模型 / 思考上限 | 跟抽卡同一个模型；`PREDICTION_CHECK_THINKING=1024` | 10-03 实测：8 条结论不变、便宜约 40 倍 |
| 摘要、标题标签 | `gemini-3.1-flash-lite` | 用户 10-02 |
| 回顾面板、笔记本综述 | 每月最多自动写一次，手动刷新随时可用 | 用户 10-02 |
| 非中国议题的便宜路由 | `deepseek-v4-flash`（`llmroute.py`），政治词一次即回 Gemini | 用户 10-01 |
| 5001 | 不开 `FLASK_DEBUG`（不热重载）；改完后端，没任务在跑时手动重启 | 用户 10-03 |
| 转写完自动做声纹 | 开，子进程里跑；`VOICES_AUTO=off` 可关 | 用户 10-02 |
| 叫法 | 产品叫 Verbatim；界面「笔记本」/ Notebook；宣传这个功能叫 Verbatim Notebook；代码和接口仍叫 project | 用户 10-03 |

## 决策记录编号（写新的之前先在这里占号）

| 号 | 主题 | 在哪 |
|---|---|---|
| 0001 | 博主按引用共用、固定字母 | main |
| 0002 | 测试一个文件一个进程、禁外网 | main |
| 0003 | chain.json 只有一个主人（chainstore） | 重构分支 |
| 0004 | 说话人按本机声纹认 | main |
| 0005 | 代码库由外部 agent 读 | main |
| 0006 | 转写文件只有一个主人（transcripts） | 重构分支 |
| 0007 | 笔记本登记表只有一个主人（projects） | 重构分支 |
| 0008 | 证据卡文件只有一个主人（cards） | 重构分支 |
| 0009 | （下一个可用） | |

## 最近做完（10-02 ~ 10-03）

- 说话人声纹进 5001；卡住的语文笔记本修好；顶栏进度条换成小药丸
- 画面证据卡、音频播客、幻灯片、代码库来源、字幕快速通道：各自提交进 main
- 省钱：回顾和综述每月一次；摘要 / 标题标签换 3.1-flash-lite；抽卡换回 2.5-flash；核对加思考上限
- 修 bug：单链接建合集丢发布日期；声纹提取卡住整个服务（改到子进程）；建合集重发建出两个；长节目抽卡失败（分 20 分钟一段）；核对没上下文；改代码 5001 自动重启
- 界面「项目」改叫「笔记本」
- 重构阶段 0–3（在分支上）
