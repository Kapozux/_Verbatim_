"""
Verbatim MCP server —— 让 agent 直接驱动本机的 Verbatim。

设计上是**纯客户端**：不 import Verbatim 的任何代码，只通过 HTTP 调它的
REST 接口。好处有三个：
  1. Verbatim 锁在 Python 3.9（torch/mlx 那套依赖），而 MCP SDK 要 3.10+，
     分成两个进程各跑各的 Python，谁也不用迁就谁。
  2. app.py 一行都不用改（唯一的例外是补了个 /api/task/<id> 状态端点，
     因为原来只有 SSE 流，agent 没法用普通请求问「好了没」）。
  3. Verbatim 挂了/没启动时，这边只是调用失败，不会把 agent 会话也拖垮。

**转写和分析都是长任务**（一个小时的视频要几分钟），所以工具设计成
「提交 → 拿 id → 轮询」两段式，而不是让 agent 干等着阻塞几分钟。
每个提交类工具的返回值里都写清了下一步该调谁。
"""

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

# Verbatim 默认只监听本机 5001；换端口/换机器用这个环境变量覆盖
BASE_URL = os.environ.get("VERBATIM_URL", "http://127.0.0.1:5001").rstrip("/")

# 转写本身是后台跑的，这里的超时只管「提交请求」这一下，给宽一点是因为
# 合集类链接在返回前要先枚举里面的视频（一次网络往返 × N）。
HTTP_TIMEOUT = float(os.environ.get("VERBATIM_TIMEOUT", "120"))

server = MCPServer(
    name="verbatim",
    instructions=(
        "Verbatim 是一个本机自托管的音视频转写 + 博主观点分析工具。\n\n"
        "转写和分析都是长任务：提交类工具会立刻返回一个 id，你需要之后用对应的 "
        "check_* 工具轮询状态，不要假设提交完就有结果了。一段几分钟的音频通常 "
        "十几秒到一分钟，一小时的视频可能要几分钟。\n\n"
        "如果所有工具都报连不上，说明用户本机的 Verbatim 没在跑，让用户先启动它。"
    ),
)


class VerbatimError(Exception):
    """把 HTTP 层的失败翻译成 agent 看得懂的话。"""


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.request(method, url, **kwargs)
    except httpx.ConnectError as exc:
        raise VerbatimError(
            f"连不上 Verbatim（{BASE_URL}）。它可能没在运行——"
            f"让用户启动 Verbatim 后再试。原始错误：{exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise VerbatimError(
            f"请求 Verbatim 超时（{HTTP_TIMEOUT}s）：{path}。"
            f"如果是刚提交的长任务，任务可能仍在后台跑，用 check_* 工具查状态。"
        ) from exc

    if response.status_code == 404:
        raise VerbatimError(f"找不到（404）：{path}")
    if response.status_code >= 400:
        # Verbatim 的错误都是 {"error": "..."} 形态，尽量把原话透出来
        try:
            detail = response.json().get("error") or response.text
        except Exception:  # noqa: BLE001
            detail = response.text
        raise VerbatimError(f"Verbatim 返回 {response.status_code}：{str(detail)[:400]}")

    if response.headers.get("content-type", "").startswith("text/markdown"):
        return response.text
    return response.json()


