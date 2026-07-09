# Verbatim — TODO

> 改代码需重启才生效；趁没有 pipeline 在跑时批量做。

## ✅ 已完成（全部清空 · 2026-07-09）
- 任务详情页改造：info 面板（Source/Settings/Progress + Continue/Re-analyze/Stop 操作）+ Episodes 折叠网格
- Settings → Models 区：Whisper 型号下拉（tiny…large-v3）+ Gemini 转写/分析/抽取模型自定义，保存即生效（运行时读 env，无需重启）
- recover 同步链条卡片：启动全量对齐 taskdb + 打开详情自愈落盘（治"卡片假死"）
- recover 不再误删未完成任务的音频（failed 保留给 Continue 直接重转；done/无主才清）
- Continue 跳过已分析的期：证据卡落盘缓存（cards_NNN.json，按 task_id 校验防陈旧）
- 降级留痕：每期记录 engine_used，详情页 info 面板汇总 ⚠ + 每期卡片小标
- Whisper 兜底换用自己的并发闸（不再借云引擎的 12 路烧 CPU）
- （更早）Stop/Continue/Re-analyze 收敛、多 provider 分析、opt-in Whisper 兜底、下载重试、B站 ai-zh 字幕、flash 转写+并发12

## 队列
（空 — 有新需求再记）

## 备忘
- 列表页卡片数字仍靠打开详情/重启来自愈（列表全量查 taskdb 太贵，暂不做实时）
- 旧的 187B 空壳分析文件（429 时代残留）不自动删，Re-analyze 会按新标题写新文件
