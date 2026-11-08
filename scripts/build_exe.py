"""用 PyInstaller 打包 Windows exe：python scripts/build_exe.py

产物：
  dist/RelayConsole/                      onedir 程序目录（exe + 运行库，可整体发人/整体替换升级）
  dist/RelayConsole-<版本>-win64.zip      发给别人的压缩包

程序与数据分离：exe 首次运行会把 config.yaml、data/、profiles/ 建在
%LOCALAPPDATA%/RelayConsole（可用环境变量 RELAY_HOME 改到任意目录，比如 D:\\RelayData）。
升级 = 用新程序目录覆盖旧的，数据目录不动，旧会话/池/登录态全部继承。

打包前请先：pip install pyinstaller
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.config import APP_VERSION  # noqa: E402


def main() -> None:
    try:
        from PyInstaller.__main__ import run as pyi_run
    except ImportError:
        sys.exit("请先安装打包器：pip install pyinstaller")

    sep = ";" if sys.platform == "win32" else ":"
    pyi_run([
        "--noconfirm", "--clean",
        "--name", "RelayConsole",
        "--paths", str(ROOT),
        # 控制台界面（app.py 按 __file__ 相对路径读取）+ 配置模板（首次引导/升级合并用）
        "--add-data", f"{ROOT / 'relay' / 'static'}{sep}relay/static",
        "--add-data", f"{ROOT / 'config.example.yaml'}{sep}.",
        # playwright 需要 node driver；keyring 靠运行时发现后端
        "--collect-all", "playwright",
        "--collect-all", "keyring",
        # uvicorn 运行时动态导入的子模块
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols", "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets", "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan", "--hidden-import", "uvicorn.lifespan.on",
        # run_console 里是 uvicorn.run("relay.app:app") 字符串导入，静态分析看不到
        "--hidden-import", "relay.app",
        str(ROOT / "run_console.py"),
    ])  # pyi_run 失败时抛 SystemExit

    out = ROOT / "dist" / "RelayConsole"
    if not (out / "RelayConsole.exe").exists():
        sys.exit("打包结束但未找到 dist/RelayConsole/RelayConsole.exe，请查看上方 PyInstaller 输出")
    zip_path = shutil.make_archive(
        str(ROOT / "dist" / f"RelayConsole-{APP_VERSION}-win64"), "zip",
        root_dir=ROOT / "dist", base_dir="RelayConsole")
    print("\n完成。")
    print("  程序目录:", out)
    print("  分发包  :", zip_path)
    print("  数据目录: 接收者首次运行 exe 时自动建在 %LOCALAPPDATA%/RelayConsole（或 RELAY_HOME），")
    print("            升级只需覆盖程序目录，数据原样继承。")


if __name__ == "__main__":
    main()
