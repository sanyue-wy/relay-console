"""A3 手动切换引擎：成功切换/失败回滚/409 互斥/不冷却/超长裁剪。全部用 mock 适配器。"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import relay.app as appmod  # noqa: E402
from adapters.spec import (AdapterInfo, Cap, Kind, LimitReached, LimitState, LimitStatus,  # noqa: E402
                           NotReady, Participant, Reply, Turn)
from relay.relay_engine import ManualHandoffError, RelayEngine  # noqa: E402
from relay.summarizer import CONFIRM_QUESTION  # noqa: E402


class _Fake(Participant):
    def __init__(self, pid, fail=False, not_ready=False, max_input=10 ** 6):
        caps = Cap.NEW_CHAT | Cap.RESUME_URL
        self.info = AdapterInfo(pid, pid, Kind.WEB, caps, max_input_chars=max_input)
        self.fail, self.not_ready = fail, not_ready
        self.sent, self.new_chats = [], 0

    async def start(self): ...
    async def stop(self): ...
    async def health(self): return True
    async def new_chat(self, preamble=None):
        self.new_chats += 1
        return "new"

    async def send(self, turn: Turn):
        if self.not_ready:
            raise NotReady("找不到输入框")
        if self.fail:
            raise LimitReached(LimitState(LimitStatus.EXHAUSTED, None, "dom", "已达上限"))
        self.sent.append(turn.content)

    async def receive(self, timeout=180):
        return Reply(f"{self.info.id}:回复", True, 1, f"https://{self.info.id}/c/1")


class _Summ:
    def __init__(self, package="【会话接力】摘要"):
        self.package = package

    async def handoff_package(self, sid, recent_rounds=None):
        return self.package

    async def maybe_update(self, sid): ...


def make_engine(pools, cfg_extra=None):
    main, _ = pools
    cfg = {"relay_order": ["deepseek", "kimi"], "platforms": {"deepseek": {"accounts": ["main"]},
                                                              "kimi": {"accounts": ["main", "alt"]}},
           "default_cooldown_minutes": 1}
    cfg.update(cfg_extra or {})
    return RelayEngine(cfg, main, browser=None, summarizer=_Summ())


@pytest.fixture
def pools(tmp_path):
    from relay.storage import ArenaPool, MainPool
    return (MainPool(tmp_path, Fernet(Fernet.generate_key())),
            ArenaPool(tmp_path, Fernet(Fernet.generate_key()), 300))


def test_manual_handoff_success_with_question(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"), ("kimi", "alt"): _Fake("kimi")}
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "历史问题")
    r = asyncio.run(eng.manual_handoff(sid, "kimi", "alt", question="下一个问题"))
    assert r["platform"] == "kimi" and r["account"] == "alt" and r["mode"] == "manual"
    assert r["relays"] == [] and r["warning"] is None
    s = main.get_session(sid)
    assert (s.current_platform, s.account) == ("kimi", "alt")
    assert s.chain == ["deepseek", "kimi"]
    # 问题先入库，回复作为 assistant 入库
    msgs = main.messages(sid)
    assert msgs[-2]["role"] == "user" and msgs[-2]["content"] == "下一个问题"
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["platform"] == "kimi"
    # 交接记录：reason=manual
    h = main.handoffs(sid)
    assert h and h[-1]["reason"] == "manual: 用户手动切换" and h[-1]["to_account"] == "alt"
    # 手动切换不给源账号加冷却
    assert eng.cooldowns == {}
    # 交接包 + 问题一起发出
    assert eng._adapters[("kimi", "alt")].sent[0].endswith("下一个问题")


def test_manual_handoff_without_question_appends_confirm(pools):
    main, _ = pools
    eng = make_engine(pools)
    kimi = _Fake("kimi")
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"), ("kimi", "main"): kimi}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.manual_handoff(sid, "kimi", "main"))
    assert kimi.sent[0].endswith(CONFIRM_QUESTION)          # 交接包末尾追加确认请求
    assert "用户特有问题XYZ" not in kimi.sent[0]             # 未附带任何用户问题
    msgs = main.messages(sid)
    assert [m["role"] for m in msgs] == ["assistant"]       # 没有入库 user 消息
    assert msgs[0]["content"] == "kimi:回复"
    assert main.get_session(sid).account == "main"


def test_manual_handoff_failure_rolls_back(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"),
                     ("kimi", "main"): _Fake("kimi", fail=True)}
    sid = main.create_session("t", "deepseek")
    main.add_message(sid, "user", "deepseek", "历史")
    with pytest.raises(LimitReached):
        asyncio.run(eng.manual_handoff(sid, "kimi", "main", question="Q"))
    s = main.get_session(sid)
    assert s.current_platform == "deepseek"                 # 会话保持原平台
    assert s.chain == ["deepseek"] and main.handoffs(sid) == []
    assert len(main.messages(sid)) == 1                     # 已入库的问题被回滚
    assert eng.cooldowns == {}                              # 也不给目标账号加冷却


def test_manual_handoff_not_ready_hint(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"),
                     ("kimi", "main"): _Fake("kimi", not_ready=True)}
    sid = main.create_session("t", "deepseek")
    with pytest.raises(NotReady, match="请先在账户页登录 kimi:main"):
        asyncio.run(eng.manual_handoff(sid, "kimi", "main", question="Q"))
    assert main.get_session(sid).current_platform == "deepseek"


def test_manual_handoff_same_target_and_bad_target(pools):
    main, _ = pools
    eng = make_engine(pools)
    sid = main.create_session("t", "deepseek")
    with pytest.raises(ManualHandoffError, match="无需切换"):
        asyncio.run(eng.manual_handoff(sid, "deepseek", "main"))
    with pytest.raises(ManualHandoffError, match="未知平台"):
        asyncio.run(eng.manual_handoff(sid, "nope", "main"))
    with pytest.raises(ManualHandoffError, match="没有登记账号"):
        asyncio.run(eng.manual_handoff(sid, "kimi", "ghost"))


def test_manual_handoff_busy_while_ask_running(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"), ("kimi", "main"): _Fake("kimi")}
    sid = main.create_session("t", "deepseek")

    async def go():
        lock = eng._session_lock(sid)
        await lock.acquire()                                 # 模拟 ask 正在占用
        try:
            with pytest.raises(ManualHandoffError) as ei:
                await eng.manual_handoff(sid, "kimi", "main")
            assert ei.value.code == "BUSY"
        finally:
            lock.release()

    asyncio.run(go())


def test_manual_handoff_cooldown_warning_only(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"), ("kimi", "main"): _Fake("kimi")}
    sid = main.create_session("t", "deepseek")
    eng.cooldowns[("kimi", "main")] = datetime.now() + timedelta(hours=1)   # 目标冷却中
    r = asyncio.run(eng.manual_handoff(sid, "kimi", "main", question="Q"))
    assert r["warning"] and "冷却" in r["warning"]           # 不阻止切换，只提示
    assert main.get_session(sid).current_platform == "kimi"


def test_manual_handoff_long_package_trimmed(pools):
    main, _ = pools
    eng = make_engine(pools)
    eng.summ = _Summ(package="【会话接力】" + "长" * 600)
    eng._adapters = {("deepseek", "main"): _Fake("deepseek"),
                     ("kimi", "main"): _Fake("kimi", max_input=300)}
    sid = main.create_session("t", "deepseek")
    r = asyncio.run(eng.manual_handoff(sid, "kimi", "main", question="Q"))
    sent = eng._adapters[("kimi", "main")].sent[0]
    assert len(sent) <= 320                                  # 走了 _fit_handoff 裁剪
    assert sent.endswith("Q") and "现在请回答" in sent


# ---------------- 接口层：状态码映射
def test_handoff_endpoint_mapping(tmp_path, monkeypatch):
    keys = {}
    monkeypatch.setattr(appmod, "get_fernet", lambda n: keys.setdefault(n, Fernet(Fernet.generate_key())))
    monkeypatch.setattr(appmod, "get_api_key", lambda: None)
    monkeypatch.setattr(appmod, "save_config", lambda cfg: None)
    with TestClient(appmod.app, base_url="http://127.0.0.1") as c:
        H = {"X-Console-Token": appmod.CONSOLE_TOKEN}
        sid = c.post("/api/sessions", headers=H, json={"title": "t", "platform": "deepseek"}).json()["id"]
        appmod.S.engine._adapters = {("deepseek", "main"): _Fake("deepseek"),
                                     ("kimi", "main"): _Fake("kimi")}
        r = c.post(f"/api/sessions/{sid}/handoff", headers=H,
                   json={"platform": "kimi", "account": "main", "question": "Q"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "manual" and body["platform"] == "kimi"
        # 目标=当前 → 400
        r = c.post(f"/api/sessions/{sid}/handoff", headers=H,
                   json={"platform": "kimi", "account": "main"})
        assert r.status_code == 400
        # 未知会话 → 404
        assert c.post("/api/sessions/s_none/handoff", headers=H,
                      json={"platform": "kimi", "account": "main"}).status_code == 404
        # 忙 → 409（mock 引擎抛 BUSY，真实锁路径在上面引擎级测试覆盖）
        async def busy(*a, **kw):
            raise ManualHandoffError("BUSY", "该会话正在问答或接力中")

        monkeypatch.setattr(appmod.S.engine, "manual_handoff", busy)
        r = c.post(f"/api/sessions/{sid}/handoff", headers=H,
                   json={"platform": "deepseek", "account": "main"})
        assert r.status_code == 409
