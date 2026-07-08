# Verbatim — TODO

> 改代码需重启才生效；趁没有 pipeline 在跑时批量做。

## ✅ 已完成（本轮）
- **0. 单视频换引擎重转** → 被 **Continue** 取代（自动补全一切缺失，无需手动逐个）
- **2. 转写失败自动降级** → 做成了更强的：云引擎（gemini/deepseek…）失败**自动落本地 Whisper**（commit 后）
- **3. B站 AI 字幕 ai-zh** → 已纳入字幕挑选
- **5. Retry 换引擎** → Continue / Re-analyze 都用表单的引擎/分析大脑设置
- **6. Stop 在分析阶段停不掉** → run_chain 逐期分析加了取消检查
- **7. download_one 加重试** → 退避重试（B站 412 自愈）
- **provider 多选** → 分析大脑可选 Gemini / DeepSeek / Kimi / GLM / Qwen（阿里云百炼，复用 DashScope key）
- **按钮收敛** → Stop / Continue / Re-analyze 三个

## 队列（还没做）

1. **任务详情页改造**
   - 点开一条 pipeline 任务 → 正经任务页：**设置/描述 + 模型区 + 进度**；
     封面/数字网格降级成里面的「查看每期」子页面。
   - 模型区 = **显示 + 可换模型重跑**：显示本次抽取/合成实际用的模型 + 降级留痕
     （pro→flash / 落 whisper 都记一笔）；可换模型对这条链重跑。

4. **Models 选择区（Settings 里）**
   - 现状：pipeline 表单已有"分析大脑"下拉。缺：**Whisper 模型大小**下拉
     （tiny/base/small/medium/large-v3；用户 M4 Max 48GB 可上 large-v3）+ 各家模型自定义框。
   - 存进 settings.local.json，随时改。

## 可选优化
- **Continue 跳过已分析的**：现在 Continue 会重分析所有；理想只补没分析的（省钱）。
- **降级留痕**：把"这条链某视频落了 whisper / 分析降到 flash"记进 chain state，详情页展示。
