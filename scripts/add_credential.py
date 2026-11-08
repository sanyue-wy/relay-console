"""把某平台某账号的登录凭据存进 Windows 凭据管理器（P3.8 可选功能）。

python scripts/add_credential.py deepseek main

凭据只被控制台进程在打开平台时代填进登录表单，不落盘、不进日志，
MCP/Agent 永远接触不到。不开启 autofill 就完全不会用到。
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapters  # noqa: E402
from relay.crypto import set_platform_credential  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in adapters.available():
        print(f"用法: python scripts/add_credential.py <平台> <账号名>   平台可选: {adapters.available()}")
        raise SystemExit(1)
    pid, account = sys.argv[1], sys.argv[2]
    user = input(f"[{pid}:{account}] 登录用户名/手机号/邮箱: ").strip()
    secret = getpass.getpass("登录密码（输入不回显）: ")
    if not user or not secret:
        print("用户名或密码为空，未保存。")
        raise SystemExit(1)
    set_platform_credential(pid, account, user, secret)
    print("已存入 Windows 凭据管理器（service=relay-console，条目名不含密码）。")
