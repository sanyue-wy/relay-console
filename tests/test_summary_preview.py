"""A2 摘要预览与编辑：refresh 回退不 500、手工摘要落库+审计只记长度、预览与 handoff 共用拼装。"""
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
from relay.relay_engine import RelayEngine  # noqa: E402
from relay.summarizer import HANDOFF_HEADER  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setattr(appmod, "save_config", lambda cfg: None)
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_refresh_without_provider_falls_back_not_500(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        # 无缓存摘要：source=none + warning
        r = c.post(f"/api/sessions/{sid}/summary/refresh", headers=H)
        assert r.status_code == 200
        assert r.json()["source"] == "none" and "未配置" in r.json()["warning"]
        # 有缓存摘要：source=cached
        appmod.S.main.set_summary(sid, "旧的四小节摘要", 0)
        r = c.post(f"/api/sessions/{sid}/summary/refresh", headers=H)
        assert r.json()["source"] == "cached" and "未配置" in r.json()["warning"]
        assert r.json()["summary"] == "旧的四小节摘要"


def test_refresh_success_and_failure(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        appmod.S.main.add_message(sid, "user", "deepseek", "问题")
        appmod.S.main.add_message(sid, "assistant", "deepseek", "回答")
        appmod.S.engine.summ.client = object()      # 视为已配置提供者（不发真实请求）

        async def ok_update(sid):
            appmod.S.main.set_summary(sid, "新摘要", 2)
            return "新摘要"

        async def bad_update(sid):
            raise RuntimeError("provider down")

        monkeypatch.setattr(appmod.S.engine.summ, "update", ok_update)
        r = c.post(f"/api/sessions/{sid}/summary/refresh", headers=H).json()
        assert r == {"summary": "新摘要", "upto_id": 2, "source": "api", "warning": None}
        # 失败 → 回退缓存摘要，warning 说明原因，绝不 500
        monkeypatch.setattr(appmod.S.engine.summ, "update", bad_update)
        r = c.post(f"/api/sessions/{sid}/summary/refresh", headers=H).json()
        assert r["source"] == "cached" and r["summary"] == "新摘要"
        assert "失败" in r["warning"] and "provider down" in r["warning"]


def test_edit_summary_roundtrip_and_audit(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        m1 = appmod.S.main.add_message(sid, "user", "deepseek", "q")
        m2 = appmod.S.main.add_message(sid, "assistant", "deepseek", "a")
        r = c.put(f"/api/sessions/{sid}/summary", headers=H, json={"summary": "手改的摘要"})
        assert r.status_code == 200 and r.json()["upto_id"] == m2      # 取最后一条消息 id
        s = appmod.S.main.get_session(sid)
        assert s.summary == "手改的摘要" and s.summarized_upto == m2
        # 审计只记长度，不记正文
        entries = [a for a in appmod.S.arena.audit_log() if a["action"] == "summary_edit"]
        assert entries and json.loads(entries[0]["detail"]) == {"session_id": sid, "chars": 5}
        assert "手改" not in json.dumps(entries[0])
        # 超长拒绝
        r = c.put(f"/api/sessions/{sid}/summary", headers=H, json={"summary": "x" * 8001})
        assert r.status_code == 400 and "8000" in r.json()["detail"]
        # 空会话（无消息）：upto 保持原值
        sid2 = c.post("/api/sessions", headers=H, json={"title": "t2", "platform": "kimi"}).json()["id"]
        r = c.put(f"/api/sessions/{sid2}/summary", headers=H, json={"summary": "空会话摘要"})
        assert r.json()["upto_id"] == appmod.S.main.get_session(sid2).summarized_upto


def test_preview_shares_assembly(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        appmod.S.main.add_message(sid, "user", "deepseek", "问题一")
        appmod.S.main.add_message(sid, "assistant", "deepseek", "回答一")
        r = c.get(f"/api/sessions/{sid}/handoff/preview?recent_rounds=2", headers=H).json()
        assert r["package"].startswith(HANDOFF_HEADER)
        assert r["chars"] == len(r["package"])
        assert r["target_limit"] == 30000          # deepseek MAX_INPUT
        assert r["mode"] == "inline"
        # 预览与 handoff_package 共用拼装：同一 recent_rounds 下逐字一致
        pkg = asyncio.run(appmod.S.engine.summ.handoff_package(sid, recent_rounds=2))
        assert pkg == r["package"]


def test_preview_target_params(tmp_path, monkeypatch, client):
    with client as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        appmod.S.main.add_message(sid, "user", "deepseek", "q")
        # 显式目标平台/账号
        r = c.get(f"/api/sessions/{sid}/handoff/preview?platform=kimi&account=main", headers=H).json()
        assert r["target_limit"] == 50000
        # 未登记账号 / 未知平台 → 400
        assert c.get(f"/api/sessions/{sid}/handoff/preview?account=ghost", headers=H).status_code == 400
        assert c.get(f"/api/sessions/{sid}/handoff/preview?platform=nope", headers=H).status_code == 400


def test_deliver_mode_pure_logic():
    info_upload = AdapterInfo("p", "p", Kind.WEB, Cap.NEW_CHAT | Cap.FILE_UPLOAD, max_input_chars=100)
    info_plain = AdapterInfo("p", "p", Kind.WEB, Cap.NEW_CHAT, max_input_chars=100)
    info_none = AdapterInfo("p", "p", Kind.WEB, Cap.NEW_CHAT, max_input_chars=None)
    dm = RelayEngine.deliver_mode
    assert dm(info_upload, 50) == "inline"
    assert dm(info_upload, 150) == "upload_md"      # 超长且有上传能力 → 走 .md 附件
    assert dm(info_plain, 150) == "trimmed"         # 不支持附件 → 裁剪
    assert dm(info_none, 10 ** 8) == "inline"       # 无上限（10**9 兜底）→ 实际永远是直发
