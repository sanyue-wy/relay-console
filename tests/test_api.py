"""接口冒烟测试：控制台令牌、Agent 令牌、授权闭环。"""
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay.app as appmod  # noqa: E402


def make_client(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setitem(appmod.CFG, "data_dir", str(tmp_path))
    return TestClient(appmod.app)


def test_auth_and_approval_loop(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        assert c.get("/api/sessions").status_code == 401          # 无控制台令牌
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        iid = c.post("/api/pool/publish", headers=H, json={"title": "背景", "mode": "text", "text": "正文"}).json()["id"]
        tok = c.post("/api/agents", headers=H, json={"name": "t"}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/sessions", headers=A).status_code == 401  # Agent 令牌进不了控制台接口
        assert c.get("/agent/pool", headers=A).json()[0]["title"] == "背景"
        rid = c.post("/agent/requests", headers=A, json={"scope_type": "items", "item_ids": [iid], "reason": "r"}).json()["request_id"]
        assert c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["error"] == "NOT_APPROVED"
        assert c.get("/api/approvals", headers=H).json()["pending"][0]["id"] == rid
        c.post(f"/api/approvals/{rid}/approve", headers=H)
        assert c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["data"][0]["content"] == "正文"
        assert c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["error"] == "TICKET_USED"


def test_auto_open_url_rules(monkeypatch):
    base = {"host": "127.0.0.1", "port": 8765, "auto_open_browser": True}
    url = appmod._auto_open_url(base)
    assert url and url.startswith("http://127.0.0.1:8765/?token=")
    # 0.0.0.0 归一化为 127.0.0.1
    assert appmod._auto_open_url({**base, "host": "0.0.0.0"}).startswith("http://127.0.0.1:")
    # 配置关闭 / 程序化启动环境变量 → 不自动打开
    assert appmod._auto_open_url({**base, "auto_open_browser": False}) is None
    monkeypatch.setenv("RELAY_NO_OPEN", "1")
    assert appmod._auto_open_url(dict(base)) is None


def test_resume_chat_and_session_status(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        # 平台历史会话接进控制台
        r = c.post("/api/sessions/resume", headers=H,
                   json={"platform": "deepseek", "url": "https://chat.deepseek.com/a/chat/s/123", "title": "旧会话"})
        sid = r.json()["id"]
        d = c.get(f"/api/sessions/{sid}", headers=H).json()
        assert d["platform"] == "deepseek" and d["account"] == "main"
        assert c.post("/api/sessions/resume", headers=H,
                      json={"platform": "nope", "url": "x"}).status_code == 400
        # Agent 读会话状态：不存在拒绝，存在需批准，批准后只给状态不给正文
        tok = c.post("/api/agents", headers=H, json={"name": "st"}).json()["token"]
        A = {"Authorization": f"Bearer {tok}"}
        assert c.post("/agent/requests", headers=A,
                      json={"scope_type": "session", "session_id": "s_none", "reason": "r"}).status_code == 400
        rid = c.post("/agent/requests", headers=A,
                     json={"scope_type": "session", "session_id": sid, "reason": "r"}).json()["request_id"]
        assert c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["error"] == "NOT_APPROVED"
        c.post(f"/api/approvals/{rid}/approve", headers=H)
        data = c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["data"]
        assert data["scope_type"] == "session" and data["platform"] == "deepseek"
        assert "summary_preview" in data and "messages" not in data   # 正文永不出池
        assert c.post(f"/agent/requests/{rid}/fetch", headers=A).json()["error"] == "TICKET_USED"


def test_pool_session_linkage(tmp_path, monkeypatch):
    """会话池 ↔ Agent 读取池一一对应：同一会话重复发布原位更新，不产生第二条。"""
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        i1 = c.post("/api/pool/publish", headers=H,
                    json={"title": "t", "session_id": sid, "mode": "text", "text": "v1"}).json()
        assert i1["linked"] is True
        i2 = c.post("/api/pool/publish", headers=H,
                    json={"title": "t", "session_id": sid, "mode": "text", "text": "v2"}).json()
        assert i2["id"] == i1["id"]                      # 同一会话 → 同一条目
        items = c.get("/api/pool/items", headers=H).json()
        assert [i["session_id"] for i in items if i["id"] == i1["id"]] == [sid]
        assert c.get(f"/api/sessions/{sid}", headers=H).json()["pool_item_id"] == i1["id"]
        # 不带会话的独立发布另起一条
        i3 = c.post("/api/pool/publish", headers=H,
                    json={"title": "独立", "mode": "text", "text": "x"}).json()
        assert i3["linked"] is False and i3["id"] != i1["id"]
        assert len(c.get("/api/pool/items", headers=H).json()) == 2


def test_settings_and_accounts_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "save_config", lambda cfg: None)   # 测试不写真实 config.yaml
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        assert c.get("/api/settings").status_code == 401
        s = c.get("/api/settings", headers=H).json()
        assert "relay_order" in s and "near_threshold_chars" in s
        # 基本管理：改接力顺序与开关
        r = c.post("/api/settings", headers=H,
                   json={"relay_order": ["kimi", "deepseek"], "relay_on_truncation": True,
                         "default_cooldown_minutes": 30}).json()
        assert r["relay_order"] == ["kimi", "deepseek"] and r["relay_on_truncation"] is True
        assert c.post("/api/settings", headers=H,
                      json={"relay_order": ["nope"]}).status_code == 400
        # 账户登记：多账号池 + autofill，立即反映到 /api/platforms
        acc = c.post("/api/platforms/deepseek/accounts", headers=H,
                     json={"accounts": ["main", "backup"], "autofill": True}).json()
        assert acc["accounts"] == ["main", "backup"] and acc["autofill"] is True
        plats = c.get("/api/platforms", headers=H).json()["platforms"]
        ds = [p for p in plats if p["id"] == "deepseek"][0]
        assert [a["account"] for a in ds["accounts"]] == ["main", "backup"]
        assert c.post("/api/platforms/deepseek/accounts", headers=H,
                      json={"accounts": []}).status_code == 400
        assert c.post("/api/platforms/deepseek/accounts", headers=H,
                      json={"accounts": ["a", "a"]}).status_code == 400
        assert c.post("/api/platforms/nope/accounts", headers=H,
                      json={"accounts": ["x"]}).status_code == 400
