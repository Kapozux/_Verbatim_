# 交接：画面描述（画面轨）

## 速览（2026-11-16）

| | |
|---|---|
| 现状 | 11-16 合进 main（`22155a2`）、14:39 重启 5001，在用。有视频默认做（转写页 + 接口），博主链没开。全量测试 76/76 过；scratch 实例真 Haiku 实测过 |
| 怎么跑 | 「转写」页勾「同时描述画面（视频）」；没声音的视频直接拖进来（不用勾）；链接来的旧期在详情页点「描述画面」。测试 `./run_tests.sh scenes` |
| 11-16 第二轮 | 用户：「大部分是有声音但不是人说话的（画面有字幕，声音里不会读）」→ 加**字幕模式**：转出白卷 / 引擎回空文本的视频改读画面字幕当正文（不再落 Whisper）；话太少（每分钟 < 40 字）的自动加画面轨，不用勾。合成视频真 Haiku 实测：背景不动、动着的两种，6 句字幕全对、时间对得上 |
| 11-16 第三轮 | 用户：「太麻烦了，一律默认都转（有视频的）」→ 转写页勾选默认勾上；接口（`/upload`、`/upload_batch`、`/api/transcribe_local`、`/api/transcribe_urls`，插件和 MCP 走这些）没传 `scenes` 也当要（`_want_scenes`；`scenes=0` 不要；`.env` 的 `SCENES_DEFAULT=0` 改回默认不做）。**博主链没开**（几百期每期重下视频、B 站风控、Haiku 额度），等用户定 |
| 已知问题 | 没拿真的「配乐 + 字幕」视频试过（只有合成的）；背景一直在动的视频字幕模式约每秒一帧，长视频封顶 300 帧会漏句；不进资料库搜索；服务重启时排队中的画面描述会丢（不自动恢复）；没声音的**链接**视频没处理（下载只下音轨）；上传的旧期补不了（视频转完就删了） |
| 11-16 第四轮 | 用户定：**所有视频都 3×3 拼图读（Haiku）**，每格 512 宽、一次调用一张图 9 帧；每格都要**完整描述画面**，画面上有字就先照抄所有字（标题、要点、字幕，错字也不改、不翻译）再描述画面，没字就只描述画面。普通 / 字幕模式只差在挑帧和去重，提示词同一个（`PROMPT`，`CAPTION_PROMPT` 删了），批之间不接话。实测见 `docs/handoff/activity.md`：拼图比单图省约 1/3、字幕相似 0.94 vs 0.95；Sonnet 更准（单图 0.98）但贵 10–15 倍，用户选 Haiku |
| 下一步 | 拿真的配乐 + 字幕视频试；博主链要不要默认开（等用户定）；用户真用几期看描述的粒度 / 语言合不合适；要的话把画面描述加进资料库搜索 |

## 一句话

用户（11-16）：「转写里面加入一个画面描述（按每个变化帧丢给 AI 读……因为有些时候视频没有音频）Haiku 就可以做。也是丢链接 / 文件（视频）才可以做。」
用户选了：有声音的视频**勾选、默认关**；画面描述**单独一条轨、按时间插进正文显示**，不写进转写稿。

## 怎么做的（scenes.py）

1. 拿视频：上传 / 本机路径的视频在转写收尾 `_finish_ok` 里留一份（`uploads/<tid>_scenes.<ext>`：上传的硬链接、本机路径软链到原件），做完删；链接来的按 `source_url` 用 `frames.download_video` 重下 ≤720p 画面。
2. 挑帧（`scene_times`，不花钱）：160×90 灰度、每秒 2 帧（超 1 小时 1 帧）；跟这一段第一帧比**整幅画面**（不像证据卡那样剔掉在动的人），平均差 > 9 开新的一段；< 1.5 秒的段并进前一段；> 20 秒的段每 20 秒补一帧；最多 150 帧（多了均匀抽）。每段取中间那帧。
3. 描述（`describe`）：一次 8 张图（768 宽）→ 每张一两句；第二批起带上一批最后一句，叫它「只说变了什么」。走 `frames._call` → `analyze._call_gemini_mm`：`TEXT_ENGINE=claude` 时先给本机 Claude Haiku（订阅，记 $0），失败落 Gemini flash-lite。
4. 存 `results/<tid>/visual/scenes.json`（`{version, model, duration, picked, scenes: [{start, end, text, img}]}`）+ `s_<十分之一秒>.jpg` 320 宽缩略图。视频不留。
5. 语言：有转写跟转写（`frames._lang`）；没声音默认中文，`SCENES_LANG=English` 可改。

