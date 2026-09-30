# 常见问题

[English](../faq.md) · [快速上手](getting-started.md) · [转写引擎](engines.md) · [博主分析](creators.md)

- [打不开（Gatekeeper 拦截）](#打不开gatekeeper-拦截)
- [打开了但什么都没出现](#打开了但什么都没出现)
- [提示「API key 缺失或无效——去 Settings 检查」](#提示api-key-缺失或无效去-settings-检查)
- [提示「连不上模型接口」或「User location is not supported」](#提示连不上模型接口或user-location-is-not-supported)
- [下载失败 / B站风控](#下载失败--b站风控)
- [用电池时 Whisper 拒绝运行](#用电池时-whisper-拒绝运行)
- [提示「内容被模型拦截」](#提示内容被模型拦截)
- [本地文件路径读不了](#本地文件路径读不了)
- [博主分析中途停了或失败了](#博主分析中途停了或失败了)
- [数据存在哪](#数据存在哪)
- [怎么卸载](#怎么卸载)
- [在哪看花了多少钱](#在哪看花了多少钱)
- [小红书页用不了](#小红书页用不了)

## 打不开（Gatekeeper 拦截）

Verbatim 没有用 Apple 开发者证书签名，所以 macOS 第一次会拦住它。

1. 在「应用程序」里右键（或按住 Control 点按）**Verbatim**，选「打开」。
2. 弹窗里点「打开」。

较新的 macOS 上，弹窗可能只有「完成」或「移到废纸篓」。这时：

1. 点「完成」。
2. 打开「系统设置 → 隐私与安全性」。
3. 往下滚，在提示 Verbatim 已被阻止的那一行点「仍要打开」。
4. 输入密码确认，再点「打开」。

如果提示应用「已损坏」，在终端里运行一次下面这行，再重新打开：

```bash
xattr -dr com.apple.quarantine /Applications/Verbatim.app
```

这个 DMG 只支持 Apple Silicon 芯片的 Mac（M1 及以后），Intel 芯片的 Mac 用不了。

## 打开了但什么都没出现

这是正常的，Verbatim 没有自己的窗口。

1. 看屏幕顶部菜单栏有没有 **◉ Verbatim**。
2. 点它，选 **在浏览器中打开**。地址是 `http://localhost:5001`。

应用已经在运行时再打开一次，只会重新打开浏览器，并提示「Verbatim 已经在运行」。启动失败会弹窗说明原因，
其中一种情况是 5001 端口被别的程序占了。

## 提示「API key 缺失或无效——去 Settings 检查」

云端引擎或分析步骤需要的 key 没填，或者 key 被拒绝了。

1. 点左边栏的 **设置**。
2. 打开 **Gemini**（Qwen-ASR 和精准模式用 **DashScope**，Claude 分析用 **OpenRouter**）。
3. 把 key 粘到 **API key**，点 **测试连接**，再点 **保存**。
4. 重新跑这一条。

哪些功能要哪个 key：

| 功能 | key |
|---|---|
| Whisper 转写 | 不需要 |
| Gemini、Gemini 3.5 Transcribe、摘要、AI 标题、博主页的全部功能 | Gemini |
| Qwen-ASR | DashScope |
| 精准模式 | Gemini 和 DashScope |
| 用 Claude 做博主分析 | OpenRouter |

完全不想填 key，就选 **Whisper**。这时摘要和 AI 标题会跳过。

## 提示「连不上模型接口」或「User location is not supported」

Gemini API 不是所有国家和地区都能用，而且要从你的网络能连上。

- 看到「User location is not supported」，说明 Google 拒绝了你当前出口所在地区的请求。换一个出口在支持地区的网络或 VPN，
  或者在 **设置 → Gemini → Base URL** 填一个代理 / 中转地址（可选，留空就是官方地址）。
- 「连不上模型接口——网络或代理问题」是请求没发出去。检查网络以及代理、VPN。批量任务中途断网的话，Verbatim 会暂停云端任务，
  网络恢复后自动重试。
- 「被限流 / 额度用完——稍后重试」是碰到了 Google 的限额。等一会儿再跑。

## 下载失败 / B站风控

链接用 yt-dlp 下载。常见原因：

- **B站返回 412。** B站按 IP 做风控，频道（空间）页面尤其严。Verbatim 已经会等一下再重试。还是不行的话：
  - 过一段时间再试。同一个 IP 马上重试只会更糟。
  - 源码运行时，在 Google Chrome 里登录 bilibili.com：下载会借用 Chrome 的登录 cookie（macOS 可能会问是否允许访问
    「Chrome Safe Storage」，选允许）。应用版默认不借浏览器 cookie，见下文。
  - 频道链接用最简单的空间地址 `https://space.bilibili.com/<uid>`，不要带 `/video`。
  - 换一个网络或 VPN 出口通常就好了。
- **YouTube 提示「Sign in to confirm you're not a bot」。** 源码运行时在 Chrome 里登录 YouTube 再试；应用版过一会儿或换个网络再试。
- **浏览器 cookie。** 源码运行时下载会借用 Chrome 的 cookie（没装 Chrome 会自动跳过），可以在 `getAudio/.env` 里把
  `YTDLP_COOKIES_BROWSER` 设成别的浏览器（`firefox`、`edge`、`brave`……）或者留空。应用版除非设了这个变量，否则不借任何浏览器的
  cookie，所以没装 Chrome 也能用，也不会弹钥匙串授权。
- **「链接无法识别」。** 粘贴视频、播放列表或频道的完整 `https://` 地址。
- **网站改版了。** 网站改版后 yt-dlp 需要更新。源码运行时执行 `brew upgrade yt-dlp`。应用里自带一份 yt-dlp，要等新版应用。

源码运行还可以用 `YTDLP_PROXY`（B站返回 412 时换这个代理再试）和 `BILI_SESSDATA`（你的 B站登录 cookie，
yt-dlp 读不到频道列表时用它来读），写进 `getAudio/.env`。

## 用电池时 Whisper 拒绝运行

这是故意的。本地 Whisper 跑多久就会占满多久的性能核，用电池可能一路耗到电脑在任务中途关机，这种事发生过。

- 插上电源再跑一次，或者
- 这一条改用云端引擎（比如 **Gemini 3.5 Transcribe**）。

云端引擎失败、自动兜底到 Whisper 时，也会做同样的检查。

## 提示「内容被模型拦截」

Gemini 有时会拒绝某些内容（比如有版权的歌词、敏感话题）。这时 Verbatim 会自动用本地 Whisper 重转这个文件。
如果 Whisper 也跑不了（在用电池），这一条会失败。插上电源重跑，或者直接选 Whisper。

## 本地文件路径读不了

macOS 不允许应用按路径读取「下载」和「桌面」文件夹。把文件挪到「文稿」或其他文件夹，或者用
**选择或拖入音频/视频文件** 上传。

## 博主分析中途停了或失败了

应用重启后，分析不会自己接着跑，会被标成失败，出现在 **没跑完或失败的** 里。

1. 打开这个博主。
2. 打开 **运行详情与操作**。
3. 点 **继续**。已完成的转写和原话会复用，只补缺的部分。

如果是 Gemini 额度用完导致好几期失败，等额度恢复再点继续。高级设置里的 **Whisper 兜底** 可以让失败的云端期改用本地 Whisper。

## 数据存在哪

| 运行方式 | 数据目录 |
|---|---|
| Mac 应用 | `~/Library/Application Support/Verbatim` |
| 源码运行 | `getAudio/` 目录本身 |

里面有：

- `results/`：每条转写一个文件夹（正文、摘要、元数据）。
- `results/_chains/`：博主分析和合集。
- `uploads/`：下载和上传的音视频。
- `tasks.db`：任务列表。`usage.db`：费用记录。
- `settings.local.json`：你的 key 和设置。

打开 Mac 应用的数据目录：在访达里选「前往 → 前往文件夹…」，粘贴上面的路径。

新的转写默认不保留音频，除非打开 **设置 → 存储 → 保留音频用于回放**。同一页能看到音频占了多少空间，也能一键删掉。

如果装了 Google Drive 桌面版并已登录，转写稿、摘要和分析文档还会自动复制到「我的云端硬盘」里的 `Verbatim_备份`（不含音频）。
**设置 → 存储** 里能看到备份状态、改备份目录。备份只增不删。

Verbatim 运行时不要删除或挪动数据目录里的文件。

## 怎么卸载

1. 点菜单栏的 **◉ Verbatim**，选 **退出 Verbatim**。
2. 把「应用程序」里的 **Verbatim** 拖到废纸篓。
3. 想连数据一起删，就删掉 `~/Library/Application Support/Verbatim`。这会删掉所有转写和分析，要留的先复制出来。
4. 第一次用 Whisper 时下载的模型不在应用里，一般在 `~/.cache/huggingface`。删掉里面的 Whisper 模型文件夹可以腾出空间。
5. Google Drive 里的备份文件夹（`Verbatim_备份`）要你自己删。

## 在哪看花了多少钱

- **设置 → 费用**：每一次模型调用，本月和累计，按服务商、按用途、按模型分开，带 token 数。
- 转写详情页顶部一行会显示这一条的费用。
- 博主页会显示这次分析的模型费用。

有些模型没有内置价格（Gemini 3.5 Transcribe、阿里云语音识别），只计次数，显示为未计价。源码运行时可以在 `tasks.db`
旁边放一个 `prices.json` 补价格。准确的花费以 Google、阿里云或 OpenRouter 的账单页为准。

## 小红书页用不了

**小红书** 页依赖一个单独的采集项目，要驱动一个已登录的浏览器，Mac 应用里没有带。只有源码运行、并配好那个项目
（`XHS_PROJECT` 环境变量和 `uv`）时才能用。
