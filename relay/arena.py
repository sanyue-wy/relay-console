"""对抗模式：网页 vs 网页 / 网页 vs Agent / Agent vs Agent。

- 每个网页席位在主池有自己的私有会话（participant_id = <arena_id>:<seat>），接力时只用自己的摘要。
- 双方只通过对抗池里的「公开轮次」看到对方。
- Agent 席位经收件箱收题，读题需用户授权，答案经 MCP submit_turn 交回。
"""
from __future__ import annotations

from adapters.agent import AgentParticipant
from adapters.spec import Turn

from .relay_engine import RelayEngine
from .storage import ArenaPool, MainPool

MODES = {"web_vs_web", "web_vs_agent", "agent_vs_agent"}


class ArenaRunner:
    def __init__(self, cfg, main: MainPool, arena: ArenaPool, engine: RelayEngine):
        self.cfg, self.main, self.pool, self.engine = cfg, main, arena, engine
        self._sessions: dict[tuple[str, str], str] = {}

    def create(self, mode: str, topic: str, rules: str, seats: list[dict]) -> str:
        """seats: [{"side": "正方", "kind": "web", "platform": "deepseek"},
                   {"side": "反方", "kind": "agent", "participant_id": "agent:xxx"}]"""
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES}")
        norm = []
        for i, s in enumerate(seats):
            if s["kind"] == "web":
                pid = f"web:{s['platform']}#{i}"
            else:
                pid = s["participant_id"]
            norm.append({"participant_id": pid, "side": s["side"], "kind": s["kind"], "platform": s.get("platform")})
        aid = self.pool.create_arena(mode, topic, rules, norm)
        for s in norm:
            if s["kind"] == "web":
                sid = self.main.create_session(f"[对抗]{topic[:20]}·{s['side']}", s["platform"],
                                               participant_id=f"{aid}:{s['participant_id']}")
                self._sessions[(aid, s["participant_id"])] = sid
        return aid

    def _session_for(self, aid: str, pid: str) -> str:
        key = (aid, pid)
        if key not in self._sessions:
            rows = self.main.list_sessions(participant_id=f"{aid}:{pid}")
            self._sessions[key] = rows[0]["id"]
        return self._sessions[key]

    def _prompt(self, arena: dict, seat: dict, rnd: int) -> str:
        turns = self.pool.turns(arena["id"])
        others = [t for t in turns if t["participant_id"] != seat["participant_id"]]
        parts = []
        if rnd == 1 and not any(t["participant_id"] == seat["participant_id"] for t in turns):
            parts.append(f"你将参加一场对抗。议题：{arena['topic']}\n你的立场：{seat['side']}\n规则：{arena['rules'] or '每轮针对对方观点回应，简洁有力。'}")
        if others:
            last = others[-1]
            parts.append(f"对方（{last['side']}）第 {last['round']} 轮发言：\n{last['content']}")
        else:
            parts.append("你先发言。")
        parts.append(f"请给出你第 {rnd} 轮的发言。")
        return "\n\n".join(parts)

    async def run_round(self, aid: str) -> list[dict]:
        arena = self.pool.arena(aid)
        rnd = self.pool.last_round(aid) + 1
        results = []
        for seat in arena["seats"]:
            prompt = self._prompt(arena, seat, rnd)
            if seat["kind"] == "web":
                r = await self.engine.ask(self._session_for(aid, seat["participant_id"]), prompt)
                text = r["text"]
            else:
                ag = AgentParticipant(self.pool, aid, seat["participant_id"], rnd)
                await ag.send(Turn("moderator", prompt, "moderator"))
                text = (await ag.receive(self.cfg["arena"]["agent_turn_timeout"])).text
            self.pool.add_turn(aid, rnd, seat["participant_id"], seat["side"], text)
            results.append({"participant_id": seat["participant_id"], "side": seat["side"], "text": text})
        return results
