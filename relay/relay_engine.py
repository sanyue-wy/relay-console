"""接力引擎：发问 → 等回复 → 命中上限则用本会话摘要接力（优先同平台换账号，再换平台），并重发这一问。"""
from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import adapters
from adapters.spec import AdapterError, Cap, LimitReached, LimitState, LimitStatus, Participant, Turn

from .browser import ChromeManager
from .config import platform_accounts
from .storage import MainPool
from .summarizer import Summarizer


class RelayPolicy:
    """可替换的平台级路由策略：默认按配置顺序，跳过冷却中和本次已失败的平台。"""

    def __init__(self, order: list[str]):
        self.order = order

    def next(self, current: str, cooldowns: dict[str, datetime], tried: set[str]) -> str | None:
        now = datetime.now()
        start = self.order.index(current) + 1 if current in self.order else 0
        for pid in self.order[start:] + self.order[:start]:
            if pid in tried or pid == current:
                continue
            if cooldowns.get(pid) and cooldowns[pid] > now:
                continue
            return pid
        return None


class RelayEngine:
    def __init__(self, cfg, main: MainPool, browser: ChromeManager, summarizer: Summarizer,
                 policy: RelayPolicy | None = None):
        self.cfg, self.main, self.browser, self.summ = cfg, main, browser, summarizer
        self.policy = policy or RelayPolicy(cfg["relay_order"])
        self.cooldowns: dict[tuple[str, str], datetime] = {}     # 键 = (平台, 账号)
        self._adapters: dict[tuple[str, str], Participant] = {}

    def accounts(self, pid: str) -> list[str]:
        return platform_accounts(self.cfg, pid)

    async def adapter(self, pid: str, account: str = "main") -> Participant:
        key = (pid, account)
        ad = self._adapters.get(key)
        if ad is None:
            url = self.cfg["platforms"].get(pid, {}).get("url")
            ad = adapters.create(pid, page_provider=self.browser.page, url=url, account=account)
            self._adapters[key] = ad
        await ad.start()
        return ad

    def platform_status(self) -> list[dict]:
        now = datetime.now()
        out = []
        for pid in adapters.available():
            accounts = []
            for a in self.accounts(pid):
                cd = self.cooldowns.get((pid, a))
                accounts.append({"account": a,
                                 "cooldown_until": cd.isoformat() if cd and cd > now else None})
            out.append({"id": pid, "accounts": accounts})
        return out

    def _cool(self, key: tuple[str, str], reset_at: datetime | None) -> None:
        self.cooldowns[key] = reset_at or datetime.now() + timedelta(
            minutes=self.cfg.get("default_cooldown_minutes", 180))

    def _cooling(self, key: tuple[str, str]) -> bool:
        cd = self.cooldowns.get(key)
        return bool(cd and cd > datetime.now())

    def _next_target(self, cur_pid: str, cur_acct: str,
                     tried: set[tuple[str, str]]) -> tuple[str, str] | None:
        """接力目标：优先同平台下一个可用账号，再按策略换平台。"""
        tried = tried | {(cur_pid, cur_acct)}
        for a in self.accounts(cur_pid):
            if (cur_pid, a) not in tried and not self._cooling((cur_pid, a)):
                return cur_pid, a
        seen = {cur_pid}
        while True:
            tried_pids = {p for p, _a in tried} | seen
            cool_pids = {p: cd for (p, _a), cd in self.cooldowns.items() if self._cooling((p, _a))}
            nxt = self.policy.next(cur_pid, cool_pids, tried_pids)
            if not nxt or nxt in seen:
                return None
            seen.add(nxt)
            for a in self.accounts(nxt):
                if (nxt, a) not in tried and not self._cooling((nxt, a)):
                    return nxt, a

    async def _deliver_handoff(self, ad: Participant, package: str, question: str) -> str:
        """把交接包 + 问题送进新平台的新会话。过长且支持上传时走 .md 文件。"""
        await ad.new_chat()
        combined = f"{package}\n\n## 现在请回答\n{question}"
        limit = ad.info.max_input_chars or 10**9
        if len(combined) <= limit:
            return combined
        if Cap.FILE_UPLOAD in ad.info.caps:
            path = Path(tempfile.gettempdir()) / f"handoff_{int(time.time())}.md"
            path.write_text(package, encoding="utf-8")
            try:
                if await ad.upload_file(str(path)):
                    return f"附件是我们之前对话的交接摘要，请据此继续，不要复述。\n\n{question}"
            finally:
                path.unlink(missing_ok=True)
        return combined[-limit:]

    async def ask(self, sid: str, text: str, timeout: float = 180) -> dict:
        s = self.main.get_session(sid)
        self.main.add_message(sid, "user", s.current_platform, text)
        platform, account = s.current_platform, s.account
        tried: set[tuple[str, str]] = set()
        relays = []
        outgoing = text
        while True:
            ad = await self.adapter(platform, account)
            lock = getattr(ad, "lock", None) or asyncio.Lock()
            try:
                async with lock:
                    s = self.main.get_session(sid)
                    if outgoing is text:          # 不是交接首条消息时，先回到本会话
                        if s.chat_ref and Cap.RESUME_URL in ad.info.caps:
                            await ad.resume_chat(s.chat_ref)
                        elif not s.chat_ref:
                            await ad.new_chat()
                    await ad.send(Turn("user", outgoing, "user"))
                    reply = await ad.receive(timeout)
                    if not reply.finished:
                        st = await ad.limit_state()
                        if st.status == LimitStatus.EXHAUSTED:
                            raise LimitReached(st)
                        if self.cfg.get("relay_on_truncation"):
                            # 被截断且页面/网络都没有报警：可选地直接接力
                            raise LimitReached(LimitState(LimitStatus.NEAR, None, "behavior",
                                                          "回复被截断（relay_on_truncation 开启）"))
                self.main.add_message(sid, "assistant", platform, reply.text)
                self.main.set_chat_ref(sid, reply.chat_ref)
                asyncio.create_task(self.summ.maybe_update(sid))
                return {"platform": platform, "account": account, "text": reply.text,
                        "finished": reply.finished, "relays": relays}
            except LimitReached as e:
                tried.add((platform, account))
                self._cool((platform, account), e.state.reset_at)
                nxt = self._next_target(platform, account, tried)
                if not nxt:
                    raise AdapterError("所有平台与账号都在冷却或不可用") from e
                nplat, nacct = nxt
                package = await self.summ.handoff_package(sid)
                self.main.switch_platform(sid, nplat, f"{e.state.signal}: {e.state.detail}",
                                          new_account=nacct)
                relays.append({"from": f"{platform}:{account}", "to": f"{nplat}:{nacct}",
                               "reason": e.state.detail})
                platform, account = nplat, nacct
                nad = await self.adapter(nplat, nacct)
                async with getattr(nad, "lock", asyncio.Lock()):
                    outgoing = await self._deliver_handoff(nad, package, text)