**不把「那时说的话」给模型**：11-16 第一次实测带了说的话，Haiku 跟着话写——后面几张幻灯片全被写成「还是那个人在雪山前」；去掉以后 10 帧全对。

## 字幕模式（有声音但没人说话）

- 判断「没人说话」三处：没音轨（预检）；引擎交白卷（`_EmptyTranscript`）或回空文本（`_NO_SPEECH_MARKERS`：「返回空文本」等，11-16 实测 Gemini 对着纯配乐回的是这个、不是白卷）——在 `_run_transcription` 的 except 里 `_no_speech_to_scenes`，先于 OR / Gemini 后备试一次、后备也回空再试一次，都先于 Whisper；转成了但 `scenes.speech_sparse`（每分钟 < 40 字，`[音乐]` 这类不算）→ 正文照存，`_finish_ok` 自动排画面轨（`captions=True`）。
- 挑帧（`scene_times(..., captions=True)`）：画面横切 6 条，任何一条跟这段第一帧比平均差 > 4 就切；最短 1 秒、最长 6 秒补一帧、取段里 70% 处那帧（字幕常淡入）；上限 300 帧。背景不动的一句一帧；背景在动的约每秒一帧。
- 读：`CAPTION_PROMPT`，每帧回 `caption`（画面上的字原样抄全）+ `text`（一句画面）；4 批同时读；`merge_captions` 把连着的同一句（去掉标点空格比）并成一条。
- 正文一行：`字幕　［画面］描述`（`scene_line`）；详情页画面轨里字幕正常字体在上、描述灰斜体在下。

## 没声音的视频

- `preflight_media` 原来拒收没音轨的文件；现在**视频**改成收下、`extra_meta={'scenes_only': True}`（音频后缀但没音轨的照旧拒收）。
- `_run_transcription` 见到 `scenes_only`：不抽音轨，`_scenes_transcribe` 同步做画面描述，`scenes.as_segments` 当正文，引擎记 `scenes`，不做模型体检（会把画面句子当垃圾删），不存一份视频当音频。摘要 / enrich 照常（走 Haiku）。

## 接口

- `GET /api/scenes/<tid>` → `{status: done|queued|running|failed|'', error, can_make, est_frames, scenes?}`；`can_make` = 有 http 链接、没做过、不是 scenes 引擎。
- `POST /api/scenes/<tid>` → 链接来的期补做（排后台队列）；没链接 400。
- `GET /api/scenes/<tid>/s_<n>.jpg` 缩略图。
- 上传 `/upload`、`/upload_batch` 表单 `scenes=1`；`/api/transcribe_local`、`/api/transcribe_urls` JSON `scenes: true`。链接走字幕快速通道命中时也排上。
- 后台队列 `scenes.queue`：一个线程一次一期，状态在内存（`scenes.status`），做完 `library.changed`。

## 界面

- 「转写」页大按钮上面一行勾选（`#scenes-opt`），勾「只要评论区」时藏起来。
- 详情页 `static/scenes.js`：标题栏最左一个按钮（`#detail-scenes-btn`）。行是 `.scene-row`（不是 `.segment`，免得打乱说话人 / 声纹按下标对行），插在第一条开始时间更晚的正文行前面。开关存 `localStorage['verbatim_scenes_shown']`。
- 截图（1700 / 1000 / 400 × 浅深）：`.claude/scratch-data/scenes-test/shots/`。

## 费用

- Haiku 订阅：$0（11-16 实测 30 秒宣传片 10 帧、2 次调用）。
- 落到 Gemini flash-lite：约每帧 $0.0004；一期最多 150 帧 ≈ $0.06；一小时讲课视频大约 $0.03–0.06。

## 文件

`scenes.py`（新）、`app.py`（预检、`_scene_meta`、`_scenes_transcribe`、`_keep_video_for_scenes`、`_queue_scenes`、三个接口、`_run_transcription` 没声音分支、`_finish_ok` 排队、`_download_then_transcribe` 带 `scenes`）、`static/scenes.js`（新）、`static/app.js`（三处提交带 `scenes`、详情页挂钩）、`templates/index.html`、`static/style.css`、`static/i18n.js`、`tests/test_scenes.py`（新）、`tests/test_upload_preflight.py`（没声音的视频改成收下）。
