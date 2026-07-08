import os
from dotenv import load_dotenv

load_dotenv()

# Flask
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
RESULTS_FOLDER = os.path.join(os.path.dirname(__file__), 'results')
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB max upload
AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'webm'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'mkv', 'avi', 'm4v'}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# 批量转录并发控制：一次可以丢进很多文件，但真正同时运行的数量按引擎区分。
# 本地 Whisper 每个任务都吃满 CPU/内存，必须保守；云引擎只是提交请求，可以放开。
ENGINE_CONCURRENCY = {
    'whisper': 2,
    # Paid Tier 1（~150 RPM）下 8 路并发稳妥。注意每个文件不止一次请求
    # （上传 + 长音频分段各一次），所以实际 QPS 会更高；若大量 429/空文本再下调。
    'gemini': 8,
    'dashscope': 9,
    # 精准模式：单个任务内部会并发跑 Gemini 转写 + 阿里云说话人分离，最后再 Gemini 合并。
    # 一个任务实际打 2~3 次 Gemini + 1 次阿里云，所以并发压低到 4，避免叠加把两边都打爆。
    'precise': 4,
}

# 链条（URL→下载→转写→分析）的全局限流：所有链条共享，不按每条链条算。
# 这样开 1 条还是 5 条链，对 YouTube 和 Gemini 的瞬时压力恒定，多开只是排队更长、不会叠加超标。
CHAIN_DOWNLOAD_CONCURRENCY = 4   # 全部链条同时下载的视频数上限（再高易被 YouTube 限速/风控）
CHAIN_ANALYSIS_CONCURRENCY = 4   # 全部链条同时进行的逐期分析请求数上限

# yt-dlp 元数据语言偏好：拉中文标题，避免 UP 主上传的英文翻译标题被抓到
YTDLP_LANG = 'zh-CN'

# 从浏览器借 cookies 给 yt-dlp（用登录态绕过 B站 412 风控、抬高 YouTube 限额）。
# 值为浏览器名（chrome/edge/firefox/brave…）；置空则不带 cookies。
# 注意：仅本机、读你自己的浏览器 cookie；换机器或没装该浏览器时设为 '' 关闭。
YTDLP_COOKIES_FROM_BROWSER = os.environ.get('YTDLP_COOKIES_BROWSER', 'chrome')

# 访问令牌：不设置（默认）= 完全不启用鉴权，本地照常用。
# 要暴露到局域网/公网前，在 .env 里加 GETAUDIO_TOKEN=一串随机字符串，
# 然后浏览器首次访问 http://host:5001/?token=该字符串 即可（之后走 cookie）。
AUTH_TOKEN = os.environ.get('GETAUDIO_TOKEN', '')

# Whisper
WHISPER_MODEL_SIZE = 'small'
WHISPER_DEVICE = 'cpu'
# None/空 = 自动检测语言（推荐，英文录音不会再被强制转成中文）；填 'zh' 可强制中文
WHISPER_LANGUAGE = os.environ.get('WHISPER_LANGUAGE') or None

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = 'gemini-2.5-pro'
# 分析/综合/核实层的模型。分层后事实判断已交给 grounding（核实模式）而非模型记忆，
# 所以默认用 2.5-pro（便宜、够用）；想要更晚的知识截止可用环境变量切到 3.x-pro。
GEMINI_ANALYSIS_MODEL = os.environ.get('GEMINI_ANALYSIS_MODEL') or 'gemini-2.5-pro'
# 逐期"抽取证据卡"是机械读写、又是调用大头（N 期 × 1）→ 用便宜的 flash（省钱大头）。
# 合成（人物画像）才用上面的 pro。advisor/orchestrator：大模型动嘴、小模型跑腿。
GEMINI_EXTRACT_MODEL = os.environ.get('GEMINI_EXTRACT_MODEL') or 'gemini-2.5-flash'
# 卡片元数据（标题/标签）生成用 Flash：快、便宜，质量足够
GEMINI_ENRICH_MODEL = 'gemini-2.5-flash'
GEMINI_INLINE_LIMIT = 19 * 1024 * 1024  # 19 MB, use File API above this

# DashScope (阿里云百炼)
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', '')
DASHSCOPE_ASR_MODEL = 'paraformer-v2'
DASHSCOPE_LLM_MODEL = 'qwen-plus'


def make_gemini_client(api_key, timeout_ms=600_000):
    """统一构造 Gemini 客户端：带超时 + 可选自定义 base_url。

    base_url 取自环境变量 GEMINI_BASE_URL（Settings 里可填），给国内用户挂代理用；
    留空则直连官方。老 SDK 不支持 HttpOptions 时回退到最简构造。
    """
    from google import genai
    base = (os.environ.get('GEMINI_BASE_URL') or '').strip() or None
    try:
        from google.genai import types
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms, base_url=base),
        )
    except Exception:
        return genai.Client(api_key=api_key)
