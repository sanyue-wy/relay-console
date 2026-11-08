"""浏览器层：每个平台每个账号一个真实 Chrome + 独立 profile，Playwright 通过 CDP 连接。

首次打开时你在窗口里手动登录（或开启 autofill 后用凭据库代填），登录态保存在
profiles/<平台>/<账号>，程序不接触、不记录密码。
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, Playwright, async_playwright

from .config import platform_accounts


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ChromeManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self._pw: Playwright | None = None
        self._browsers: dict[tuple[str, str], Browser] = {}
        self._procs: dict[tuple[str, str], subprocess.Popen] = {}
        self._lock = asyncio.Lock()

    def _port(self, pid: str, account: str | None = None) -> int:
        base = int(self.cfg["platforms"][pid]["port"])
        accounts = platform_accounts(self.cfg, pid)
        return base + accounts.index(account) if account in accounts else base

    def launch(self, pid: str, url: str, account: str | None = None) -> None:
        account = account or "main"
        port = self._port(pid, account)
        if _port_open(port):
            return
        profile = Path(self.cfg["profiles_dir"]) / pid / account
        profile.mkdir(parents=True, exist_ok=True)
        self._procs[(pid, account)] = subprocess.Popen([
            self.cfg["chrome_path"],
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            url,
        ])

    async def page(self, pid: str, url: str, account: str | None = None) -> Page:
        account = account or "main"
        async with self._lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
            self.launch(pid, url, account)
            port = self._port(pid, account)
            for _ in range(60):
                if _port_open(port):
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError(f"{pid}:{account} 的 Chrome 调试端口 {port} 未就绪")
            br = self._browsers.get((pid, account))
            if br is None or not br.is_connected():
                br = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                self._browsers[(pid, account)] = br
            ctx = br.contexts[0] if br.contexts else await br.new_context()
            host = urlparse(url).netloc
            for p in ctx.pages:
                if urlparse(p.url).netloc == host:
                    return p
            p = ctx.pages[0] if ctx.pages and ctx.pages[0].url in ("about:blank", "chrome://newtab/") else await ctx.new_page()
            await p.goto(url)
            return p

    async def close(self) -> None:
        for br in self._browsers.values():
            try:
                await br.close()   # 只断开 CDP，不关闭用户的 Chrome
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()
