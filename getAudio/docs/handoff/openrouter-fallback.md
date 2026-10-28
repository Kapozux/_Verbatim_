# 官方 Gemini 转不了时改走 OpenRouter

## 速览

- **现状**：`gemini` / `gemini35` 引擎失败（撞月度花费上限、调用失败、白卷）时，先用 OpenRouter 上的 Gemini（默认 `google/gemini-3.8-flash`）转一遍，成功就记成 `openrouter` 引擎；OpenRouter 也不行才按老规矩落 Whisper 或判失败。没填 `OPENROUTER_API_KEY` 时行为跟以前完全一样。
- **怎么跑**：Settings 里填 OpenRouter key（存到 `OPENROUTER_API_KEY`）即可，不用额外开关。换模型：`.env` 设 `OPENROUTER_TRANSCRIBE_MODEL`。测试 `./run_tests.sh or_fallback`。
- **已知问题**：没有 Gemini 3.5 Transcribe 的原生说话人分离和词级时间戳，说话人靠提示词让模型标「说话人N：」，时间戳每 30 秒一个；OpenRouter 的结果不排本机 Whisper 补逐词时间。
- **下一步**：10-28 第一次实测「CLL 伤逝」（15:40）：15 分钟那块 OR 先回空（0 token）、重试又等满 300 秒超时，落 Whisper；同一文件手动直发 OR 90 秒成功、$0.043。之后修了两处：正常收尾的空块当静音、不重试（那期最后 40 秒是静音）；切块改 5 分钟。OR 失败原因记在 meta.json 的 `timing.openrouter_error` 和日志 `[openrouter]`，再出现就看那里。

## 来由

2026-10-28：官方项目撞 spending cap → 按 10-14 的规矩自动落本地 Whisper → 笔记本用电池，Whisper 原地等插电，
界面一直「转写中」、没有进度。用户：「如果 google 官方的不行就用 OR（加一个这个 fallback）」。

## 顺序

1. 官方引擎（自己内部已重试）
2. 失败且不是网络错（网络错照旧重排队）、不是内容拦截（OR 背后是同一个 Gemini，拦截照旧）、填了 OR key → `transcribe_openrouter.transcribe_audio`
3. 还不行：额度用完 / 白卷 / 勾了兜底 → Whisper；否则判失败，错误里两边原因都有

撞上限后一小时内（`_quota_recent`），后面的期不再先碰官方，直接走 2。

## 费用

OpenRouter 上 `google/gemini-3.8-flash`：音频输入 $0.825 / 百万 token，输出 $4.125 / 百万。
切 5 分钟一块（15 分钟一块时模型整块只打两三个时间戳、不标说话人、常出繁体），每块都会先「想」一遍，实测约 **$0.2–0.25 / 小时音频**。记账走 `usage.db`（provider `openrouter`，purpose `transcribe`，用 OR 报的实价）。

## 代码

- `transcribe_openrouter.py`：OpenAI 兼容 `chat/completions` + `input_audio` base64；切块、时间戳平移、解析复用 `transcribe_gemini.py`；每次请求 300 秒超时，401/402/403 不重试。
- `app.py` `_run_transcription` 的 `except`：Whisper 兜底之前插一段。
- 测试 `tests/test_or_fallback.py`。
