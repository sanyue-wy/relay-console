"""C3 V1.1 验收：密钥零泄漏、摘要 API 不可用仍能手动切换。

测试里的“密钥”全部由程序拼接的非凭据假值（不指向任何真实服务），
满足约束：源码与测试不写可用的凭据字面量。
"""
import asyncio
import json
import sys
import yaml
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from adapters.spec import AdapterInfo, Cap, Kind, Participant, Reply, Turn  # noqa: 402
from relay.relay_engine import RelayEngine  # noqa: E402
from relay.summarizer import Summarizer  # noqa: E402

# 非凭据假值：变量拼接，不出现 api_key= 形态的字面量
_D1, _D2 = "dummy", "key"
SUM_KEY = f"{_D1}-{_D2}-0001"
FP_KEY = f"{_D1}-{_D2}-0002"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离所有密钥 store 与 config 落盘，结束后恢复 CFG。"""
    snap_fp = json.dumps(appmod.CFG.get("fingerprint") or {}, ensure_ascii=False, sort_keys=True)
    snap_sum = json.dumps(appmod.CFG["summary"], ensure_ascii=False, sort_keys=True)
    snap_p = json.dumps(appmod.CFG["platforms"], ensure_ascii=False, sort_keys=True)
    appmod.CFG["fingerprint"] = {"backend": "chrome", "api_key": "", "profiles": {}}
    appmod.CFG["summary"].update({"provider": "deepseek", "base_url": "https://api.deepseek.com",
                                  "model": "deepseek-pytest", "timeout_seconds": 25})
    appmod.CFG["platforms"] = {"deepseek": {"port": 9301, "accounts": ["main"]},
                               "kimi": {"port": 9303, "accounts": ["main"]}}
    store: dict[str, str] = {}
    saved: list[str] = []

    def fake_save(cfg):
        saved.append(yaml.safe_dump(json.loads(json.dumps(cfg, default=str)), allow_unicode=True))

    monkeypatch.setattr(appmod, "get_api_key", lambda: store.get("summary_api_key"))
    monkeypatch.setattr(appmod, "set_api_key", lambda v: store.__setitem__("summary_api_key", v))
    monkeypatch.setattr(appmod, "delete_api_key", lambda: store.pop("summary_api_key", None))
    monkeypatch.setattr(appmod, "get_fingerprint_api_key", lambda: store.get("fingerprint_api_key"))
    monkeypatch.setattr(appmod, "set_fingerprint_api_key", lambda v: store.__setitem__("fingerprint_api_key", v))
    monkeypatch.setattr(appmod, "save_config", fake_save)
    yield {"store": store, "saved": saved}
    appmod.CFG["fingerprint"] = json.loads(snap_fp)
    appmod.CFG["summary"] = json.loads(snap_sum)
    appmod.CFG["platforms"] = json.loads(snap_p)


def make_client(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_keys_never_in_get_responses_or_config(tmp_path, monkeypatch, env):
    """两类密钥用过后：所有 GET 响应、保存的 config.yaml、审计里都搜不到密钥。"""
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        # 用上两类密钥（真实写路径：POST/PUT + set_*_api_key）
        assert c.post("/api/settings/summary", headers=H, json={
            "provider": "deepseek", "base_url": "https://api.deepseek.com",
            "model": "deepseek-pytest", "timeout_seconds": 25, "api_key": SUM_KEY}).status_code == 200
        assert c.put("/api/settings/fingerprint", headers=H, json={
            "backend": "chrome", "api_key": FP_KEY}).status_code == 200
        # 手工摘要走一次审计
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        c.put(f"/api/sessions/{sid}/summary", headers=H, json={"summary": "审计不记正文"})
        # 所有 GET 响应里搜不到密钥
        endpoints = ["/api/settings", "/api/settings/summary", "/api/settings/fingerprint",
                     "/api/platforms", f"/api/sessions/{sid}", "/api/sessions",
                     "/api/setup/status", "/api/platforms/deepseek/accounts",
                     "/api/platforms/deepseek/status", "/api/audit", "/api/pool/items"]
        for ep in endpoints:
            body = c.get(ep, headers=H).text
            assert SUM_KEY not in body, f"{ep} 泄漏摘要密钥"
            assert FP_KEY not in body, f"{ep} 泄漏指纹密钥"
        # 保存的 config.yaml（save_config 的写入内容）里搜不到密钥
        for s in env["saved"]:
            assert SUM_KEY not in s and FP_KEY not in s
        # 审计明细里也没有密钥
        dump = json.dumps(appmod.S.arena.audit_log(), ensure_ascii=False)
        assert SUM_KEY not in dump and FP_KEY not in dump
        # GET 永不出现密钥字段（has_key 布尔除外）
        assert c.get("/api/settings/summary", headers=H).json().get("api_key") is None
        assert c.get("/api/settings/fingerprint", headers=H).json().get("api_key") is None


class _BrokenClient:
    """摘要 API 不可用：任何调用都抛网络异常。"""

    class chat:
        class completions:
            @staticmethod
            async def create(**kw):
                raise ConnectionError("provider down")


class _Fake(Participant):
    def __init__(self, pid):
        self.info = AdapterInfo(pid, pid, Kind.WEB, Cap.NEW_CHAT, max_input_chars=10 ** 6)
        self.sent = []

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True
    async def new_chat(self, preamble=None): return "new"

    async def send(self, turn: Turn):
        self.sent.append(turn.content)

    async def receive(self, timeout=180):
        return Reply(f"{self.info.id}:回复", True, 1, None)


def test_manual_handoff_still_works_without_summary_api(tmp_path):
    """摘要 API 不可用（每次调用都抛异常）时，手动切换用缓存摘要照常完成。"""
    from relay.storage import MainPool
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    cfg = {"summary": {"recent_rounds_raw": 3, "timeout_seconds": 5},
           "platforms": {"deepseek": {"port": 9301, "accounts": ["main"]},
                         "kimi": {"port": 9303, "accounts": ["main"]}},
           "relay_order": ["deepseek", "kimi"]}
    fake_key = SUM_KEY
    summ = Summarizer(cfg, main, api_key=fake_key)
    summ.client = _BrokenClient()                      # API 挂了
    eng = RelayEngine(cfg, main, browser=None, summarizer=summ)
    eng._adapters = {("kimi", "main"): _Fake("kimi")}
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "历史问题")
    main.set_summary(sid, "缓存摘要：目标 A，已完成 B", 1)
    r = asyncio.run(eng.manual_handoff(sid, "kimi", "main", question="下一问"))
    assert r["platform"] == "kimi" and r["mode"] == "manual"
    s = main.get_session(sid)
    assert s.current_platform == "kimi" and s.chain == ["deepseek", "kimi"]
    sent = eng._adapters[("kimi", "main")].sent[0]
    assert "缓存摘要：目标 A，已完成 B" in sent          # 用的是缓存摘要
    assert sent.endswith("下一问")
    assert main.handoffs(sid)[-1]["reason"] == "manual: 用户手动切换"


def test_recent_rounds_zero_sends_summary_only(tmp_path):
    """A4 的「不附带原文」选项：recent_rounds=0 只发摘要，不回放任何消息。"""
    from relay.storage import MainPool
    main = MainPool(tmp_path, Fernet(Fernet.generate_key()))
    summ = Summarizer({"summary": {"recent_rounds_raw": 3}}, main, api_key=None)
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "秘密原文")
    main.set_summary(sid, "只有摘要", 1)
    pkg = asyncio.run(summ.handoff_package(sid, recent_rounds=0))
    assert "秘密原文" not in pkg and "只有摘要" in pkg
    assert "## 最近的对话（原文回放）" in pkg           # 小节标题仍在（内容为空）
