"""
Whisper transcription engine.
Uses openai-whisper library to run local speech-to-text with progress tracking.
"""

import threading
import whisper
import whisper.transcribe as whisper_transcribe
import tqdm as tqdm_module
from config import WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_LANGUAGE

# Lazy singleton model
_model = None
_model_lock = threading.Lock()


def get_model():
    """Load Whisper model (lazy, thread-safe singleton)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = whisper.load_model(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE)
    return _model


class ProgressTqdm(tqdm_module.tqdm):
    """Custom tqdm that intercepts update() calls to report progress."""
    _callback = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def update(self, n=1):
        super().update(n)
        if ProgressTqdm._callback and self.total:
            pct = min(99, int(self.n / self.total * 100))
            ProgressTqdm._callback(pct)


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

    # Monkey-patch tqdm in whisper.transcribe to intercept progress
    original_tqdm = whisper_transcribe.tqdm.tqdm
    ProgressTqdm._callback = progress_callback
    whisper_transcribe.tqdm.tqdm = ProgressTqdm

    try:
        result = model.transcribe(
            filepath,
            language=WHISPER_LANGUAGE,
            verbose=False,
            word_timestamps=False,
        )
    finally:
        # Restore original tqdm
        whisper_transcribe.tqdm.tqdm = original_tqdm
        ProgressTqdm._callback = None

    return result.get('segments', [])
