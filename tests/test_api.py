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
