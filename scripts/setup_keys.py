"""把摘要 API key 存进系统凭据库（不落盘）。python scripts/setup_keys.py"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay.crypto import CRED_ARENA, CRED_MAIN, get_fernet, set_api_key  # noqa: E402

if __name__ == "__main__":
    key = getpass.getpass("摘要 API key（DeepSeek 或其他 OpenAI 兼容服务）: ").strip()
    if key:
        set_api_key(key)
    get_fernet(CRED_MAIN)
    get_fernet(CRED_ARENA)
    print("已保存到系统凭据库；主池与对抗池密钥已就绪。")
