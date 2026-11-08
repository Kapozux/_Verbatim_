# agy 转写引擎（Gemini 订阅）

- **现状**：`agy_flash`（默认）/ `agy_argon` 两个引擎，转写走用户自己的 Gemini 订阅，不花 API 的钱。分支 `feature/agy-engine`，测试 `tests/test_agy_engine.py`（19 项）。
- **怎么跑**：本机要有 agy（`brew install --cask antigravity-cli`），并在「终端」App 里运行一次 `agy`，用有 Gemini 订阅的账号登录。查额度：`agy -p /usage`。
- **已知问题**：Flash 专有名词会听错、常被安全过滤误拦（误拦的块会换 Argon，吃 Argon 周额度）；时间戳靠事后本机 Whisper 对齐才准；`test_custom_llm` / `test_llmroute` 在 main 上本来就挂（不是这个分支弄的）。
- **11-06 改动**（`3b81e04`、`7062ccc`）：并发按**切片**算——`AGY_FLASH_CHUNKS`（默认 6，作者本机 5，跟闸的非 live 上限对齐）/ `AGY_ARGON_CHUNKS`（默认 2）管全局同时几片，一个视频有空位就多拿；Flash 被拦改 Argon 时占 Argon 的位子。`AGY_QUOTA_WAIT=on`（默认关，作者本机开着）：额度到闸的保留线、或闸抛 Paused，就每 5 分钟看一次、最多等 48 小时，恢复了接着转，不改走付费 OpenRouter；等的时候链条被停 → 记失败，不落 OpenRouter / Whisper。注意：非额度类的 agy 失败仍会走 OpenRouter（老行为）。频道期数上限 `MAX_CHAIN_VIDEOS`（默认 600，作者本机 800）。测试 `tests/test_agy_chunks.py`（12 项）、`tests/test_chain_cap.py`。
- **11-07 改动**（分支 `feature/agy-stuck`）：Flash 单片卡住就掐。gate.db 1319 次 Flash 实测：15 分钟一片中位 40–50 秒、九成 80 秒内；卡住的（思考绕圈、输出上万 token）要 300–500 秒才回 ERROR（11-07 一期 68 分钟录音被一片拖到 9 分钟，API 只要 3 分钟）。现在每分钟音频给 8 秒（15 分钟 = 120 秒，最少 30 秒），到点杀掉 agy 马上重来，最多掐 2 次，第 3 次按 `AGY_TIMEOUT_S`（900）等；Argon 不掐。`AGY_STUCK_PER_MIN` 调每分钟几秒，0 关掉。测试 `tests/test_agy_stuck.py`（13 项）。
- **11-08 改动**（分支 `feature/agy-giveup`）：「最多掐 2 次再按 900 秒等」在实际里失效——一期 13 分钟单片卡住和「status ERROR + 半截稿」轮着来，掐满 2 次后第 3 次一等 15 分钟。现在每次都计时，每掐一次限时放宽 1.5 倍（15 分钟片 120 → 180 → 270 秒）；这一片 Flash 卡住 + 带半截稿的 ERROR 合计 3 次就换 Argon 转这一片（Argon 不掐、吃周额度，不花钱）。空的 ERROR 是断线，不算，照旧 20/60/180 秒重试。测试 `tests/test_agy_stuck.py`（15 项）。
- **下一步**：看邵艾伦频道（679 期，链条 `bb8a3b68…`，11-06 14:4x 开跑）的实际吞吐、误拦率、额度；跑几周看每小时音频实际扣多少额度、误拦率多少；向量也想换本机（Qwen3-Embedding，在 Ephemeris 那边评测）。

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
