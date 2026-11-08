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
    def __init__(self, pid, exhausted, truncated=False):
        self.info = AdapterInfo(pid, pid, Kind.WEB, Cap.NEW_CHAT)
        self.exhausted, self.truncated, self.sent = exhausted, truncated, []

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True
    async def new_chat(self, preamble=None): return "new"

    async def send(self, turn):
        if self.exhausted:
            raise LimitReached(LimitState(LimitStatus.EXHAUSTED, None, "dom", "已达上限"))
        self.sent.append(turn.content)

    async def receive(self, timeout=180):
        return Reply(f"{self.info.id}:ok", not self.truncated, 1, f"https://{self.info.id}/c/1")


class _Summ:
    async def handoff_package(self, sid): return "【会话接力】摘要"
    async def maybe_update(self, sid): ...


def test_engine_relays_on_limit(pools):
    main, _ = pools
    cfg = {"relay_order": ["deepseek", "kimi"], "platforms": {}, "default_cooldown_minutes": 1}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ())
    eng._adapters = {("deepseek", "main"): _Fake("deepseek", True), ("kimi", "main"): _Fake("kimi", False)}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.ask(sid, "继续讲"))
    assert r["platform"] == "kimi" and r["relays"][0]["to"] == "kimi:main"
    sent = eng._adapters[("kimi", "main")].sent[0]
    assert sent.startswith("【会话接力】") and sent.endswith("继续讲")
    assert main.get_session(sid).chain == ["deepseek", "kimi"]


def test_handoff_package_ghost_replay(pools):
    from relay.summarizer import Summarizer
    main, _ = pools
    s = Summarizer({"summary": {"recent_rounds_raw": 3}}, main, api_key=None)
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "你好")
    main.add_message(sid, "assistant", "deepseek", "你好，我在")
    pkg = asyncio.run(s.handoff_package(sid))
    assert "## 最近的对话（原文回放）" in pkg
    assert "> **用户**：你好" in pkg and "> **AI（deepseek）**：你好，我在" in pkg


def test_account_rotation_same_platform(pools):
    main, _ = pools
    cfg = {"relay_order": ["deepseek"], "platforms": {"deepseek": {"accounts": ["a1", "a2"]}},
           "default_cooldown_minutes": 1}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ())
    eng._adapters = {("deepseek", "a1"): _Fake("deepseek", True), ("deepseek", "a2"): _Fake("deepseek", False)}
    sid = main.create_session("t", "deepseek", account="a1")
    r = asyncio.run(eng.ask(sid, "继续"))
    assert r["platform"] == "deepseek" and r["account"] == "a2"   # 优先同平台换账号
    assert r["relays"][0] == {"from": "deepseek:a1", "to": "deepseek:a2", "reason": "已达上限"}
    assert main.get_session(sid).chain == ["deepseek"]            # 换账号不改接力链
    assert ("deepseek", "a1") in eng.cooldowns
    s = main.get_session(sid)
    assert s.account == "a2"


def test_relay_on_truncation(pools):
    main, _ = pools
    cfg = {"relay_order": ["deepseek", "kimi"], "platforms": {},
           "default_cooldown_minutes": 1, "relay_on_truncation": True}
    eng = RelayEngine(cfg, main, browser=None, summarizer=_Summ())
    eng._adapters = {("deepseek", "main"): _Fake("deepseek", False, truncated=True),
                     ("kimi", "main"): _Fake("kimi", False)}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.ask(sid, "写长文"))
    assert r["platform"] == "kimi" and "截断" in r["relays"][0]["reason"]

    eng2 = RelayEngine({**cfg, "relay_on_truncation": False}, main, browser=None, summarizer=_Summ())
    eng2._adapters = {("deepseek", "main"): _Fake("deepseek", False, truncated=True),
                      ("kimi", "main"): _Fake("kimi", False)}
    sid2 = main.create_session("t2", "deepseek")
    r2 = asyncio.run(eng2.ask(sid2, "写长文"))
    assert r2["platform"] == "deepseek" and r2["finished"] is False   # 开关关闭：截断不接力


def test_session_scope_approval(pools):
    main, arena = pools
    sid = main.create_session("t", "deepseek")
    pid, _ = arena.create_agent("s")
    with pytest.raises(PoolError, match="BAD_SCOPE"):
        arena.create_request(pid, "session", {}, "r")
    rid = arena.create_request(pid, "session", {"session_id": sid}, "查状态")
    arena.decide(rid, True)
    data = arena.fetch(pid, rid)
    assert data == {"scope_type": "session", "session_id": sid}
    with pytest.raises(PoolError, match="TICKET_USED"):
        arena.fetch(pid, rid)


def test_platform_credential_roundtrip(monkeypatch):
    import relay.crypto as crypto
    store = {}
    monkeypatch.setattr(crypto.keyring, "get_password", lambda s, n: store.get(n))
    monkeypatch.setattr(crypto.keyring, "set_password", lambda s, n, v: store.__setitem__(n, v))
    user, secret = "user1", "p" + str(1)
    crypto.set_platform_credential("deepseek", "main", user, secret)
    assert crypto.get_platform_credential("deepseek", "main") == (user, secret)
    assert crypto.has_platform_credential("deepseek", "main")
    assert crypto.get_platform_credential("deepseek", "other") is None


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