def _trim(text: str, limit: int) -> str:
    """长文本截断。全量转写动辄几万字，无脑塞进上下文既慢又挤掉别的信息；
    需要全文的场景由调用方显式要（见 get_transcript 的 full 参数）。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（已截断，全文共 {len(text)} 字）"


# ---------- 转写 ----------

@server.tool(
    description=(
        "提交视频链接进行转写。支持 YouTube / B站的单个视频、播放列表、合集、"
        "频道主页——合集类链接会自动展开成里面的每个视频，各自成为独立任务。\n\n"
        "链接行尾可以加时间段只转其中一段，例如 "
        "'https://... @10:00-25:00'（时间戳会还原成原视频里的真实位置）。\n\n"
        "立刻返回每个视频的 task_id；转写在后台跑，之后用 check_task 查状态、"
        "用 get_transcript 取结果。"
    )
)
async def transcribe_urls(
    urls: str,
    engine: str = "whisper",
    max_videos: int = 20,
) -> dict:
    """
    urls: 一个或多个链接，换行或逗号分隔（一次最多 20 行）
    engine: whisper（本机，免费，慢）/ gemini / dashscope（云端，快，要 API key）
    max_videos: 合集类链接最多展开多少个视频，1-300
    """
    payload = await _request(
        "POST", "/api/transcribe_urls",
        json={"urls": urls, "engine": engine, "max_videos": max_videos},
    )
    tasks = payload.get("tasks", [])
    return {
        "submitted": len(tasks),
        "tasks": tasks,
        "errors": payload.get("errors", []),
        "next_step": "用 check_task(task_id) 轮询状态，done 之后用 get_transcript(task_id) 取结果。",
    }


@server.tool(
    description=(
        "转写本机上已有的音频/视频文件。走软链不做拷贝，所以大文件也不会占额外"
        "磁盘、不需要上传。路径必须是运行 Verbatim 那台机器上的绝对路径。\n\n"
        "立刻返回 task_id，之后用 check_task 查状态。"
    )
)
async def transcribe_file(path: str, engine: str = "whisper") -> dict:
    """
    path: 本机绝对路径，如 /Users/me/Movies/talk.mp4
    engine: whisper / gemini / dashscope
    """
    payload = await _request(
        "POST", "/api/transcribe_local",
        json={"path": path, "engine": engine},
    )
    return {
        "task_id": payload.get("task_id"),
        "next_step": "用 check_task(task_id) 轮询状态。",
    }


@server.tool(
    description=(
        "查一个转写任务现在是什么状态。status 为 done 时才能用 get_transcript "
        "取结果；failed 时 error 字段说明原因。progress 是 0-100 的百分比"
        "（没在跑时为 null）。"
    )
)
async def check_task(task_id: str) -> dict:
    return await _request("GET", f"/api/task/{task_id}")


@server.tool(
    description=(
        "取一个已完成转写的正文。默认返回前 4000 字的预览加上元信息"
        "（AI 生成的标题、摘要、标签）；要完整全文时传 full=True。\n\n"
        "任务还没转完时这里会报找不到，先用 check_task 确认 status 是 done。"
    )
)
async def get_transcript(task_id: str, full: bool = False) -> dict:
    payload = await _request("GET", f"/api/history/{task_id}")
    segments = payload.get("segments") or []
    text = " ".join(
        (s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()
    )
    return {
        "task_id": task_id,
        "title": payload.get("ai_title") or payload.get("filename"),
        "one_line": payload.get("ai_one_line"),
        "tags": payload.get("ai_tags"),
        "summary": payload.get("summary"),
        "duration": payload.get("duration"),
        "engine": payload.get("engine"),
        "segment_count": len(segments),
        "text": text if full else _trim(text, 4000),
    }


# ---------- 检索已有的转写库 ----------

@server.tool(
    description=(
        "全文搜索转写库：文件名、AI 标题、标签、转写正文都会被搜到，"
        "返回带命中片段的条目。想读某条的完整内容用 get_transcript(task_id)。"
    )
)
async def search_transcripts(query: str, limit: int = 20) -> dict:
    payload = await _request("GET", "/api/search", params={"q": query})
    items = payload if isinstance(payload, list) else []
    return {"query": query, "total": len(items), "results": items[:limit]}


@server.tool(
    description=(
        "列出转写库里的条目（按时间倒序）。source 字段区分来源："
        "'mine' 是用户自己转的，'pipeline' 是博主分析流程产出的。"
    )
)
async def list_transcripts(limit: int = 30, source: str = "all") -> dict:
    payload = await _request("GET", "/api/history")
    items = payload if isinstance(payload, list) else []
    if source in ("mine", "pipeline"):
        items = [e for e in items if e.get("source") == source]
    slim = [
        {
            "task_id": e.get("id"),
            "title": e.get("ai_title") or e.get("filename"),
            "date": e.get("date"),
            "duration": e.get("duration"),
            "source": e.get("source"),
            "creator": e.get("creator"),
        }
        for e in items[:limit]
    ]
    return {"total": len(items), "showing": len(slim), "items": slim}


# ---------- 博主观点分析（Pipeline） ----------

@server.tool(
    description=(
        "对一个博主/频道做系统性观点分析：自动枚举其视频 → 逐个转写 → "
        "AI 逐期精读 → 按议题综合成一份总文档。这是重量级长任务，几十个视频"
        "可能要跑很久。\n\n"
        "立刻返回 chain_id，之后用 check_analysis 查进度、"
        "list_analysis_files + get_analysis_file 取产出的文档。"
    )
)
async def analyze_creator(
    url: str,
    max_videos: int = 0,
    engine: str = "gemini",
    author: str = "",
    lang: str = "auto",
) -> dict:
    """
    url: 博主主页 / 频道 / 合集链接
    max_videos: 最多分析几个视频，0 表示不限
    engine: 转写引擎 gemini / whisper / dashscope
    author: 博主名字（留空则自动探测）
    lang: 输出语言，auto / zh / en
    """
    body: dict[str, Any] = {
        "url": url, "engine": engine, "lang": lang, "analyze": True,
    }
    if max_videos > 0:
        body["max_videos"] = max_videos
    if author:
        body["author"] = author
    payload = await _request("POST", "/api/chain", json=body)
    return {
        "chain_id": payload.get("chain_id"),
        "next_step": "用 check_analysis(chain_id) 查进度。这个流程很慢，别频繁轮询。",
    }


@server.tool(
    description=(
        "查博主分析的进度。stage 字段是当前阶段"
        "（starting / transcribing / analyzing / done / failed / cancelled），"
        "videos 里是每个视频各自的状态。"
    )
)
async def check_analysis(chain_id: str) -> dict:
    payload = await _request("GET", f"/api/chain/{chain_id}")
    videos = payload.get("videos") or []
    return {
        "chain_id": chain_id,
        "stage": payload.get("stage"),
        "author": payload.get("author"),
        "url": payload.get("url"),
        "video_count": len(videos),
        "videos_done": sum(1 for v in videos if v.get("status") == "done"),
        "error": payload.get("error"),
        "videos": [
            {
                "title": v.get("title"),
                "status": v.get("status"),
                "progress": v.get("progress"),
                "task_id": v.get("task_id"),
            }
            for v in videos
        ],
    }


@server.tool(description="列出某次博主分析产出的所有 Markdown 文档（总分析 + 各期分析）。")
async def list_analysis_files(chain_id: str) -> dict:
    payload = await _request("GET", f"/api/chain/{chain_id}/files")
    files = payload if isinstance(payload, list) else []
    return {"chain_id": chain_id, "files": files}


@server.tool(
    description=(
        "读取博主分析产出的某个 Markdown 文档。文件名从 list_analysis_files 拿。"
        "默认截断到 8000 字，要全文传 full=True。"
    )
)
async def get_analysis_file(chain_id: str, name: str, full: bool = False) -> dict:
    text = await _request("GET", f"/api/chain/{chain_id}/file", params={"name": name})
    if not isinstance(text, str):
        text = str(text)
    return {
        "chain_id": chain_id,
        "name": name,
        "length": len(text),
        "content": text if full else _trim(text, 8000),
    }


# ---------- 杂项 ----------

@server.tool(
    description=(
        "检查 Verbatim 是否在运行，以及各个 AI 引擎的 API key 配置情况。"
        "任何工具报连不上时，先用这个确认一下。"
    )
)
async def verbatim_status() -> dict:
    settings = await _request("GET", "/api/settings")
    configured = [
        name for name in ("gemini", "dashscope", "openrouter")
        if isinstance(settings.get(name), dict) and settings[name].get("set")
    ]
    return {
        "reachable": True,
        "base_url": BASE_URL,
        "engines_configured": configured,
        "whisper_model": settings.get("whisper_model") or "默认",
        "note": "whisper 是本机引擎，不需要 API key，永远可用。",
    }


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
