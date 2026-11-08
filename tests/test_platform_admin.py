"""B2 平台与账号：/status 不启动浏览器、/probe 选择器计数、PUT 平台校验与 needs_restart、Chrome 路径。"""
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
from adapters.spec import AdapterInfo, Cap, Kind  # noqa: E402
from relay.probe import probe_selectors  # noqa: E402
from relay.relay_engine import RelayEngine  # noqa: E402


@pytest.fixture
def plats():
    """确定性的平台配置（结束后恢复），避免依赖本机 config.yaml。"""
    snap_p = json.dumps(appmod.CFG["platforms"], ensure_ascii=False, sort_keys=True)
    snap_o = list(appmod.CFG["relay_order"])
    snap_c = appmod.CFG.get("chrome_path")
    appmod.CFG["platforms"] = {
        "deepseek": {"port": 9301, "accounts": ["main"], "enabled": True},
        "kimi": {"port": 9303, "accounts": ["main", "alt"], "enabled": True},
    }
    appmod.CFG["relay_order"] = ["deepseek", "kimi"]
    appmod.CFG["chrome_path"] = "C:/nonexistent/chrome.exe"
    yield appmod.CFG
    appmod.CFG["platforms"] = json.loads(snap_p)
    appmod.CFG["relay_order"] = snap_o
    appmod.CFG["chrome_path"] = snap_c


@pytest.fixture
def client(tmp_path, monkeypatch, plats):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setattr(appmod, "save_config", lambda cfg: None)
    return TestClient(appmod.app, base_url="http://127.0.0.1")


