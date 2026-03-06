import os

# Flask
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB max upload
AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'webm'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'mkv', 'avi', 'm4v'}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# Whisper
WHISPER_MODEL_SIZE = 'small'
WHISPER_DEVICE = 'cpu'
WHISPER_LANGUAGE = 'zh'

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = 'gemini-2.5-pro'
GEMINI_INLINE_LIMIT = 19 * 1024 * 1024  # 19 MB, use File API above this
