"""Relay Console MCP Server（stdio）。

本进程不读任何本地数据文件，只持有一个 Agent 令牌，调用控制台的 /agent/* 接口。
每次读取内容都会在控制台弹出授权，由用户批准后才能取到一次。

环境变量：
  RELAY_AGENT_TOKEN   控制台「新建 Agent」时显示的令牌（必填）
  RELAY_CONSOLE_URL   默认 http://127.0.0.1:8765
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

README = (Path(__file__).with_name("AGENT_README.md")).read_text(encoding="utf-8")

_BASE_RAW = os.environ.get("RELAY_CONSOLE_URL", "http://127.0.0.1:8765")


def _validated_base(url: str) -> str:
    """控制台只应跑在本机；MCP 进程只允许连回环地址，防止被配置指向内网/外部服务（SSRF）。"""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise SystemExit(f"RELAY_CONSOLE_URL 必须是 http(s) 地址：{url!r}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"出于安全考虑 RELAY_CONSOLE_URL 只允许本机回环地址，收到：{host}")
    return url.rstrip("/")


BASE = _validated_base(_BASE_RAW)
TOKEN = os.environ.get("RELAY_AGENT_TOKEN", "")

mcp = FastMCP("relay-console", instructions=README)


def _call(method: str, path: str, body: dict | None = None):
    if not TOKEN:
        return {"error": "NO_TOKEN", "detail": "未配置 RELAY_AGENT_TOKEN"}
    try:
        r = httpx.request(method, BASE + path, json=body, timeout=15,
                          headers={"Authorization": f"Bearer {TOKEN}"})
    except httpx.HTTPError as e:
        return {"error": "CONSOLE_UNREACHABLE", "detail": str(e)}
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text}
    if r.status_code >= 400:
        return {"error": data.get("error", f"HTTP_{r.status_code}"), "detail": data.get("detail", "")}
    return data


@mcp.tool()
def list_pool() -> list | dict:
    """列出对抗池中你可见的条目（只有 ID 和用户写的标题，不含内容）。"""
    return _call("GET", "/agent/pool")


@mcp.tool()
def request_read(item_ids: list[str], reason: str) -> dict:
    """申请读取指定条目。reason 必填，用户会在控制台看到并决定是否批准。"""
    return _call("POST", "/agent/requests", {"scope_type": "items", "item_ids": item_ids, "reason": reason})


@mcp.tool()
def get_turn(arena_id: str, reason: str) -> dict:
    """申请读取对局中轮到你的题目（议题 + 对方公开发言）。同样需要用户批准。"""
    return _call("POST", "/agent/requests", {"scope_type": "turn", "arena_id": arena_id, "reason": reason})


@mcp.tool()
def check_request(request_id: str) -> dict:
    """查询申请状态：pending | approved | denied | expired。"""
    return _call("GET", f"/agent/requests/{request_id}")


@mcp.tool()
def fetch_approved(request_id: str) -> dict:
    """取回已批准的内容。票据一次性，批准后 5 分钟内有效。"""
    return _call("POST", f"/agent/requests/{request_id}/fetch")


@mcp.tool()
def submit_turn(arena_id: str, content: str) -> dict:
    """提交你本轮的发言。必须先经 get_turn → 批准 → fetch_approved 读到题目。"""
    return _call("POST", f"/agent/arenas/{arena_id}/submit", {"content": content})


if __name__ == "__main__":
    mcp.run()
