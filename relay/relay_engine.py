"""接力引擎：发问 → 等回复 → 命中上限则用本会话摘要接力到下一个平台，并重发这一问。"""
from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import adapters
from adapters.spec import AdapterError, Cap, LimitReached, LimitStatus, Participant, Turn

from .browser import ChromeManager
from .storage import MainPool
from .summarizer import Summarizer


class RelayPolicy:
    """可替换的路由策略：默认按配置顺序，跳过冷却中和本次已失败的平台。"""

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
        self.cooldowns: dict[str, datetime] = {}
        self._adapters: dict[str, Participant] = {}

    async def adapter(self, pid: str) -> Participant:
        ad = self._adapters.get(pid)
        if ad is None:
            url = self.cfg["platforms"].get(pid, {}).get("url")
            ad = adapters.create(pid, page_provider=self.browser.page, url=url)
            self._adapters[pid] = ad
        await ad.start()
        return ad

    def platform_status(self) -> list[dict]:
        out = []
        for pid in adapters.available():
            cd = self.cooldowns.get(pid)
            out.append({"id": pid, "cooldown_until": cd.isoformat() if cd and cd > datetime.now() else None})
        return out

    def _cool(self, pid: str, reset_at: datetime | None) -> None:
        self.cooldowns[pid] = reset_at or datetime.now() + timedelta(minutes=self.cfg.get("default_cooldown_minutes", 180))

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
        platform, tried, relays = s.current_platform, set(), []
        outgoing = text
        while True:
            ad = await self.adapter(platform)
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
                self.main.add_message(sid, "assistant", platform, reply.text)
                self.main.set_chat_ref(sid, reply.chat_ref)
                asyncio.create_task(self.summ.maybe_update(sid))
                return {"platform": platform, "text": reply.text, "finished": reply.finished, "relays": relays}
            except LimitReached as e:
                tried.add(platform)
                self._cool(platform, e.state.reset_at)
                nxt = self.policy.next(platform, self.cooldowns, tried)
                if not nxt:
                    raise AdapterError("所有平台都在冷却或不可用") from e
                package = await self.summ.handoff_package(sid)
                self.main.switch_platform(sid, nxt, f"{e.state.signal}: {e.state.detail}")
                relays.append({"from": platform, "to": nxt, "reason": e.state.detail})
                platform = nxt
                nad = await self.adapter(nxt)
                async with getattr(nad, "lock", asyncio.Lock()):
                    outgoing = await self._deliver_handoff(nad, package, text)
