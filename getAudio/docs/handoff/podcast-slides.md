# 交接：音频播客 + 幻灯片（2026-10-02 ~ 10-03）

## 速览（2026-10-03 更新）

| | |
|---|---|
| 现状 | 两个工作台格子都在用：已在 main 本地提交（`15c3915`），5001 已重启。下面正文里「没提交、没重启」是 10-02 写时的状态 |
| 怎么跑 | 笔记本 → 工作台格子「音频播客」/「幻灯片」；后端测试 `./run_tests.sh podcast slides` |
| 已知问题 | Windows 上幻灯片字体（PingFang SC）会被替换，没验证；TTS 2026 年底前免费，但费用页按以后的价记 |
| 下一步 | 播客改单句；幻灯片 AI 配图、导出 Google Slides（都等用户定） |

两个都是 NotebookLM 式的工作台产出，已经做完、测试全过，**但都没提交，5001 也还没重启**（重启后才看得到）。

## 一句话现状

| 功能 | 状态 | 用户反馈 |
|---|---|---|
| 音频播客 | 完成，工作台格子「音频播客」 | 试听：「和 NotebookLM 一模一样」 |
| 幻灯片 | 完成，工作台格子「幻灯片」，第二版排版 | 第一版「太垃圾」→ 对照 pptx skill 参照稿重做了两轮（演讲版、详细版） |

## 文件

**新文件**
- `podcast.py`：播客全流程（提示词、写脚本、编辑润色、检查、原声裁剪、TTS、拼接、产出）
- `slides.py`：幻灯片全流程（提示词、检查、python-pptx 排版、预览图、改单页）
- `static/podcast.js`、`static/slides.js`：弹框、播放器 / 预览、改单页
- `static/slide-icons/`：62 个 Lucide 图标 × 白 / 珊瑚两色 PNG + LICENSE（ISC）
- `tests/test_podcast.py`（15 项）、`tests/test_slides.py`（14 项）
- `tools/slides_dev/gen_deck.py`、`render_sheet.sh`：调排版用（见下）

**改过的共用文件（都是增量）**
- `app.py`：路由 `/studio/podcast`、`/studio/podcast/estimate`、`/studio/<oid>/audio`、`/studio/slides`、
  `/studio/slides/estimate`、`/studio/<oid>/pptx`、`/studio/<oid>/slide/<n>`、`/studio/<oid>/revise`；
  DELETE 产出时连 mp3 / pptx / 预览图一起删；`_static_version()` 加了 podcast.js、slides.js
- `static/study.js`（每个 visual 分支旁边加 podcast / slides 分支）、`templates/index.html`（两个格子 + script）、
  `static/i18n.js`（`pod.*`、`sl.*`，中英）、`static/style.css`（末尾两段 + `--deck-*` token）
- `usage.py`（两个 TTS 价格）、`requirements.txt` + 两个打包 spec + `packaging/requirements-windows.txt`（python-pptx）
- `docs/UI_DESIGN.md`（位置表 + 决策记录）、`GLOSSARY.md`（产出 kind、「原声」）

**同一工作区里还有别的 session 没提交的改动**：画面证据卡（frames.py / visual.js）、代码库当来源（repos.py）、
字幕快速通道（app.py 转写部分）。提交时按功能分开，别混成一个 commit；按 CLAUDE.md 只本地提交、不 push。

## 播客怎么工作（podcast.py）

1. flash-lite 写脚本：一次调用里先 `plan` 再逐句 JSON（speaker / text / style / cite），可插 `{"clip": 卡片id}`
2. flash-lite 编辑：对照原段落挑错、改无聊的地方、加口语（语气词、接话、`<laugh>` 等）
3. 代码检查：说话人白名单、出处换真 id、语气标记白名单、原声只能是代码筛的候选（证据卡、有时间点、8–60 字）
4. TTS：`gemini-3.8-flash-tts` 走 REST（SDK 1.47 不认 `speechMetadata`），每块 ≤10 句 ≤2 人，3 路并行；
   **线程池里要 `usage.bound` 包住**，不然 TTS 费用记不到产出上
