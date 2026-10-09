# 0014 接口文档从路由表和 docstring 现生成，不手写 schema

日期：2026-10-09

## 背景

Open Notebook 用 FastAPI，接口有类型声明，OpenAPI 文档是框架白送的。Verbatim 是 Flask，140 来个接口是一个个加出来的，
没有 schema。技术对比报告把「接口有 OpenAPI」记成 ON 胜。想要给脚本、MCP、别人接入用的接口文档。

考虑过：
- **手写一份 openapi.yaml**：140 个接口，几个窗口同时在加接口，写完第二天就对不上。
- **换成带 schema 的框架 / 给每个接口加 marshmallow / pydantic 声明**：要动全部路由，跟重构分支冲突，收益不抵代价。

## 决定

1. `openapi.py` 从 `app.url_map` 和每个接口函数的 docstring 现生成 OpenAPI 3：路径、方法、路径参数（`<int:x>` 写成 integer）、
   docstring 第一段当 summary、其余当 description，按 `/api/<第一段>` 分组。
2. **请求体、返回体不声明，文档里也不假装有**：开头写明「body 没声明，要看 app.py 里那个接口」。
   docstring 里已经写了 body 的（比如问答）照原样显示。
3. `/api/openapi.json` 给工具用；`/api/docs` 是一页不连外网的列表（可筛选），不引 Swagger UI 的 CDN。
4. 鉴权跟别的接口一样（设了 GETAUDIO_TOKEN / VERBATIM_PASSWORD 才要）。

## 后果

- 加了接口自动出现在文档里；`tests/test_openapi.py` 检查每个路由、每个方法都在。
- 10-09 时 42 个接口没有 docstring，文档里只有路径没有说明。以后新接口写一行 docstring 就有说明。
