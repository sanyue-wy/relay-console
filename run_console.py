"""启动控制台：python run_console.py

控制台运行期间在 Windows 右下角显示托盘图标：
  双击 / “打开控制台” → 生成一个新的一次性登录地址并在浏览器打开
  “重启控制台” → 停掉服务后在同一进程里重新加载配置和 app（改了 config.yaml 时用）
  “退出” → 停止服务并退出
"""
from __future__ import annotations

import importlib
import threading
import time
import webbrowser

import uvicorn


def _tray_image():
    """程序生成托盘图标（紫底白字），不依赖额外图片文件。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((3, 3, 61, 61), radius=15, fill=(124, 58, 237, 255))
    try:
        font = ImageFont.truetype("arialbd.ttf", 38)
    except OSError:
        font = ImageFont.load_default()
    d.text((32, 33), "R", font=font, fill=(255, 255, 255, 255), anchor="mm")
    return img


def start_tray(server: uvicorn.Server, cfg, action: dict) -> tuple[object, threading.Thread] | None:
    """在后台线程启动托盘；pystray 不可用时返回 None，控制台照常运行。"""
    try:
        import pystray
    except Exception as exc:            # 没装 pystray / 无图形界面
        print(f"  [托盘] 未启用：{exc}", flush=True)
        return None

    from relay import app as appmod

    def open_console(icon, item):
        url = appmod.mint_bootstrap_url()
        if url:
            webbrowser.open(url)

    def restart(icon, item):
        action["value"] = "restart"
        server.should_exit = True

    def quit_app(icon, item):
        action["value"] = "quit"
        server.should_exit = True

    menu = pystray.Menu(
        pystray.MenuItem("打开控制台", open_console, default=True),
        pystray.MenuItem("重启控制台", restart),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("relay-console", _tray_image(),
                        f"Relay Console · 运行中 {cfg['host']}:{cfg['port']}", menu)
    thread = threading.Thread(target=icon.run, name="relay-tray", daemon=True)
    thread.start()
    for _ in range(40):                       # 等托盘线程真正把图标挂上去
        if getattr(icon, "visible", False):
            break
        time.sleep(0.05)
    if not getattr(icon, "visible", False):
        print("  [托盘] 图标启动失败（控制台继续运行，不影响使用）", flush=True)
        return None
    print("  托盘图标已显示在右下角（双击打开控制台，右键可重启/退出）", flush=True)
    return icon, thread


def run_once(reload_app: bool = True) -> str:
    """跑一轮控制台，返回 'restart' 或 'quit'。

    重启不另起进程：uvicorn 停掉后重新 import 配置和 relay.app，
    这样 config.yaml 的 host/port/平台/指纹设置都会重新加载。
    """
    import relay.app as appmod

    if reload_app:
        importlib.reload(appmod)              # 重新读 config.yaml，生成新的令牌
    cfg = appmod.CFG
    server = uvicorn.Server(uvicorn.Config(
        appmod.app, host=cfg["host"], port=int(cfg["port"]), log_level="info"))
    action = {"value": "quit"}
    tray = start_tray(server, cfg, action)
    try:
        server.run()
    finally:
        if tray:
            icon, thread = tray
            try:
                icon.stop()
            except Exception:
                pass
            thread.join(timeout=2)
    return action["value"]


def main() -> None:
    first = True
    while run_once(reload_app=not first) == "restart":
        first = False
        time.sleep(0.3)                       # 让端口/托盘先释放


if __name__ == "__main__":
    main()
