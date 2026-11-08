"""P1 任务回归测试（T5-T10）。"""
import asyncio
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from adapters.spec import NotReady  # noqa: E402
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


# ---------------- T6：未登记账号不再静默回落到 main
def test_t6_unregistered_account_raises(tmp_path):
    mgr = ChromeManager(_chrome_cfg(tmp_path, {
        "deepseek": {"port": 9301, "accounts": ["main"]},
    }))
    with pytest.raises(PoolError, match="NOT_FOUND"):
        mgr._port("deepseek", "ghost")
    assert mgr._port("deepseek", "main") == 9301


# ---------------- T7：NotReady 不切换会话平台
class _FakeInfo:
    max_input_chars = 1000
    caps = 0


class _FakeAd:
    def __init__(self):
        self.info = _FakeInfo()
        self.lock = asyncio.Lock()

    async def new_chat(self):
        return None

    async def send(self, turn):
        raise NotReady("未登录")


def test_t7_not_ready_does_not_switch_platform(tmp_path):
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    sid = main.create_session("t", "deepseek")
    cfg = {"relay_order": ["deepseek", "kimi"], "default_cooldown_minutes": 1,
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main"]},
                         "kimi": {"port": 9303, "accounts": ["main"]}}}
    eng = RelayEngine(cfg, main=main, browser=None, summarizer=None)

    async def fake_adapter(pid, account="main"):
        return _FakeAd()

    eng.adapter = fake_adapter
    with pytest.raises(NotReady, match="请先在账户页登录 deepseek:main"):
        asyncio.run(eng.ask(sid, "hello", timeout=1))
    s = main.get_session(sid)
    assert s.current_platform == "deepseek"      # 没有被切到 kimi
    assert s.account == "main"
    assert main.messages(sid) == []              # 用户消息已回滚


# ---------------- T8：NOT_FOUND → 404；内部 KeyError → 500
def test_t8_not_found_404_and_keyerror_500(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    with TestClient(appmod.app, base_url="http://127.0.0.1", raise_server_exceptions=False) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.get("/api/sessions/s_nope", headers=H)
        assert r.status_code == 404 and r.json()["error"] == "NOT_FOUND"

        monkeypatch.setattr(appmod.S.arena, "list_agents",
                            lambda: (_ for _ in ()).throw(KeyError("boom")))
        r = c.get("/api/agents", headers=H)
        assert r.status_code == 500 and r.json()["error"] == "KeyError"
