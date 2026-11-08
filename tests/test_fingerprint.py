"""指纹浏览器后端离线自测：不连真实客户端，用 httpx.MockTransport 模拟本地 API。"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relay.browser import ChromeManager
from relay.fingerprint import (AdsPowerBackend, FingerprintError, OvoBrowserBackend, build_backend,
                               resolve_profile_id)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(coro):
    return asyncio.run(coro)


# ---------------- 配置解析
def test_resolve_profile_id_ok():
    assert resolve_profile_id({"deepseek": {"main": "p1"}}, "deepseek", "main") == "p1"


def test_resolve_profile_id_missing_names_key():
    with pytest.raises(FingerprintError, match=r"deepseek\.alt"):
        resolve_profile_id({"deepseek": {"main": "p1"}}, "deepseek", "alt")


def test_build_backend_unknown():
    with pytest.raises(FingerprintError, match="可选"):
        build_backend({"backend": "camoufox"})


def test_build_backend_ovo_defaults():
    b = build_backend({"backend": "ovobrowser"})
    assert isinstance(b, OvoBrowserBackend)
    assert b.api_base == "http://127.0.0.1:50325"


# ---------------- AdsPower
def test_ads_start_returns_ws_and_sends_api_key():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("api-key")
        return httpx.Response(200, json={
            "code": 0, "msg": "success",
            "data": {"ws": {"puppeteer": "ws://127.0.0.1:5555/devtools/browser/x"},
                     "debug_port": "5555"},
        })

    backend = AdsPowerBackend(api_key="k-123", client=_client(handler))

    async def go():
        return await backend.start("u1")

    assert _run(go()) == "ws://127.0.0.1:5555/devtools/browser/x"
    assert seen["path"] == "/api/v1/browser/start"
    assert seen["key"] == "k-123"


def test_ads_start_falls_back_to_debug_port():
    async def handler(request):
        return httpx.Response(200, json={"code": 0, "data": {"debug_port": "6001"}})

    backend = AdsPowerBackend(client=_client(handler))
    assert _run(backend.start("u1")) == "http://127.0.0.1:6001"


def test_ads_already_running_queries_active():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"code": 10003, "msg": "Browser is already open"})
        return httpx.Response(200, json={
            "code": 0,
            "data": {"status": "Active", "debug_port": 6002,
                     "ws": {"puppeteer": "ws://127.0.0.1:6002/devtools/browser/y"}},
        })

    backend = AdsPowerBackend(client=_client(handler))
    assert _run(backend.start("u1")) == "ws://127.0.0.1:6002/devtools/browser/y"
    assert calls == ["/api/v1/browser/start", "/api/v1/browser/active"]


def test_ads_error_is_actionable():
    async def handler(request):
        return httpx.Response(200, json={"code": -1, "msg": "Require api-key"})

    backend = AdsPowerBackend(client=_client(handler))
    with pytest.raises(FingerprintError, match="Require api-key"):
        _run(backend.start("u1"))


def test_ads_list_profiles():
    async def handler(request):
        assert request.url.path == "/api/v1/user/list"
        return httpx.Response(200, json={"code": 0, "data": {"list": [
            {"user_id": "u1", "name": "一号", "group_id": "g1"},
            {"user_id": "u2", "name": "二号", "group_id": "g1"},
        ]}})

    backend = AdsPowerBackend(client=_client(handler))
    profiles = _run(backend.list_profiles())
    assert [(p.id, p.name) for p in profiles] == [("u1", "一号"), ("u2", "二号")]


# ---------------- ovoBrowser
def test_ovo_start_uses_debug_port():
    seen = {}

    async def handler(request):
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(200, json={"success": True, "data": {
            "profileId": "abc", "status": "running", "debugPort": 6123,
        }})

    backend = OvoBrowserBackend(client=_client(handler))
    assert _run(backend.start("abc")) == "http://127.0.0.1:6123"
    assert seen["method"] == "POST"
    assert b'"abc"' in seen["body"]


def test_ovo_failure_uses_msg():
    async def handler(request):
        return httpx.Response(200, json={"success": False, "msg": "profile not found"})

    backend = OvoBrowserBackend(client=_client(handler))
    with pytest.raises(FingerprintError, match="profile not found"):
        _run(backend.start("nope"))


def test_ovo_list_profiles():
    async def handler(request):
        return httpx.Response(200, json={"success": True, "data": {"list": [
            {"id": "p1", "name": "DeepSeek 主号", "groupName": "问答"},
        ]}})

    backend = OvoBrowserBackend(client=_client(handler))
    profiles = _run(backend.list_profiles())
    assert [(p.id, p.name, p.group) for p in profiles] == [("p1", "DeepSeek 主号", "问答")]


def test_connection_refused_mentions_setup():
    async def handler(request):
        raise httpx.ConnectError("refused", request=request)

    backend = OvoBrowserBackend(client=_client(handler))
    with pytest.raises(FingerprintError, match="本地 API"):
        _run(backend.start("p1"))


# ---------------- 管理器接线（假 Playwright，不启浏览器）
class _FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.goto_calls = []

    async def goto(self, url):
        self.goto_calls.append(url)
        self.url = url


class _FakeCtx:
    def __init__(self):
        self.pages = [_FakePage()]

    async def new_page(self):
        p = _FakePage()
        self.pages.append(p)
        return p


class _FakeBrowser:
    def __init__(self):
        self.contexts = [_FakeCtx()]

    def is_connected(self):
        return True


class _FakeChromium:
    def __init__(self):
        self.endpoints = []

    async def connect_over_cdp(self, endpoint):
        self.endpoints.append(endpoint)
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakeBackend:
    def __init__(self):
        self.started = []
        self.closed = False

    async def start(self, profile_id):
        self.started.append(profile_id)
        return "http://127.0.0.1:7777"

    async def aclose(self):
        self.closed = True


def test_manager_routes_to_fingerprint_backend():
    cfg = {
        "platforms": {"deepseek": {"port": 9301, "accounts": ["main"]}},
        "profiles_dir": "profiles",
        "fingerprint": {"backend": "ovobrowser", "profiles": {"deepseek": {"main": "env-1"}}},
    }
    mgr = ChromeManager(cfg)
    fake_backend = _FakeBackend()
    fake_pw = _FakePlaywright()
    mgr.backend = fake_backend
    mgr._pw = fake_pw

    page = _run(mgr.page("deepseek", "https://chat.deepseek.com/", "main"))
    assert fake_backend.started == ["env-1"]
    assert fake_pw.chromium.endpoints == ["http://127.0.0.1:7777"]
    assert page.goto_calls == ["https://chat.deepseek.com/"]

    _run(mgr.close())
    assert fake_backend.closed is True
