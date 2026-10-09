# Verbatim 总表

谁在做什么、做到哪、下一步是什么，都在这一张表里。**每个窗口开工先读这张表**，认领一件事就在「谁」里写上自己的窗口名；收工前更新状态和下一步。
新冒出来的 bug、想法、要用户拍板的事，也先记到这里，不要只写在聊天里或别的交接文档里。规矩见 `getAudio/CLAUDE.md`「多窗口一起开发」。

最后更新：2026-10-09

## 正在做

| 事 | 谁 | 在哪 | 状态 |
|---|---|---|---|
| 来源标签（照 NotebookLM：按主题自动分组、一个来源一个标签、排序：最近 / 标题 / 类型），取代文件夹 | getaudio-fa | main（10-05 合进、5001 已重启） | 已本地提交、测试全过；IB 计算机科学实测 19 份 → 7 组，$0.0003、2.6 秒 |
| 点出处就地看原视频 / 听原音（YouTube / B 站嵌播放器从那一秒开始）+ README GIF + YC 演示 | cite-video 窗口 | main（10-04 合进、5001 已重启） | 上线。README GIF 在 `docs/media/verbatim-cite.gif`（18 秒，4.7 MB；另有 mp4）；YC 演示：等用户定（见「等用户拍板」）。交接 `docs/handoff/cite-video.md` |
| gemini35 段落时间不可信（短文件中位数只有真实的 0.337，转 16k 再送也一样，不是格式问题）| gemini35-time 窗口 | worktree `.claude/worktrees/gemini35-time`，分支 fix/gemini35-timeline（已合进 main、5001 已重启） | 10-05 停：本机 Whisper 回补到 848/1031 电脑太卡，用户决定不补旧期、也不花钱补；新 Gemini 期仍自动排本机对齐 |
| 把 Open Notebook 的做法和功能搬进 Verbatim（用户 10-09：工程做法 + 用户功能两块都搬，代码按自己的办法写；ON 贡献照旧并行） | on-port 窗口 | worktree `.claude/worktrees/on-port`，分支 feature/on-port | 10-09 开工：先做技术对比报告第 4 节的工程项，一项一个本地提交；交接 `docs/handoff/on-port.md` |
| 给 Open Notebook 做贡献 | getaudio-1d | 仓库外 `.claude/scratch-data/open-notebook`（fork：Kapozux/open-notebook） | #1479（上传上限）10-07 推了 P1 修复，cubic 回复草稿待发；#1449（翻译）有冲突待 rebase；#1315 已回复；下一个候选 content-core 的 B 站来源。GitHub 动作一次一件、用户点头。交接 `docs/handoff/open-notebook.md` |
| 宣传片（朋友圈中文版）| 宣传片窗口（getaudio-1d） | 仓库外 `~/verbatim_outreach/promo/v2_src`（Remotion） | 10-05 暂停：可发的仍是 v15；v16/v17（NotebookLM 结构、Canvas 配乐、只能内部测试）用户说粗糙；v18「逐镜头复刻 Canvas」镜头表已出、没开始做。交接 `docs/handoff/promo-video.md` |
| 读书笔记层（ADR 0010）：所有来源类型都读成统一格式的笔记，播客 / 报告 / 幻灯片按笔记定计划、细讲处回原文 | getaudio-fa | main（10-03 20:0x 合进、5001 已重启） | IB 计算机科学 19 份已读（$0.23）；马督工 1021 期没读（估 $4.23，等用户点）；下一步：两份失败的 PPTX 点重试、1 小时长播客方案 |
| 笔记本 8 个问题 + 播客原声核对 + DeepSeek / Kimi key + 大量资料的默认要求 + PPTX 直接读字 | getaudio-fa | main（10-03 合进、5001 已重启） | 完成；没推远端 |

## 等用户拍板

