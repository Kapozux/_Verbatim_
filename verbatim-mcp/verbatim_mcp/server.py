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
        "分析过的博主 / 合集可以直接提问（ask_creator）：只凭他视频里的原话回答，每句带出处。"
        "先用 list_creators 拿 chain_id。一个话题想横向看所有博主用 topic_radar。\n\n"
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
        "duration": payload.get("duration_seconds"),
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
            "duration": e.get("duration_seconds"),
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


# ---------- 问博主 / 合集（只凭原话回答、带出处）----------

def _cite_list(citations: dict, limit: int = 40) -> list:
    """出处精简成 agent 好用的样子：原话 + 谁说的 + 哪一期 + 时间点 + 原视频链接。"""
    out = []
    for cid, c in list((citations or {}).items())[:limit]:
        out.append({
            "id": cid, "quote": c.get("quote"), "speaker": c.get("speaker") or None,
            "creator": c.get("creator") or None, "episode": c.get("episode"), "date": c.get("date") or None,
            "time": c.get("ts"), "video_url": c.get("video_url") or None, "task_id": c.get("task_id"),
        })
    return out


@server.tool(
    description=(
        "列出分析过的博主和合集（能提问 / 看立场的那些）。返回 id、名字、类型、期数、"
        "有没有话题标签。后面几个工具的 chain_id 从这里拿。"
    )
)
async def list_creators(include_transcript_only: bool = False) -> dict:
    chains = await _request("GET", "/api/chains")
    seen, out = set(), []
    for c in chains if isinstance(chains, list) else []:
        if c.get("hidden") or (not c.get("has_cards") and not include_transcript_only):
            continue
        key = c.get("url") or c.get("id")
        if key in seen:
            continue
        seen.add(key)
        vids = c.get("videos") or []
        out.append({
            "chain_id": c.get("id"), "name": c.get("author"),
            "kind": "collection" if c.get("kind") == "collection" else "creator",
            "episodes": sum(1 for v in vids if v.get("status") == "done"),
            "can_ask": bool(c.get("has_cards")), "has_topics": bool(c.get("has_tags")),
            "url": c.get("url") or None,
        })
    return {"count": len(out), "creators": out}


@server.tool(
    description=(
        "向一个博主或合集提问。只凭从他视频里摘出的原话回答，每句话带出处（原话、哪一期、"
        "时间点、原视频链接）；他没谈过的会直说没谈过。mode='about' 第三人称（默认），"
        "mode='as' 模拟他本人口吻回答（AI 模拟，仍然附真实原话）。topic 可限定在某个话题里问。"
        "不会写进网页上的聊天记录。一次约 5 秒、不到 1 美分。"
    )
)
async def ask_creator(chain_id: str, question: str, mode: str = "about", topic: str = "") -> dict:
    body: dict[str, Any] = {"question": question, "mode": mode, "ephemeral": True}
    if topic:
        body["topic"] = topic
    payload = await _request("POST", f"/api/chain/{chain_id}/ask", json=body)
    m = payload.get("message") or {}
    cov = m.get("coverage") or {}
    return {
        "answer": m.get("content"),
        "sources": _cite_list(m.get("citations") or {}),
        "searched": {"cards": cov.get("pool_cards"), "episodes": cov.get("pool_episodes"),
                     "read": cov.get("cards_used"), "mode": cov.get("mode")},
        "note": "answer 里的 [#3-12] 对应 sources 里同 id 的原话。",
    }


@server.tool(
    description=(
        "一个博主 / 合集的话题表：每个话题多少条原话、覆盖几期、看好 / 看空 / 两面 / 中立各多少。"
        "想看某个话题下的具体原话用 topic_quotes。"
    )
)
async def creator_topics(chain_id: str) -> dict:
    p = await _request("GET", f"/api/chain/{chain_id}/topics")
    return {
        "tagged_cards": p.get("tagged"), "total_cards": p.get("cards_total"), "has_dates": p.get("has_dates"),
        "topics": [{"topic": t.get("topic"), "quotes": t.get("count"), "episodes": t.get("episodes"),
                    "stances": t.get("stances")} for t in p.get("topics") or []],
    }


