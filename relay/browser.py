"""浏览器层：每个平台每个账号一个独立浏览器环境，Playwright 通过 CDP 接管页面。

两种承载方式（config.yaml 的 fingerprint.backend）：
  - chrome（默认）：本工程自己拉起真实 Chrome，登录态存在 profiles/<平台>/<账号>；
  - adspower / ovobrowser：交给本机已装的指纹浏览器，环境级指纹由它负责，本工程只通过
    它的本地 API 打开环境、再用 CDP 连接（见 relay/fingerprint.py）。

首次打开时你在窗口里手动登录（或开启 autofill 后用凭据库代填），程序不接触、不记录密码。
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, Page, Playwright, async_playwright

from adapters.spec import AdapterError, ChromeNotFound, PortNotReady, ProfileInUse

from .config import platform_accounts
from .fingerprint import FingerprintError, build_backend, resolve_profile_id


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
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._start_lock = asyncio.Lock()      # 只保护 Playwright 的首次启动
        fp_cfg = cfg.get("fingerprint") or {}
        self.fp_cfg = dict(fp_cfg)
        self.backend_name = str(fp_cfg.get("backend") or "chrome").strip().lower()
        self.backend = None if self.backend_name == "chrome" else build_backend(fp_cfg)
        self.profile_map = dict(fp_cfg.get("profiles") or {})
        self._validate_ports()

    def _validate_ports(self) -> None:
        """启动时全局校验端口：base + 10*i 仍可能被手工配置成相撞（如 9301 与 9311）。"""
        seen: dict[int, tuple[str, str]] = {}
        conflicts = []
        for pid in self.cfg.get("platforms", {}):
            for account in platform_accounts(self.cfg, pid):
                port = self._port(pid, account)
                if port in seen:
                    conflicts.append((seen[port], (pid, account), port))
                else:
                    seen[port] = (pid, account)
        if conflicts:
            detail = "；".join(
                f"{a[0]}:{a[1]} 与 {b[0]}:{b[1]} 都使用端口 {port}"
                for a, b, port in conflicts)
            raise ValueError(f"平台端口配置冲突（每个账号需要独立调试端口）：{detail}")

    def _port(self, pid: str, account: str | None = None) -> int:
        """每平台预留 10 个连续端口（base+10*i）。

        旧算法 base+index 会让 deepseek 的第二个账号落到 doubao 的 base 上，
        而 _port_open 看到端口占用就直接复用，等于连到别的平台的浏览器。
        """
        base = int(self.cfg["platforms"][pid]["port"])
        accounts = platform_accounts(self.cfg, pid)
        idx = accounts.index(account) if account in accounts else 0
        if idx >= 10:
            raise RuntimeError(f"{pid} 最多支持 10 个账号（端口段 base+0..base+90）")
        return base + 10 * idx

    def launch(self, pid: str, url: str, account: str | None = None) -> None:
        account = account or "main"
        chrome = Path(str(self.cfg.get("chrome_path") or ""))
        if not chrome.exists():
            raise ChromeNotFound(
                f"找不到 Chrome：{chrome}。请到设置页把 chrome_path 改成实际安装路径，"
                f"或安装 Chrome 后重启控制台。")
        port = self._port(pid, account)
        if _port_open(port):
            return
        profile = Path(self.cfg["profiles_dir"]) / pid / account
        profile.mkdir(parents=True, exist_ok=True)
        self._procs[(pid, account)] = subprocess.Popen([
            str(chrome),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            url,
        ])

    async def _chrome_endpoint(self, pid: str, url: str, account: str) -> str:
        self.launch(pid, url, account)
        port = self._port(pid, account)
        proc = self._procs.get((pid, account))
        for _ in range(60):
            if _port_open(port):
                return f"http://127.0.0.1:{port}"
            if proc is not None and proc.poll() is not None:
                raise ProfileInUse(
                    f"{pid}:{account} 的 Chrome 已退出（调试端口 {port} 未打开）。"
                    f"常见原因：同一个 profile 已被另一个 Chrome 占用；"
                    f"请先关闭那个窗口再重试。")
            await asyncio.sleep(0.5)
        raise PortNotReady(f"{pid}:{account} 的 Chrome 调试端口 {port} 等待 30 秒仍未就绪")

    async def _pick_page(self, br: Browser, url: str) -> Page:
        ctx = br.contexts[0] if br.contexts else await br.new_context()
        host = urlparse(url).netloc
        for p in ctx.pages:
            if urlparse(p.url).netloc == host:
                return p
        p = (ctx.pages[0] if ctx.pages and ctx.pages[0].url in ("about:blank", "chrome://newtab/")
             else await ctx.new_page())
        await p.goto(url)
        return p

    async def page(self, pid: str, url: str, account: str | None = None) -> Page:
        account = account or "main"
        key = (pid, account)
        lock = self._locks.setdefault(key, asyncio.Lock())   # 每个平台账号一把锁，互不阻塞
        async with lock:
            if self._pw is None:
                async with self._start_lock:
                    if self._pw is None:
                        self._pw = await async_playwright().start()
            if self.backend is None:
                endpoint = await self._chrome_endpoint(pid, url, account)
            else:
                profile_id = resolve_profile_id(self.profile_map, pid, account)
                try:
                    endpoint = await self.backend.start(profile_id)
                except FingerprintError as exc:
                    raise AdapterError(
                        f"连接 {self.backend_name} 本地 API 失败（{pid}:{account}）：{exc}。"
                        f"请先启动 AdsPower/ovoBrowser 客户端并在设置里开启本地 API。") from exc
            br = self._browsers.get(key)
            if br is None or not br.is_connected():
                try:
                    br = await self._pw.chromium.connect_over_cdp(endpoint)
                except Exception as exc:
                    raise AdapterError(
                        f"连接 {pid}:{account} 的浏览器失败（{endpoint}）：{exc}") from exc
                self._browsers[key] = br
            return await self._pick_page(br, url)

    async def close(self) -> None:
        for br in self._browsers.values():
            try:
                await br.close()   # 只断开 CDP，不关闭用户的浏览器
            except Exception:
                pass
        self._browsers.clear()
        if self.backend is not None:
            try:
                await self.backend.aclose()
            except Exception:
                pass
        if self._pw:
            await self._pw.stop()
            self._pw = None
