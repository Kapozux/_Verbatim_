# 资料库的「问」（一句话描述 → 最像的几份转写）

## 速览

- **现状**：10-19 合进 main、5001 已重启，上线。测试 63/63 过（`tests/test_libask.py` 38 项）。
- **怎么跑**：资料库 → 转写记录，搜索框里写一句描述，回车或点框内的「问」。终端也能直接查：`python look.py search 俄语 SHAP`（看 `look.py` 开头的用法）。
- **已知问题**：状态只在内存里，重启 5001 时正在找的那次没了；退回「只看目录」那条只在测试里用假模型测过，没有真调过。
- **下一步**：用一阵后看找得准不准、要不要加「最近问过的」。

## 做了什么

- `look.py`：只读的查资料命令（照 Ephemeris 的 look.py），读 `library.db` 和 `results/<tid>/`。出处记号 `[[t:短号@时:分:秒]]`，短号是 id 前 8 位。
- `libask.py`：后台问、轮询状态、把模型的 JSON 认回真实的转写（编的、重复的丢掉，时间对到真有的一句）；没有 Claude Code 时走目录。
- `claude_cli.agent()`：让 Claude 只用 Bash、只放行一个命令前缀；停止按钮掐进程。用途 `find` 算交互（live）。
- `app.py`：`GET / POST /api/find`、`POST /api/find/stop`。
- 前端 `static/find.js`（新文件，不往 app.js 里塞）、`index.html` 搜索框包了一层 `.find-box`、`i18n.js` 的 `find.*`、`style.css` 的 `.find-*`。
- 顺手修：`openDetailView` 先滚到那一秒、再画摘要，那一句被顶到屏幕外（证据卡点进来也是这样）；改成最后再定位。

## 测试和实测

- `tests/test_libask.py`：look 的每条命令（含 sql 改不了库）、假 claude 事件流（只放行 look.py、进度、结果清洗、时间对齐）、目录退回、接口（忙、停止、出错）。
- 10-19 真库 3574 份：「宿舍里那个说俄语的人讲机器学习模型解释的录音」12 秒 4 步，找到两版转写，跳 07:41；
  「YC 说 solo founder 越来越多」17 秒 3 步，找到「2026年创业趋势深度解读」29:02。
- 界面：5048 用复制的 110 份（`.claude/scratch-data/find-ask/`），1700 / 1000 / 400 浅色 + 深色截图在同一目录。
