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
  `uploads/`、`tasks.db`、`usage.db`（每次模型调用的 token / 费用记账，Settings → Costs 汇总；
  价格表可用 `prices.json` 覆盖）。别 `rm -rf`、别改动结构。
- 详细功能与架构见 `README.md`（写于 2026-08-10，之后新增的 Gemini 3.5 引擎、Reflect
  面板、演示模式、合并转写、单期重转写还没写进去）。
- MCP server 在 `../verbatim-mcp`（已发 PyPI）；打包脚本在 `packaging/`。

## 工作约定

- 改前端（`static/app.js`、`templates/index.html`）时，界面文案走 `static/i18n.js` 的中英字典，
  不要硬编码英文。
- 改界面（笔记本页、工作台、弹框、菜单、样式）前先读 `docs/UI_DESIGN.md`：功能放哪、用哪种容器、
  视觉 token、三个宽度的验证都在里面；加了新功能要补进它的位置表。
- 叫法以 `GLOSSARY.md` 为准（笔记本 / 博主 / 人 / 来源 / 期 / 证据卡 / 原文段落 / 出处 / 产出……；
  界面说「笔记本」，代码和接口仍叫 project），架构上的决定记在 `docs/adr/`，别推翻没写理由的。
- 改完后端先跑 `./run_tests.sh`（约 10 秒；只用临时目录和假 key、禁外网，不碰资料库、不花钱），
  全过才算改完。新功能、修 bug 都在 `tests/` 里补一条能失败的测试。
- 分析/回顾面板的叙事、模型选择约定见记忆里的「回顾面板约定」。
- 长任务（转写、分析）都是后台线程 + 轮询/SSE，验证功能时用 curl 打接口或看 `server.log`，
  不要重启正在跑任务的服务。

## 多窗口一起开发（2026-10-03 起）

常常有好几个 Claude 窗口同时改这个仓库。以前各干各的：改同一个 `app.py`、一边改一边自动重启
5001、各自改 `.env` 里的模型、决策记录撞号、交接文档散在四处。以下规矩就是为这些写的。

1. **开工先读 `docs/BOARD.md`**（总表）。认领一件事就在「正在做」写上自己；新发现的 bug、想法、
   要用户拍板的事都记进去；收工前更新状态和下一步。总表是唯一的待办清单，别另起。
2. **不在主目录（5001 跑的那份）上直接改。** 每个窗口一个 worktree 一个分支：
   `git worktree add .claude/worktrees/<名字> -b feature/<名字>`，再
   `ln -s /Users/kapozux/Documents/CODEelse/venv .claude/worktrees/<名字>/venv`。
   要看界面就另开一个端口、用复制出来的数据：
   `GETAUDIO_DATA_DIR=<scratch 里的副本> BACKUP_DIR=<scratch> PORT=50xx bash run.sh`，
   别让两个实例写同一个 `results/` 和 `tasks.db`。要 key 就把主目录的 `getAudio/.env` 软链过去，
   不复制、不提交。
3. **合进 main、重启 5001 只由一个窗口做**：先问其他窗口（ListAgents / SendMessage），确认
   `tasks.db` 里没有在跑的任务、笔记本没有建到一半的，全量 `./run_tests.sh` 过了再合、再重启。
   5001 不开 `FLASK_DEBUG`（改文件就自动重启会打断正在跑的任务）。
4. **全局设置先登记、等用户点头**：`.env` 里的模型、`config.py` 的默认值、价格表、几个功能共用的
   提示词，改之前先写进总表「等用户拍板」。只给自己的功能加新开关可以，默认值要跟现在一样。
5. **做完的标准**：先写会失败的测试再改；改界面截 1700 / 1000 / 400 三张图看过；会花钱的给出
   估价（每次 / 每月）；叫法、界面位置、架构决定分别补进 `GLOSSARY.md`、`docs/UI_DESIGN.md`、
   `docs/adr/`；交接文档写好；本地提交。
6. **交接文档一个功能一份**，放 `docs/handoff/<功能>.md`，开头是「速览」四行：现状 / 怎么跑 /
   已知问题 / 下一步。功能有变化就改速览，别在别处另写一份。
7. **决策记录先占号**：在总表「决策记录编号」里登记了再写 `docs/adr/00xx-*.md`。
8. **共用文件先打招呼**：`app.py`、`static/i18n.js`、`static/style.css`、`static/study.js`、
   `templates/index.html` 几个窗口都会碰。动之前 SendMessage 说一声改哪一段；提交时只暂存
   自己的那几块（把 `git diff <文件>` 里自己的部分存成补丁再 `git apply --cached`），别把别人
   没提交的改动一起带走。
9. **不用后台 Workflow 跑长活**：会话一重开它就被杀。用前台 Agent 一步一步做，每步做完就提交。
10. **省钱**：会花钱的批量操作先给估价等用户点头；大段 AI 文字（回顾、综述）定期写，不跟着每次
    数据变动重写，留手动刷新。
