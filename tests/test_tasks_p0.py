"""P0 任务回归测试（T1-T4）。"""
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402


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
