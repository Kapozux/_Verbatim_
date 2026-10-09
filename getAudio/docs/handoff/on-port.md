# 把 Open Notebook 的做法和功能搬进 Verbatim

## 速览

- **现状**（10-09）：分支 `feature/on-port`（worktree `.claude/worktrees/on-port`），10-09 14:47 合进 main（`3521766`）、5001 已重启（tasks.db 已加上 `kwargs` 列）。工程项做完 8 件，用户功能做完 4 件（自己的模型、只问这一份、模板库、密码登录 + 接口文档），每件一个本地提交、带测试，全量 51 个测试文件全过。
- **怎么跑**：`cd .claude/worktrees/on-port/getAudio && ./run_tests.sh`；要看界面用复制出来的数据开别的端口（CLAUDE.md 多窗口第 2 条）。
- **已知问题**：tasks.db 加了 `kwargs` 列。新代码一启动就会给库加上这一列，之后**旧代码**的 `taskdb.create`（`INSERT ... VALUES` 9 个值、没写列名）会报列数不对。所以合进 main 时 5001 必须一次换成新代码；别的 worktree 不要拿已经加过列的库副本跑旧代码。
- **下一步**：合进 main 要按 CLAUDE.md 多窗口第 3 条（先问别的窗口、确认没有在跑的任务）；第 3 项和模型注册表用户 10-09 定了先不做（留给重构分支阶段 4、5）；第 10 项（迁移）还没做；更多界面语言没做。

## 来源

用户 10-09 拍板：技术对比报告（`~/verbatim_outreach/Verbatim_vs_OpenNotebook_技术对比_2026-10-04.md`）第 4 节的工程做法，加上 ON 有、Verbatim 没有的用户功能，**两块都搬**。代码不照抄 ON，按 Verbatim 自己的结构用更好的办法写。给 ON 贡献代码那条线照旧并行（`docs/handoff/open-notebook.md`）。

## 工程做法（报告第 4 节）

| # | 事 | 状态 | 提交 / 测试 |
|---|---|---|---|
| 1 | embeddings.npz 记下模型名和维度，对不上当作没有、整份重算 | 完成 | `52f9568` · `tests/test_embed_model.py` |
| 2 | tasks.db 存转写的完整入参，重启恢复时带上 | 完成 | `4f99642` · `tests/test_task_recover_args.py` |
| 3 | 引擎层抛类型化异常、每个引擎声明重试策略 | 没做 | 报告说工期要翻倍；动 app.py 的 `_is_network_error` 一片 |
| 4 | 下载失败分 7 类（`downloader.DOWNLOAD_ERRORS`），给能照着做的提示；yt-dlp 原话只进日志；视频没了 / 要登录 / 地区限制 / 不支持的网站试一次就停 | 完成 | `a822b9e` · `tests/test_download_errors.py`；文档 / 网页提取（sources.py）还没分类 |
| 5 | 上传 / 本机路径进队列前用 ffprobe 预检 | 完成 | `7142f95` · `tests/test_upload_preflight.py` |
| 6 | i18n 中英键、占位符、重复键测试；localStorage 包 try/catch | 完成 | `fbc41e5` · `tests/test_i18n.py`（要 node，没有就跳过） |
| 7 | 笔记本的轮询离开就停、后台标签页暂停（`cxLater` / `notebookLeave`） | 完成 | `09443f0` · `tests/test_notebook_polling.py`（要 node） |
| 8 | 只送了一部分材料时告诉模型 | 完成 | `b0f59c9` · `tests/test_partial_material.py` |
| 9 | 删除预览（`/delete_preview`，确认框里列出牵连）+ 级联：删转写从笔记本和 tasks.db 拿掉、删博主从引用它的笔记本拿掉 | 完成 | `f3ab653` · `tests/test_delete_preview.py`；博主频道 chain.json 里的 task_id 故意不动（动了下次同步会重转、花钱） |
| 10 | 编号迁移 + schema_version | 没做 | 先在总表占 ADR 号 |
| — | 单一来源的模型注册表（价格、上下文、成本档） | 没做 | 跟重构阶段 5「模型网关」是同一件事，先看重构分支 |
| — | 本机 pre-commit、docs/SECURITY.md、长活拆独立 worker、SSE 空闲看门狗、弹框状态进 URL | 没做 | |

## 用户功能（ON 有、Verbatim 没有）

用户 10-09 选了四个，按顺序做完：

| 功能 | 怎么用 | 提交 / 测试 |
|---|---|---|
| 自己的模型（OpenAI 兼容：Ollama / LM Studio / OpenAI） | 设置 →「自己的模型」填端点、模型名、key 可空；分析大脑选「自己的模型」；「日常文字也先用它」默认关 | `599cae3` · `tests/test_custom_llm.py`；ADR 0013（选了就不换云端）；本机 Ollama `qwen3-vl:8b` 实测 9.6 秒、记 $0 |
| 只问这一份来源（ON 的 Source Chat） | 来源栏阅读器栏头「只问这一份」：提问范围只勾它、光标跳到输入框 | `dfd82ae`；复用现成的勾选范围（`scope_pool` 已有测试），界面在浏览器里点过 |
| 复习笔记的模板库（ON 的全局 transformation） | 「复习笔记」弹框：「从模板库套用…」下拉、「存进模板库」 | `86a3de8` · `tests/test_template_library.py`；存在 `revnote_library.json` |
| 密码登录 | 设环境变量 `VERBATIM_PASSWORD`：浏览器到 /login，签名 cookie 30 天；脚本用 `Authorization: Bearer <密码>`；10 分钟输错 5 次先拦 | `e9340c8` · `tests/test_password_login.py`；cookie = HMAC(数据目录 `.session_secret`, 密码) |
| 接口文档 | `/api/openapi.json`、`/api/docs` | `5c3e403` · `tests/test_openapi.py`；ADR 0014（从路由表生成） |

没做：更多界面语言（现在只有中英）。

## 注意

- 第 1 项：换了 `EMBED_MODEL` 之后，每个笔记本第一次被提问时会在后台整份重算向量，**这一步花钱**（马督工那种 1.8 万段约 $1）。不换模型就不会触发；旧文件按 `gemini-embedding-001` 认。
- 第 8 项改了问答和工作台的提示词（只在材料没全送时多一句），不影响全部送得下的情况。
- 新加的全局开关（`CUSTOM_LLM_*`、`VERBATIM_PASSWORD`）默认都是关的，不设就跟以前一样（CLAUDE.md 多窗口第 4 条）。
- 改了共用文件 `app.py`、`static/i18n.js`、`static/style.css`、`static/study.js`、`templates/index.html`，合进 main 时可能跟别的窗口的改动撞。
