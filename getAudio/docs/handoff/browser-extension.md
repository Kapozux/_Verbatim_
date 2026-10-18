# 浏览器插件（在视频页一键转写）

## 速览

- **现状**：Chrome MV3 插件在 `extension/`，分支 `feature/video-ext` 已本地提交，还没合进 main、5001 还没重启（重启后插件才能连上 5001）。
- **怎么跑**：`chrome://extensions` → 开发者模式 →「加载已解压的扩展程序」选 `getAudio/extension`；安装说明见 `extension/README.md`。
- **已知问题**：只认 localhost / 127.0.0.1；「从这里开始转」读播放器时间只在 Playwright 里间接测过（没有真点工具栏）；右键菜单、快捷键、系统通知没有自动化测试。
- **下一步**：合进 main、重启 5001 后，用户在自己的 Chrome 里装上，真点一次 YouTube 和 B 站。

## 做了什么

- 后端只改了跨站防护：`app.py` 的 `EXTENSION_ORIGIN` 放行这一个插件 ID（manifest 里写死了 key，ID 固定）。决定和理由见 ADR 0020。
- 插件文件：
  - `verbatim.js`：设置、调接口、整理视频链接、记录提交过的任务（`chrome.storage.local` 的 `jobs`，最多 30 条）、读播放器时间。
  - `background.js`：右键菜单、`Alt+Shift+V`、查进度（头一分钟 5 秒一次，之后 alarms 30 秒一次）、系统通知。
  - `popup.*`：工具栏弹窗。颜色是从 `static/style.css` 抄过来的 token（插件引用不到那份文件）。
  - `i18n.js`：中英文案，跟着浏览器语言走。

## 测试

- `tests/test_cross_site.py`：自己的插件放行、别的插件拒绝、manifest key 算出来的 ID 跟后端对得上。
- 端到端（10-18，没写进 `run_tests.sh`，因为要连外网和真 Chromium）：脚本在 `.claude/scratch-data/video-ext/e2e.py`、`e2e2.py`。
  用 5047 端口的空数据实例，不放 key（没字幕的话只会失败、不会花钱）。实测：
  - TED 视频（带 `list=` 和 `t=`）→ 链接整理成单个视频 → 走字幕通道，268 段，$0 → 弹窗显示「转好了」。
  - B 站 `?p=1&spm_id_from=…` + 从 83 秒开始 + 指定笔记本 → 提交成功，链接整理对了（没 key，转写这一步失败）。
  - 笔记本下拉能列出来。

## 踩过的坑

- `/api/task/<id>` 正常返回（200）时里面也有 `error` 字段，那是任务本身的失败原因；一开始把它当成请求出错，状态一直停在「排队中」。
- Playwright 的 `channel='chromium'` 加 `--load-extension` 能在无头模式下装插件；Chrome 正式版从 137 起不认 `--load-extension` 这个参数。
