"""B4 新手引导：/api/setup/status 四步状态、统计不启动浏览器、不逐个探测本地 API。"""
import asyncio
import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from relay.relay_engine import RelayEngine  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    snap_fp = json.dumps(appmod.CFG.get("fingerprint") or {}, ensure_ascii=False, sort_keys=True)
    appmod.CFG["fingerprint"] = {"backend": "chrome", "profiles": {}}
    yield appmod.CFG
    appmod.CFG["fingerprint"] = json.loads(snap_fp)


@pytest.fixture
def client(tmp_path, monkeypatch, env):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setattr(appmod, "save_config", lambda cfg: None)
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_setup_status_shape(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.get("/api/setup/status", headers=H).json()
        assert set(r) == {"chrome_ok", "backend", "backend_ok", "platforms_logged_in", "summary_ok"}
        assert r["platforms_logged_in"] == 0 and r["summary_ok"] is False
        assert c.get("/api/setup/status").status_code == 401


def test_setup_status_chrome_backend(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        chrome = tmp_path / "chrome.exe"
        chrome.write_text("", encoding="utf-8")
        appmod.CFG["chrome_path"] = str(chrome)
        r = c.get("/api/setup/status", headers=H).json()
        assert r["chrome_ok"] is True and r["backend_ok"] is True and r["backend"] == "chrome"
        # 路径坏了 → 两项都 False
        appmod.CFG["chrome_path"] = str(tmp_path / "nope.exe")
        r = c.get("/api/setup/status", headers=H).json()
        assert r["chrome_ok"] is False and r["backend_ok"] is False


def test_setup_status_summary_and_logged_in(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        appmod.S.engine.summ.client = object()        # 视为已配置
        appmod.S.engine._adapters[("deepseek", "main")] = _HealthAd()
        monkeypatch.setattr(appmod.S.browser, "is_launched", lambda pid, acct: True)
        r = c.get("/api/setup/status", headers=H).json()
        assert r["summary_ok"] is True
        assert r["platforms_logged_in"] == 1          # 启动且已登录的账号数


class _HealthAd:
    def __init__(self):
        self.info = appmod.S.engine.adapter_info("deepseek", "main")
        self.lock = asyncio.Lock()

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True


def test_account_status_probe_remote_false_skips_backend_query(tmp_path):
    """引导统计（probe_remote=False）不逐个问指纹客户端，避免加载变慢。"""
    from relay.fingerprint import Profile
    from relay.storage import MainPool

    class FakeBackend:
        def __init__(self):
            self.asked = []

        async def is_running(self, profile_id):
            self.asked.append(profile_id)
            return True

    class FakeBrowser:
        backend = FakeBackend()
        profile_map = {"deepseek": {"main": "E1"}}

        def is_launched(self, pid, account):
            return False

    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    eng = RelayEngine({"platforms": {"deepseek": {"port": 9301, "accounts": ["main"]}},
                       "relay_order": ["deepseek"]}, main, browser=FakeBrowser(), summarizer=None)
    st = asyncio.run(eng.account_status("deepseek", "main"))
    assert st["launched"] is True                        # probe_remote=True：问客户端
    assert eng.browser.backend.asked == ["E1"]
    st2 = asyncio.run(eng.account_status("deepseek", "main", probe_remote=False))
    assert st2["launched"] is False                      # 只看本地已知，不再问
    assert eng.browser.backend.asked == ["E1"]           # 没有新增查询
    # 适配器没启动过（launched 判定失败时绝不连接浏览器）
    assert ("deepseek", "main") not in eng._adapters


def test_setup_guide_ui_static():
    html = (ROOT / "relay" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "relay" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="setupGuide"' in html and 'id="setupSteps"' in html
    assert 'data-act="setupDismiss"' in html and 'data-act="setupRefresh"' in html
    assert "跳过后交接时只附带最近几轮原文" in html        # ③ 可跳过的说明
    for mark in ("loadSetupGuide", "relay_setup_dismissed", "relay_setup_skip3",
                 "setupGo", "setupSkip3", "card-fingerprint", "card-sumapi"):
        assert mark in js, f"app.js 缺少 {mark}"
    assert "localStorage.setItem('relay_setup_dismissed', '1')" in js   # 关闭状态存 localStorage
