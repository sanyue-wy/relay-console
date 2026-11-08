"""B3 指纹浏览器连接向导：明文密钥迁移、密钥只进凭据库、测试/保存接口、needs_restart。"""
import json
import re
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from relay.fingerprint import FingerprintError  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离 fingerprint 段与密钥 store；结束时恢复。"""
    snap_fp = json.dumps(appmod.CFG.get("fingerprint") or {}, ensure_ascii=False, sort_keys=True)
    snap_p = json.dumps(appmod.CFG["platforms"], ensure_ascii=False, sort_keys=True)
    appmod.CFG["fingerprint"] = {"backend": "ovobrowser", "api_base": "",
                                 "api_key": "", "startup_timeout_seconds": 90,
                                 "profiles": {"deepseek": {"main": "W1"}}}
    appmod.CFG["platforms"] = {"deepseek": {"port": 9301, "accounts": ["main"]},
                               "kimi": {"port": 9303, "accounts": ["main"]}}
    store: dict[str, str] = {}
    saved: list[str] = []
    monkeypatch.setattr(appmod, "get_fingerprint_api_key", lambda: store.get("fingerprint_api_key"))
    monkeypatch.setattr(appmod, "set_fingerprint_api_key", lambda v: store.__setitem__("fingerprint_api_key", v))
    monkeypatch.setattr(appmod, "save_config", lambda cfg: saved.append(
        json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str)))
    yield {"store": store, "saved": saved}
    appmod.CFG["fingerprint"] = json.loads(snap_fp)
    appmod.CFG["platforms"] = json.loads(snap_p)


def make_client(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_plaintext_api_key_migrated_on_startup(tmp_path, monkeypatch, env):
    appmod.CFG["fingerprint"]["api_key"] = "sk-fp-plain-text"     # 老配置里的明文密钥
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        # 迁移进凭据库，配置值置空
        assert env["store"]["fingerprint_api_key"] == "sk-fp-plain-text"
        assert appmod.CFG["fingerprint"]["api_key"] == ""
        # 保存的配置里搜不到密钥
        for s in env["saved"]:
            assert "sk-fp-plain-text" not in s
        # 审计只说明事件，不记密钥
        audits = [a for a in appmod.S.arena.audit_log() if a["action"] == "fingerprint_key_migrated"]
        assert audits and "sk-fp-plain-text" not in audits[0]["detail"]
        # GET 返回 has_key，永不回传密钥
        r = c.get("/api/settings/fingerprint", headers=H).json()
        assert r["has_key"] is True and "sk-" not in json.dumps(r)


def test_get_fingerprint_settings(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.get("/api/settings/fingerprint", headers=H).json()
        assert r["backend"] == "ovobrowser" and r["has_key"] is False
        assert r["profiles"] == {"deepseek": {"main": "W1"}}
        assert c.get("/api/settings/fingerprint").status_code == 401


def test_fingerprint_test_endpoint(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        # chrome：无需连接，直接通过
        r = c.post("/api/settings/fingerprint/test", headers=H, json={"backend": "chrome"}).json()
        assert r == {"ok": True, "profiles": [], "error": None}

        class FakeBackend:
            async def list_profiles(self):
                from relay.fingerprint import Profile
                return [Profile(id="E1", name="DeepSeek 主号", group="g1")]

            async def aclose(self): ...

        seen = {}

        def fake_build(fp):
            seen.update(fp)
            return FakeBackend()

        monkeypatch.setattr(appmod, "build_backend", fake_build)
        r = c.post("/api/settings/fingerprint/test", headers=H, json={
            "backend": "adspower", "api_base": "http://127.0.0.1:50325", "api_key": "sk-t"}).json()
        assert r["ok"] is True and r["profiles"][0]["id"] == "E1"
        assert seen["api_key"] == "sk-t"          # 临时参数优先
        # 失败 → 中文错误，不 500
        def bad_build(fp):
            raise FingerprintError("无法连接 ovoBrowser 本地 API（http://127.0.0.1:50325）：请确认客户端已启动")
        monkeypatch.setattr(appmod, "build_backend", bad_build)
        r = c.post("/api/settings/fingerprint/test", headers=H, json={"backend": "ovobrowser"}).json()
        assert r["ok"] is False and "无法连接" in r["error"]


def test_fingerprint_put_and_needs_restart(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.put("/api/settings/fingerprint", headers=H, json={
            "backend": "adspower", "api_base": "", "startup_timeout_seconds": 60,
            "profiles": {"deepseek": {"main": "E9"}}, "api_key": "sk-fp-new"})
        assert r.status_code == 200
        body = r.json()
        assert body["needs_restart"] is True       # backend 变化
        assert appmod.CFG["fingerprint"]["backend"] == "adspower"
        assert appmod.CFG["fingerprint"]["api_key"] == ""     # 配置里永远为空
        assert env["store"]["fingerprint_api_key"] == "sk-fp-new"
        for s in env["saved"]:
            assert "sk-fp-new" not in s
        # 只换密钥也提示重启（运行中的后端实例仍持有旧密钥）
        r = c.put("/api/settings/fingerprint", headers=H, json={
            "backend": "adspower", "api_key": "sk-fp-2"})
        assert r.json()["needs_restart"] is True
        # 校验错误：未知 backend / 同一环境分给两个账号
        r = c.put("/api/settings/fingerprint", headers=H, json={"backend": "camoufox"})
        assert r.status_code == 400
        assert "backend" in r.json()["detail"]["errors"][0]["field"]
        r = c.put("/api/settings/fingerprint", headers=H, json={
            "backend": "adspower",
            "profiles": {"deepseek": {"main": "E1"}, "kimi": {"main": "E1"}}})
        assert r.status_code == 400
        assert "同时分配" in r.json()["detail"]["errors"][0]["message"]
        # 不带 profiles 的保存保留原有映射
        r = c.put("/api/settings/fingerprint", headers=H, json={"backend": "adspower"}).json()
        assert r["profiles"] == {"deepseek": {"main": "E9"}}


def test_fingerprint_ui_static():
    html = (ROOT / "relay" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "relay" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="card-fingerprint"' in html
    for v in ("chrome", "adspower", "ovobrowser"):
        assert f'value="{v}"' in html
    assert "设置 → 本地 API" in html                    # 各后端的说明
    assert "两个客户端不能同时开启" in html               # 已知边界说明
    assert "data-act=\"fpTest\"" in html and "data-act=\"fpSave\"" in html
    assert "renderFpMapping" in js and "fpenv_" in js   # 映射表渲染
    assert "esc(e.id)" in js                            # 环境名/ID 经转义
