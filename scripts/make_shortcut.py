"""创建免注册表的快速启动方式。

只生成 .lnk 快捷方式文件（桌面 + 开始菜单程序文件夹），不写注册表、不装服务、
不设开机自启；删除快捷方式即完全卸载干净。

自动选择目标：优先源码运行（存在 .venv 时），否则指向 dist/RelayConsole/RelayConsole.exe。

用法：
  python scripts/make_shortcut.py             # 创建（auto）
  python scripts/make_shortcut.py --target exe    # 强制指向 exe
  python scripts/make_shortcut.py --remove        # 删除创建的快捷方式
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "Relay Console"


def resolve_target(mode: str) -> tuple[Path, Path]:
    exe = ROOT / "dist" / "RelayConsole" / "RelayConsole.exe"
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if mode == "exe":
        if not exe.exists():
            sys.exit("未找到打包产物，请先 python scripts/build_exe.py")
        return exe, exe.parent
    if mode == "source" or py.exists():
        if not py.exists():
            sys.exit("未找到 .venv，请先按 README 安装，或用 --target exe 指向打包产物")
        return py, ROOT                       # 源码：venv python + 仓库根为工作目录
    if exe.exists():
        return exe, exe.parent
    sys.exit("既没有 .venv 也没有打包好的 exe，无可启动目标")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["auto", "source", "exe"], default="auto")
    ap.add_argument("--remove", action="store_true", help="删除桌面与开始菜单里的快捷方式")
    a = ap.parse_args()

    import win32com.client                    # pywin32（requirements 已含）

    ws = win32com.client.Dispatch("WScript.Shell")
    folders = [Path(ws.SpecialFolders("Desktop")), Path(ws.SpecialFolders("Programs"))]
    if a.remove:
        for d in folders:
            p = d / f"{NAME}.lnk"
            p.unlink(missing_ok=True)
            print("已删除:", p)
        return

    target, workdir = resolve_target(a.target)
    for d in folders:
        lnk = ws.CreateShortcut(str(d / f"{NAME}.lnk"))
        lnk.TargetPath = str(target)
        lnk.WorkingDirectory = str(workdir)
        lnk.IconLocation = str(target)
        lnk.Description = "Relay Console 控制台（免注册表快捷方式）"
        lnk.Save()
        print("已创建:", d / f"{NAME}.lnk")
    print(f"目标: {target}")
    print("说明: 仅生成 .lnk 文件，未写注册表/服务/自启；删除这两个 .lnk 即彻底移除。")


if __name__ == "__main__":
    main()
