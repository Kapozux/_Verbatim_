import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _resolve_data_dir():
    """
    源码直跑（dev）：数据就放在项目目录旁，跟以前一样，不改变任何人的工作流。
    打包成 .app 跑（sys.frozen，PyInstaller 设的标记）：改存到
    ~/Library/Application Support/Verbatim ——包本身签名后是只读的，
    应用更新/重装也不该把用户的转写结果和任务库带走。
    GETAUDIO_DATA_DIR 环境变量可显式覆盖，两种场景都认。
    """
    override = os.environ.get('GETAUDIO_DATA_DIR')
    if override:
        return os.path.abspath(override)
    if getattr(sys, 'frozen', False):
        return os.path.expanduser('~/Library/Application Support/Verbatim')
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _resolve_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)


def _find_binary(name):
    """
    找 ffmpeg/ffprobe/yt-dlp 这类外部可执行文件，打包和源码直跑两种场景都要能定位到。
    打包成 .app 后（sys.frozen，PyInstaller 设的标记）：优先找内置在 Resources/bin/
    里的版本——收件人的机器大概率没装 Homebrew，不能指望 /opt/homebrew/bin 有东西。
    源码直跑：PATH → Homebrew 默认路径，跟以前完全一样，不改变任何人的开发体验。
    找不到就原样返回 name，让调用方按「命令不存在」正常报错，而不是这里静默失败。
    """
    if getattr(sys, 'frozen', False):
        bundle_root = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        # Windows 下可执行文件必须带 .exe 后缀，Mac/Linux 没有
        filename = f'{name}.exe' if sys.platform == 'win32' else name
        bundled = os.path.join(bundle_root, 'bin', filename)
        if os.path.isfile(bundled):
            return bundled
    import shutil as _shutil
    found = _shutil.which(name)
    if found:
        return found
    homebrew = f'/opt/homebrew/bin/{name}'
    return homebrew if os.path.isfile(homebrew) else name


FFMPEG_BIN = _find_binary('ffmpeg')
FFPROBE_BIN = _find_binary('ffprobe')
YTDLP_BIN = _find_binary('yt-dlp')

