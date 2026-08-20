# Verbatim MCP

让 AI agent 直接驱动 [Verbatim](https://github.com/xyzxinlu-max/getAudio) —— 本机自托管的音视频转写 + 博主观点分析工具。

装好之后，你可以直接对 Claude 说：

> 把这个 B 站链接转写了，转完告诉我讲了什么
>
> 搜一下我的转写库里提到「量化交易」的内容
>
> 分析一下这个 UP 主的观点，最多看 20 个视频

Claude 会自己调用转写、轮询进度、取回结果。

## 前置条件

1. **Verbatim 本体在跑**（默认 `http://127.0.0.1:5001`）。这个 MCP server 只是个客户端，不含转写能力。
2. Python 3.10+（Verbatim 本体锁在 3.9，这里是独立进程，互不影响）。

## 安装

```bash
git clone https://github.com/xyzxinlu-max/getAudio.git
cd getAudio/verbatim-mcp
python3 -m venv .venv && .venv/bin/pip install -e .
```

## 接到 Claude Code

```bash
claude mcp add verbatim --scope user -- /绝对路径/verbatim-mcp/.venv/bin/verbatim-mcp
```

接到 Claude Desktop 的话，编辑 `claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "verbatim": {
      "command": "/绝对路径/verbatim-mcp/.venv/bin/verbatim-mcp"
    }
  }
}
```

Verbatim 不在默认地址时，加一个环境变量：

```json
{
  "mcpServers": {
    "verbatim": {
      "command": "/绝对路径/verbatim-mcp/.venv/bin/verbatim-mcp",
      "env": { "VERBATIM_URL": "http://127.0.0.1:5002" }
    }
  }
}
```

## 提供的工具

| 工具 | 作用 |
|---|---|
| `transcribe_urls` | 提交视频链接转写（支持播放列表/合集/频道，可指定时间段） |
| `transcribe_file` | 转写本机文件（走软链，不拷贝、不上传） |
| `check_task` | 查转写任务状态 |
| `get_transcript` | 取转写正文 + AI 标题/摘要/标签 |
| `search_transcripts` | 全文搜索转写库 |
| `list_transcripts` | 列出转写库条目 |
| `analyze_creator` | 对一个博主做系统性观点分析（重量级长任务） |
| `check_analysis` | 查分析进度 |
| `list_analysis_files` | 列出分析产出的文档 |
| `get_analysis_file` | 读取某份分析文档 |
| `verbatim_status` | 检查 Verbatim 是否在跑、各引擎 key 配置情况 |

## 设计说明

**转写和分析都是长任务**，所以工具是「提交 → 拿 id → 轮询」两段式，不会让 agent 阻塞几分钟干等。每个提交类工具的返回值里都写了下一步该调谁。

**纯 HTTP 客户端**，不 import Verbatim 任何代码。好处是两边的 Python 版本互不牵扯，Verbatim 挂了也只是工具调用失败，不会拖垮 agent 会话。

**长文本默认截断**（转写 4000 字、分析文档 8000 字），避免几万字正文无脑塞进上下文。需要全文时传 `full=True`。

## 隐私提醒

转写引擎选 `gemini` / `dashscope` 时，音频会上传到对应厂商。要完全本地处理就用 `whisper`（默认），不需要任何 API key。

## License

MIT
