# 出处就地看原视频 / 听原音

## 速览

- **现状**：10-04 已在 main、5001 在跑。第二轮（`feature/cite-cards`）：左栏标题跟视频一起固定；回答下面的出处卡片里缩略图 + ▶ 就地播放。
- **怎么跑**：YC 副本开在 5071：`GETAUDIO_DATA_DIR=CODEelse/.claude/scratch-data/yc-dev PORT=5071 bash run.sh`，打开 Y Combinator 笔记本，在回答里点一个出处。测试 `./run_tests.sh cite_media`。
- **已知问题**：B 站播放器没有跳转命令，换时间只能重新加载 iframe；关了嵌入的 YouTube 视频只能点下面的外链；出处时间点走逐词时间轴（ADR 0012），目前只有 YouTube 字幕这一个来源，B 站 / 本机录音 / 无字幕的仍是段首（界面写「≈」）。
- **下一步**：README 第一屏放 `docs/media/verbatim-cite.gif`（在 `feature/demo-yc`，没合）；B 站缩略图要另调接口，现在只放 ▶。

## 做了什么

以前在笔记本里点出处，转写来源只会滚到那一句文字；YouTube 字幕直取的期本机没有音频，连声音都没有。现在：

- `projects.js` 的 `openTranscriptViewer`：按 `/api/history/<tid>` 的 `source_url` / `video_id` / `has_audio` 选播放器（`mediaSourceOf`）：
  - YouTube → `youtube-nocookie.com/embed/<id>?start=<秒>&enablejsapi=1`；同一期再点出处用 `postMessage` 的 `seekTo`，不重新加载；
  - B 站 → `player.bilibili.com/player.html?bvid=…&p=…&t=…`；
  - 本机有音频 → `<audio>`，`currentTime` 跳过去；
  - 都没有 → 跟以前一样只滚到那一句。
- 播放器贴在阅读器栏头下面（`.rd-media` sticky，`top` 由 JS 按栏头高度设），16:9，最高占屏幕 38%（窄屏一栏铺满时不把文字稿挤没）。下面一行「在 YouTube 打开（34:00）」。
- 高亮那一句滚到播放器正下方（`revealBelowMedia`）。点文字稿里任一句，播放器跳到那一句。
- 换一期、关阅读器时 `stopReaderMedia` 拿掉 iframe（藏起来的 iframe 会接着放）。
- 顺手修的两处：回答里的编号列表每项之间空一行时，原来每项都显示成「1.」（`renderMarkdown` 现在照原编号写 `<ol start>`）；阅读器打开时工作台只剩约 210px，两列格子把 Predictions 这类英文名截断了，改成一列横排。

## 第二轮（10-04）：出处卡片就地播放、标题固定

- `explore.js` `sourceItemHtml`：转写出处有 YouTube / B 站链接时，原话下面放缩略图（`mediaThumb`，YouTube 的 `i.ytimg.com/vi/<id>/mqdefault.jpg`）+ ▶ 时间；点了 `playCiteCard` 换成 iframe，`stopCiteCards` 把别的换回缩略图，`pauseReaderMedia` 让左栏暂停（不拿掉，点字稿还能接着放）。左栏开始放时反过来停卡片。
- 左栏有视频时 `#src-reader.rd-has-media`：标题 sticky（`top` 由 JS 按栏头高度设），视频贴在标题下面；`readerStickyBottom` 算出处那一句该滚到哪。
- 顺手：笔记本页左栏内边距是 16px，返回键那一栏原来按 14px 往上提，顶上露出 2px 滚上去的字，宽屏补了一条规则。

## 第三轮（10-04）：出处准到原话那一秒（ADR 0012）

- 问题：卡片时间点是段首，Gemini 一段 25–30 秒；「8% → 20%」出处 1:05，原话实际 1:19.4。
- `wordtime.py`：`words.json` 逐词时间轴（YouTube json3）+ `locate`（模糊比对，锚点用少见词）+ `queue`（新转写后台补）。
- `downloader.fetch_word_captions`：先要自动字幕 `-orig` 轨（词级时间），429 时原因里带 '429'。
- `backfill_words.py <chain_id>`：给已有的期补，间隔 20 秒、连着两次 429 停手。
- `ask.card_view` / `ask.retime_citations`：出口处换时间、标 `exact`；前端没 exact 的写「≈」；能内嵌播放的出处不再给「听这段 / 下载这段」。
- 测试 `tests/test_wordtime.py`（16 条，用 1:00–1:35 实测字幕，听错的词原样保留）。
- 阿里云转写（qwenasr / 精准模式）请求开 `enable_words`，`transcribe_audio(words_out=…)` 收词时间（`wordtime.merge_pieces` 拼片段），`_finish_ok` 在 `_save_results` 之前存 `words.json`（加片段偏移）。测试 `tests/test_wordtime_qwen.py`。
- Gemini 转写（来源不是 YouTube）：`_finish_ok` 调 `wordtime.queue_whisper`，音频硬链接到 `uploads/_align/`，后台一条线程跑本机 Whisper（跟 Whisper 转写共用 `_mlx_gpu_lock`），存 `words.json`（来源 whisper）后删音频；重启后剩下的重新排。`WORDTIME_WHISPER=0` 关。10 分钟音频约 70 秒。测试 `tests/test_wordtime_whisper.py`。
- 下一步：旧的 Gemini 期（音频没留、没 YouTube 字幕，比如 B 站）点出处时只下前后约 1 分钟现场对齐。

## 验证

- `tests/test_cite_media.py` 9 条：认来源、起点、外链、时间写法、编号列表、接口字段。指到旧版 `static`（`CITE_STATIC=…`）时 9 条全红。
- 实测（5071，YC）：点 34:00 的出处，YouTube 播放器停在 34:02；点文字稿 2:45 那一句，播放器跳到 2:45。1700 / 1440 / 1000 / 400 四个宽度都没有横向滚动；1000 宽度下播放器 513×289，高亮那一句紧挨在播放器下面。截图在 `CODEelse/.claude/scratch-data/shots/`。
- 不花钱：只读现成的回答和转写。