# Flask
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
RESULTS_FOLDER = os.path.join(DATA_DIR, 'results')
# 上传上限：视频（尤其 1080p 一小时）常轻松超过 500MB，之前会被 413 顶掉。
# 默认 4GB，可用 MAX_UPLOAD_MB 环境变量调。本地单用户，放宽无碍。
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', '4096'))
MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
AUDIO_EXTENSIONS = {'mp3', 'wav', 'flac', 'm4a', 'ogg', 'opus', 'webm'}
VIDEO_EXTENSIONS = {'mp4', 'mov', 'mkv', 'avi', 'm4v'}
ALLOWED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# 批量转录并发控制：一次可以丢进很多文件，但真正同时运行的数量按引擎区分。
# 本地 Whisper 每个任务都吃满 CPU/内存，必须保守；云引擎只是提交请求，可以放开。
ENGINE_CONCURRENCY = {
    # 本地 Whisper 并发。M 系多核（如 M4 Max 12 性能核）可开多路；配合下面的
    # WHISPER_CPU_THREADS，num_workers×cpu_threads ≈ 性能核数，避免多路互抢核。
    'whisper': int(os.environ.get('WHISPER_CONCURRENCY') or 4),
    # Paid Tier 1（~150 RPM）下 8 路并发稳妥。注意每个文件不止一次请求
    # （上传 + 长音频分段各一次），所以实际 QPS 会更高；若大量 429/空文本再下调。
    'gemini': int(os.environ.get('GEMINI_CONCURRENCY') or 12),
    'dashscope': 9,
    # Qwen-ASR：和 dashscope 同一套异步转写接口，配额也是同一个账号，给同样的并发。
    'qwenasr': 9,
    # 精准模式：单个任务内部会并发跑 Gemini 转写 + 阿里云说话人分离，最后再 Gemini 合并。
    # 一个任务实际打 2~3 次 Gemini + 1 次阿里云，所以并发压低到 4，避免叠加把两边都打爆。
    'precise': 4,
    # Gemini 3.5 Transcribe：2026-08 才公开预览的新端点，配额/稳定性还没摸透，
    # 先保守给 6（后面观察没问题再跟 gemini 引擎的 12 看齐）。
    'gemini35': 6,
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
# large-v3 精度最好（口语 + 专有名词多的内容值得）；M4 Max 扛得住。首次用会下载 ~3GB。
# 想快/省内存可在 Settings 或 env 改回 small/medium（WHISPER_MODEL_SIZE 运行时读 env）。
WHISPER_MODEL_SIZE = os.environ.get('WHISPER_MODEL_SIZE') or 'large-v3'
WHISPER_DEVICE = 'cpu'
# faster-whisper 每路用几个 CPU 线程 + 几个并行 worker。
# num_workers 让一个模型实例并行处理多路请求；cpu_threads 是每路的线程数。
# 目标：WHISPER_CONCURRENCY(=num_workers) × cpu_threads ≈ 性能核数（M4 Max 12）。
WHISPER_CPU_THREADS = int(os.environ.get('WHISPER_CPU_THREADS') or 3)
# None/空 = 自动检测语言（推荐，英文录音不会再被强制转成中文）；填 'zh' 可强制中文
WHISPER_LANGUAGE = os.environ.get('WHISPER_LANGUAGE') or None

# Gemini
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_TRANSCRIBE_MODEL') or 'gemini-2.5-flash'
# 分析/综合/核实层的模型。分层后事实判断已交给 grounding（核实模式）而非模型记忆，
# 所以默认用 2.5-pro（便宜、够用）；想要更晚的知识截止可用环境变量切到 3.x-pro。
GEMINI_ANALYSIS_MODEL = os.environ.get('GEMINI_ANALYSIS_MODEL') or 'gemini-2.5-pro'
# 逐期"抽取证据卡"是机械读写、又是调用大头（N 期 × 1）→ 用便宜的 flash（省钱大头）。
# 合成（人物画像）才用上面的 pro。advisor/orchestrator：大模型动嘴、小模型跑腿。
GEMINI_EXTRACT_MODEL = os.environ.get('GEMINI_EXTRACT_MODEL') or 'gemini-2.5-flash'
# 主模型 429/限流/挂了就自动降级到这些（flash 速率额度更宽、更便宜），保命用。
GEMINI_FALLBACK_MODELS = [
    m.strip() for m in
    os.environ.get('GEMINI_FALLBACK_MODELS', 'gemini-2.5-flash,gemini-flash-latest').split(',')
    if m.strip()
]

# ===== 分析层「大脑」预设 =====
# 阿里云百炼一把 DashScope key 通吃 DeepSeek/Qwen/Kimi/GLM（OpenAI 兼容端点）。
# 有内容审查：只用于非敏感博主。转录不走这里（都是文本模型）。
ALIYUN_COMPAT_BASE = os.environ.get('ALIYUN_COMPAT_BASE') or \
    'https://dashscope.aliyuncs.com/compatible-mode/v1'
# OpenRouter：一把 key 通吃 Claude 等海外模型（OpenAI 兼容端点，无内容审查）。
OPENROUTER_COMPAT_BASE = os.environ.get('OPENROUTER_COMPAT_BASE') or \
    'https://openrouter.ai/api/v1'
ANALYSIS_PRESET_DEFAULT = os.environ.get('ANALYSIS_PRESET') or 'gemini'
# 预设 → (provider, 抽取模型, 合成模型)
ANALYSIS_PRESETS = {
    'gemini':   ('gemini', GEMINI_EXTRACT_MODEL, GEMINI_ANALYSIS_MODEL),
    'deepseek': ('aliyun', 'deepseek-v4-flash', 'deepseek-v4-pro'),
    'qwen':     ('aliyun', 'qwen3.7-plus', 'qwen3.7-plus'),
    'kimi':     ('aliyun', 'kimi-k2.6', 'kimi-k2.6'),
    'glm':      ('aliyun', 'glm-5.2', 'glm-5.2'),
    # Claude（走 OpenRouter，带 thinking）：贵但强，抽取+合成同模型，成本随期数线性涨
    'opus46':   ('openrouter', 'anthropic/claude-opus-4.6', 'anthropic/claude-opus-4.6'),
    'opus5':    ('openrouter', 'anthropic/claude-opus-5', 'anthropic/claude-opus-5'),
}


def resolve_analysis(preset):
    """预设名 → (provider, 抽取模型, 合成模型)。未知则回落 gemini。

    运行时读 env（Settings 保存即生效，不用重启）：gemini 预设的两档模型
    可被 GEMINI_EXTRACT_MODEL / GEMINI_ANALYSIS_MODEL 覆盖。
    """
    p = ANALYSIS_PRESETS.get(preset or ANALYSIS_PRESET_DEFAULT,
                             ANALYSIS_PRESETS['gemini'])
    if p[0] == 'gemini':
        return ('gemini',
                os.environ.get('GEMINI_EXTRACT_MODEL') or p[1],
                os.environ.get('GEMINI_ANALYSIS_MODEL') or p[2])
    return p
# 卡片元数据（标题/标签）生成用 Flash：快、便宜，质量足够
GEMINI_ENRICH_MODEL = 'gemini-2.5-flash'
GEMINI_INLINE_LIMIT = 19 * 1024 * 1024  # 19 MB, use File API above this

# 送云引擎的音频一律先压成 Opus 单声道 16k（ogg 容器）。
# 下载下来的本来就是压缩过的 opus/m4a，之前先解成 WAV 再上传等于把体积放大
# 三到八倍：15 分钟 WAV 29MB 超过上面的内联上限，每块都得走 File API 上传+轮询。
# Opus 48k 一小时 ≈ 21MB、15 分钟一块 ≈ 5MB，直接内联字节送过去，省掉上传和等待。
# Gemini 收到后内部统一降到 16kbps 处理，阿里云 ASR 也收 ogg/opus，精度不受影响。
# 本地 Whisper 不走这条（无损 WAV 对它没有上传成本，也没必要多一次有损编码）。
CLOUD_AUDIO_BITRATE = os.environ.get('CLOUD_AUDIO_BITRATE') or '48k'

# Gemini 长音频按 15 分钟切块后，单个任务内同时在飞的块数（之前是一块一块串行）。
# 全局总在飞请求数仍由 ENGINE_CONCURRENCY['gemini'] 封顶（transcribe_gemini 里有全局闸），
# 所以这里只决定单个长文件能把自己拆多宽，不会叠加突破配额。
GEMINI_CHUNK_CONCURRENCY = int(os.environ.get('GEMINI_CHUNK_CONCURRENCY') or 4)

# DashScope (阿里云百炼)
DASHSCOPE_API_KEY = os.environ.get('DASHSCOPE_API_KEY', '')
# 阿里云 ASR 统一走 Qwen-Audio-3.0（2026-08 起）。paraformer-v2 已被阿里官方标为
# 上一代并建议迁移；实测同一段真人录音，paraformer 会把「AI」听成「悲哀」、
# 「语音识别」听成「原因识别」，Qwen 则准确，说话人分离两者相当（都正确分出 2 人）。
# 两代接口完全一致（同异步端点、同 diarization_enabled/speaker_count 参数、
# 同 sentences[].begin_time/text/speaker_id 返回结构），所以换模型名即可。
# 单文件上限 12 小时 / 2GB。需要退回旧模型时设 DASHSCOPE_ASR_MODEL=paraformer-v2。
DASHSCOPE_ASR_MODEL = (os.environ.get('DASHSCOPE_ASR_MODEL')
                       or 'qwen-audio-3.0-asr-flash-filetrans')
DASHSCOPE_LLM_MODEL = 'qwen-plus'


def make_gemini_client(api_key, timeout_ms=600_000, base_url=None):
    """统一构造 Gemini 客户端：带超时 + 可选自定义 base_url。

    base_url 显式传入优先；否则取环境变量 GEMINI_BASE_URL（Settings 里可填），
    给国内用户挂代理用；留空则直连官方。显式传入避免测试时改动全局 env 污染并发调用。
    老 SDK 不支持 HttpOptions 时回退到最简构造。
    """
    from google import genai
    base = ((base_url if base_url is not None else os.environ.get('GEMINI_BASE_URL'))
            or '').strip() or None
    try:
        from google.genai import types
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms, base_url=base),
        )
    except Exception:
        return genai.Client(api_key=api_key)
