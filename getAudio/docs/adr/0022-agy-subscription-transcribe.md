# 0022 转写默认走 Gemini 订阅（agy），API 只做后备

2026-11-04，用户：「Verbatim 先增加一下 Flash 和 Argon 选项（默认 Flash）」。

## 背景

用户想把日常开销都放进订阅里：API key 绑的是家里的卡，自己有 Claude 20x、ChatGPT Plus、Gemini 订阅。
文字活早就交给本机 Claude Code（ADR 0015–0018），转写还在花 API 的钱（gemini35、OpenRouter）。
Gemini CLI 2026-06-18 起不服务个人订阅，接班的是 Antigravity CLI（`agy`），能无头跑，也能读音频。

## 决定

- 新增两个转写引擎 `agy_flash`（gemini-3.8-flash-low）和 `agy_argon`（gemini-4-argon-low），都走 `agy -p`。
- 装了 agy 就默认 `agy_flash`（`config.default_engine()`；`DEFAULT_ENGINE` 环境变量可改；没装 agy 还是 gemini35，
  所以开源用户不受影响）。
- 按 15 分钟切 mp3、各块并行；Flash 某块被安全过滤误拦，先再试一次 Flash，还拦才换 Argon。
- 转之前读 `agy -p /usage`，对应额度桶剩不到 `AGY_MIN_REMAINING`（默认 5%）就当撞上限，
  交给原来的后备：OpenRouter → 本机 Whisper。
- 段落时间是模型写的，跟 gemini35 一样，转完让本机 Whisper 逐词对齐再改（`wordtime.RETIME_ENGINES`）。

## 为什么不用 Argon 当默认

Argon 最准（同一节 44 分钟课 15.6k 字、专有名词全对），但 Pro 订阅给它单独一个周额度桶，44 分钟就耗约 2.7%，
一周大概 25 小时音频，批量转频道不够。Flash 用的是另一个桶，44 分钟只耗 5 小时桶的 1% 上下、周桶几乎不动，
用户自己平时也用 Gemini，不能把共用额度吃光。

## 代价

- Flash 专有名词会听错（「福克纳」→「布克奖」），时间戳会偏，被误拦的概率不低（那节课三块里两块被拦过）。
- 依赖用户本机登录的 agy；没登录会报「agy 没登录」，走后备。
- 用程序调 agy 合不合 Google 的条款没查到明确说法。
