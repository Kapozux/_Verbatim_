# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置。

用 --onedir 而不是 --onefile：torch/mlx/scipy/onnxruntime 这些加起来快 1GB，
onefile 模式每次启动都要先解压到 /tmp 再跑，1GB 量级下这个开销是几十秒起——
onedir 直接从磁盘跑，双击基本秒开。

torch / numba / mlx 这几个包会在运行时动态 import 子模块或加载非 .py 的数据文件
（编译好的 kernel、tokenizer 词表等），PyInstaller 静态扫描 import 语句抓不全，
必须用 collect_all 把整个包（代码+数据+动态库）原样搬进去，不能只指望 hiddenimports。
"""

from PyInstaller.utils.hooks import collect_all

datas = [
    ('static', 'static'),
    ('templates', 'templates'),
    ('bin', 'bin'),   # 内置的 ffmpeg / ffprobe / yt-dlp（见 config.py 的 _find_binary）
]
binaries = []
hiddenimports = []

# 这些包用 collect_all：既要代码里静态分析抓不到的动态 import，
# 也要打包内附带的非 .py 资源（编译产物、模型配置、词表等）。
COLLECT_ALL_PACKAGES = [
    'torch',
    'mlx',
    'mlx_whisper',
    'faster_whisper',
    'ctranslate2',
    'numba',
    'llvmlite',
    'onnxruntime',
    'whisper',        # openai-whisper 回退路径
    'tiktoken',
    'google.genai',
    'dashscope',
    'av',             # faster-whisper 解码用
]

for package in COLLECT_ALL_PACKAGES:
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# app.py 里各引擎模块是按需 import 的（用户没配的引擎不会在启动时 import 到），
# 静态扫描找不到，显式列出来避免打包后点了却报 ModuleNotFoundError。
hiddenimports += [
    'transcribe_whisper',
    'transcribe_gemini',
    'transcribe_dashscope',
    'transcribe_precise',
    'analyze',
    'audioutil',
    'downloader',
    'enrich',
    'harness',
    'sanitize',
    'summarize',
    'taskdb',
    'config',
]

a = Analysis(
    ['packaging/launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # PyInstaller 打包时进程会重新执行入口脚本来发现子进程需求，
        # 这些库常见的 multiprocessing spawn 探测在 frozen 环境下没意义，
        # 排除掉减小体积、避免误触发。
        'matplotlib', 'tkinter', 'PyQt5', 'PySide2', 'PySide6', 'PyQt6',
        'IPython', 'notebook', 'jupyter',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Verbatim',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # UPX 压缩对 torch/mlx 这类超大动态库收益有限，还会拖慢启动/偶发误报杀软，不用
    console=False,   # 菜单栏 App，不要弹终端窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Verbatim',
)

app = BUNDLE(
    coll,
    name='Verbatim.app',
    icon=None,
    bundle_identifier='com.kapozux.verbatim',
    info_plist={
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleName': 'Verbatim',
        'NSHighResolutionCapable': True,
        # 麦克风/文件访问不需要特殊权限声明——本地文件由 Flask 常规文件 I/O 读取，
        # 不经系统隐私框架；如果以后加浏览器录音之类功能才需要额外加 usage description。
    },
)
