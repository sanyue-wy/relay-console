"""离线自测：不需要浏览器、不需要 API key。python -m pytest -q"""
import asyncio
import sys
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.spec import (AdapterInfo, Cap, Kind, LimitReached, LimitState, LimitStatus, Participant,
                           Reply, register)
from relay.relay_engine import RelayEngine, RelayPolicy
from relay.storage import ArenaPool, MainPool, PoolError


@pytest.fixture
def pools(tmp_path):
    return MainPool(tmp_path, Fernet(Fernet.generate_key())), ArenaPool(tmp_path, Fernet(Fernet.generate_key()), 300)


# ---------------- 双池隔离
def test_pools_are_separate_files(tmp_path, pools):
    main, arena = pools
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "机密内容")
    assert (tmp_path / "main.db").exists() and (tmp_path / "arena.db").exists()
    assert b"\xe6\x9c\xba\xe5\xaf\x86" not in (tmp_path / "main.db").read_bytes()   # 正文加密
    assert arena.list_items() == []


# ---------------- 授权流程
def test_every_read_needs_approval(pools):
    _, arena = pools
    iid = arena.publish_item("背景", "正文")
    pid, token = arena.create_agent("a1")
    assert arena.agent_by_token(token) == pid
    with pytest.raises(PoolError, match="REASON_REQUIRED"):
        arena.create_request(pid, "items", {"item_ids": [iid]}, "")
    rid = arena.create_request(pid, "items", {"item_ids": [iid]}, "需要背景")
    with pytest.raises(PoolError, match="NOT_APPROVED"):
        arena.fetch(pid, rid)
    arena.decide(rid, True)
    assert arena.fetch(pid, rid)[0]["content"] == "正文"
    with pytest.raises(PoolError, match="TICKET_USED"):
        arena.fetch(pid, rid)                     # 一次性
    rid2 = arena.create_request(pid, "items", {"item_ids": [iid]}, "再看一次")
    assert arena.check_request(pid, rid2) == "pending"   # 不存在永久授权


def test_ticket_expires(pools):
    _, arena = pools
    arena.ticket_ttl = -1
    iid = arena.publish_item("x", "y")
    pid, _ = arena.create_agent("a2")
    rid = arena.create_request(pid, "items", {"item_ids": [iid]}, "r")
    arena.decide(rid, True)
    with pytest.raises(PoolError, match="TICKET_EXPIRED"):
        arena.fetch(pid, rid)


def test_audience_and_other_agents(pools):
    _, arena = pools
    a, _ = arena.create_agent("a")
    b, _ = arena.create_agent("b")
    iid = arena.publish_item("only a", "s", audience=a)
    assert [i["id"] for i in arena.list_items(a)] == [iid]
    assert arena.list_items(b) == []
    with pytest.raises(PoolError, match="NOT_IN_POOL"):
        arena.create_request(b, "items", {"item_ids": [iid]}, "r")
    rid = arena.create_request(a, "items", {"item_ids": [iid]}, "r")
    arena.decide(rid, True)
    with pytest.raises(PoolError, match="NOT_APPROVED"):
        arena.fetch(b, rid)                       # 别人的票据用不了


def test_agent_turn_flow(pools):
    _, arena = pools
    a, _ = arena.create_agent("x")
    aid = arena.create_arena("web_vs_agent", "议题", "", [
        {"participant_id": "web:deepseek#0", "side": "正方", "kind": "web", "platform": "deepseek"},
        {"participant_id": a, "side": "反方", "kind": "agent"}])
    bid = arena.post_inbox(aid, 1, a, "请发言")
    with pytest.raises(PoolError, match="NO_OPEN_TURN"):
        arena.submit_turn(a, aid, "抢答")          # 没读题不能交
    rid = arena.create_request(a, "turn", {"arena_id": aid}, "读题")
    arena.decide(rid, True)
    assert arena.fetch(a, rid)["prompt"] == "请发言"
    arena.submit_turn(a, aid, "我的发言")
    assert arena.inbox_answer(bid) == "我的发言"


# ---------------- 接力
def test_policy_skips_cooldown():
    from datetime import datetime, timedelta
    p = RelayPolicy(["deepseek", "kimi", "doubao"])
    assert p.next("deepseek", {"kimi": datetime.now() + timedelta(hours=1)}, set()) == "doubao"
    assert p.next("doubao", {}, {"deepseek"}) == "kimi"


class _Fake(Participant):
    replies: dict = {}

    def __init__(self, pid, exhausted):
        self.info = AdapterInfo(pid, pid, Kind.WEB, Cap.NEW_CHAT)
        self.exhausted, self.sent = exhausted, []

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True
    async def new_chat(self, preamble=None): return "new"

    async def send(self, turn):
        if self.exhausted:
            raise LimitReached(LimitState(LimitStatus.EXHAUSTED, None, "dom", "已达上限"))
        self.sent.append(turn.content)

    async def receive(self, timeout=180):
        return Reply(f"{self.info.id}:ok", True, 1, f"https://{self.info.id}/c/1")


class _Summ:
    async def handoff_package(self, sid): return "【会话接力】摘要"
    async def maybe_update(self, sid): ...


def test_engine_relays_on_limit(pools):
    main, _ = pools
    cfg = {"relay_order": ["deepseek", "kimi"], "platforms": {}, "default_cooldown_minutes": 1}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ())
    eng._adapters = {"deepseek": _Fake("deepseek", True), "kimi": _Fake("kimi", False)}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.ask(sid, "继续讲"))
    assert r["platform"] == "kimi" and r["relays"][0]["to"] == "kimi"
    sent = eng._adapters["kimi"].sent[0]
    assert sent.startswith("【会话接力】") and sent.endswith("继续讲")
    assert main.get_session(sid).chain == ["deepseek", "kimi"]


# ---------------- 主动阈值预生成
def test_summarizer_near_threshold(pools):
    from relay.summarizer import Summarizer
    main, _ = pools
    cfg = {"summary": {"every_n_turns": 100, "near_threshold_chars": 10, "recent_rounds_raw": 3}}
    s = Summarizer(cfg, main, api_key=None)
    called = []

    async def fake_update(sid):
        called.append(sid)
    s.update = fake_update
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "x" * 20)
    asyncio.run(s.maybe_update(sid))          # 未到轮数，但字符量过阈值 → 提前生成
    assert called == [sid]
    main.set_summary(sid, "已摘要", 1)
    asyncio.run(s.maybe_update(sid))          # 无未摘要内容 → 不触发
    assert called == [sid]
