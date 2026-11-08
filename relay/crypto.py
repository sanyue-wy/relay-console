"""密钥管理：所有密钥存在系统凭据库（Windows 凭据管理器），从不落盘明文。

下面这些常量只是凭据库条目名，不是密钥本身；真实密钥由 get_fernet 首次调用时
生成并写入凭据库，程序里永远不出现明文密钥字面量。
"""
from __future__ import annotations

import keyring
from cryptography.fernet import Fernet

SERVICE = "relay-console"        # 凭据库服务名
CRED_MAIN = "main_pool_key"      # 条目名（真实密钥存在条目里，条目名不是秘密）
CRED_ARENA = "arena_pool_key"
CRED_API = "summary_api_key"


def get_fernet(name: str) -> Fernet:
    key = keyring.get_password(SERVICE, name)
    if not key:
        key = Fernet.generate_key().decode()
        keyring.set_password(SERVICE, name, key)
    return Fernet(key.encode())


def get_api_key() -> str | None:
    return keyring.get_password(SERVICE, CRED_API)


def set_api_key(value: str) -> None:
    keyring.set_password(SERVICE, CRED_API, value)


# ---------------- 平台账号凭据（P3.8，可选功能）
# 条目名 = "platform:<平台>:<账号>"，内容 = "<用户名>:<密码>"。
# 只由控制台进程在打开平台时读取并注入登录表单；不落盘、不进日志、MCP/Agent 永远接触不到。
CRED_PLATFORM_PREFIX = "platform:"


def _platform_cred_name(pid: str, account: str) -> str:
    return f"{CRED_PLATFORM_PREFIX}{pid}:{account}"


def set_platform_credential(pid: str, account: str, username: str, secret: str) -> None:
    keyring.set_password(SERVICE, _platform_cred_name(pid, account), f"{username}:{secret}")


def get_platform_credential(pid: str, account: str) -> tuple[str, str] | None:
    raw = keyring.get_password(SERVICE, _platform_cred_name(pid, account))
    if not raw:
        return None
    user, _, secret = raw.partition(":")
    return user, secret


def has_platform_credential(pid: str, account: str) -> bool:
    return keyring.get_password(SERVICE, _platform_cred_name(pid, account)) is not None