# ---------------- 状态接口
def test_status_not_launched(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.get("/api/platforms/deepseek/status?account=main", headers=H).json()
        assert r == {"platform": "deepseek", "account": "main", "launched": False,
                     "logged_in": False, "cooldown_until": None}
        assert c.get("/api/platforms/deepseek/status?account=ghost", headers=H).status_code == 400
        assert c.get("/api/platforms/nope/status", headers=H).status_code == 400
        # 冷却状态带出
        from datetime import datetime, timedelta
        appmod.S.engine.cooldowns[("deepseek", "main")] = datetime.now() + timedelta(hours=1)
        r = c.get("/api/platforms/deepseek/status", headers=H).json()
        assert r["cooldown_until"]


class _FakeAd:
    """探测/健康检查用的假适配器：sel + page + health。"""

    class _Loc:
        def __init__(self, css):
            self.css = css

        async def count(self):
            return 0 if self.css.endswith("broken") else 2

    class _Page:
        def locator(self, css):
            return _FakeAd._Loc(css)

    class _Sel:
        input = "css-input"
        send_button = "css-send"
        stop_button = None
        last_reply = "css-reply-broken"
        new_chat = "css-new"
        upload_input = None
        chat_list_item = None
        login_user = None
        login_pass = None
        login_submit = None

    def __init__(self):
        self.sel, self.page = _FakeAd._Sel(), _FakeAd._Page()
        self.info = AdapterInfo("deepseek", "DeepSeek", Kind.WEB, Cap.NEW_CHAT)
        self.lock = asyncio.Lock()

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True


def test_status_launched_logged_in(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        appmod.S.engine._adapters[("deepseek", "main")] = _FakeAd()
        monkeypatch.setattr(appmod.S.browser, "is_launched", lambda pid, acct: True)
        r = c.get("/api/platforms/deepseek/status", headers=H).json()
        assert r["launched"] is True and r["logged_in"] is True


# ---------------- 选择器探测
def test_probe_selectors_unit():
    out = asyncio.run(probe_selectors(_FakeAd()))
    assert out["ok"] is False                       # last_reply 匹配 0 → 必检字段不过
    by = {f["field"]: f for f in out["fields"]}
    assert by["input"]["count"] == 2 and by["input"]["pass"] is True
    assert by["last_reply"]["count"] == 0 and by["last_reply"]["pass"] is False
    assert by["upload_input"]["count"] is None      # 未配置的可选字段不判失败


def test_probe_endpoint(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        appmod.S.engine._adapters[("kimi", "alt")] = _FakeAd()
        r = c.post("/api/platforms/kimi/probe", headers=H, json={"account": "alt"})
        assert r.status_code == 200
        body = r.json()
        assert body["platform"] == "kimi" and body["account"] == "alt" and body["ok"] is False
        assert c.post("/api/platforms/kimi/probe", headers=H, json={"account": "ghost"}).status_code == 400


# ---------------- PUT 平台
def test_put_platform_enabled_roundtrip(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.put("/api/platforms/deepseek", headers=H, json={"enabled": False})
        assert r.status_code == 200 and r.json()["ok"] is True
        plats = c.get("/api/platforms", headers=H).json()["platforms"]
        ds = [p for p in plats if p["id"] == "deepseek"][0]
        assert ds["enabled"] is False and ds["port"] == 9301     # 控制台视图带 url/port
        # 停用后不能新建会话
        r = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"})
        assert r.status_code == 400 and "停用" in r.json()["detail"]
        # Agent 视图（engine.platform_status）不带 url/port（Agent 读不到配置）
        st = appmod.S.engine.platform_status()
        assert all("url" not in p and "port" not in p for p in st)
        assert [p["enabled"] for p in st if p["id"] == "deepseek"] == [False]


def test_put_platform_validation_errors(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.put("/api/platforms/deepseek", headers=H, json={"url": "http://x.example.com"})
        assert r.status_code == 400
        errs = r.json()["detail"]["errors"]
        assert errs[0]["field"] == "platforms.deepseek.url" and "https" in errs[0]["message"]
        r = c.put("/api/platforms/deepseek", headers=H, json={"port": 80})
        assert r.status_code == 400 and "1024-65535" in r.json()["detail"]["errors"][0]["message"]
        r = c.put("/api/platforms/deepseek", headers=H, json={"accounts": ["a", "a"]})
        assert r.status_code == 400 and "重复" in r.json()["detail"]["errors"][0]["message"]
        # 全局端口冲突：9303 已被 kimi:main 占用
        r = c.put("/api/platforms/deepseek", headers=H, json={"port": 9303})
        assert r.status_code == 400 and "9303" in r.json()["detail"]["errors"][0]["message"]
        # 未知平台
        assert c.put("/api/platforms/nope", headers=H, json={"enabled": True}).status_code == 400


def test_put_platform_needs_restart(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        monkeypatch.setattr(appmod.S.browser, "platform_open", lambda pid: True)
        r = c.put("/api/platforms/deepseek", headers=H, json={"port": 9399})
        assert r.json()["needs_restart"] is True
        # 只改开关不提示重启
        r = c.put("/api/platforms/deepseek", headers=H, json={"enabled": False})
        assert r.json()["needs_restart"] is False


# ---------------- 接力跳过已停用平台（引擎级）
class _FakeP:
    def __init__(self, pid, fail=False):
        self.info = AdapterInfo(pid, pid, Kind.WEB, Cap.NEW_CHAT, max_input_chars=1000)
        self.fail = fail
        self.sent = []

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True
    async def new_chat(self, preamble=None): return "new"

    async def send(self, turn):
        if self.fail:
            from adapters.spec import LimitReached, LimitState, LimitStatus
            raise LimitReached(LimitState(LimitStatus.EXHAUSTED, None, "dom", "已达上限"))
        self.sent.append(turn.content)

    async def receive(self, timeout=180):
        from adapters.spec import Reply
        return Reply(f"{self.info.id}:ok", True, 1, None)


class _Summ2:
    async def handoff_package(self, sid, recent_rounds=None):
        return "【会话接力】摘要"

    async def maybe_update(self, sid): ...


def test_relay_skips_disabled_platform(tmp_path):
    from relay.storage import MainPool
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    cfg = {"relay_order": ["deepseek", "kimi", "doubao"],
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main"], "enabled": False},
                         "kimi": {"port": 9303, "accounts": ["main"]},
                         "doubao": {"port": 9302, "accounts": ["main"]}},
           "default_cooldown_minutes": 1}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ2())
    eng._adapters = {("deepseek", "main"): _FakeP("deepseek", fail=True),
                     ("kimi", "main"): _FakeP("kimi"),
                     ("doubao", "main"): _FakeP("doubao")}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.ask(sid, "继续"))
    assert r["platform"] == "kimi"                     # deepseek 已停用 → 跳过


def test_relay_no_target_when_all_disabled(tmp_path):
    from relay.storage import MainPool
    from adapters.spec import AdapterError
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    cfg = {"relay_order": ["deepseek", "kimi"],
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main"]},
                         "kimi": {"port": 9303, "accounts": ["main"], "enabled": False}},
           "default_cooldown_minutes": 1}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ2())
    eng._adapters = {("deepseek", "main"): _FakeP("deepseek", fail=True),
                     ("kimi", "main"): _FakeP("kimi")}
    sid = main.create_session("t", "deepseek")
    with pytest.raises(AdapterError, match="冷却或不可用"):
        asyncio.run(eng.ask(sid, "继续"))
    assert eng._enabled("kimi") is False


# ---------------- Chrome 路径
def test_chrome_detect_and_validate(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        monkeypatch.setattr(appmod, "find_chrome", lambda: "C:/fake/Google/chrome.exe")
        r = c.post("/api/settings/chrome/detect", headers=H).json()
        assert r["path"] == "C:/fake/Google/chrome.exe"
        # 校验：存在且名为 chrome.exe
        real = tmp_path / "chrome.exe"
        real.write_text("", encoding="utf-8")
        r = c.post("/api/settings/chrome/validate", headers=H, json={"path": str(real)}).json()
        assert r == {"ok": True, "exists": True, "is_chrome": True, "message": "有效"}
        # 名字不对 / 不存在
        other = tmp_path / "browser.exe"
        other.write_text("", encoding="utf-8")
        r = c.post("/api/settings/chrome/validate", headers=H, json={"path": str(other)}).json()
        assert r["exists"] is True and r["is_chrome"] is False
        r = c.post("/api/settings/chrome/validate", headers=H, json={"path": str(tmp_path / "no.exe")}).json()
        assert r["exists"] is False and r["ok"] is False


def test_settings_save_chrome_path(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        target = tmp_path / "chrome.exe"
        target.write_text("", encoding="utf-8")
        r = c.post("/api/settings", headers=H, json={"chrome_path": str(target)})
        assert r.status_code == 200 and r.json()["chrome_path"] == str(target)
        assert c.post("/api/settings", headers=H, json={"chrome_path": "  "}).status_code == 400
