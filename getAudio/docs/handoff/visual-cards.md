# 交接：画面证据卡（测试版）

最后更新：2026-10-03。下次开新会话时先读这份。

## 一句话

项目工作台多了一个「画面证据卡」格子：选全部或勾选几期 → 重新下一份 ≤720p 的视频 → 代码挑出画面变化的关键帧 → Gemini flash-lite 读幻灯片 / 图表 / 公式 / 代码上的字 → 每张卡 = 截图 + 原样抄的字 + 描述 + 当时在说的话，点时间点跳到转写那一秒。磁盘只留截图，视频删掉。

方法来自 `~/verbatim_outreach/video_understanding/README.md`（先看数值曲线、带时间戳的缩略图拼图、粗看再细看、要读字才看原图）。

## 状态

- 能用、测试通过（`tests/test_visual.py`，整套 `./run_tests.sh` 全绿）。
- **没提交。** 主目录里还混着别的会话没提交的改动（播客、幻灯片、代码仓库卡片、字幕快速通道），提交时只挑画面卡的文件，见下面「文件」。
- **5001 没重启。** Python 改动要重启后才生效，重启前确认没有在跑的转写 / 分析。
- 真实资料库里已经有一期做好了：`results/2410e424-d323-440d-be51-92c1387dad3d/visual/`（GLM-5.2 论文精读，36 张），弹框里显示「已做」、估价 0。

## 怎么做的（frames.py）

1. `download_video`：yt-dlp 只下画面，`bv*[height<=720]`，cookie / ffmpeg 位置跟下音频同一套。需要 meta.json 里有 `source_url`（http 开头）；本地上传读不了（上传的视频转写完就删了）。
2. `sample_gray`：160×90 灰度，每秒 2 帧（超过 1 小时降到 1 帧，不超过原生帧率）。
3. `content_mask`：相邻帧之间超过 15% 的时间都在变的像素算「在动的」（讲的人、摄像头），不参与比较。
4. `keyframes`：每帧跟这一段的第一帧比（不是跟上一帧），平均差 > 7 开新一段；一段取**最后一帧**（内容最满、擦掉之前）；短于 2 秒的段丢掉；dHash 汉明距离 ≤ 5 算同一张，记下它出现过的几段时间；最多 90 张。
5. `contact_sheets` + `triage`：30 张一张缩略图，每格烧「#编号 时间」，问模型哪些有信息。
6. `read_frames`：留下的原图（1280 宽）6 张一批，配上那段时间说的话（`said_during`，往前多算 5 秒），要 kind / title / text（原样抄）/ desc / obs。
7. 存 `results/<task_id>/visual/cards.json` + `f_<十分之一秒>.jpg`，`VERSION = 1`。做过的期 `load()` 直接返回，跨项目共用，不再花钱。

模型：`config.GEMINI_VISUAL_MODEL`（默认 gemini-3.1-flash-lite），记账 purpose = `visual`，ref = `visual:<task_id>`。

## 实测

| 期 | 关键帧 / 卡 | 用时 | 费用 |
|---|---|---|---|
| GLM-5.2 论文精读 7.6 分钟（幻灯片） | 38 / 36 | 41 秒 | $0.021 |
| Tabby 实测 4 分钟（录屏） | 37 / 35 | 约 40 秒 | $0.019 |

费用跟留下的帧数走，不跟时长走：约 $0.00055 / 张，一期封顶约 $0.05。`estimate_usd()` 按「每 8 秒一张、最多 90 张」估。

意外发现：画面能抓出转写的错。同一刻屏幕写「Fable 5 和 Mythos 5」「GPT-5.5」，转写写成了「Claude 3 Opus 和 Claude 3.5 Sonnet」「GPT-4.5」——转写模型把不认识的新名字换成了熟悉的旧名字。

## 文件（画面卡自己的）

新文件：`frames.py`、`static/visual.js`、`tests/test_visual.py`、这份 `docs/handoff/visual-cards.md`

共用文件里画面卡的部分（别的会话也改过这些文件，提交时用 `git add -p` 只挑这几块）：
- `app.py`：`_visual_rows`、`/api/chain/<id>/visual/episodes`、`/api/chain/<id>/studio/visual`、`/api/visual/<task_id>/<name>`；`_static_version()` 列表里的 `'visual.js'`
- `study.py`：`start_visual`
- `analyze.py`：`_call_gemini_mm` 多了 `purpose` 参数
- `config.py`：`GEMINI_VISUAL_MODEL`
- `static/study.js`：`ST_PATHS.visual`、stTitle / stMeta（进度「第 i / n 期」）/ 重试 / stWire 点击分发 / stCoverage / stRender / stExportDoc 里的 visual 分支
- `templates/index.html`：`data-st="visual"` 格子 + `visual.js` script 标签
- `static/i18n.js`：`st.k.visual` + `vs.*`（中英两块，紧跟 `st.k.compare` 后面）
- `static/style.css`：「画面证据卡」那一段
- `docs/UI_DESIGN.md`：位置表一行 + 决策记录一行；`GLOSSARY.md`：「画面证据卡」词条
- `packaging/*.spec`：hiddenimports 里加了 `'frames'`（双保险，不加也行）

## 还没做 / 已知问题

- **没做质量评测。** 只测了同一博主的两期。计划：8 期覆盖幻灯片 / 录屏 / 白板 / 真人出镜无字 / 混剪 / 竖屏 / 长课 / 英文，人工标标准答案，量漏帧率、重复率、抄字错误率（重点看数字）、编造率，约 $0.2。等用户点头。
- 挑帧那一遍几乎不筛（38 留 36）；缺真人出镜这类反例来验证它会不会筛。
- 同一张图加了红框会重复成两张（例：#14/#15、#27/#28）。
- obs 字段偏空（「用于佐证演讲者……」），评测后决定留不留。
- 「全部」没有数量上限、没有间隔，一次勾几十期会连着下几十个 B 站视频，有风控风险。
- 一份产出要等所有期都做完才有结果；中途服务重启会标成失败（已做完的期其实存下了，不会重复花钱，但界面看不出来）。
- 画面卡不进问答语料。真要接时，跟 getaudio-f6 会话约好了统一位置格式：card_view 加 `loc`，画面卡是 `{kind: 'frame', task_id, sec, until, img}`，点击退回 `openTranscriptViewer(task_id, sec)`；写在 `docs/adr/0005` 的「后果」里。动 card_view 前先跟那边说。

## 等用户拍板的三件事

1. 重启 5001（确认没在跑任务）。
2. 跑上面那轮小评测（约 $0.2）。
3. 分功能各自提交到本地（画面卡一个 commit，只 `git add` 上面列的部分；不 push，commit 尾行只留 Co-Authored-By，见 CLAUDE.md 的 GitHub 红线）。

## 验证方法

- 后端：`./run_tests.sh visual`（合成视频，禁外网，不花钱）。
- 界面：不要碰 5001。用独立数据目录另起一个实例：
  `GETAUDIO_DATA_DIR=<草稿目录>/data PORT=5003 ../venv/bin/python app.py`，把要测的 `results/<id>` 复制进去，建项目、加转写，再用系统 python3 的 Playwright（chromium headless shell，**别用你自己装的 Chrome**）截 1700 / 1000 / 400 × 浅 / 深。窄屏要先点 `.nb-switch [data-nb=studio]` 再点列表里那一条。
