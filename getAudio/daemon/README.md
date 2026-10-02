# daemon/：Verbatim 自带的读码 agent

这是用户自己写的 SWE agent Daemon（`~/Documents/Code_no_big_AI`，MIT）的副本，给「代码库」来源用
（见 `repos.py`、`docs/adr/0005`）。拷自 Daemon 的 b2fdc57。

- `My_agent.py`、`actions.py`、`chat_export.py`、`system_prompt.txt`：照抄，只删了用不到的
  `openai` / `dotenv` 导入。危险命令拦截跟原版一样（2026-10-02 用户同意放宽：只读的 read / search 放行，
  `.env` 只认文件名、不误伤 `os.environ`，llm.py 这类名字只在 Daemon 自己目录里算它的文件）。
- `llm.py`：换成 Verbatim 的接法——用 Settings 里的 DashScope key 走阿里云兼容端点、调用记进 `usage.db`、
  内容审查拒收时跳过那段输出。接口不变。

`repos.py` 用 Verbatim 自己的 Python 跑 `daemon/My_agent.py task.txt`（cwd = 快照）。想改用外面那份 Daemon
（它自己的 venv 和 .env），设 `VERBATIM_DAEMON_DIR=~/Documents/Code_no_big_AI`。

Daemon 原版改了想同步过来：照抄那几个文件、再删掉那两行导入；`llm.py` 别覆盖。