@server.tool(
    description=(
        "某个话题下的原话，按时间先后排（能看出立场怎么变的）。stance 可筛 pro / con / mixed / neutral。"
    )
)
async def topic_quotes(chain_id: str, topic: str, stance: str = "", limit: int = 40) -> dict:
    p = await _request("GET", f"/api/chain/{chain_id}/topics", params={"topic": topic})
    hit = next((t for t in p.get("topics") or [] if t.get("topic") == topic), None)
    if not hit:
        return {"error": f"没有这个话题：{topic}。用 creator_topics 看有哪些。"}
    cards = [c for c in hit.get("cards") or [] if not stance or c.get("stance") == stance]
    return {
        "topic": topic, "total": len(cards),
        "quotes": [{"quote": c.get("quote"), "summary": c.get("obs"), "stance": c.get("stance"),
                    "speaker": c.get("speaker") or None, "episode": c.get("episode"), "date": c.get("date"),
                    "time": c.get("ts"), "video_url": c.get("video_url") or None}
                   for c in cards[:max(1, min(limit, 200))]],
    }


@server.tool(
    description=(
        "一个博主的预测对账：他说过哪些可核对的预测、联网核对后说中 / 没说中 / 待定。"
        "verdict 可筛 true / false / pending / unclear。"
    )
)
async def creator_predictions(chain_id: str, verdict: str = "", limit: int = 50) -> dict:
    p = await _request("GET", f"/api/chain/{chain_id}/predictions")
    items = [i for i in p.get("items") or [] if ((i.get("check") or {}).get("verdict") or "unchecked") != "na"]
    if verdict:
        items = [i for i in items if ((i.get("check") or {}).get("verdict") or "unchecked") == verdict]
    return {
        "counts": p.get("counts"), "hit_rate": p.get("hit_rate"), "resolved": p.get("resolved"),
        "predictions": [{"quote": i.get("quote"), "summary": i.get("obs"), "date_said": i.get("date"),
                         "verdict": (i.get("check") or {}).get("verdict") or "unchecked",
                         "why": (i.get("check") or {}).get("why"), "source": (i.get("check") or {}).get("source"),
                         "video_url": i.get("video_url")} for i in items[:max(1, min(limit, 300))]],
    }


@server.tool(
    description=(
        "把 2~4 个博主放在一起，问同一个问题：每人一段，只用他自己的原话，最后说共识与分歧。"
    )
)
async def compare_creators(chain_ids: list[str], question: str) -> dict:
    p = await _request("POST", "/api/compare", json={"chains": chain_ids, "question": question})
    return {"answer": p.get("answer"), "creators": p.get("creators"), "sources": _cite_list(p.get("citations") or {}, 60)}


@server.tool(
    description=(
        "话题雷达：一个话题，扫一遍所有分析过的博主——谁谈得多、总体看好还是看空、前期到近期变没变、"
        "代表性原话。按意思匹配，不只是关键词。"
    )
)
async def topic_radar(topic: str) -> dict:
    p = await _request("POST", "/api/radar", json={"query": topic})
    return {
        "topic": topic,
        "creators": [{"chain_id": r.get("chain_id"), "name": r.get("author"), "quotes": r.get("cards"),
                      "episodes": r.get("episodes"), "stances": r.get("stances"),
                      "overall": r.get("net"), "earlier": r.get("early"), "later": r.get("late"),
                      "first_date": r.get("first") or None, "last_date": r.get("last") or None,
                      "sample_quotes": [b.get("quote") for b in r.get("best") or []]}
                     for r in p.get("rows") or []],
        "note": "overall / earlier / later = (看好 − 看空) / 表态原话数，-1 到 1。",
    }


@server.tool(description="预测排行：每个博主核对过的预测里说中了几成（有结果不足 5 条的不排名）。")
async def prediction_leaderboard() -> dict:
    return await _request("GET", "/api/leaderboard")


@server.tool(
    description=(
        "新建合集：把任意一批转写（task_ids，从 list_transcripts / search_transcripts 拿）和 / 或整个博主"
        "（chain_ids）放在一起，之后就能对整批提问、看立场、导出。kind: interview / course / meeting / "
        "podcast / mixed。已分析过的内容直接复用，新的每条约 5 美分、一两分钟。返回 chain_id，用 "
        "check_analysis 查进度，建好后用 ask_creator 提问。"
    )
)
async def create_collection(name: str, task_ids: list[str] | None = None, chain_ids: list[str] | None = None,
                            kind: str = "mixed") -> dict:
    p = await _request("POST", "/api/collections", json={
        "name": name, "kind": kind, "task_ids": task_ids or [], "chain_ids": chain_ids or []})
    return {"chain_id": p.get("id"), "items": p.get("items"),
            "next_step": "用 check_analysis(chain_id) 查进度，stage=done 后用 ask_creator 提问。"}


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
