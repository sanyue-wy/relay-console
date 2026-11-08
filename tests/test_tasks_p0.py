"""P0 任务回归测试（T1-T4）。"""
import asyncio
import sys
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
import relay.browser as browser_mod  # noqa: E402
from adapters.spec import ChromeNotFound, PortNotReady, ProfileInUse  # noqa: E402
from relay.browser import ChromeManager  # noqa: E402
from relay.config import find_chrome, load_config  # noqa: E402


def make_client(tmp_path, monkeypatch, **kwargs):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    return TestClient(appmod.app, base_url="http://127.0.0.1", **kwargs)


# ---------------- T1：未处理异常返回 JSON 500
def test_t1_unhandled_exception_returns_json(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, raise_server_exceptions=False) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        monkeypatch.setattr(appmod.S.arena, "list_agents",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        r = c.get("/api/agents", headers=H)
        assert r.status_code == 500
        assert r.json() == {"error": "RuntimeError", "detail": "x"}


# ---------------- T2：Chrome 启动失败的可读错误
def _chrome_cfg(tmp_path, chrome: Path | None = None):
    chrome = chrome or (tmp_path / "chrome.exe")
    if chrome.name != "missing.exe":
        chrome.parent.mkdir(parents=True, exist_ok=True)
        chrome.write_text("", encoding="utf-8")
    return {"platforms": {"deepseek": {"port": 9301, "accounts": ["main"]}},
            "profiles_dir": str(tmp_path / "profiles"),
            "chrome_path": str(chrome),
            "fingerprint": {"backend": "chrome"}}


def test_t2_chrome_not_found(tmp_path):
    cfg = _chrome_cfg(tmp_path, tmp_path / "missing.exe")
    mgr = ChromeManager(cfg)
    with pytest.raises(ChromeNotFound, match="missing.exe"):
        mgr.launch("deepseek", "https://example.com", "main")


class _FakeProc:
    def __init__(self, code):
        self._code = code

    def poll(self):
        return self._code


def test_t2_profile_in_use(tmp_path, monkeypatch):
    mgr = ChromeManager(_chrome_cfg(tmp_path))
    monkeypatch.setattr(browser_mod, "_port_open", lambda p: False)
    monkeypatch.setattr(browser_mod.subprocess, "Popen", lambda *a, **k: _FakeProc(1))
    with pytest.raises(ProfileInUse, match="已被另一个 Chrome"):
        asyncio.run(mgr._chrome_endpoint("deepseek", "https://example.com", "main"))


def test_t2_port_not_ready(tmp_path, monkeypatch):
    mgr = ChromeManager(_chrome_cfg(tmp_path))
    monkeypatch.setattr(browser_mod, "_port_open", lambda p: False)
    monkeypatch.setattr(browser_mod.subprocess, "Popen", lambda *a, **k: _FakeProc(None))

    async def _nosleep(*a, **k):
        return None

    monkeypatch.setattr(browser_mod.asyncio, "sleep", _nosleep)
    with pytest.raises(PortNotReady, match="9301"):
        asyncio.run(mgr._chrome_endpoint("deepseek", "https://example.com", "main"))


@pytest.mark.parametrize("exc", [ChromeNotFound("x"), ProfileInUse("x"), PortNotReady("x")])
def test_t2_api_returns_502_with_code(tmp_path, monkeypatch, exc):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}

        async def boom(*a, **k):
            raise exc

        monkeypatch.setattr(appmod.S.browser, "page", boom)
        r = c.post("/api/platforms/deepseek/open", headers=H, json={"account": "main"})
        assert r.status_code == 502
        assert r.json()["error"] == exc.code


# ---------------- T3：自动探测 Chrome 路径
def _make_chrome(base: Path) -> Path:
    p = base / "Google/Chrome/Application/chrome.exe"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return p


def test_t3_find_chrome_order(tmp_path, monkeypatch):
    pf = _make_chrome(tmp_path / "pf")
    pf86 = _make_chrome(tmp_path / "pf86")
    local = _make_chrome(tmp_path / "local")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "pf86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    assert find_chrome() == str(pf)                 # ProgramFiles 优先
    pf.unlink()
    assert find_chrome() == str(pf86)               # 其次 ProgramFiles(x86)
    pf86.unlink()
    assert find_chrome() == str(local)              # 最后 LOCALAPPDATA
    local.unlink()
    assert find_chrome() is None


def test_t3_load_config_fills_empty_chrome_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({
        "host": "127.0.0.1", "port": 8801, "chrome_path": "",
        "data_dir": "./data", "profiles_dir": "./profiles",
        "summary": {"model": "deepseek-chat"},
    }), encoding="utf-8")
    pf = _make_chrome(tmp_path / "pf")
    monkeypatch.setenv("RELAY_HOME", str(home))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nope"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope2"))
    cfg = load_config()
    assert cfg["chrome_path"] == str(pf)


# ---------------- T4：0.0.0.0 打印地址换成 127.0.0.1
def test_t4_console_host_and_printed_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_NO_OPEN", "1")
    monkeypatch.setitem(appmod.CFG, "host", "0.0.0.0")
    monkeypatch.setitem(appmod.CFG, "port", 8765)
    assert appmod._console_host({"host": "0.0.0.0"}) == "127.0.0.1"
    assert appmod._console_host({"host": "::"}) == "127.0.0.1"
    assert appmod._console_host({"host": "127.0.0.1"}) == "127.0.0.1"
    assert appmod.mint_bootstrap_url({"host": "0.0.0.0", "port": 8765}).startswith(
        "http://127.0.0.1:8765/?token=")

    with make_client(tmp_path, monkeypatch):
        pass
    out = capsys.readouterr().out
    assert "控制台地址" in out
    assert "http://127.0.0.1:8765/?token=" in out
    assert "http://0.0.0.0" not in out


# ---------------- T13：可选托盘依赖与登录排查文档
def test_t13_optional_tray_and_troubleshooting_docs():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pystray" in req and "Pillow" in req
    assert req.count('; sys_platform == "win32"') >= 2      # 仅 Windows 安装
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for code in ("CHROME_NOT_FOUND", "PROFILE_IN_USE", "PORT_NOT_READY"):
        assert code in readme
    assert "Google 登录被拦" in readme
