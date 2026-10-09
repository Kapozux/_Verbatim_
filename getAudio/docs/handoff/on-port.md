# 把 Open Notebook 的做法和功能搬进 Verbatim

## 速览

- **现状**（10-09）：分支 `feature/on-port`（worktree `.claude/worktrees/on-port`），没合进 main、5001 没动。工程项做完 5 件，每件一个本地提交、带测试，全量测试过。
- **怎么跑**：`cd .claude/worktrees/on-port/getAudio && ./run_tests.sh`；要看界面用复制出来的数据开别的端口（CLAUDE.md 多窗口第 2 条）。
- **已知问题**：tasks.db 加了 `kwargs` 列。新代码一启动就会给库加上这一列，之后**旧代码**的 `taskdb.create`（`INSERT ... VALUES` 9 个值、没写列名）会报列数不对。所以合进 main 时 5001 必须一次换成新代码；别的 worktree 不要拿已经加过列的库副本跑旧代码。
- **下一步**：第 7 项（轮询跟着视图生命周期走），然后第 9、4、3、10 项，再做用户功能（见下面清单）。

## 来源

用户 10-09 拍板：技术对比报告（`~/verbatim_outreach/Verbatim_vs_OpenNotebook_技术对比_2026-10-04.md`）第 4 节的工程做法，加上 ON 有、Verbatim 没有的用户功能，**两块都搬**。代码不照抄 ON，按 Verbatim 自己的结构用更好的办法写。给 ON 贡献代码那条线照旧并行（`docs/handoff/open-notebook.md`）。

## 工程做法（报告第 4 节）

| # | 事 | 状态 | 提交 / 测试 |
|---|---|---|---|
| 1 | embeddings.npz 记下模型名和维度，对不上当作没有、整份重算 | 完成 | `52f9568` · `tests/test_embed_model.py` |
| 2 | tasks.db 存转写的完整入参，重启恢复时带上 | 完成 | `4f99642` · `tests/test_task_recover_args.py` |
| 3 | 引擎层抛类型化异常、每个引擎声明重试策略 | 没做 | 报告说工期要翻倍；动 app.py 的 `_is_network_error` 一片 |
| 4 | 下载 / 文档提取的错误分几类，前端按类给可操作的提示，原始 stderr 只进日志 | 没做 | |
| 5 | 上传 / 本机路径进队列前用 ffprobe 预检 | 完成 | `7142f95` · `tests/test_upload_preflight.py` |
| 6 | i18n 中英键、占位符、重复键测试；localStorage 包 try/catch | 完成 | `fbc41e5` · `tests/test_i18n.py`（要 node，没有就跳过） |
| 7 | 轮询和视图生命周期绑在一起 | 没做 | |
| 8 | 只送了一部分材料时告诉模型 | 完成 | `b0f59c9` · `tests/test_partial_material.py` |
| 9 | 删除前出影响预览、删来源时级联清理 | 没做 | |
| 10 | 编号迁移 + schema_version | 没做 | 先在总表占 ADR 号 |
| — | 单一来源的模型注册表（价格、上下文、成本档） | 没做 | 跟重构阶段 5「模型网关」是同一件事，先看重构分支 |
| — | 本机 pre-commit、docs/SECURITY.md、长活拆独立 worker、SSE 空闲看门狗、弹框状态进 URL | 没做 | |

## 用户功能（ON 有、Verbatim 没有）

本地模型（Ollama）、更多模型商（OpenAI 兼容接口）、可编辑的 transformation、单篇来源对话、更多界面语言、密码登录、OpenAPI 文档。都没做。其中模型相关的会碰全局设置，按 CLAUDE.md 多窗口第 4 条先登记、默认值不变。

## 注意

- 第 1 项：换了 `EMBED_MODEL` 之后，每个笔记本第一次被提问时会在后台整份重算向量，**这一步花钱**（马督工那种 1.8 万段约 $1）。不换模型就不会触发；旧文件按 `gemini-embedding-001` 认。
- 第 8 项改了问答和工作台的提示词（只在材料没全送时多一句），不影响全部送得下的情况。
