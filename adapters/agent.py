"""Agent 参与者：不直接调用 Agent。

send    = 把本轮题目写进对抗池中该 Agent 的收件箱
receive = 等 Agent 经授权读题后，通过 MCP submit_turn 交回发言
"""
from __future__ import annotations

import asyncio
import time

from .spec import AdapterInfo, Cap, Kind, Participant, Reply, ReplyTimeout, Turn


class AgentParticipant(Participant):
    def __init__(self, arena_pool, arena_id: str, participant_id: str, round_no: int):
        self.info = AdapterInfo(participant_id, participant_id, Kind.AGENT, Cap.NEW_CHAT)
        self.pool, self.arena_id, self.round_no = arena_pool, arena_id, round_no
        self._inbox_id: str | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def health(self) -> bool:
        return True

    async def new_chat(self, preamble: str | None = None) -> str:
        return f"{self.arena_id}:{self.info.id}"

    async def send(self, turn: Turn) -> None:
        self._inbox_id = self.pool.post_inbox(self.arena_id, self.round_no, self.info.id, turn.content)

    async def receive(self, timeout: float = 600) -> Reply:
        t0 = time.time()
        while time.time() - t0 < timeout:
            ans = self.pool.inbox_answer(self._inbox_id)
            if ans is not None:
                return Reply(ans, True, int((time.time() - t0) * 1000))
            await asyncio.sleep(2)
        raise ReplyTimeout(f"{self.info.id} 在 {timeout}s 内未提交发言")
