"""
Whisper transcription engine (local).

优先使用 faster-whisper（CTranslate2 实现：同精度、约 4 倍速度、内存减半，
且自带 VAD 跳过静音段）；未安装或初始化失败时自动回退到 openai-whisper，
保证本地始终可用。

Returns 与旧实现完全一致：list of {'start': float, 'end': float, 'text': str}。
"""

import threading

from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# Lazy singleton model
_model = None
_backend = None  # 'faster' | 'openai'
_model_lock = threading.Lock()


def get_model():
    """Load Whisper model (lazy, thread-safe singleton). 优先 faster-whisper。"""
    global _model, _backend
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from faster_whisper import WhisperModel

                    # CPU 上 int8 量化最快且精度损失可忽略
                    compute = 'int8' if WHISPER_DEVICE == 'cpu' else 'float16'
                    _model = WhisperModel(
                        WHISPER_MODEL_SIZE,
                        device=WHISPER_DEVICE,
                        compute_type=compute,
                    )
                    _backend = 'faster'
                except Exception:
                    # faster-whisper 不可用（未安装/模型下载失败等）→ 回退旧实现
                    import whisper

                    _model = whisper.load_model(
                        WHISPER_MODEL_SIZE, device=WHISPER_DEVICE
                    )
                    _backend = 'openai'
    return _model


def transcribe_audio(filepath, progress_callback=None):
    """
    Run Whisper transcription on an audio file.

    Args:
        filepath: Path to the audio file.
        progress_callback: Optional callable(percent: int) for progress updates.

    Returns:
        List of segment dicts with keys: start (float), end (float), text (str).
    """
    model = get_model()

    if _backend == 'faster':
        return _transcribe_faster(model, filepath, progress_callback)
    return _transcribe_openai(model, filepath, progress_callback)


def _transcribe_faster(model, filepath, progress_callback=None):
    """faster-whisper 路径：流式产出 segment，按已处理时长报进度。"""
    segments_iter, info = model.transcribe(
        filepath,
        language=WHISPER_LANGUAGE,  # None = 自动检测
        vad_filter=True,            # 跳过静音，长音频显著提速
    )
    duration = getattr(info, 'duration', None) or 0

    results = []
    for seg in segments_iter:
        results.append({
            'start': seg.start,
            'end': seg.end,
            'text': seg.text,
        })
        if progress_callback and duration:
            progress_callback(min(99, int(seg.end / duration * 100)))
    return results


# ========== openai-whisper 回退路径（与旧实现一致） ==========

def _transcribe_openai(model, filepath, progress_callback=None):
    import whisper.transcribe as whisper_transcribe
    import tqdm as tqdm_module

    class ProgressTqdm(tqdm_module.tqdm):
        """Custom tqdm that intercepts update() calls to report progress."""

        def update(self, n=1):
            super().update(n)
            if progress_callback and self.total:
                pct = min(99, int(self.n / self.total * 100))
                progress_callback(pct)

    original_tqdm = whisper_transcribe.tqdm.tqdm
    whisper_transcribe.tqdm.tqdm = ProgressTqdm

    try:
        result = model.transcribe(
            filepath,
            language=WHISPER_LANGUAGE,
            verbose=False,
            word_timestamps=False,
        )
    finally:
        whisper_transcribe.tqdm.tqdm = original_tqdm

    return result.get('segments', [])
