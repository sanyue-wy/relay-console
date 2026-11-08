"""指纹浏览器后端：把每个平台账号交给本机已装的指纹浏览器（AdsPower / ovoBrowser）承载。

分工：
  - 指纹层（UA、时区、语言、屏幕、Canvas/WebGL、字体、WebRTC、硬件参数…）由指纹浏览器
    的“环境”负责，本工程不自己实现、也不伪造这些维度；
  - 本工程只调用它们的本地 API 打开指定环境，拿到 CDP 调试端点后用 Playwright 接管页面。

因此多账号隔离从“数据层”（独立 profile）升级为“环境层”：每个账号一个指纹环境，环境之间
的浏览器指纹互不相同。前提是本机已安装对应客户端并在设置里开启本地 API。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASES = {
    "adspower": "http://127.0.0.1:50325",
    # ovoBrowser 的本地 API 默认也用 50325（与 AdsPower 兼容，路径别名一致），
    # 实际端口以客户端 设置 → 本地 API 里显示的为准，可用 fingerprint.api_base 覆盖。
    "ovobrowser": "http://127.0.0.1:50325",
}

BACKEND_TITLES = {
    "adspower": "AdsPower",
    "ovobrowser": "ovoBrowser",
}


class FingerprintError(RuntimeError):
    """指纹浏览器本地 API 调用失败；消息面向用户，含下一步该做什么。"""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str = ""
    group: str = ""


def resolve_profile_id(profile_map: dict[str, Any], pid: str, account: str) -> str:
    """取某平台某账号对应的指纹环境 ID；缺失时报出要补的配置项。"""
    per_platform = profile_map.get(pid) or {}
    value = per_platform.get(account)
    if not value:
        raise FingerprintError(
            f"fingerprint.profiles 未配置 {pid}.{account} 的环境 ID。"
            f"请先在指纹浏览器里建好该账号的环境，再把环境 ID 填进 config.yaml"
            f"（可用 python scripts/fingerprint_profiles.py 列出已有环境）。"
        )
    return str(value)


class AntidetectBackend:
    """本地 API 客户端基类；子类只需实现 start/stop/list_profiles 的协议差异。"""

    name = ""
    default_base = ""

    def __init__(self, api_base: str | None = None, api_key: str | None = None,
                 timeout_seconds: float = 90, client: httpx.AsyncClient | None = None):
        self.api_base = (api_base or self.default_base).rstrip("/")
        self.api_key = api_key or ""
        self.timeout_seconds = float(timeout_seconds)
        self._client = client
        self._own_client = client is None

    # ---- HTTP 基础设施
    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
                follow_redirects=False,
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {}

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        client = self._client_or_create()
        url = self.api_base + path
        try:
            resp = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise FingerprintError(
                f"无法连接 {BACKEND_TITLES.get(self.name, self.name)} 本地 API（{self.api_base}）："
                f"请确认客户端已启动，并在设置里开启本地 API。原始错误：{exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise FingerprintError(
                f"{BACKEND_TITLES.get(self.name, self.name)} 本地 API 超时（{self.timeout_seconds:.0f}s）："
                f"{method} {path}。环境启动较慢时可调大 fingerprint.startup_timeout_seconds。"
            ) from exc
        if resp.status_code >= 400:
            snippet = resp.text[:300].replace("\n", " ")
            raise FingerprintError(
                f"{BACKEND_TITLES.get(self.name, self.name)} 本地 API 返回 HTTP {resp.status_code}："
                f"{method} {path} → {snippet}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise FingerprintError(
                f"{BACKEND_TITLES.get(self.name, self.name)} 本地 API 返回的不是 JSON："
                f"{method} {path} → {resp.text[:300]}"
            ) from exc

    async def aclose(self) -> None:
        if self._own_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 协议
    async def start(self, profile_id: str) -> str:
        """打开环境并返回 Playwright connect_over_cdp 可用的端点。"""
        raise NotImplementedError

    async def stop(self, profile_id: str) -> None:
        raise NotImplementedError

    async def list_profiles(self) -> list[Profile]:
        raise NotImplementedError

    @staticmethod
    def _endpoint(data: dict[str, Any]) -> str | None:
        ws = data.get("ws")
        if isinstance(ws, dict):
            ws = ws.get("puppeteer") or ws.get("chrome")
        if isinstance(ws, str) and ws.startswith(("ws://", "wss://")):
            return ws
        port = data.get("debug_port") or data.get("debugPort")
        try:
            if port:
                return f"http://127.0.0.1:{int(port)}"
        except (TypeError, ValueError):
            pass
        return None


class AdsPowerBackend(AntidetectBackend):
    """AdsPower Local API（默认 127.0.0.1:50325，需在 设置 → API 开启并填 api-key）。"""

    name = "adspower"
    default_base = DEFAULT_BASES["adspower"]

    def __init__(self, api_base: str | None = None, api_key: str | None = None, **kwargs):
        api_key = api_key or os.environ.get("ADSPOWER_API_KEY", "")
        super().__init__(api_base=api_base, api_key=api_key, **kwargs)

    def _headers(self) -> dict[str, str]:
        # AdsPower 的本地 API 安全开关要求 api-key 头；未开启安全开关时多一个头也无妨。
        return {"api-key": self.api_key} if self.api_key else {}

    async def start(self, profile_id: str) -> str:
        body = await self._request(
            "GET", "/api/v1/browser/start", params={"user_id": profile_id}
        )
        if body.get("code") == 0:
            data = body.get("data") or {}
            endpoint = self._endpoint(data)
            if endpoint:
                return endpoint
            raise FingerprintError(
                f"AdsPower 已启动环境 {profile_id}，但响应里没有 ws/debug_port"
                f"（code={body.get('code')}, msg={body.get('msg')}）"
            )
        # 常见于“环境已在运行”：查一次 active，能拿到端点就继续
        active = await self._request(
            "GET", "/api/v1/browser/active", params={"user_id": profile_id}
        )
        data = active.get("data") or {}
        if data.get("status") == "Active":
            endpoint = self._endpoint(data)
            if endpoint:
                return endpoint
        raise FingerprintError(
            f"AdsPower 启动环境 {profile_id} 失败：{body.get('msg') or body.get('code')}"
        )

    async def stop(self, profile_id: str) -> None:
        await self._request("GET", "/api/v1/browser/stop", params={"user_id": profile_id})

    async def list_profiles(self) -> list[Profile]:
        body = await self._request(
            "GET", "/api/v1/user/list", params={"page": 1, "page_size": 100}
        )
        rows = (body.get("data") or {}).get("list") or []
        return [
            Profile(
                id=str(row.get("user_id") or ""),
                name=str(row.get("name") or ""),
                group=str(row.get("group_id") or ""),
            )
            for row in rows
            if row.get("user_id")
        ]


class OvoBrowserBackend(AntidetectBackend):
    """ovoBrowser Local API（默认 127.0.0.1:3001，需在客户端设置里开启本地 API）。"""

    name = "ovobrowser"
    default_base = DEFAULT_BASES["ovobrowser"]

    async def start(self, profile_id: str) -> str:
        body = await self._request("POST", "/browser/open", json={"id": profile_id})
        if body.get("success") is not True:
            msg = body.get("msg") or body.get("message") or body
            raise FingerprintError(f"ovoBrowser 启动环境 {profile_id} 失败：{msg}")
        data = body.get("data") or {}
        endpoint = self._endpoint(data)
        if not endpoint:
            raise FingerprintError(
                f"ovoBrowser 已启动环境 {profile_id}，但响应里没有 debugPort"
                f"（status={data.get('status')}）"
            )
        return endpoint

    async def stop(self, profile_id: str) -> None:
        await self._request("POST", "/browser/close", json={"id": profile_id})

    async def list_profiles(self) -> list[Profile]:
        body = await self._request("POST", "/browser/list", json={"page": 0, "pageSize": 100})
        if body.get("success") is not True:
            raise FingerprintError(
                f"ovoBrowser 环境列表获取失败：{body.get('msg') or body.get('message') or body}"
            )
        rows = (body.get("data") or {}).get("list") or []
        return [
            Profile(
                id=str(row.get("id") or row.get("profileId") or ""),
                name=str(row.get("name") or ""),
                group=str(row.get("groupName") or ""),
            )
            for row in rows
            if row.get("id") or row.get("profileId")
        ]


_BACKENDS = {
    AdsPowerBackend.name: AdsPowerBackend,
    OvoBrowserBackend.name: OvoBrowserBackend,
}


def build_backend(fp_cfg: dict[str, Any] | None) -> AntidetectBackend:
    """按 config.yaml 的 fingerprint 段构造后端；backend=chrome 时调用方不应走到这里。"""
    fp_cfg = fp_cfg or {}
    name = str(fp_cfg.get("backend") or "").strip().lower()
    if name not in _BACKENDS:
        supported = ", ".join(["chrome", *_BACKENDS])
        raise FingerprintError(f"未知的 fingerprint.backend={name!r}，可选：{supported}")
    cls = _BACKENDS[name]
    return cls(
        api_base=fp_cfg.get("api_base") or None,
        api_key=fp_cfg.get("api_key") or None,
        timeout_seconds=float(fp_cfg.get("startup_timeout_seconds") or 90),
    )