| 事 | 为什么要拍板 | 建议 |
|---|---|---|
| YC 100 期演示 | DeepSeek 卡只在 scratch 副本里；要放进 `verbatim-demo` 得删旧的 5 期 YC、停掉一直崩的 launchd 任务（`com.kapozux.verbatim-demo`），公开上线还要定放哪、提问花谁的钱 | 本机先跑 5002 + 开提问（DeepSeek 一问约 $0.001），公开前加每天上限 |
| 推送远端 | GitHub Kapozux 10-03 已推到 `38cd934`；GitLab 落后 29 个；Daemon 原版（`~/Documents/Code_no_big_AI`）3 个本地提交没推；`refactor/deep-modules` 等分支只在本地 | 一次只推一个远端、推前 fetch，按 CLAUDE.md 红线来；GitHub 新账号一天最多推一次 |
| 宣传片要不要改 / 需求对不对 | 朋友看了说看不懂（「那个什么妈妈……」）；可能只是梗太早，也可能「在视频里找原话」这个需求打不动大众 | 先问清看不懂的是梗还是产品；梗的话开头换大白话、梗挪片尾，两版给几个人看前 15 秒 |
| 画面证据卡质量评测 | 8 期覆盖幻灯片 / 录屏 / 白板 / 真人出镜等，人工标答案，量漏帧、重复、抄错、编造，约 $0.2 | 跑，再按结果调挑帧和去重 |
| All-In 预测对账发布 | 51 条已核完，标了「需人工复核」的几条要人看；发布形式（X 串推 / Show HN / 公开页面）没定 | 见 `~/verbatim_outreach/HANDOFF.md` 第 6 节 |
| 转写音频的单价 | Gemini 3.5 Transcribe（约 377 小时）和阿里云 Qwen-Audio（约 422 小时）没有单价，费用页里这部分是 0 | 用户从控制台抄单价，写进 `prices.json` |
| 官方 DeepSeek / Kimi 的价格 | 10-03 加了自己的 DeepSeek / Kimi key：官方 DeepSeek 先按百炼同名模型的价记；Kimi（`kimi-k2.6`）没有价，费用页记 0；模型名是照百炼上的叫法猜的，填 key 后点「测试连接」会列出真名 | 用户填 key 后测一次，模型名 / 单价不对就在设置里改、写进 `prices.json` |
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
| P2 | 只转写没分析的博主（`analyze: False`、没开 `index_transcripts`）放进笔记本后，对话 / 工作台点了只报「Nothing to work from」：应该先说清楚，并给一个「把原文加进检索（约 $X）」的按钮。马督工 10-03 已手动补上（17,859 段向量，$1.0） | 笔记本 | `docs/handoff/notebook-fixes.md` |
| P3 | 追问「你确定吗」有时接到更早的那张闪卡上（聊天记录里贴过好几张时） | 问答 | `docs/handoff/notebook-fixes.md` |
| P3 | 镜头输出偶尔是繁体中文 | 分析 | 10-01 记的 |
| P3 | 全量测试偶尔有一个文件失败，重跑就过（10-04 跑了 4 遍没复现，第一次是哪个文件没记下） | 测试 | — |
| P3 | 414 条转写时间戳中途乱跳 | 转写 | 10-01 开过单独任务，状态待查 |
| P3 | 演示实例 5002 一直崩（launchd 读不了 `~/Documents`） | 演示 | 用户说先把 5001 稳定了再管 |
| — | 墨页（Moye）的架构修复 | 墨页 | 另一个会话在做，先别动 |

## 各功能现状

| 功能 | 状态 | 主要代码 | 交接 |
|---|---|---|---|
| 复习笔记模板 | 在用 | `revnotes.py` `study.js` `revnotesPicker` | `docs/handoff/review-notes.md` |
| 笔记本（三栏、多博主、复习工具、对比） | 在用 | `projects.js` `explore.js` `study.py` `ask.py` | 记忆「Verbatim 项目页/问答/复习工具」 |
| 说话人（本机声纹） | 在用 | `voices.py` `static/voices.js` | `docs/handoff/voices.md` |
| 画面证据卡 | 测试版 | `frames.py` `static/visual.js` | `docs/handoff/visual-cards.md` |
| 音频播客、幻灯片 | 在用 | `podcast.py` `slides.py` | `docs/handoff/podcast-slides.md` |
| 代码库当来源（Daemon 读码） | 在用 | `repos.py` `daemon/` `static/repos.js` | `docs/handoff/code-repo-source.md` |
| 链接转写的字幕快速通道 | 在用 | `app.py` `_download_then_transcribe` | `tests/test_subtitle_lane.py` |
| 预测对账 | 在用；All-In 那份走外面的 `ledger_proto.py` | `ask.py` `check_predictions` | `~/verbatim_outreach/HANDOFF.md` §6 |
| 深模块重构 | 阶段 0–3 完成，在分支上，没进 5001 | worktree `.claude/worktrees/deep-modules` | `docs/handoff/refactor-deep-modules.md` |

## 全局设置（改之前先在「等用户拍板」登记）

