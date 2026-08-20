"""独立 yt-dlp 可执行文件的入口，Mac/Windows 通用：直接调用 yt_dlp 包自己的
main()，命令行行为跟 pip/brew 装的 yt-dlp 完全一致。CI 里单独用 PyInstaller
把这一个文件打成 bin/yt-dlp(.exe)，跟主 App 的打包互不干扰。"""
from yt_dlp import main

if __name__ == "__main__":
    main()
