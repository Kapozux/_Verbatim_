# getAudio

音视频转文字工具 —— 上传音频或视频文件，自动转录为带时间戳的文字。

## 功能

- **双引擎转写**：支持 Whisper（本地离线）和 Gemini API（云端）
- **长音频支持**：自动将长音频按 15 分钟切片，逐段转写后合并，时间戳连续不中断
- **视频支持**：上传视频文件时自动提取音轨，复用音频转写流程
- **实时进度**：通过 SSE 实时推送转写进度和分段结果
- **结果导出**：一键复制文本或下载为 TXT 文件
- **时间戳格式**：统一 `[HH:MM:SS]`，支持任意时长音频

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

### 配置（使用 Gemini 时需要）

创建 `.env` 文件：

```
GEMINI_API_KEY=your_api_key_here
```

## 运行

```bash
source venv/bin/activate
python app.py
```

浏览器打开 [http://localhost:5001](http://localhost:5001)

## 项目结构

```
├── app.py                 # Flask 主应用，路由 / SSE / 视频音轨提取
├── config.py              # 配置项（模型、文件大小限制、允许格式等）
├── transcribe_gemini.py   # Gemini 转写引擎（含切片、时间戳偏移、合并）
├── transcribe_whisper.py  # Whisper 本地转写引擎
├── templates/index.html   # 前端页面
├── static/
│   ├── app.js             # 前端交互逻辑
│   └── style.css          # 样式
├── requirements.txt       # Python 依赖
└── run.sh                 # 快速启动脚本
```

## 转写引擎对比

| | Whisper | Gemini API |
|---|---------|------------|
| 运行方式 | 本地 | 云端 |
| 费用 | 免费 | 需 API Key |
| 离线可用 | 是 | 否 |
| 首次启动 | 需下载模型 | 即用 |
| 长音频 | 直接处理 | 自动切片合并 |
