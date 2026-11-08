"""代码审查修复的回归测试：XSS/CSP、端口、冷却路由、授权内容哈希、凭据格式等。"""
import asyncio
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from relay.browser import ChromeManager  # noqa: E402
from relay.relay_engine import RelayEngine  # noqa: E402
from relay.storage import ArenaPool, MainPool, PoolError  # noqa: E402


@pytest.fixture
def arena(tmp_path):
    return ArenaPool(tmp_path, Fernet(Fernet.generate_key()), 300)


# ---------------- 前端：转义与 CSP 形态
def test_frontend_escapes_quotes_and_has_no_inline_handlers():
    html = (ROOT / "relay/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "relay/static/app.js").read_text(encoding="utf-8")
    assert "onclick=" not in html and "onchange=" not in html
    assert 'src="/app.js"' in html
    assert "&quot;" in js and "&#39;" in js   # esc 覆盖双引号/单引号
    assert "data-act" in html and "data-act" in js


def test_security_headers_and_csp(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "get_fernet", lambda n: Fernet(Fernet.generate_key()))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    with TestClient(appmod.app, base_url="http://127.0.0.1") as c:
        c.cookies.set("console_token", appmod.CONSOLE_TOKEN)
        r = c.get("/")
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert c.get("/app.js").status_code == 200


def test_bootstrap_token_is_one_time(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "get_fernet", lambda n: Fernet(Fernet.generate_key()))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    monkeypatch.setattr(appmod, "_BOOTSTRAP_TOKEN", "boot-test")
    with TestClient(appmod.app, base_url="http://127.0.0.1") as c:
        r = c.get("/?token=boot-test", follow_redirects=False)
        assert r.status_code in (302, 307) and "console_token" in r.headers["set-cookie"]
        # 同一 bootstrap token 第二次使用不再有效（清掉刚拿到的 cookie）
        c.cookies.clear()
        assert c.get("/?token=boot-test", follow_redirects=False).status_code == 401


# ---------------- 端口分配
def test_port_allocation_no_collision():
    cfg = {"platforms": {p: {"port": base, "accounts": ["main", "a1", "a2"]}
                         for p, base in zip(["deepseek", "doubao", "kimi", "gemini", "hark"],
                                            [9301, 9302, 9303, 9304, 9305])},
           "profiles_dir": "profiles", "fingerprint": {"backend": "chrome"}}
    mgr = ChromeManager(cfg)
    ports = [mgr._port(p, a) for p in cfg["platforms"] for a in ("main", "a1", "a2")]
    assert len(ports) == len(set(ports))
    assert mgr._port("deepseek", "a1") == 9311


# ---------------- 按账号冷却
def test_cooldown_is_per_account():
    cfg = {"relay_order": ["deepseek", "kimi"],
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main", "alt"]},
                         "kimi": {"port": 9303, "accounts": ["main"]}}}
    eng = RelayEngine(cfg, main=None, browser=None, summarizer=None)
    eng._cool(("deepseek", "main"), None)
    assert eng._next_target("deepseek", "main", set()) == ("deepseek", "alt")
    # 同平台全部冷却后才轮到 kimi
    eng._cool(("deepseek", "alt"), None)
    assert eng._next_target("deepseek", "main", set()) == ("kimi", "main")


# ---------------- 交接包截断
def test_handoff_truncation_keeps_header_and_question():
    package = ("【会话接力】头部\n\n## 摘要\n" + "摘" * 500 +
               "\n\n## 最近的对话（原文回放）\n" + "回放" * 1000)
    out = RelayEngine._fit_handoff(package, "现在的问题", 600)
    assert out.startswith("【会话接力】头部")
    assert out.endswith("现在的问题")
    assert len(out) <= 600


# ---------------- 授权内容哈希
def test_content_change_after_approval_is_rejected(arena):
    iid = arena.publish_item("标题", "第一版", session_id="s1")
    rid = arena.create_request("agent:a", "items", {"item_ids": [iid]}, "理由")
    arena.decide(rid, True)
    arena.publish_item("标题", "第二版（未审）", session_id="s1")   # 批准后原位更新
    with pytest.raises(PoolError, match="CONTENT_CHANGED"):
        arena.fetch("agent:a", rid)


def test_agent_name_rules(arena):
    arena.create_agent("ok-1")
    with pytest.raises(PoolError, match="AGENT_EXISTS"):
        arena.create_agent("ok-1")
    with pytest.raises(PoolError, match="BAD_NAME"):
        arena.create_agent("bad,name")


# ---------------- 凭据格式
def test_credential_with_colon_roundtrip(monkeypatch):
    import relay.crypto as crypto
    store = {}
    monkeypatch.setattr(crypto.keyring, "get_password", lambda s, n: store.get(n))
    monkeypatch.setattr(crypto.keyring, "set_password", lambda s, n, v: store.__setitem__(n, v))
    crypto.set_platform_credential("deepseek", "main", "user:with:colon", "p@ss:word")
    assert crypto.get_platform_credential("deepseek", "main") == ("user:with:colon", "p@ss:word")


# ---------------- 消息只取最后 N 条
def test_messages_limit_in_sql(tmp_path):
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    sid = main.create_session("t", "deepseek")
    for i in range(5):
        main.add_message(sid, "user", "deepseek", f"m{i}")
    assert [m["content"] for m in main.messages(sid, limit=2)] == ["m3", "m4"]


# ---------------- 会话不存在 → 404
def test_missing_session_returns_404(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(appmod, "get_fernet", lambda n: Fernet(Fernet.generate_key()))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    with TestClient(appmod.app, base_url="http://127.0.0.1") as c:
        r = c.get("/api/sessions/s_nope", headers={"X-Console-Token": appmod.CONSOLE_TOKEN})
        assert r.status_code == 404 and r.json()["error"] == "NOT_FOUND"


def test_ask_rolls_back_user_message_on_failure(tmp_path):
    """接力全部失败时不留下孤儿用户消息（避免重试出现两条）。"""
    from adapters.spec import AdapterError

    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    sid = main.create_session("t", "deepseek")
    cfg = {"relay_order": ["deepseek"], "default_cooldown_minutes": 1,
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main"]}}}
    eng = RelayEngine(cfg, main=main, browser=None, summarizer=None)

    async def boom(*a, **k):
        raise AdapterError("no browser")

    eng.adapter = boom
    with pytest.raises(AdapterError):
        asyncio.run(eng.ask(sid, "hello", timeout=1))
    assert main.messages(sid) == []


# ---------------- 托盘：一次性地址与重启循环
def test_mint_bootstrap_url_rotates_token():
    old = appmod._BOOTSTRAP_TOKEN
    try:
        u1 = appmod.mint_bootstrap_url({"host": "127.0.0.1", "port": 8765})
        u2 = appmod.mint_bootstrap_url({"host": "0.0.0.0", "port": 8765})
        assert u1 != u2 and u1.startswith("http://127.0.0.1:8765/?token=")
        assert u2.startswith("http://127.0.0.1:8765/?token=")   # 0.0.0.0 归一化
        assert appmod._BOOTSTRAP_TOKEN and appmod._BOOTSTRAP_TOKEN in u2
    finally:
        appmod._BOOTSTRAP_TOKEN = old


def test_run_once_returns_restart(monkeypatch):
    import run_console

    class FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            pass

    def fake_tray(server, cfg, action):
        action["value"] = "restart"
        return None

    monkeypatch.setattr(run_console.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(run_console, "start_tray", fake_tray)
    assert run_console.run_once(reload_app=False) == "restart"