5. 原声：本机音频（results/<tid>/audio.* / voice.m4a / uploads/<tid>.*）优先，没有就按链接只下那几秒
   （B 站走 playurl 接口，yt-dlp 遇活动页会失败）；先多裁一截，再用本机 mlx-whisper 逐词对齐原话，没有 Whisper 就按停顿下刀；
   取不到由主持人念原话
6. ffmpeg 拼 mp3，每句记开始时间（前端高亮当前句、点句跳转）

费用：TTS 2026 年底前免费；价格表按之后的价记（10 分钟约 $0.27），所以免费期内费用页会多算。

## 幻灯片怎么工作（slides.py）

- 模型只出 JSON：8 种版式（cover / section / points / quote / stat / compare / timeline / closing），
  要点带 `detail`（详细版）和 `icon`（从 `ICONS` 词表挑）；备注第一句会排到页面上当「这一页说的是」
- 代码检查：引语要在被引原文里逐字找得到（找不到换卡片原话或降级）、数字要在原文里、编造的出处删掉、缺标题补上
- 排版：深底封面 / 章节 / 结尾 + 白底内容页；每页圆底图标；要点页轮换 cards / rows（右侧深色栏）/ panel / bars，
  只有一条要点用大字结论；字号按框自动缩、防孤字（`no_orphan`）；形状去掉主题自带阴影（删 `p:style`）
- 预览：本机有 LibreOffice + pdftoppm 时，把真 .pptx 渲染成 PNG（`studio/<id>_slides/`，profile 在 `DATA_DIR/.lo_profile`）；
  没有就退回 HTML 近似预览
- 改单页：只重写那一页 → 重排文件 → 重出预览（同步，十来秒）
- skill 的许可证不允许把它的代码 / 提示词放进 Verbatim：slides.py 是自己写的，只借鉴设计思路

## 示例产出（真实项目里）

- 罗肖尼（`1ef93c920370484b85c7ddd60894f06e`）：播客 `c2acc8c3857c`（6 分钟）、幻灯片 `428d71094244`（详细 10 页）
- 夸克说（`7d44ba27ae9f492093529b3cbb17f4d5`）：幻灯片 `f06b95479cc2`（详细 12 页，最新）、`1c5f8c1aee86`（演讲 6 页）、
  `8561279ab9c9`（用户截图那份旧大纲，没图标，已按新排版重排）

## 怎么验证 / 接着调

```bash
cd ~/Documents/CODEelse/getAudio
./run_tests.sh                     # 全部后端测试（约 10 秒，假 key、禁外网）
./run_tests.sh podcast slides      # 只跑这两个

# 调幻灯片排版：出一份（几美分）→ 改 slides.py → 只重排（不花钱）→ 看拼图
../venv/bin/python tools/slides_dev/gen_deck.py 7d44ba27ae9f492093529b3cbb17f4d5 detailed 10 kd
../venv/bin/python tools/slides_dev/gen_deck.py --render kd
bash tools/slides_dev/render_sheet.sh /tmp/slides_dev/kd.pptx /tmp/slides_dev/kd.jpg

# 看界面：别动 5001，另起一个带临时数据目录的实例
GETAUDIO_DATA_DIR=/tmp/vb_ui PORT=5003 ../venv/bin/python app.py
# 截图用系统 python3（带 playwright）：/Library/Developer/CommandLineTools/usr/bin/python3
```

## 还没做 / 可以做的

- 播客：改单句（只重写、重录一句）
- 幻灯片：AI 配图（用户说第一版先不画）；直接导出 Google Slides
- Windows：幻灯片字体用 PingFang SC，Windows 上会被替换，没验证过
- 费用页：TTS 免费期内可以改成记 $0
- 提交（按功能分开）、重启 5001
