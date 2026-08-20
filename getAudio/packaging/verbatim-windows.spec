# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（Windows）。跟 verbatim-mac.spec 是同一套思路，
区别只有两处：
  1. 没有 mlx / mlx_whisper —— 那是 Apple Silicon 专属，requirements-windows.txt
     里本来就没装，这里也就不用 collect_all 它。
  2. 没有 BUNDLE() 步骤 —— .app 是 macOS 专属概念，Windows 到 COLLECT 就是
     最终产物（一个装了 Verbatim.exe 的文件夹，直接分发/加个安装程序都行）。

跟 mac 版一样用 --onedir：torch/scipy/onnxruntime 加起来接近 1GB，
onefile 每次启动都要先解压，onedir 直接从磁盘跑。
"""

from PyInstaller.utils.hooks import collect_all

datas = [
    ('static', 'static'),
    ('templates', 'templates'),
    ('bin', 'bin'),   # 内置的 ffmpeg.exe / ffprobe.exe / yt-dlp.exe（见 config.py 的 _find_binary）
]
binaries = []
hiddenimports = []

COLLECT_ALL_PACKAGES = [
    'torch',
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
    upx=False,
    console=False,   # 托盘 App，不弹黑窗口
    disable_windowed_traceback=False,
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
