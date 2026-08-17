"""可选：以透明、置顶、无边框窗口运行桌宠（pywebview + Qt 后端）。

用法：
    .venv\\Scripts\\python.exe backend\\pet_window.py

说明：
    - 透明窗口使用 pywebview 的 Qt 后端（PySide6 + QtWebEngine）。
      Qt 原生支持 WA_TranslucentBackground + 透明页面背景：透明区域直接露出桌面，
      且鼠标输入正常（EdgeChromium 后端的透明模式无法接收鼠标输入）。
    - 前端通过 window.pywebview.api 调用 close / minimize 关闭或最小化桌宠。
"""
import threading

import uvicorn
import webview


class PetApi:
    """暴露给前端 JS 的桌宠控制接口（window.pywebview.api.*）。"""

    def __init__(self):
        self._window = None

    def set_window(self, window):
        self._window = window

    def close(self):
        """关闭桌宠窗口并退出程序。"""
        if self._window is not None:
            self._window.destroy()

    def minimize(self):
        """最小化桌宠窗口。"""
        if self._window is not None:
            self._window.minimize()


def main():
    server = uvicorn.Server(
        uvicorn.Config("main:app", host="127.0.0.1", port=8000, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()

    api = PetApi()
    window = webview.create_window(
        "MikuAgent - 初音未来桌宠",
        "http://127.0.0.1:8000",
        width=640,
        height=700,
        transparent=True,
        frameless=True,
        on_top=True,
        easy_drag=True,
        js_api=api,
    )
    api.set_window(window)
    webview.start(gui="qt")


if __name__ == "__main__":
    main()
