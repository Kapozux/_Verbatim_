# agy 转写引擎（Gemini 订阅）

- **现状**：`agy_flash`（默认）/ `agy_argon` 两个引擎，转写走用户自己的 Gemini 订阅，不花 API 的钱。分支 `feature/agy-engine`，测试 `tests/test_agy_engine.py`（19 项）。
- **怎么跑**：本机要有 agy（`brew install --cask antigravity-cli`），并在「终端」App 里运行一次 `agy`，用有 Gemini 订阅的账号登录。查额度：`agy -p /usage`。
- **已知问题**：Flash 专有名词会听错、常被安全过滤误拦（误拦的块会换 Argon，吃 Argon 周额度）；时间戳靠事后本机 Whisper 对齐才准；`test_custom_llm` / `test_llmroute` 在 main 上本来就挂（不是这个分支弄的）。
- **下一步**：跑几周看每小时音频实际扣多少额度、误拦率多少；向量也想换本机（Qwen3-Embedding，在 Ephemeris 那边评测）。

## 实测（2026-11-04，伤逝那节课 44 分钟）

| | 用时 | 字数 | 额度 |
|---|---|---|---|
| agy_argon 整段 | 180 秒 | 15,622 | Argon 周桶约 2.7% |
| agy_flash 15 分钟三块（经 transcribe_agy） | 约 370 秒 | 15,151 | Gemini 5 小时桶约 0.8%、周桶约 0.03%；两块被拦换 Argon，Argon 周桶约 2% |
| 原来资料库里的 gemini35 | — | 6,434（中间 25 分钟几乎是空的） | API 付费 |

## 坑

- `@文件` 不附音频；提示词里要让它「用 view_file 打开绝对路径」，并说「不要运行命令」，否则它去跑 `ls` 被拒、一字不出。
- 音频必须在 agy 的工作目录里（`cwd`），否则 `read_file` 被拒，返回 `denied_actions` + 空文本。现在会明确报错。
- 格式：m4a 不认；ogg/opus 只转出最后几秒；mp3 稳。所以自己切 mp3，不复用 transcribe_gemini 那个切 ogg 的。
- Verbatim 通用的长提示词（[HH:MM:SS]、八条规矩）喂给 Flash，15 分钟只出 5 个时间戳；换成短提示词好很多。时间戳太稀还会带「再强调一次」再要一遍。
- launchd 起的 5001 的 PATH 常常没有 /opt/homebrew/bin：`config.agy_installed()` / `transcribe_agy.agy_bin()` 会找 Homebrew 默认位置，也可设 `AGY_BIN`。

## 开关

`DEFAULT_ENGINE`（默认引擎）、`AGY_MIN_REMAINING`（额度剩多少就让给后备，默认 0.05）、`AGY_FLASH_CONCURRENCY` / `AGY_ARGON_CONCURRENCY`（同时几个任务，默认 4 / 2）、`AGY_BIN`。
