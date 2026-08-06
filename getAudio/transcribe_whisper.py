"""
Whisper transcription engine (local).

后端优先级：
  1. mlx-whisper —— Apple Silicon 上跑 Metal GPU。实测 M4 Max large-v3：
     5.1 分钟音频 12.2 秒（25× 实时），比 CPU 的 faster-whisper 快约 9.5 倍。
  2. faster-whisper（CTranslate2 CPU，自带 VAD）—— 非 Apple Silicon 或 mlx 不可用时。
  3. openai-whisper —— 最后兜底。

Returns 与旧实现完全一致：list of {'start': float, 'end': float, 'text': str}。
"""

import os
import platform
import threading

from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# 型号 → mlx-community 上的 MLX 权重仓库
_MLX_REPOS = {
    'tiny': 'mlx-community/whisper-tiny-mlx',
    'base': 'mlx-community/whisper-base-mlx',
    'small': 'mlx-community/whisper-small-mlx',
    'medium': 'mlx-community/whisper-medium-mlx',
    'large-v2': 'mlx-community/whisper-large-v2-mlx',
    'large-v3': 'mlx-community/whisper-large-v3-mlx',
    'large-v3-turbo': 'mlx-community/whisper-large-v3-turbo',
}


def _mlx_usable():
    """Apple Silicon + 装了 mlx_whisper + 没被显式关掉 → 用 GPU 后端。"""
    if os.environ.get('WHISPER_DISABLE_MLX') == '1':
        return False
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False

# 按型号缓存 (model, backend)：Settings 里换档（如 small→large-v3）保存即生效，
# 下一个任务用新型号，无需重启。
_models = {}
_model_lock = threading.Lock()

# mlx 的 Metal 命令编码器**不是线程安全的**：两个转写同时调 GPU 会触发
# AGXG16XFamilyCommandBuffer 断言 → SIGABRT，崩的是整个 Flask 进程（不只是这个任务）。
# 所以 GPU 路径全局串行。GPU 版 ~25× 实时，串行吞吐仍远高于 CPU 多路并行。
_mlx_gpu_lock = threading.Lock()


def _current_size():
    """运行时读 Whisper 型号（env 优先，Settings 保存写 env）。"""
    return (os.environ.get('WHISPER_MODEL_SIZE') or WHISPER_MODEL_SIZE).strip()


def _get_model():
    """Load Whisper model (lazy, per-size cache, thread-safe). 优先 faster-whisper。"""
    size = _current_size()
    if size not in _models:
        with _model_lock:
            if size not in _models:
                # 1) Apple Silicon：走 Metal GPU（快约 9.5 倍）。mlx 不持有模型对象，
                #    权重按 repo 名惰性加载并由 mlx_whisper 内部缓存，这里只存 repo 名。
                if _mlx_usable() and size in _MLX_REPOS:
                    _models[size] = (_MLX_REPOS[size], 'mlx')
                    return _models[size]
                try:
                    from faster_whisper import WhisperModel
                    from config import WHISPER_CPU_THREADS, ENGINE_CONCURRENCY

                    # CPU 上 int8 量化最快且精度损失可忽略
                    compute = 'int8' if WHISPER_DEVICE == 'cpu' else 'float16'
                    # num_workers=并发路数 → 一个模型实例并行处理多路；cpu_threads=每路线程数。
                    # 二者乘积贴近性能核数，避免多路互抢核导致整体变慢。
                    workers = max(1, ENGINE_CONCURRENCY.get('whisper', 1))
                    _models[size] = (WhisperModel(
                        size,
                        device=WHISPER_DEVICE,
                        compute_type=compute,
                        cpu_threads=WHISPER_CPU_THREADS,
                        num_workers=workers,
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

    if backend == 'mlx':
        try:
            return _transcribe_mlx(model, filepath, progress_callback)
        except Exception:  # noqa: BLE001
            # GPU 路径出任何问题都别让任务失败：退回 CPU 的 faster-whisper 重跑
            _models.pop(_current_size(), None)
            os.environ['WHISPER_DISABLE_MLX'] = '1'
            model, backend = _get_model()
    if backend == 'faster':
        return _transcribe_faster(model, filepath, progress_callback)
    return _transcribe_openai(model, filepath, progress_callback)


def _transcribe_mlx(repo, filepath, progress_callback=None):
    """mlx-whisper（Apple Silicon GPU）路径。

    反幻听参数和 faster-whisper 那条对齐；**temperature 固定 0**——默认的温度回退
    会把可疑段最多重试 6 次，既慢 6 倍又正是「是是是是…」这类复读循环的来源
    （实测同一段音频：默认 79s 且出现死循环，固定 0 后 12.2s 且循环消失）。
    mlx 没有 VAD，静音段靠 no_speech_threshold + 上层 sanitize 的静音掩码兜。
    """
    import mlx_whisper

    if progress_callback:
        progress_callback(5)
    # 全局串行：并发调 Metal 会崩整个进程（见 _mlx_gpu_lock 注释）
    with _mlx_gpu_lock:
        r = mlx_whisper.transcribe(
            filepath,
            path_or_hf_repo=repo,
            language=WHISPER_LANGUAGE,          # None = 自动检测
            temperature=0.0,                    # 不做温度回退（见上）
            condition_on_previous_text=False,   # 每段独立解码 → 断掉自我喂养的复读
            compression_ratio_threshold=2.4,    # 成片重复判为幻听
            logprob_threshold=-1.0,             # 置信度过低丢弃
            no_speech_threshold=0.6,            # 判定静音就不出字
            word_timestamps=False,
        )
    results = []
    for seg in (r.get('segments') or []):
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        results.append({
            'start': float(seg.get('start') or 0.0),
            'end': float(seg.get('end') or 0.0),
            'text': text,
        })
    if progress_callback:
        progress_callback(99)
    return results


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
