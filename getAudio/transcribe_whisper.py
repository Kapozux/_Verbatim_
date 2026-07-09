"""
Whisper transcription engine (local).

优先使用 faster-whisper（CTranslate2 实现：同精度、约 4 倍速度、内存减半，
且自带 VAD 跳过静音段）；未安装或初始化失败时自动回退到 openai-whisper，
保证本地始终可用。

Returns 与旧实现完全一致：list of {'start': float, 'end': float, 'text': str}。
"""

import os
import threading

from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# 按型号缓存 (model, backend)：Settings 里换档（如 small→large-v3）保存即生效，
# 下一个任务用新型号，无需重启。
_models = {}
_model_lock = threading.Lock()


def _current_size():
    """运行时读 Whisper 型号（env 优先，Settings 保存写 env）。"""
    return (os.environ.get('WHISPER_MODEL_SIZE') or WHISPER_MODEL_SIZE).strip()


def _get_model():
    """Load Whisper model (lazy, per-size cache, thread-safe). 优先 faster-whisper。"""
    size = _current_size()
    if size not in _models:
        with _model_lock:
            if size not in _models:
                try:
                    from faster_whisper import WhisperModel

                    # CPU 上 int8 量化最快且精度损失可忽略
                    compute = 'int8' if WHISPER_DEVICE == 'cpu' else 'float16'
                    _models[size] = (WhisperModel(
                        size,
                        device=WHISPER_DEVICE,
                        compute_type=compute,
                    ), 'faster')
                except Exception:
                    # faster-whisper 不可用（未安装/模型下载失败等）→ 回退旧实现
                    import whisper

                    _models[size] = (whisper.load_model(
                        size, device=WHISPER_DEVICE
                    ), 'openai')
    return _models[size]


def get_model():
    """兼容旧调用方：只返回 model。"""
    return _get_model()[0]


def transcribe_audio(filepath, progress_callback=None):
    """
    Run Whisper transcription on an audio file.

    Args:
        filepath: Path to the audio file.
        progress_callback: Optional callable(percent: int) for progress updates.

    Returns:
        List of segment dicts with keys: start (float), end (float), text (str).
    """
    model, backend = _get_model()

    if backend == 'faster':
        return _transcribe_faster(model, filepath, progress_callback)
    return _transcribe_openai(model, filepath, progress_callback)


def _transcribe_faster(model, filepath, progress_callback=None):
    """faster-whisper 路径：流式产出 segment，按已处理时长报进度。"""
    segments_iter, info = model.transcribe(
        filepath,
        language=WHISPER_LANGUAGE,      # None = 自动检测
        vad_filter=True,                # 跳过静音，长音频显著提速、也少幻听
        # ↓ 反幻听 / 反复读（对着静音编「嗯嗯嗯」、卡进死循环复读整句的根因）：
        condition_on_previous_text=False,  # 每段独立解码，不被前文污染 → 断掉自我喂养的循环
        no_repeat_ngram_size=3,            # 禁止 3-gram 立刻重复 → 掐断复读
        compression_ratio_threshold=2.4,   # 压缩率过高（成片重复）判为幻听丢弃
        log_prob_threshold=-1.0,           # 置信度过低的段丢弃
        no_speech_threshold=0.6,           # 判定为静音就不出字
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
            # 与 faster 路径一致的反幻听 / 反复读设置
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            logprob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
    finally:
        whisper_transcribe.tqdm.tqdm = original_tqdm

    return result.get('segments', [])
