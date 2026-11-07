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
