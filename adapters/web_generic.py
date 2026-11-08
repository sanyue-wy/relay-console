"""通用网页适配器：全部行为由 SelectorSet 驱动，平台子类只在必要时覆盖钩子。"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin, urlparse

from .spec import (AdapterInfo, LimitReached, LimitState, LimitStatus, NotReady, Reply, SelectorBroken,
                   SelectorSet, Turn, WebAdapter)

STABLE_SECONDS = 2.5   # 回复文本连续这么久不变，且停止按钮消失，视为生成结束
POLL = 0.5


class GenericWebAdapter(WebAdapter):
    def __init__(self, info: AdapterInfo, selectors: SelectorSet, page_provider,
                 account: str | None = None):
        super().__init__(info, selectors, page_provider, account=account)
        self.page = None
        self.current_chat: str | None = None
        self._baseline = 0
        self._net_hits: list[tuple[float, int]] = []
        self.lock = asyncio.Lock()          # 同一平台页面同一时刻只服务一个会话

    # ---------------- 生命周期
    async def start(self) -> None:
        if self.page and not self.page.is_closed():
            return
        self.page = await self._page_provider(self.info.id, self.sel.url, self.account)
        self.page.on("response", self._on_response)

    async def stop(self) -> None:
        self.page = None

    async def health(self) -> bool:
        try:
            await self.start()
            return await self.page.locator(self.sel.input).first.is_visible(timeout=3000)
        except Exception:
            return False

    def _on_response(self, resp) -> None:
        if resp.status not in self.sel.limit_status_codes:
            return
        target = urlparse(self.sel.url).hostname or ""
        host = urlparse(resp.url).hostname or ""
        # 精确主机或子域：endswith("deepseek.com") 会误匹配 evildeepseek.com
        if host == target or host.endswith("." + target):
            self._net_hits.append((time.time(), resp.status))

    async def _wait_input(self, timeout: float = 20) -> None:
        try:
            await self.page.locator(self.sel.input).first.wait_for(state="visible", timeout=timeout * 1000)
        except Exception as e:
            raise NotReady(f"{self.info.id}: 找不到输入框，可能未登录或选择器失效（{self.sel.input}）") from e

    # ---------------- 会话
    async def new_chat(self, preamble: str | None = None) -> str:
        await self.start()
        clicked = False
        if self.sel.new_chat:
            btn = self.page.locator(self.sel.new_chat).first
            try:
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    clicked = True
            except Exception:
                pass
        if not clicked:
            await self.page.goto(self.sel.url)
        await self._wait_input()
        self.current_chat = None
        return self.page.url

    async def resume_chat(self, chat_ref: str) -> None:
        await self.start()
        if self.page.url != chat_ref:
            await self.page.goto(chat_ref)
        await self._wait_input()
        self.current_chat = chat_ref

    # ---------------- 收发
    async def _reply_count(self) -> int:
        return await self.page.locator(self.sel.last_reply).count()

    async def _extract_last_reply(self) -> str:
        loc = self.page.locator(self.sel.last_reply)
        n = await loc.count()
        return (await loc.nth(n - 1).inner_text()).strip() if n else ""

    async def _generating(self) -> bool:
        if not self.sel.stop_button:
            return False
        try:
            return await self.page.locator(self.sel.stop_button).first.is_visible(timeout=200)
        except Exception:
            return False

    async def send(self, turn: Turn) -> None:
        await self.start()
        await self._wait_input()
        st = await self._limit_from_dom()
        if st:
            raise LimitReached(st)
        self._baseline = await self._reply_count()
        box = self.page.locator(self.sel.input).first
        await box.click()
        try:
            await box.fill(turn.content)
        except Exception:
            await self.page.keyboard.insert_text(turn.content)
        await asyncio.sleep(0.3)
        if self.sel.send_button:
            btn = self.page.locator(self.sel.send_button).first
            try:
                await btn.click(timeout=5000)
            except Exception:
                await box.press("Enter")
        else:
            await box.press("Enter")

    async def receive(self, timeout: float = 180) -> Reply:
        t0 = time.time()
        last_text, last_change = "", time.time()
        started = False
        while time.time() - t0 < timeout:
            st = await self._limit_from_dom() or self._limit_from_net()
            if st:
                raise LimitReached(st)
            if not started:
                started = await self._reply_count() > self._baseline
                await asyncio.sleep(POLL)
                continue
            text = await self._extract_last_reply()
            if text != last_text:
                last_text, last_change = text, time.time()
            elif text and time.time() - last_change >= STABLE_SECONDS and not await self._generating():
                self.current_chat = self.page.url
                return Reply(text, True, int((time.time() - t0) * 1000), self.page.url)
            await asyncio.sleep(POLL)
        if not started:
            raise SelectorBroken(f"{self.info.id}: {timeout}s 内没有出现新回复，检查 last_reply 选择器")
        self.current_chat = self.page.url
        return Reply(last_text, False, int((time.time() - t0) * 1000), self.page.url)

    # ---------------- 上限检测
    async def _limit_from_dom(self) -> LimitState | None:
        if not self.sel.limit_texts:
            return None
        for t in self.sel.limit_texts:
            try:
                if await self.page.get_by_text(t, exact=False).first.is_visible(timeout=150):
                    return LimitState(LimitStatus.EXHAUSTED, None, "dom", t)
            except Exception:
                continue
        return None

    def _limit_from_net(self) -> LimitState | None:
        now = time.time()
        self._net_hits = [h for h in self._net_hits if now - h[0] < 60]
        if self._net_hits:
            return LimitState(LimitStatus.EXHAUSTED, None, "network", f"HTTP {self._net_hits[-1][1]}")
        return None

    async def limit_state(self) -> LimitState:
        await self.start()
        return await self._limit_from_dom() or self._limit_from_net() or LimitState(LimitStatus.OK)

    async def upload_file(self, path: str) -> bool:
        if not self.sel.upload_input:
            return False
        await self.page.locator(self.sel.upload_input).first.set_input_files(path)
        await asyncio.sleep(2)
        return True

    # ---------------- 历史会话发现（P1.2）
    async def list_chats(self, limit: int = 30) -> list[dict]:
        """从侧栏解析历史会话 [{title, url}]。选择器未配置或解析失败返回 []，不抛错。"""
        if not self.sel.chat_list_item:
            return []
        await self.start()
        out: list[dict] = []
        try:
            loc = self.page.locator(self.sel.chat_list_item)
            n = min(await loc.count(), limit)
            for i in range(n):
                el = loc.nth(i)
                try:
                    title = (await el.inner_text()).strip().splitlines()[0] if await el.count() else ""
                except Exception:
                    title = ""
                try:
                    href = await el.get_attribute("href")
                except Exception:
                    href = None
                url = urljoin(self.sel.url, href) if href else None
                if title or url:
                    out.append({"title": title or url, "url": url or self.page.url})
        except Exception:
            return []
        return out

    # ---------------- 凭据代填（P3.8，只由控制台侧调用；凭据不进日志）
    async def fill_login(self, username: str, password: str) -> bool:
        """把账号密码填进本平台登录表单并提交。页面上找不到登录表单返回 False。"""
        await self.start()
        pass_loc = self.page.locator(self.sel.login_pass or "input[type='password']").first
        try:
            if not await pass_loc.is_visible(timeout=2500):
                return False
            user_loc = self.page.locator(self.sel.login_user or
                                         "input[type='text'], input[type='tel'], input[autocomplete*='username']").first
            try:
                if await user_loc.is_visible(timeout=1000):
                    await user_loc.fill(username)
            except Exception:
                pass
            await pass_loc.fill(password)
            if self.sel.login_submit:
                try:
                    await self.page.locator(self.sel.login_submit).first.click(timeout=3000)
                except Exception:
                    await pass_loc.press("Enter")
            else:
                await pass_loc.press("Enter")
            return True
        except Exception:
            return False
