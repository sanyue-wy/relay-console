"""A1 摘要提供者设置：密钥永不回传/落盘、热更新、测试连接的错误翻译。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
import relay.summarizer as smod  # noqa: E402
from relay.summarizer import Summarizer, validate_base_url  # noqa: E402

H_ADMIN = None     # 每个 client 里取（CONSOLE_TOKEN 由模块加载时随机生成）


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离：密钥进内存 store、config 不落盘、CFG 的 summary 段用确定值并在结束后恢复。"""
    snap = json.dumps(appmod.CFG["summary"], ensure_ascii=False, sort_keys=True)
    appmod.CFG["summary"].update({"provider": "deepseek", "base_url": "https://api.deepseek.com",
                                  "model": "deepseek-chat", "timeout_seconds": 25})
    store: dict[str, str] = {}
    saved: list[str] = []

    def fake_set(v):
        store["summary_api_key"] = v

    def fake_get():
        return store.get("summary_api_key")

    def fake_delete():
        store.pop("summary_api_key", None)

    def fake_save(cfg):
        saved.append(json.dumps(cfg, ensure_ascii=False, sort_keys=True, default=str))

    monkeypatch.setattr(appmod, "set_api_key", fake_set)
    monkeypatch.setattr(appmod, "get_api_key", fake_get)
    monkeypatch.setattr(appmod, "delete_api_key", fake_delete)
    monkeypatch.setattr(appmod, "save_config", fake_save)
    yield {"store": store, "saved": saved}
    appmod.CFG["summary"] = json.loads(snap)


