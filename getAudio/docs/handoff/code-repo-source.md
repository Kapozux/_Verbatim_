# 交接：代码库当来源（Daemon 读码）

## 速览（2026-10-03 更新）

| | |
|---|---|
| 现状 | 在用。4 个提交已推 GitHub Kapozux；正文里「未提交的两个修复」已在 main 本地提交（`88f1fed`），没推。GitLab 没推。Daemon 原版 3 个本地提交没推 |
| 怎么跑 | 笔记本 → 「＋ 添加来源」→「代码库」→ 填本机 git 仓库路径；后端测试 `./run_tests.sh repos` |
| 已知问题 | 导览卡偏「文档怎么说」，缺「代码实际怎么做」；deepseek-v4-pro 价格来自第三方文章，待核实；估价只有两个实测点；打包版跑不了自带 Daemon |
| 下一步 | 正文「已知问题 / 待办」第 2–7 条（第 1 条已做完；第 3 条里的自动重启已经关掉，5001 不再开 FLASK_DEBUG） |

写于 2026-10-03。给下一个接手的 session：先读这份，再读 `docs/adr/0005-code-repos-read-by-external-agent.md`。

## 一句话

Verbatim 的项目里可以加一个本机 git 仓库当来源：Verbatim 按 commit 做快照，让读码 agent Daemon 在快照里读代码、
写证据卡（文件 + 行号 + 原样代码 + 观察），每张卡跟代码逐字核对过才收；提问时回答里的出处是「文件:行」，
点开在左栏看到那几行高亮。

## 现在的状态

| 东西 | 状态 |
|---|---|
| getAudio 4 个提交 | 已提交，**已推 GitHub Kapozux**（`Kapozux/_Verbatim_` main = `400e611`） |
| 　`f74c575` | 自带 Daemon 副本 `daemon/` |
| 　`fee5988` | 阿里云 deepseek-v4-pro 价格 + 费用页「读代码（Daemon）」标签 |
| 　`bae3dc0` | 后端：`repos.py`、ask / citations / app 的接法、`tests/test_repos.py`、ADR-0005、GLOSSARY |
| 　`400e611` | 界面：`static/repos.js` + projects / explore / index.html / style / i18n / UI_DESIGN |
| GitLab | **没推**，落后 14 个提交（推的话单独推一次） |
| 未提交的两个修复（getAudio） | `repos.py` 的 `current()`、`app.py` 里两处改用它（读到一半服务重启 → 显示「中断」不一直转圈）；`static/repos.js` 末尾补画左栏（直接打开项目页时代码库那行不显示）；`tests/test_repos.py` 补一条。全量测试过。**注意 app.py 里还混着别的 session 没提交的改动，提交时只挑自己的块** |
| Daemon 原版 `~/Documents/Code_no_big_AI` | 3 个本地提交**没推**（远端 `Kapozux/Daemon`）：`d038ee2` 认 DeepSeek 的 `<invoke>` 写法、`2f99bc7` 认单独一行的 exit-verified、`b2fdc57` 危险命令检查放宽（用户 10-02 明确同意）。用户自己改的 README 没提交，别动 |
| 5001 上的实测项目 | 「读墨页」（chain `19c6ff3123b746feb95c4ea2af265fd2`），墨页 `pdf2md-web@e8ae781` 读过一次导览：12/12 卡对上、$1.09 |

## 关键文件

- `repos.py`：快照（`git archive`，跳过 .env / 像密钥的文件）、跑 Daemon、核对收卡（行号偏了改正、只差首尾空白收文件原文、被已有卡包住算重复）、估价（15 万 + 每行 16 个输入 token，按价格表算钱）、`current()`。
- `daemon/`：Daemon 的副本。只有 `llm.py` 是重写的（用 Settings 的 DashScope key 走阿里云、记 `usage.db` purpose=`repo_read`、内容审查拒收时跳过那段输出），其余照抄原版、只删了 openai / dotenv 导入。同步原版：照抄那几个文件再删两行导入，`llm.py` 别覆盖。`daemon/README.md` 记着拷自哪个提交。
- 数据：`results/_repos/<repo_id>/`（meta.json、snapshot/、cards.json、runs/<n>.log）；项目在 `sources.json` 的 `repos` 里登记。
- 接口：`POST /api/chain/<id>/sources/repo {path}`、`POST …/sources/repo/<repo_id>/read {question}`、`GET /api/repos/<id>`、`GET /api/repos/<id>/file?path=`。
- 卡片 id `r<固定号>-<下标>`，提示词里短别名 `r1-3`（citations.prompt_id）。
- 测试：`./run_tests.sh repos`（36 条左右，含副本对本机假阿里云的端到端，不连外网、不花钱）。

## 怎么用

5001 打开任意项目 → 左栏「＋ 添加来源」→「代码库」→ 填本机 git 仓库路径 → 弹框里可写一个问题（留空 = 导览），
按钮上有估价 → 开始读，几分钟后左栏那行变成「N 张卡」→ 提问。读一次大约 $0.3–2.5（getAudio 4.6 万行 $1.58，墨页 $1.09）。

## 已知问题 / 待办（按建议顺序）

1. 提交上面那两个未提交修复（只挑自己的块）。
2. 导览读出来的卡偏「文档怎么说 / 测试证明」，几乎没有「代码实际怎么做」：可以改 `repos.DEFAULT_TASK`，要求至少一半是「自证」卡。
3. 5001 是 Flask 自动重载：别的 session 改 .py 就重启，读到一半的 Daemon 会被打断（现在只是显示「中断」）。长期办法：读码放到独立进程 / 重启后接着收快照里已经写好的卡。
4. 百炼 deepseek-v4-pro 的价格（12 / 24 元每百万 token）来自第三方文章，用户在控制台核实后改 `usage.py` 或 `prices.json`。
5. 估价只有两个实测点，多读几个仓库再校准。
6. 打包版（DMG / EXE）跑不了自带 Daemon：`sys.executable` 是打包后的程序，不是 Python。
7. 卡片位置字段：已跟画面证据卡 session 说定，画面卡真进提问语料时一起迁成一个 `loc` 对象（见 ADR-0005 后果），现在不动 `card_view`。

## 规矩（别忘）

- GitHub / GitLab：一次只推一个远端、推前 fetch、commit 尾行只留 Co-Authored-By，不加 Claude-Session（CLAUDE.md 红线）。
- 5001 上有任务在跑时不要重启。
- 启动 Daemon（会执行 shell）前要用户当次同意；Claude Code 自动模式默认会拦。
- 工作区里常有别的 session 没提交的改动，提交只挑自己的块（按 difflib 块挑、用 `git update-index --cacheinfo` 写暂存区，路径要带 `getAudio/` 前缀）。
