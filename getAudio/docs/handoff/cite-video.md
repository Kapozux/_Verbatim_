# 出处就地看原视频 / 听原音

## 速览

- **现状**：分支 `feature/cite-video`（worktree `.claude/worktrees/cite-video`），10-04 本地提交，还没合进 main / 5001。
- **怎么跑**：YC 副本开在 5071：`GETAUDIO_DATA_DIR=CODEelse/.claude/scratch-data/yc-dev PORT=5071 bash run.sh`，打开 Y Combinator 笔记本，在回答里点一个出处。测试 `./run_tests.sh cite_media`。
- **已知问题**：B 站播放器没有跳转命令，换时间只能重新加载 iframe；关了嵌入的 YouTube 视频只能点下面的外链；出处准不准取决于转写时间点（YC 的 DeepSeek 卡 94% 在 ±30 秒内）。
- **下一步**：合进 main（先问其他窗口、确认 5001 空闲）；用它录 README 第一屏 GIF。

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

## 验证

- `tests/test_cite_media.py` 9 条：认来源、起点、外链、时间写法、编号列表、接口字段。指到旧版 `static`（`CITE_STATIC=…`）时 9 条全红。
- 实测（5071，YC）：点 34:00 的出处，YouTube 播放器停在 34:02；点文字稿 2:45 那一句，播放器跳到 2:45。1700 / 1440 / 1000 / 400 四个宽度都没有横向滚动；1000 宽度下播放器 513×289，高亮那一句紧挨在播放器下面。截图在 `CODEelse/.claude/scratch-data/shots/`。
- 不花钱：只读现成的回答和转写。
