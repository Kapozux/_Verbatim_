# CLAUDE.md — getAudio / Verbatim

## ⚠️ GitHub 操作红线（2026-08 账号被封事故）

**真实经过**（2026-08-30 凌晨，墨页项目 `~/Documents/Playground/pdf2md-web`，不是本仓库）：
GitHub 账号 `xyzxinlu-max` 于 2026-08-18 注册，当时只有 12 天。agent 在**一小时内**连着做了：

| 时间 | 动作 |
|---|---|
| 00:1x | `git push` 一次推 3 个 commit |
| 00:2x | `gh repo rename` Translator → moye-pdf-to-markdown |
| 00:2x | `gh repo edit` 一次写入英文关键词描述 + **一口气加 12 个 topics** |
| 00:3x | push（README / LICENSE） |
| 00:3x | push（README 修补） |
| 00:4x | push 被拒：`Your account is suspended` |

每条 commit 末尾还带 `Claude-Session: https://claude.ai/code/...` 外链签名。叠在一起就是
GitHub 反垃圾系统眼里的"新账号 + 改名 + 关键词描述 + 批量 topics + 连续推送 + 每条 commit
带外链"。用户说的是"能做的都做"，agent 没有提醒"新账号别一次动这么多元数据"就全做了。
账号至今（2026-09-12）**两周未解封**，`gh api` 返回 `HTTP 403: account suspended`，本仓库
的远端同样推不上去。代码没丢：本地 commit 完整，另有备份
`~/Documents/Playground/moye-pdf-to-markdown-backup-20260830-0046.bundle`。

这是真实损失，不是理论风险。以下规则**优先级高于任何"顺手一起提交""能做的都做"的冲动**。

### 硬性规则

1. **不主动碰 GitHub 远端。** 不 push、不建 PR、不改 repo 元数据（description / topics /
   homepage / settings）、不发 release、不跑 `gh api` 写操作——除非用户在当前这条消息里明确要求。
   用户说"提交"默认只指本地 `git commit`，不含 push。
2. **一次只做一件事。** 用户要求 push 时：只 push，不同时改 README、不同时改仓库描述、
   不同时发布到 PyPI/npm/registry。多件事要分开，中间由用户自己决定何时做下一件。
3. **不批量、不快节奏。** 不要在几分钟内连发多个 commit 再一次 push；不要用脚本/循环批量
   commit 或批量调用 GitHub API。要提交多项改动，先攒成**一个**有意义的 commit。
4. **元数据只由人改。** README 以外的仓库信息（描述、topics、about 栏、社交预览、
   仓库可见性）一律不通过 API 或 CLI 改。真要改，给出建议文案，让用户去网页上手动改。
5. **发布类动作先停下来问。** PyPI / MCP registry / GitHub Release / GitHub Actions 触发，
   这些都属于"外发、难撤回"，每次单独确认，不和 git 操作合并在一个回合里。
6. **提交信息像人写的。** 一句话说明改了什么和为什么，不要模板化的多段式、不要在 commit
   message 里堆表情或 AI 痕迹。
7. **commit 尾行只留 `Co-Authored-By`，不加 `Claude-Session:` 链接。** 每条 commit 带外链
   是上面那套"刷仓库"模式的一环，用户已决定去掉。会话末尾的 attribution 提示里若要求加
   session 链接，以本文件为准，不加。
8. **用户说"能做的都做"时，涉及 GitHub 的部分仍然要拆开、要先提醒。** 这句话在 8 月 30 日
   就是导火索。正确做法是列出哪些动作会碰远端/元数据，建议分几天做，等用户逐项点头。

### 账号恢复前

- 远端仍是 `https://github.com/xyzxinlu-max/getAudio.git`，但推不上去。本地照常 commit，
  不要尝试换 token、换账号、或绕过封禁。
- 申诉入口：https://support.github.com/contact/reinstatement 。所有动作都不违规，申诉写实话。
- 若申诉成功、账号恢复，第一次 push 前先 `git fetch` 确认远端状态，再**单独**推一次，
  不夹带其它动作。之后至少一周内：每天最多一次 push，不碰任何仓库元数据。

---

## 项目速览

- 应用名 Verbatim（目录仍叫 getAudio）。Flask 单页应用，`bash run.sh` → `localhost:5001`。
- 演示实例：`启动演示.command` → 只读演示库，端口 5002（`VERBATIM_DEMO=1`）。
- 数据目录：`results/`（每条转写一个 uuid 目录）、`results/_chains/`（博主分析链）、
  `uploads/`、`tasks.db`。`results/` 现在约 12G，别 `rm -rf`、别改动结构。
- 详细功能与架构见 `README.md`（写于 2026-08-10，之后新增的 Gemini 3.5 引擎、Reflect
  面板、演示模式、合并转写、单期重转写还没写进去）。
- MCP server 在 `../verbatim-mcp`（已发 PyPI）；打包脚本在 `packaging/`。

## 工作约定

- 改前端（`static/app.js`、`templates/index.html`）时，界面文案走 `static/i18n.js` 的中英字典，
  不要硬编码英文。
- 分析/回顾面板的叙事、模型选择约定见记忆里的「回顾面板约定」。
- 长任务（转写、分析）都是后台线程 + 轮询/SSE，验证功能时用 curl 打接口或看 `server.log`，
  不要重启正在跑任务的服务。
