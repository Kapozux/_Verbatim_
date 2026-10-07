# 复习笔记模板

## 速览
- **现状**：10-07 合进 main、上 5001。笔记本里每份来源（文档、录音）按用户自己的模板出一份复习笔记；开了「加来源时自动出」，新加的来源自动出。
- **怎么跑**：`./run_tests.sh revnotes`（10 项）；界面在笔记本工作台格子「复习笔记」。
- **已知问题**：改了模板不会回头重出已有的笔记（故意的，花钱）；想重出得在列表里删掉那份再点「现在也出」。代码库来源不出。估价按问答模型价算，实际走 DeepSeek 时更便宜。
- **下一步**：用户拿真的一学期课堂录音试；看要不要「按模板重出全部」按钮、要不要几个模板（语文 / 数学各一个）。

## 是什么
用户唯一被验证的用途是「课堂录音 + 卷子 → 复习笔记」，每节课格式要一样，不想每次去工作台再写一遍要求。照 Open Notebook 的 transformation 做。

## 怎么做的
- `revnotes.py`：配置存 `_chains/<cid>/revnotes.json` = `{template, auto}`，没保存过就是没开。
- 一份笔记 = `study.start(kind='report', fmt='custom', prompt=模板, scope={'type':'all','sources':[sid]}, extra={'revnote': {kind, sid, title}})`。出处、导出、列表、记账都跟报告一样。
- 「出过没有」看 studio 里带 `revnote` 且状态 running / done 的；失败的下次会补。查和登记在同一把锁里，连着刷新不会重复出。
- 自动：`app._project_refresh` 末尾（`_notes_auto` 之后）调 `_revnotes_auto`；一次最多 `AUTO_MAX=20` 份、估价合计不超过 `AUTO_BUDGET_USD=0.5`，剩下的弹框里写「还没出的 N 份」。
- 接口：`GET /api/chain/<id>/revnotes?lang=` → 模板、开关、默认模板、已出份数、还没出的、估价；`POST` `{template, auto, run_missing}`。演示模式 404。
- 前端：`static/study.js` 末尾 `revnotesPicker()`；列表图标和面包屑看 `o.revnote`；文案 `st.k.revnotes`、`rn.*`。

## 费用
每份约 $0.003–0.01（一节 40 分钟课）。估价每份约 $0.011（偏保守）。
