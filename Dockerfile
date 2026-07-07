# Verbatim — self-host image
# Build context is the repo root (the app lives in getAudio/).
FROM python:3.11-slim

# System deps: ffmpeg (audio extraction) + yt-dlp (downloads).
# yt-dlp is installed as the standalone binary (always latest) rather than the
# pip package, matching how the app resolves it via PATH.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl ca-certificates \
 && curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
        -o /usr/local/bin/yt-dlp \
 && chmod a+rx /usr/local/bin/yt-dlp \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/getAudio

# CPU-only PyTorch first (keeps the image ~2 GB smaller than the default CUDA build).
# Torch is only used by the openai-whisper fallback; faster-whisper uses CTranslate2.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY getAudio/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY getAudio/ .

# GETAUDIO_DB lives under results/ (a mounted volume) so task state
# survives image rebuilds, not just container restarts.
ENV PYTHONUNBUFFERED=1 \
    FLASK_DEBUG=0 \
    HOST=0.0.0.0 \
    PORT=5001 \
    YTDLP_COOKIES_BROWSER="" \
    GETAUDIO_DB=/app/getAudio/results/tasks.db

EXPOSE 5001
CMD ["python", "app.py"]
