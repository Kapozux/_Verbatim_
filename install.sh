#!/bin/bash
# Verbatim：装好并启动（macOS；Linux 先自己装好 ffmpeg、yt-dlp、uv）。
#
#   git clone https://github.com/Kapozux/_Verbatim_.git && cd _Verbatim_ && ./install.sh
#
# 做三件事：缺的 ffmpeg / yt-dlp / uv 用 Homebrew 装；用 uv 装一份 Python 3.12 和依赖到仓库根目录的 venv/
# （不碰系统 Python——系统的 pip 要么没有，要么被 Homebrew 拦下「externally-managed-environment」）；启动并打开浏览器。
# 再跑一遍也安全，装过的跳过。以后启动：bash getAudio/run.sh。只装不启动：./install.sh --no-start
set -eo pipefail
cd "$(dirname "$0")"
PY_VERSION=3.12
PORT="${PORT:-5001}"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }

# Homebrew 装在 /opt/homebrew（Apple 芯片）或 /usr/local（Intel），新开的终端不一定在 PATH 里
for b in /opt/homebrew/bin /usr/local/bin; do
    case ":$PATH:" in *":$b:"*) ;; *) [ -d "$b" ] && PATH="$b:$PATH" ;; esac
done

# 1. 系统工具：ffmpeg 抽音频、yt-dlp 下载视频（用 Homebrew 的，常更新；pip 版在老 Python 上会锁在旧版）、uv 装 Python 和依赖
missing=""
for tool in ffmpeg yt-dlp uv; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
    if command -v brew >/dev/null 2>&1; then
        say "用 Homebrew 安装：$missing"
        brew install $missing
    elif [ "$(uname)" = "Darwin" ]; then
        die "缺少$missing。先装 Homebrew（https://brew.sh 页面上那一行命令），再重跑 ./install.sh"
    else
        die "缺少$missing。用系统的包管理器装 ffmpeg；uv：curl -LsSf https://astral.sh/uv/install.sh | sh；yt-dlp：uv tool install yt-dlp。装好再重跑 ./install.sh"
    fi
fi

# 2. Python 和依赖：装进仓库根目录的 venv/（getAudio/run.sh 认这个位置）
if [ ! -x venv/bin/python ]; then
    say "准备 Python $PY_VERSION（uv 自己下载，不碰系统 Python）"
    uv venv venv --python "$PY_VERSION"
fi
say "安装依赖（第一次几分钟，要下载 PyTorch、Whisper 等几百 MB）"
uv pip install --python venv/bin/python -r getAudio/requirements.txt
venv/bin/python -c "import flask, google.genai, numpy" || die "依赖没装全，看上面的报错"

if [ "$1" = "--no-start" ]; then
    say "装好了。启动：bash getAudio/run.sh，然后打开 http://localhost:$PORT"
    exit 0
fi

# 3. 启动，起来以后打开浏览器
say "启动 Verbatim：http://localhost:$PORT（Ctrl+C 停止；以后启动用 bash getAudio/run.sh）"
echo "    本地 Whisper 第一次转写时会下载模型（large-v3 约 3 GB）；云端引擎和博主分析要在「设置」里填 Gemini key。"
(
    for _ in $(seq 1 60); do
        if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then
            if [ "$(uname)" = "Darwin" ]; then open "http://localhost:$PORT"
            elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:$PORT" >/dev/null 2>&1
            fi
            exit 0
        fi
        sleep 1
    done
) &
export PORT
exec bash getAudio/run.sh
