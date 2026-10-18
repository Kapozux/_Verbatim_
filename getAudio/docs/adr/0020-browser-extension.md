# 0020 浏览器插件：在视频页点一下就交给本机 Verbatim 转写；跨站防护只多放行这一个插件

日期：2026-10-18

## 背景

用户 10-18：「要是能进入视频网站，有个插件自动能点一下，后台就给我转字幕了」。后端早就有
`/api/transcribe_urls`（字幕快速通道、合集展开、` @start-end` 时间段、`project_id` 进笔记本），缺的是浏览器那一头。

难点在 10-06 加的跨站防护（`_cross_site_guard`）：改动类请求的 Origin 必须是 Verbatim 自己。插件发请求的 Origin 是
`chrome-extension://<id>`，会被拦。另一个方案是「书签按钮跳回 Verbatim 确认页再提交」，防护一行不改；用户选了真插件。

## 决定

1. **插件放在仓库里 `extension/`（Chrome MV3，没有构建步骤），自己用「加载已解压的扩展程序」装。**
   不上架 Chrome 商店：上架是对外发布，要单独问用户。
2. **manifest 里写死公钥 `key`，所以扩展 ID 固定是 `dncnefkhhhkebmpiponfhcmmoaakaani`**（装在哪个目录都一样）。
   后端 `EXTENSION_ORIGIN` 只放行这一个；别的插件、别的网站照样 403。测试里从 manifest 的 key 现算 ID，跟后端对不上就失败。
   这个公钥谁都能抄进自己的插件冒充，但能往你浏览器里装插件的人本来就什么都能做，不算新口子。私钥没进仓库，也用不着。
3. **插件不加新接口**，只用现成的：`POST /api/transcribe_urls`、`GET /api/task/<id>`、`GET /api/chains`（列笔记本）。
   设了密码 / 令牌的话，插件发 `Authorization: Bearer`。
4. **整理链接在插件里做**：YouTube 去掉 `list=`（不然后端当播放列表整个展开）、`t=` 和跟踪参数；B 站只留分 P 的 `p=`。
   「从这里开始转」读播放器当前秒数，发 ` @<秒>-`。
5. **进度靠插件自己查，不用 SSE**：提交后头一分钟每 5 秒查一次（有字幕的几秒就完），之后 `chrome.alarms` 每 30 秒；
   转好 / 失败弹系统通知，点了打开 `#/detail/<id>`。

## 后果

- 只认 `localhost` / `127.0.0.1`（`host_permissions`）。Verbatim 部署在局域网别的机器上时，要把那个地址加进 manifest。
- 一点就开始转：字幕快速通道不花钱，没字幕就按插件设置里的引擎花钱（默认 Gemini 3.5），跟在网页里贴链接一样。
