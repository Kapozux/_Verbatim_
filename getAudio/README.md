# getAudio

音视频转文字工具 —— 上传音频或视频文件，自动转录为带时间戳的文字。

## 功能

- **三引擎转写**：Whisper（本地离线）、Gemini API（云端）、阿里云百炼（Paraformer ASR，便宜且中文优化）
- **AI 内容总结**：转写完成后自动生成整体概括 + 分段结构化摘要（Gemini 或 Qwen）
- **长音频支持**：自动将长音频按 15 分钟切片，逐段转写后合并，时间戳连续不中断
- **视频支持**：上传视频文件时自动提取音轨，复用音频转写流程
- **音频回放**：内嵌播放器，点击时间戳跳转播放，播放时自动高亮当前段落
- **拖拽上传**：支持拖入文件，即时识别格式和大小
- **多格式导出**：复制文本 / 下载 TXT / 下载 SRT 字幕文件
- **历史记录**：服务端持久化存储（音频 + 转写 + 总结），随时回看和回放
- **实时进度**：通过 SSE 实时推送转写进度和分段结果

## 支持格式

| 类型 | 格式 |
|------|------|
| 音频 | MP3, WAV, FLAC, M4A, OGG, WebM |
| 视频 | MP4, MOV, MKV, AVI, M4V |

## 安装

### 前置依赖

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/)（用于长音频切片和视频音轨提取）

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg
```

### 项目安装

```bash
git clone https://github.com/xyzxinlu-max/getAudio.git
cd getAudio

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 配置

创建 `.env` 文件，按需填写：

```
# Gemini（使用 Gemini 引擎时需要）
GEMINI_API_KEY=your_gemini_api_key

# 阿里云百炼（使用阿里云引擎时需要）
DASHSCOPE_API_KEY=your_dashscope_api_key
```

Whisper 为本地引擎，无需 API Key。

## 运行

```bash
source venv/bin/activate
python app.py
```

浏览器打开 [http://localhost:5001](http://localhost:5001)

## 项目结构

```
├── app.py                   # Flask 主应用，路由 / SSE / 视频提取 / 历史 API
├── config.py                # 配置项（模型、API Key、文件限制等）
├── summarize.py             # AI 总结模块（支持 Gemini / Qwen）
├── transcribe_whisper.py    # Whisper 本地转写引擎
├── transcribe_gemini.py     # Gemini 转写引擎（含切片、偏移、合并）
├── transcribe_dashscope.py  # 阿里云百炼转写引擎（Paraformer ASR）
├── templates/index.html     # 前端页面（主视图 + 历史详情视图）
├── static/
│   ├── app.js               # 前端交互（拖拽、播放、高亮、历史、导出）
│   └── style.css            # 样式
├── results/                 # 持久化存储（每次转写一个文件夹）
├── requirements.txt         # Python 依赖
└── .env                     # API Key（不提交到 Git）
```

## 转写引擎对比

| | Whisper | Gemini API | 阿里云百炼 |
|---|---------|------------|-----------|
| 运行方式 | 本地 | 云端 | 云端 |
| 费用 | 免费 | 需 API Key | 需 API Key（极便宜） |
| 离线可用 | 是 | 否 | 否 |
| 首次启动 | 需下载模型 | 即用 | 即用 |
| 长音频 | 直接处理 | 自动切片合并 | 直接处理（最大 2GB / 12h） |
| 中文效果 | 一般 | 好 | 非常好 |
| 总结引擎 | Gemini | Gemini | Qwen |
