"""P1 任务回归测试（T5-T10）。"""
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from relay.browser import ChromeManager  # noqa: E402
from relay.config import Config  # noqa: E402
from relay.relay_engine import RelayEngine  # noqa: E402
from relay.storage import ArenaPool, MainPool, PoolError  # noqa: E402


def _chrome_cfg(tmp_path, platforms):
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("", encoding="utf-8")
    return {"platforms": platforms, "profiles_dir": str(tmp_path / "profiles"),
            "chrome_path": str(chrome), "fingerprint": {"backend": "chrome"}}


# ---------------- T5：端口全局校验
def test_t5_port_conflict_raises(tmp_path):
    cfg = _chrome_cfg(tmp_path, {
        "deepseek": {"port": 9301, "accounts": ["main", "alt"]},   # 9301, 9311
        "doubao": {"port": 9311, "accounts": ["main"]},            # 与 deepseek:alt 撞
    })
    with pytest.raises(ValueError, match="9311"):
        ChromeManager(cfg)


def test_t5_default_ports_ok(tmp_path):
    cfg = _chrome_cfg(tmp_path, {
        "deepseek": {"port": 9301, "accounts": ["main", "alt"]},
        "doubao": {"port": 9302, "accounts": ["main", "alt"]},
        "kimi": {"port": 9303, "accounts": ["main"]},
    })
    ChromeManager(cfg)      # 不抛异常
