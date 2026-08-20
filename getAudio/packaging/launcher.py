"""
桌面启动壳：常驻托盘/菜单栏图标 + 后台起 Flask + 自动开浏览器。

Verbatim 本质是个本地 Web 服务（app.py 一行没改），这层壳只解决「打包成
桌面 App 之后怎么启动」这一件事，具体是三步：
  1. 在后台线程里跑 Flask（复用 app.py 的服务器逻辑，不走命令行）
  2. 等端口真正起来后，打开默认浏览器指向它
  3. 给一个看得见、点得到的常驻图标——纯后台进程用户没法判断死活，
     也没法优雅退出（只能 Force Quit / 结束任务，可能中断正在写的任务）。

Mac 用 rumps（菜单栏），Windows 用 pystray（系统托盘）——两边原生 UI 习惯
不同，库也不同，但下面的 Flask 启动/探活逻辑完全共享，只在最后创建图标
这一步分支。
"""
import os
import socket
import sys
import threading
import time
import webbrowser


def _port():
    return int(os.environ.get('PORT', 5001))


def _port_reachable(port, timeout=0.5):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_reachable(port):
            return True
        time.sleep(0.3)
    return False


def _run_flask(ready_flag, error_box):
    """在后台线程里跑 Flask。异常存进 error_box 让主线程能看到（线程里的
    异常默认只会打到 stderr，GUI 应用没有终端，用户会以为它卡死了）。"""
    try:
        import app as flask_app  # noqa: PLC0415  # import 时触发 app.py 顶层初始化（taskdb.init() 等）
        # app.py 里这两个恢复调用原本包在 `if __name__ == '__main__'` 里，
        # 只有直接 `python app.py` 跑才会触发；这里是当模块 import，得手动补上。
        flask_app.recover_unfinished_tasks()
        flask_app.recover_unfinished_chains()
        ready_flag.set()
        flask_app.app.run(host='127.0.0.1', port=_port(), debug=False,
                           threaded=True, use_reloader=False)
    except Exception as exc:  # noqa: BLE001  — 让主线程能弹出人话错误，而不是静默卡死
        error_box['error'] = exc
        ready_flag.set()


def _fatal(title, message):
    """启动失败时弹个原生对话框——两边都没有终端可看，静默退出等于「装了没反应」。"""
    if sys.platform == 'darwin':
        os.system(f'osascript -e \'display alert "{title}" message "{message}"\' >/dev/null 2>&1')
    else:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
        except Exception:  # noqa: BLE001
            pass


def _run_mac(port):
    import rumps

    class VerbatimMenuBar(rumps.App):
        def __init__(self):
            super().__init__('Verbatim', title='◉ Verbatim', quit_button='退出 Verbatim')
            self.menu = ['在浏览器中打开']

        @rumps.clicked('在浏览器中打开')
        def open_browser(self, _sender):
            webbrowser.open(f'http://127.0.0.1:{port}')

    VerbatimMenuBar().run()


def _run_windows(port):
    import pystray
    from PIL import Image, ImageDraw

    # 简单画一个圆点当托盘图标，不额外打包 .ico 资源文件
    image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 56, 56), fill=(31, 91, 65, 255))

    def open_browser(_icon, _item):
        webbrowser.open(f'http://127.0.0.1:{port}')

    def quit_app(icon, _item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem('在浏览器中打开', open_browser, default=True),
        pystray.MenuItem('退出 Verbatim', quit_app),
    )
    pystray.Icon('Verbatim', image, 'Verbatim', menu).run()


def main():
    port = _port()

    # 重复启动（用户又点了一次图标）：这种情况不再起第二个 Flask——
    # 同端口第二次 bind 会直接失败——只开浏览器指过去。
    if _port_reachable(port):
        webbrowser.open(f'http://127.0.0.1:{port}')
        _fatal('Verbatim 已经在运行', '已经有一份服务在跑，直接给你打开浏览器了。')
        return

    ready = threading.Event()
    error_box = {}
    threading.Thread(target=_run_flask, args=(ready, error_box), daemon=True).start()

    if not ready.wait(timeout=60) or error_box.get('error'):
        detail = str(error_box.get('error') or '服务在 60 秒内没能启动，不确定原因。')
        _fatal('Verbatim 启动失败', detail)
        sys.exit(1)

    if not _wait_for_port(port, timeout=15):
        _fatal('Verbatim 启动失败', f'服务进程起来了，但端口 {port} 一直连不上。')
        sys.exit(1)

    webbrowser.open(f'http://127.0.0.1:{port}')

    if sys.platform == 'darwin':
        _run_mac(port)
    else:
        _run_windows(port)


if __name__ == '__main__':
    main()
