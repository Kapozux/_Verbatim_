# B 站评论区（抓评论 + 工作台「评论区整理」）

## 速览
- **现状**：10-13 22:35 合进 main、5001 已重启（分支 `feature/comments` 留着）。来源行 ⋯「抓评论区」→ 阅读器里看；工作台格子「评论区整理」→ 概述 + 一字不改的原话，带出处圆点；「转写」页贴 B 站链接 → 那一行「下载评论区」→ 弹窗（不转写，能总结、导出 CSV），或勾「只要评论区（不转写）」一批下载（`commentsBatch()`）；转写详情页也有同一张评论区卡片。评论和单个视频的总结按 BV 号存在 `results/_comments/`。登录态失效自动从 Chrome 续（存 `bili_login.local.json`）。
- **怎么跑**：`./run_tests.sh comments`（47 项，假 B 站接口 + 假模型）。真机：10-13 在 5031 用 scratch 数据实测 BV1s1pY6cEZo，抓 2,699 条约 4 分钟，Claude Opus 整理 55 条出处、$0；三个宽度 × 浅 / 深截图过。
- **模型 / 上限**：总结都用 Opus（自动 `comments-auto` low、batch 档；手动 `comments-digest` medium），退回 Gemini flash；同时 3 份。评论按热度抓 500 楼、每楼 50 条回复（设置 → 模型）。
- **已知问题**：「重新抓」是整期重抓；后台抓的进度只在内存里，服务重启就没了（再点一次）；整理只支持 B 站。
- **下一步**：之后可做 YouTube 评论、「观众怎么看」（见 ADR 0019「没做」）。

## 文件
- `comments.py`：登录态（`sessdata()`）、抓（`fetch()`，一级按时间翻完 + 每楼回复翻全）、存（`comments.json`）、整理提示词和出处（`build_prompt` / `parse_digest`）、后台抓（`start_fetch`）、工作台任务（`start_digest`，存法照 `study.py`）。
- `app.py`：`/api/comments/<tid>`、`/api/comments/<tid>/fetch`、`/api/chain/<id>/comments/episodes`、`/api/chain/<id>/studio/comments`（+ `/estimate`）；`_rec_rows` 多一个 `bili` 字段。
- `static/comments.js`：评论列表 `commentsBlock()`（阅读器 `readerComments()` 和转写页共用）、工作台弹框 `commentsPicker()`、结果 `commentsHtml()` / `commentsWire()` / 导出、一个视频的卡片 `commentsCard()`（转写详情页 `detailComments()`、贴链接弹窗 `commentsLinkModal()` 共用）。`app.js` 的 `buildIntakeRow()` 给 B 站链接加按钮。
- 挂钩：`study.js`（图标、标题、进度、渲染、导出、重试）、`projects.js`（⋯ 菜单、`srcIsBili`、阅读器清空 / 打开时调）、`index.html`（格子、`#src-reader-comments`、脚本）、`i18n.js`（`cm.*`、`st.k.comments`）、`style.css`（`.cm-*`）。

## 踩过的坑
- 不登录 B 站只给 3 条，旧接口 `x/v2/reply?pn=` 不登录直接空：所以抓之前先验登录，拿不到就报 `bili_login`。
- 楼中楼的 `parent` 是被回复那条，不是楼主；按楼归组要用 `root`。
- `comments.py` 别在模块顶上 `import asyncio`：`_rec_rows` 在请求线程里 import comments，跟后台线程同时首次导入 asyncio 会撞循环导入（test_notebook_fixes 偶发失败就是它）。
- 测试数据的期 ID 要是带横线的 UUID：`/api/history/<id>` 只认这种，左栏打开转写会 400（评论接口自己的校验更宽，只防路径穿越）。
- 截图用 Playwright 自带的 `chromium_headless_shell`（`~/Library/Caches/ms-playwright/…-1228`）+ promo 项目里的 `puppeteer-core`；**别用用户装的 Chrome**（10-02 弄崩过）。`evaluateOnNewDocument` 里别碰 `document.documentElement`（那时还是 null）。