def make_client(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    return TestClient(appmod.app, base_url="http://127.0.0.1")


def test_get_summary_settings_never_returns_key(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.get("/api/settings/summary", headers=H)
        assert r.status_code == 200
        body = r.json()
        assert body["has_key"] is False
        assert "api_key" not in body and "sk-" not in json.dumps(body)
        assert "deepseek" in body["presets"]
        assert c.get("/api/settings/summary").status_code == 401    # 无令牌一律 401


def test_post_summary_saves_and_hot_updates(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.post("/api/settings/summary", headers=H, json={
            "provider": "deepseek", "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat", "timeout_seconds": 30, "api_key": "sk-live-test-1"})
        assert r.status_code == 200
        body = r.json()
        assert body["has_key"] is True and "api_key" not in body
        # 密钥只进凭据库：保存的配置里搜不到密钥
        assert env["store"]["summary_api_key"] == "sk-live-test-1"
        for snap in env["saved"]:
            assert "sk-live-test-1" not in snap
        # 热更新：无需重启，运行中的 Summarizer 已用新配置
        summ = appmod.S.engine.summ
        assert summ.available is True
        assert summ.cfg["model"] == "deepseek-chat" and summ.cfg["timeout_seconds"] == 30
        assert str(summ.client.base_url).rstrip("/").endswith("api.deepseek.com")
        # 空字符串不覆盖已存密钥
        c.post("/api/settings/summary", headers=H, json={
            "provider": "deepseek", "base_url": "https://api.deepseek.com",
            "model": "deepseek-reasoner", "timeout_seconds": 30})
        assert env["store"]["summary_api_key"] == "sk-live-test-1"
        assert appmod.S.engine.summ.cfg["model"] == "deepseek-reasoner"


def test_post_summary_clear_key_and_preset_fill(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        c.post("/api/settings/summary", headers=H, json={
            "provider": "deepseek", "api_key": "sk-1"})     # 不带 base_url/model → 用预设填默认值
        assert appmod.CFG["summary"]["base_url"] == "https://api.deepseek.com"
        assert appmod.CFG["summary"]["model"] == "deepseek-chat"
        assert env["store"]["summary_api_key"] == "sk-1"
        r = c.post("/api/settings/summary", headers=H, json={
            "provider": "deepseek", "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat", "timeout_seconds": 25, "clear_key": True})
        assert r.status_code == 200 and r.json()["has_key"] is False
        assert "summary_api_key" not in env["store"]
        assert appmod.S.engine.summ.available is False      # 客户端退回未配置状态


def test_post_summary_validation_errors(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        r = c.post("/api/settings/summary", headers=H, json={"provider": "zhipu"})
        assert r.status_code == 400
        errs = r.json()["detail"]["errors"]
        assert any(e["field"] == "provider" for e in errs)
        # 非 https 外网地址 → 400 + 字段级中文错误
        r = c.post("/api/settings/summary", headers=H, json={
            "provider": "custom", "base_url": "http://api.example.com", "model": "m"})
        assert r.status_code == 400
        assert any(e["field"] == "base_url" and "https" in e["message"]
                   for e in r.json()["detail"]["errors"])
        # custom 缺 base_url
        r = c.post("/api/settings/summary", headers=H, json={"provider": "custom", "model": "m"})
        assert r.status_code == 400
        assert any(e["field"] == "base_url" for e in r.json()["detail"]["errors"])


def test_base_url_rules():
    assert validate_base_url("https://api.deepseek.com") is None
    assert validate_base_url("http://127.0.0.1:11434/v1") is None       # Ollama 本机例外
    assert validate_base_url("http://localhost:1234/v1") is None
    assert "https" in validate_base_url("http://api.example.com")
    assert "http(s)" in validate_base_url("ftp://x")
    assert "不能为空" in validate_base_url("")


def _api_status_exc(cls, status: int, msg: str) -> Exception:
    req = httpx.Request("POST", "http://provider.test/v1/chat/completions")
    return cls(msg, response=httpx.Response(status, request=req), body=None)


def _fake_openai(exc) -> object:
    """替换 smod.AsyncOpenAI：client.chat.completions.create(*, ...) 一律抛 exc。"""

    class FakeCompletions:
        async def create(self, **kw):
            raise exc

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    return FakeClient()


def test_test_connection_translates_errors(monkeypatch):
    import openai
    s = Summarizer({"summary": {}}, main=None, api_key=None)
    cases = [
        (_api_status_exc(openai.AuthenticationError, 401, "bad key"), "密钥无效"),
        (_api_status_exc(openai.NotFoundError, 404, "no model"), "模型或地址错误"),
        (_api_status_exc(openai.RateLimitError, 429, "slow down"), "限流"),
    ]
    for exc, want in cases:
        monkeypatch.setattr(smod, "AsyncOpenAI", lambda **kw: _fake_openai(exc))
        out = asyncio.run(s.test_connection("https://x", "m", "k"))
        assert out["ok"] is False and want in out["error"], out
        assert isinstance(out["latency_ms"], int)
        assert out["model"] == "m"


def test_test_connection_timeout_translated(monkeypatch):
    import openai
    s = Summarizer({"summary": {}}, main=None, api_key=None)

    class FakeTimeoutCompletions:
        async def create(self, **kw):
            raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))

    class FakeChat:
        completions = FakeTimeoutCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(smod, "AsyncOpenAI", lambda **kw: FakeClient())
    out = asyncio.run(s.test_connection("https://x", "m", "k"))
    assert out["ok"] is False and "网络超时" in out["error"]


def test_test_endpoint_flow(tmp_path, monkeypatch, env):
    with make_client(tmp_path, monkeypatch) as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        # 未配置任何密钥 → 中文提示，不 500
        r = c.post("/api/settings/summary/test", headers=H, json={})
        assert r.status_code == 200 and r.json()["ok"] is False and "密钥" in r.json()["error"]
        # 凭据库里有密钥 → 走 test_connection；这里 mock 掉真实网络调用
        env["store"]["summary_api_key"] = "sk-x"

        async def fake_tc(base_url, model, api_key, timeout=20.0):
            return {"ok": True, "latency_ms": 123, "model": model, "error": None}

        monkeypatch.setattr(appmod.S.engine.summ, "test_connection", fake_tc)
        r = c.post("/api/settings/summary/test", headers=H, json={})
        assert r.json() == {"ok": True, "latency_ms": 123, "model": "deepseek-chat", "error": None}
        # 临时参数优先且不保存
        called = {}

        async def fake_tc2(base_url, model, api_key, timeout=20.0):
            called.update({"base_url": base_url, "model": model, "key": api_key})
            return {"ok": False, "latency_ms": 5, "model": model, "error": "密钥无效（401）：请检查 API 密钥是否正确"}

        monkeypatch.setattr(appmod.S.engine.summ, "test_connection", fake_tc2)
        r = c.post("/api/settings/summary/test", headers=H, json={
            "base_url": "https://api.other.com", "model": "m2", "api_key": "sk-tmp"})
        assert r.json()["ok"] is False and "密钥无效" in r.json()["error"]
        assert called == {"base_url": "https://api.other.com", "model": "m2", "key": "sk-tmp"}
        assert appmod.CFG["summary"]["base_url"] == "https://api.deepseek.com"   # 未被临时参数污染
        # 非法 base_url 直接中文报错，不发请求
        r = c.post("/api/settings/summary/test", headers=H, json={"base_url": "http://evil.com"})
        assert r.json()["ok"] is False and "https" in r.json()["error"]