- 10-04 用户拍板：国内模型（百炼 / 官方 DeepSeek / Kimi）**不再按内容预判**，一律先发，报错（审核拦截 / 超时 / 接口错）这一次改用 Gemini 同档。以前的词表预判把「央行 / 政府 / 国家」当泛词，财经节目（小Lin说）10 期全被送去 Gemini，$1.46 里 $1.44 花在 Gemini 上。
- 10-04 用户同意：阿里云转写请求一律开 `enable_words`（逐词时间，不另收费，正文不变），转写存好时写 `words.json`（ADR 0012）。

| 设置 | 现在 | 谁定的 |
|---|---|---|
| 抽卡模型 `GEMINI_EXTRACT_MODEL` | 默认 `gemini-2.5-flash`（`.env` 里 3.8-flash 那两行已注释掉） | 用户 10-03：3.8-flash 只在 All-In 对账用 |
| 预测核对模型 / 思考上限 | 跟抽卡同一个模型；`PREDICTION_CHECK_THINKING=1024` | 10-03 实测：8 条结论不变、便宜约 40 倍 |
| 摘要、标题标签 | `gemini-3.1-flash-lite` | 用户 10-02 |
| 回顾面板、笔记本综述 | 每月最多自动写一次，手动刷新随时可用 | 用户 10-02 |
| 非中国议题的便宜路由 | `deepseek-v4-flash`（`llmroute.py`），政治词一次即回 Gemini；先走阿里云，填了自己的 DeepSeek key 时阿里云没 key / 欠费就接官方 DeepSeek | 用户 10-01；官方 DeepSeek 兜底 10-03 |
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
| 0009 | 转写列表和搜索走 SQLite 索引，文件仍是唯一原始数据 | main |
| 0010 | 每份来源一份读书笔记，工作台产出从笔记出发 | 分支 feature/notebook-fixes |
| 0011 | 长播客按章节写 | 分支 feature/long-podcast |
| 0012 | 出处时间点走逐词时间轴（YouTube 字幕 / Qwen / 本机 Whisper 对齐），不估算 | 分支 feature/cite-cards |
| 0013 | （下一个可用） | |

## 最近做完（10-02 ~ 10-03）

- 10-07 复习笔记模板：工作台格子「复习笔记」，笔记本存一份模板，开了「加来源时自动出」就给每份新来源按模板出一份笔记（带出处，跟报告一样能导出）；实测两节《药》课 $0.0027 / $0.0091、24 秒。交接 `docs/handoff/review-notes.md`
- 10-06 备份补漏：上传的文档原件（_docs，PDF / PPTX，之前一份没备过）、笔记本 studio 里的播客 mp3 / 幻灯片 pptx、声纹和向量 .npz；跨站防护：别的网页不能再让本机 Verbatim 转写花钱，只监听本机时防 DNS 重绑定（`a9617d9`，已上 5001；脚本 / MCP 不受影响）

- 说话人声纹进 5001；卡住的语文笔记本修好；顶栏进度条换成小药丸
- 画面证据卡、音频播客、幻灯片、代码库来源、字幕快速通道：各自提交进 main
- 省钱：回顾和综述每月一次；摘要 / 标题标签换 3.1-flash-lite；抽卡换回 2.5-flash；核对加思考上限
- 修 bug：单链接建合集丢发布日期；声纹提取卡住整个服务（改到子进程）；建合集重发建出两个；长节目抽卡失败（分 20 分钟一段）；核对没上下文；改代码 5001 自动重启
- 界面「项目」改叫「笔记本」
- 侧栏笔记本图标换成本子；转写页「最近转写」从 2.3 秒降到 0.2 秒（列转写时每条都重查一遍笔记本，改成一次请求查一次；最近转写只取 12 条）
- 转写列表和搜索走 SQLite 索引（`library.py` / `library.db`，ADR 0009）：最近 12 条 122 ms → 3 ms，搜索 600 ms → 7–150 ms，结果跟原来逐条一样
- 从源码一键安装：`git clone … && cd _Verbatim_ && ./install.sh`（uv 装 Python 3.12 + 依赖到 venv/，再启动）。新克隆空缓存 42 秒装完，3.12 下测试全过、Whisper 转写走通；README / 两份上手文档 / getAudio/README 的旧步骤（`pip install` 在新 Mac 上必失败）都改了。Docker 和 Linux 没实测
- 重构阶段 0–3（在分支上）
